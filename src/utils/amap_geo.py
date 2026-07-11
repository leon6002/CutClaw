"""高德逆地理编码兜底 — 治 Immich 本地地名库的"查无此地"。

背景(2026-07-10):Immich 逆地理是纯本地查表(GeoNames cities500 + 中国
增强版),25km 半径内没有入库聚居点就回落国家级——天山/独库公路这类
旅拍胜地全军覆没。Immich 无外部 API 插件机制,所以兜底接在 CutClaw 侧:
有 GPS 但 Immich 没给出城市时调高德(个人免费 key,AMAP_API_KEY in .env)。

- **网格缓存**:坐标四舍五入到 0.01°(~1km)为键,同一片区域只调一次,
  几乎不耗配额;缓存永久落盘 Output/asset_index/amap_geo_cache.json。
- **WGS84→GCJ02**:照片 GPS 是 WGS84,高德要火星坐标——城市级命名差
  几百米无所谓,但转换是标准做法,顺手做对。
- 任何失败返回 None,绝不阻塞调用方。
"""

from __future__ import annotations

import json
import math
import os
import threading
import urllib.parse
import urllib.request

_LOCK = threading.Lock()
_CACHE: dict | None = None
_CACHE_PATH = os.path.join("Output", "asset_index", "amap_geo_cache.json")
_LOG_PATH = os.path.join("Output", "logs", "amap_geo.jsonl")


def _log(entry: dict) -> None:
    """逐条追加解析日志(哪张照片/什么坐标/走没走缓存/解析出什么)。"""
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        import time as _t
        entry = {"ts": _t.strftime("%Y-%m-%d %H:%M:%S"), **entry}
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    """标准 WGS84→GCJ02(中国境外原样返回)。"""
    if not (0.8293 <= lat <= 55.8271 and 72.004 <= lon <= 137.8347):
        return lat, lon
    a = 6378245.0
    ee = 0.00669342162296594323

    def _t(x, y, mode):
        r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
             + 0.2 * math.sqrt(abs(x))) if mode == "lat" else \
            (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
             + 0.1 * math.sqrt(abs(x)))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        if mode == "lat":
            r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
            r += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        else:
            r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
            r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return r

    dlat = _t(lon - 105.0, lat - 35.0, "lat")
    dlon = _t(lon - 105.0, lat - 35.0, "lon")
    radlat = lat / 180.0 * math.pi
    magic = 1 - ee * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                _CACHE = json.load(f)
        except Exception:  # noqa: BLE001
            _CACHE = {}
    return _CACHE


def reverse_geocode(lat: float, lon: float, context: str = "") -> dict | None:
    """{'province','city','district','township','label','formatted'} 或 None。

    label 是给 UI/片头用的短地名:district(县市)优先,township 补细节,
    如 "乌苏市·赛力克提牧场"。context(通常是文件名)进解析日志,
    Output/logs/amap_geo.jsonl 可审计"逆编码了哪些照片、地址是什么"。
    """
    key = os.environ.get("AMAP_API_KEY", "").strip()
    if not key:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    gk = f"{round(lat, 2):.2f},{round(lon, 2):.2f}"
    with _LOCK:
        cache = _load()
        if gk in cache:
            hit = cache[gk] or None
            _log({"file": context, "lat": round(lat, 5), "lon": round(lon, 5),
                  "grid": gk, "cached": True,
                  "label": (hit or {}).get("label"),
                  "formatted": (hit or {}).get("formatted")})
            return hit
    glat, glon = _wgs84_to_gcj02(lat, lon)
    url = ("https://restapi.amap.com/v3/geocode/regeo?"
           + urllib.parse.urlencode({"key": key, "location": f"{glon:.6f},{glat:.6f}",
                                     "extensions": "base"}))
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.load(r)
        if d.get("status") != "1":
            _log({"file": context, "lat": round(lat, 5), "lon": round(lon, 5),
                  "grid": gk, "cached": False, "error": str(d.get("info"))[:60]})
            return None          # key 配额/失效——不缓存失败,下次再试
        ac = (d.get("regeocode") or {}).get("addressComponent") or {}

        def _s(v):
            return v if isinstance(v, str) and v else ""
        province, city = _s(ac.get("province")), _s(ac.get("city"))
        district, township = _s(ac.get("district")), _s(ac.get("township"))
        core = district or city or province
        label = (f"{core}·{township}" if township and core else (core or township))
        out = {
            "province": province, "city": city, "district": district,
            "township": township, "label": label,
            "formatted": _s((d.get("regeocode") or {}).get("formatted_address")),
        } if (province or city or district) else None
        with _LOCK:
            cache = _load()
            cache[gk] = out
            try:
                os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
                with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=0)
            except Exception:  # noqa: BLE001
                pass
        _log({"file": context, "lat": round(lat, 5), "lon": round(lon, 5),
              "grid": gk, "cached": False,
              "label": (out or {}).get("label"),
              "formatted": (out or {}).get("formatted")})
        return out
    except Exception as e:  # noqa: BLE001
        _log({"file": context, "lat": round(lat, 5), "lon": round(lon, 5),
              "grid": gk, "cached": False, "error": str(e)[:60]})
        return None

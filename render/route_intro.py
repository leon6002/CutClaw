"""旅程轨迹开场(§21)— 高德静态底图(电影化调色)+ 轨迹渐进描画。

输入:route_intro.json(server 构建):
  {"points": [{"t","lat","lon"}], "start_label", "end_label", "date_range",
   "total_km", "map": {"image": png, "center": [gcj_lng, gcj_lat], "zoom", "px"}}
有底图 → 退饱和/压暗/蓝移后垫底,轨迹点转 GCJ-02 + web-mercator 对位投影;
无底图(没 key / 配额错)→ 回退暗色抽象风格。全程带缓推镜头(ken-burns),
首尾黑场淡入淡出,由 render_video 的 ending 同款转码分支归一后进 concat。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

_SIZES = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (960, 960)}
_BG = (11, 15, 26)
_PATH = (125, 211, 252)
_FPS = 30
_DUR = 10.0                 # 默认时长(7s 仍被反馈太快);预览面板可调
_BRIGHT = 0.62              # 底图亮度(0.42 被反馈太暗);预览面板可调
_CANVAS = 2048              # 底图/抽象画布统一尺寸(1024*1024@scale2)


def _font(size: int):
    for f in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
              r"C:\Windows\Fonts\simsun.ttc"):
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _projector(route: dict):
    """返回 geo→2048 画布坐标的投影函数(轨迹点和冒泡照片共用)。
    有底图走 GCJ-02+mercator 对位;没有则抽象投影(等距圆柱居中适配)。"""
    pts = route.get("points") or []
    m = route.get("map") or {}
    if m.get("center") and m.get("zoom"):
        try:
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _root not in sys.path:
                sys.path.insert(0, _root)
            from src.utils.amap_geo import _wgs84_to_gcj02 as _gcj
        except Exception:  # noqa: BLE001
            def _gcj(la, ln):  # noqa: ANN001
                return la, ln
        z = int(m["zoom"])
        n = 256 * (2 ** z) * 2

        def merc(la: float, ln: float):
            r = math.radians(la)
            return ((ln + 180) / 360 * n,
                    (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
        c_x, c_y = merc(m["center"][1], m["center"][0])

        def proj(lat: float, lon: float):
            x, y = merc(*_gcj(lat, lon))
            return (x - c_x + _CANVAS / 2, y - c_y + _CANVAS / 2)
        return proj
    # 抽象模式
    mid = math.radians(sum(p["lat"] for p in pts) / len(pts))
    xs = [p["lon"] * math.cos(mid) for p in pts]
    ys = [p["lat"] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    s = _CANVAS * 0.52 / max(x1 - x0, y1 - y0, 1e-9)

    def proj(lat: float, lon: float):
        return ((lon * math.cos(mid) - (x0 + x1) / 2) * s + _CANVAS / 2,
                _CANVAS / 2 - (lat - (y0 + y1) / 2) * s)
    return proj


def _make_polaroid(thumb_path: str, size: int, rot: float) -> Image.Image | None:
    """冒泡照片:方形裁剪 + 白框拍立得 + 轻微旋转,预生成 RGBA 贴纸。"""
    try:
        im = Image.open(thumb_path).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    s = min(im.size)
    im = im.crop(((im.width - s) // 2, (im.height - s) // 2,
                  (im.width + s) // 2, (im.height + s) // 2)) \
        .resize((size, size), Image.LANCZOS)
    b = max(3, size // 22)
    card = Image.new("RGBA", (size + 2 * b, size + 2 * b), (248, 248, 246, 255))
    card.paste(im, (b, b))
    return card.rotate(rot, expand=True, resample=Image.BICUBIC)


def _resample(pts: list[tuple], n: int = 260) -> list[tuple]:
    """按弧长均匀重采样,描画进度才是匀速的。"""
    if len(pts) < 2:
        return pts
    seg = [0.0]
    for a, b in zip(pts, pts[1:]):
        seg.append(seg[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = seg[-1] or 1.0
    out, j = [], 0
    for k in range(n):
        d = total * k / (n - 1)
        while j < len(seg) - 2 and seg[j + 1] < d:
            j += 1
        f = (d - seg[j]) / max(seg[j + 1] - seg[j], 1e-9)
        out.append((pts[j][0] + (pts[j + 1][0] - pts[j][0]) * f,
                    pts[j][1] + (pts[j + 1][1] - pts[j][1]) * f))
    return out


def _prep_canvas(route: dict, brightness: float = _BRIGHT) -> Image.Image:
    """2048 底板:真实底图做电影化处理;否则暗色画布 + 淡网格。"""
    m = route.get("map") or {}
    if m.get("image") and os.path.exists(m["image"]):
        img = Image.open(m["image"]).convert("RGB")
        if img.size != (_CANVAS, _CANVAS):
            img = img.resize((_CANVAS, _CANVAS), Image.BILINEAR)
        img = ImageEnhance.Color(img).enhance(0.28)        # 退饱和
        img = ImageEnhance.Brightness(img).enhance(brightness)
        arr = np.asarray(img).astype(np.float32)
        arr[..., 2] = np.clip(arr[..., 2] * 1.18 + 8, 0, 255)   # 蓝移
        arr[..., 0] *= 0.90
        return Image.fromarray(arr.astype(np.uint8))
    img = Image.new("RGB", (_CANVAS, _CANVAS), _BG)
    d = ImageDraw.Draw(img)
    for i in range(1, 6):
        g = int(_CANVAS * i / 6)
        d.line([(g, 0), (g, _CANVAS)], fill=(20, 26, 42), width=2)
        d.line([(0, g), (_CANVAS, g)], fill=(20, 26, 42), width=2)
    return img


def _vignette(w: int, h: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt(((xx - w / 2) / (w * 0.72)) ** 2 + ((yy - h / 2) / (h * 0.72)) ** 2)
    return np.clip(1.0 - 0.55 * np.clip(d - 0.42, 0, 1) ** 1.5, 0.4, 1.0)[..., None]


def render_route_intro(route_json: str, out_mp4: str, ratio: str = "16:9",
                       fps: int = _FPS, duration: float | None = None) -> float:
    with open(route_json, encoding="utf-8") as f:
        route = json.load(f)
    if len(route.get("points") or []) < 2:
        raise ValueError("route needs >= 2 GPS points")
    style = route.get("style") or {}
    if duration is None:
        try:
            duration = float(style.get("duration") or 0) or None
        except Exception:  # noqa: BLE001
            duration = None
    if duration is None:
        try:
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _root not in sys.path:
                sys.path.insert(0, _root)
            from src import config as _cfg
            duration = float(getattr(_cfg, "ROUTE_INTRO_DURATION_SEC", _DUR))
        except Exception:  # noqa: BLE001
            duration = _DUR
    duration = max(4.0, min(20.0, float(duration)))
    try:
        brightness = max(0.2, min(1.2, float(style.get("map_brightness") or _BRIGHT)))
    except Exception:  # noqa: BLE001
        brightness = _BRIGHT
    bubbles_on = bool(style.get("bubbles", True))
    w, h = _SIZES.get(ratio, _SIZES["16:9"])

    proj = _projector(route)
    raw = [proj(p["lat"], p["lon"]) for p in route["points"]]
    if len(raw) >= 5:                    # 轻度平滑去 GPS 抖动
        raw = [(sum(p[0] for p in raw[max(0, i - 2):i + 3]) / len(raw[max(0, i - 2):i + 3]),
                sum(p[1] for p in raw[max(0, i - 2):i + 3]) / len(raw[max(0, i - 2):i + 3]))
               for i in range(len(raw))]
    path = _resample(raw, 260)
    dots = [(raw[i], i / max(1, len(raw) - 1))
            for i in range(0, len(raw), max(1, len(raw) // 26))]

    # 裁剪窗口:bbox 占画面 ~70%,窗口按输出比例取,缓推镜头(轻微 zoom-in)
    xs, ys = zip(*path)
    bcx, bcy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    aspect = w / h
    cw0 = max((max(xs) - min(xs)) / 0.70, (max(ys) - min(ys)) / 0.70 * aspect,
              420 * max(1.0, aspect))
    ch0 = cw0 / aspect
    f_ = min(_CANVAS / cw0, _CANVAS / ch0, 1.0)
    cw0, ch0 = cw0 * f_, ch0 * f_

    canvas = _prep_canvas(route, brightness)
    vig = _vignette(w, h)

    # 冒泡照片:预生成拍立得贴纸,head 走到对应弧长位置时弹出
    bubbles: list[dict] = []
    _excl = set(style.get("bubble_exclude") or [])
    if bubbles_on:
        for k, b in enumerate(x for x in (route.get("bubbles") or [])
                               if x.get("id") not in _excl):
            spr = _make_polaroid(str(b.get("thumb") or ""), int(h * 0.13),
                                 -6 if k % 2 else 6)
            if spr is None:
                continue
            bpx = proj(b["lat"], b["lon"])
            near = min(range(len(path)),
                       key=lambda j: (path[j][0] - bpx[0]) ** 2 + (path[j][1] - bpx[1]) ** 2)
            bubbles.append({"px": bpx, "frac": min(near / max(1, len(path) - 1), 0.985),
                            "spr": spr, "side": k % 2, "born": None})

    f_small = _font(max(14, int(h * 0.026)))
    start_label = str(route.get("start_label") or "").strip()
    end_label = str(route.get("end_label") or "").strip()
    if start_label and end_label and start_label != end_label:
        sp_, ep_ = start_label.split("·"), end_label.split("·")
        if len(sp_) > 1 and len(ep_) > 1 and sp_[0] == ep_[0]:
            title = f"{sp_[0]} · {'·'.join(sp_[1:])} → {'·'.join(ep_[1:])}"
        else:
            title = f"{start_label}  →  {end_label}"
    else:
        title = start_label or end_label
    _fs = max(22, int(h * 0.045))
    f_big = _font(_fs)
    _probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    while _fs > 16 and _probe.textlength(title, font=f_big) > w * 0.90:
        _fs = int(_fs * 0.9)
        f_big = _font(_fs)
    km = route.get("total_km")
    sub_parts = [str(route.get("date_range") or "").strip()]
    if km and float(km) >= 1:
        sub_parts.append(f"全程 {float(km):.0f} 公里")
    subtitle = " · ".join(x for x in sub_parts if x)

    n_frames = int(duration * fps)
    draw_t0, draw_t1 = 0.7, duration - 1.6      # 描画窗口(7s → 0.7~5.4)
    text_t0 = duration - 2.3                    # 文字浮现
    fade_in, fade_out = 0.4, 0.5

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(fps), "-i", "-",
           "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
           "-color_trc", "bt709", out_mp4]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for fi in range(n_frames):
            t = fi / fps
            # 缓推:窗口从 1.05× 缓慢收到 0.94×(zoom-in ~10%)
            k = 1.05 - 0.11 * _smoothstep(t / duration)
            cw, ch = cw0 * k, ch0 * k
            cx = min(max(bcx, cw / 2), _CANVAS - cw / 2)
            cy = min(max(bcy, ch / 2), _CANVAS - ch / 2)
            frame = canvas.crop((int(cx - cw / 2), int(cy - ch / 2),
                                 int(cx + cw / 2), int(cy + ch / 2))) \
                .resize((w, h), Image.BILINEAR)

            def T(p):  # 画布坐标 → 输出坐标
                return ((p[0] - (cx - cw / 2)) * w / cw,
                        (p[1] - (cy - ch / 2)) * h / ch)

            ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            dr = ImageDraw.Draw(ov)
            p = _smoothstep((t - draw_t0) / (draw_t1 - draw_t0))
            seg = [T(q) for q in path[:max(2, int(len(path) * p))]]

            for dot, frac in dots:                 # 拍照落点:轨迹经过后亮起
                if frac <= p:
                    dx, dy = T(dot)
                    dr.ellipse([dx - 2.5, dy - 2.5, dx + 2.5, dy + 2.5],
                               fill=(56, 189, 248, 120))
            if len(seg) >= 2:                      # 宽淡辉光 + 细亮主线
                dr.line(seg, fill=(*_PATH, 60), width=9, joint="curve")
                dr.line(seg, fill=(*_PATH, 255), width=3, joint="curve")
            s0 = T(path[0])                        # 起点环
            dr.ellipse([s0[0] - 5, s0[1] - 5, s0[0] + 5, s0[1] + 5],
                       outline=(255, 255, 255, 220), width=2)
            head = seg[-1]
            if p < 1.0:                            # 行进头:白点 + 呼吸辉光
                glow = 10 + 3 * math.sin(t * 5)
                dr.ellipse([head[0] - glow, head[1] - glow,
                            head[0] + glow, head[1] + glow], fill=(255, 255, 255, 36))
                dr.ellipse([head[0] - 4, head[1] - 4, head[0] + 4, head[1] + 4],
                           fill=(255, 255, 255, 255))
            else:                                  # 终点定桩
                e0 = T(path[-1])
                dr.ellipse([e0[0] - 6, e0[1] - 6, e0[0] + 6, e0[1] + 6],
                           fill=(255, 255, 255, 255))
                dr.ellipse([e0[0] - 11, e0[1] - 11, e0[0] + 11, e0[1] + 11],
                           outline=(255, 255, 255, 90), width=2)

            # 冒泡照片:head 经过即弹出(0.5s 过冲缩放),之后常驻
            for bb in bubbles:
                if bb["born"] is None and p >= bb["frac"]:
                    bb["born"] = t
                if bb["born"] is None:
                    continue
                x_ = min(1.0, (t - bb["born"]) / 0.5)
                s_ = _smoothstep(x_) * (1 + 0.16 * (1 - x_))
                spr = bb["spr"]
                sw2 = max(2, int(spr.width * s_))
                sh2 = max(2, int(spr.height * s_))
                bx, by = T(bb["px"])
                dr.ellipse([bx - 3, by - 3, bx + 3, by + 3], fill=(255, 255, 255, 230))
                ox = 16 if bb["side"] else -16 - sw2
                pos = (int(min(max(bx + ox, 4), w - sw2 - 4)),
                       int(min(max(by - sh2 - 10, 4), h - sh2 - 4)))
                spr2 = spr if x_ >= 1.0 else spr.resize((sw2, sh2), Image.BILINEAR)
                ov.alpha_composite(spr2, pos)

            ta = _smoothstep((t - text_t0) / 0.7)
            if ta > 0 and title:
                # 文字底衬(底图上直接写字会花)
                band = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                bd = ImageDraw.Draw(band)
                bd.rectangle([0, int(h * 0.765), w, int(h * 0.765) + int(_fs * 3.1)],
                             fill=(5, 8, 16, int(150 * ta)))
                ov = Image.alpha_composite(band, ov)
                dr = ImageDraw.Draw(ov)
                tw = dr.textlength(title, font=f_big)
                dr.text(((w - tw) / 2, h * 0.80), title, font=f_big,
                        fill=(255, 255, 255, int(235 * ta)))
                if subtitle:
                    sw = dr.textlength(subtitle, font=f_small)
                    dr.text(((w - sw) / 2, h * 0.80 + _fs * 1.45), subtitle,
                            font=f_small, fill=(148, 163, 184, int(200 * ta)))

            frame = Image.alpha_composite(frame.convert("RGBA"), ov).convert("RGB")
            arr = np.asarray(frame, dtype=np.float32) * vig
            g = min(1.0, t / fade_in, max(0.0, (duration - t) / fade_out))
            proc.stdin.write(np.clip(arr * g, 0, 255).astype(np.uint8).tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise RuntimeError(f"route intro ffmpeg failed: {err[-400:]}")
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
    return duration

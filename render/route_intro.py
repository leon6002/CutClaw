"""旅程轨迹开场(§21)— 极简暗色风格的路线动画,零地图瓦片零版权负担。

输入:route_intro.json(server 从 Immich GPS + 高德地名构建):
  {"points": [{"t": ISO, "lat": .., "lon": ..}], "start_label": "..",
   "end_label": "..", "date_range": "..", "total_km": 12.3}
输出:一段 mp4(暗色底 + 轨迹渐进描画 + 地名/里程浮字,首尾自带黑场淡入淡出,
     与正片硬切也不突兀)。renders 到目标比例,由 render_video 的 ending 同款
     转码分支归一化编码参数后进 concat。
"""
from __future__ import annotations

import json
import math
import os
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_SIZES = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (960, 960)}
_BG = (11, 15, 26)          # 与 Web UI 同族的深海军蓝
_PATH = (125, 211, 252)     # 轨迹主色(淡青)
_FPS = 30
_DUR = 4.2                  # 总时长;描画 0.4→3.0s,余下定格给文字


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


def _project(points: list[dict], w: int, h: int, margin: float = 0.20):
    """等距圆柱投影 + 适配画布(经度按中纬度余弦校正,保持形状不横向压扁)。"""
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    mid = math.radians(sum(lats) / len(lats))
    xs = [lon * math.cos(mid) for lon in lons]
    ys = lats
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    span_x = max(x1 - x0, 1e-9)
    span_y = max(y1 - y0, 1e-9)
    box_w = w * (1 - 2 * margin)
    box_h = h * (1 - 2 * margin)
    s = min(box_w / span_x, box_h / span_y)
    off_x = (w - span_x * s) / 2
    off_y = (h - span_y * s) / 2
    out = []
    for x, y in zip(xs, ys):
        px = off_x + (x - x0) * s
        py = h - (off_y + (y - y0) * s)      # 纬度向上
        out.append((px, py))
    return out


def _resample(pts: list[tuple], n: int = 240) -> list[tuple]:
    """按弧长均匀重采样,描画进度才是匀速的(GPS 点密度天差地别)。"""
    if len(pts) < 2:
        return pts
    seg = [0.0]
    for a, b in zip(pts, pts[1:]):
        seg.append(seg[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = seg[-1] or 1.0
    out = []
    j = 0
    for k in range(n):
        d = total * k / (n - 1)
        while j < len(seg) - 2 and seg[j + 1] < d:
            j += 1
        f = (d - seg[j]) / max(seg[j + 1] - seg[j], 1e-9)
        out.append((pts[j][0] + (pts[j + 1][0] - pts[j][0]) * f,
                    pts[j][1] + (pts[j + 1][1] - pts[j][1]) * f))
    return out


def _vignette(w: int, h: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2, h / 2
    d = np.sqrt(((xx - cx) / (w * 0.72)) ** 2 + ((yy - cy) / (h * 0.72)) ** 2)
    return np.clip(1.0 - 0.5 * np.clip(d - 0.45, 0, 1) ** 1.5, 0.45, 1.0)[..., None]


def render_route_intro(route_json: str, out_mp4: str, ratio: str = "16:9",
                       fps: int = _FPS, duration: float = _DUR) -> float:
    with open(route_json, encoding="utf-8") as f:
        route = json.load(f)
    pts_geo = route.get("points") or []
    if len(pts_geo) < 2:
        raise ValueError("route needs >= 2 GPS points")
    w, h = _SIZES.get(ratio, _SIZES["16:9"])

    raw = _project(pts_geo, w, h)
    # 轻度平滑去 GPS 抖动
    if len(raw) >= 5:
        sm = []
        for i in range(len(raw)):
            lo, hi = max(0, i - 2), min(len(raw), i + 3)
            sm.append((sum(p[0] for p in raw[lo:hi]) / (hi - lo),
                       sum(p[1] for p in raw[lo:hi]) / (hi - lo)))
        raw = sm
    path = _resample(raw, 240)
    photo_dots = raw[:: max(1, len(raw) // 24)]     # 拍照落点的星星点点

    vig = _vignette(w, h)
    base = Image.new("RGB", (w, h), _BG)
    d0 = ImageDraw.Draw(base)
    # 极淡的经纬参考线(纯装饰,给"地图感")
    for gx in range(1, 6):
        d0.line([(w * gx / 6, 0), (w * gx / 6, h)], fill=(20, 26, 42), width=1)
    for gy in range(1, 6):
        d0.line([(0, h * gy / 6), (w, h * gy / 6)], fill=(20, 26, 42), width=1)

    f_small = _font(max(14, int(h * 0.026)))
    start_label = str(route.get("start_label") or "").strip()
    end_label = str(route.get("end_label") or "").strip()
    if start_label and end_label and start_label != end_label:
        # 同前缀合并:「抚松县·东岗镇 → 抚松县·漫江镇」→「抚松县 · 东岗镇 → 漫江镇」
        sp_, ep_ = start_label.split("·"), end_label.split("·")
        if len(sp_) > 1 and len(ep_) > 1 and sp_[0] == ep_[0]:
            title = f"{sp_[0]} · {'·'.join(sp_[1:])} → {'·'.join(ep_[1:])}"
        else:
            title = f"{start_label}  →  {end_label}"
    else:
        title = start_label or end_label
    # 标题自适应字号:超出画幅 90% 就逐级缩小(720 宽装不下长地名,实测裁字)
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
    draw_t0, draw_t1 = 0.4, 3.0          # 描画窗口
    text_t0 = 2.6                        # 文字浮现
    fade_in, fade_out = 0.35, 0.45

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(fps), "-i", "-",
           "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
           "-color_trc", "bt709", out_mp4]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        for fi in range(n_frames):
            t = fi / fps
            frame = base.copy()
            ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            dr = ImageDraw.Draw(ov)

            p = _smoothstep((t - draw_t0) / (draw_t1 - draw_t0))
            n_vis = max(2, int(len(path) * p))
            seg = path[:n_vis]

            # 拍照落点:轨迹经过后亮起
            head = seg[-1]
            for dot in photo_dots:
                if any(math.hypot(dot[0] - q[0], dot[1] - q[1]) < 3 for q in seg[:: 8]):
                    dr.ellipse([dot[0] - 2.5, dot[1] - 2.5, dot[0] + 2.5, dot[1] + 2.5],
                               fill=(56, 189, 248, 110))
            # 轨迹:宽淡描边作辉光 + 细亮主线
            if len(seg) >= 2:
                dr.line(seg, fill=(*_PATH, 60), width=9, joint="curve")
                dr.line(seg, fill=(*_PATH, 255), width=3, joint="curve")
            # 起点环
            s0 = path[0]
            dr.ellipse([s0[0] - 5, s0[1] - 5, s0[0] + 5, s0[1] + 5],
                       outline=(255, 255, 255, 220), width=2)
            # 行进头:白点 + 呼吸辉光
            if p < 1.0:
                glow = 10 + 3 * math.sin(t * 6)
                dr.ellipse([head[0] - glow, head[1] - glow, head[0] + glow, head[1] + glow],
                           fill=(255, 255, 255, 36))
                dr.ellipse([head[0] - 4, head[1] - 4, head[0] + 4, head[1] + 4],
                           fill=(255, 255, 255, 255))
            else:  # 终点定桩
                e0 = path[-1]
                dr.ellipse([e0[0] - 6, e0[1] - 6, e0[0] + 6, e0[1] + 6],
                           fill=(255, 255, 255, 255))
                dr.ellipse([e0[0] - 11, e0[1] - 11, e0[0] + 11, e0[1] + 11],
                           outline=(255, 255, 255, 90), width=2)

            # 文字(底部居中,路径快画完时浮现)
            ta = _smoothstep((t - text_t0) / 0.7)
            if ta > 0 and title:
                a = int(235 * ta)
                tw = dr.textlength(title, font=f_big)
                dr.text(((w - tw) / 2, h * 0.80), title, font=f_big,
                        fill=(255, 255, 255, a))
                if subtitle:
                    sw = dr.textlength(subtitle, font=f_small)
                    dr.text(((w - sw) / 2, h * 0.80 + _fs * 1.45), subtitle,
                            font=f_small, fill=(148, 163, 184, int(200 * ta)))

            frame = Image.alpha_composite(frame.convert("RGBA"), ov).convert("RGB")
            arr = np.asarray(frame, dtype=np.float32) * vig
            # 首尾黑场淡入淡出(与正片硬切也不突兀)
            g = min(1.0, t / fade_in, max(0.0, (duration - t) / fade_out))
            arr *= g
            proc.stdin.write(np.clip(arr, 0, 255).astype(np.uint8).tobytes())
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

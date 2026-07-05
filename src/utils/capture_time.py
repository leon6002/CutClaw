"""Capture-time extraction — the journey's chronological spine.

A travel-memory montage should generally move forward in time (day 1 →
day N, morning → night). That needs the REAL shooting time of each source,
which we recover without any network calls:

  1. filename patterns — DJI embeds the recording start second in the name
     (DJI_20251213150209_0357_D), phones use VID_/PXL_/YYYYMMDD_HHMMSS forms
  2. container metadata — QuickTime/MP4 creation_time via ffprobe (iPhone
     IMG_*.MOV lands here)
  3. give up → None (the Screenwriter may place such scenes freely)

Timezone caveat: filename times are local (what the traveller experienced);
container creation_time is often UTC. We deliberately DON'T convert — for
ordering scenes within one trip, consistency per source is what matters.
"""
import os
import re
import subprocess
from datetime import datetime, timedelta

_PATTERNS = [
    re.compile(r"(?:DJI|VID|PXL|MVIMG)_(\d{8})[_-]?(\d{6})"),   # DJI_20251213150209 / VID_20251213_150209
    re.compile(r"(?<!\d)(\d{8})[_-](\d{6})(?!\d)"),             # 20251213_150209 anywhere
]

_CACHE: dict = {}


def _parse_dt(date8: str, time6: str):
    try:
        dt = datetime.strptime(date8 + time6, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return dt if 2000 <= dt.year <= 2100 else None


def get_capture_time(video_path: str):
    """Best-effort recording START time of a media file → datetime | None."""
    key = os.path.normpath(video_path or "")
    if key in _CACHE:
        return _CACHE[key]
    dt = _get(video_path)
    _CACHE[key] = dt
    return dt


def _get(video_path: str):
    name = os.path.basename(video_path or "")
    for pat in _PATTERNS:
        m = pat.search(name)
        if m:
            dt = _parse_dt(m.group(1), m.group(2))
            if dt:
                return dt
    if not video_path or not os.path.exists(video_path):
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
             "-of", "default=nw=1:nk=1", video_path],
            capture_output=True, text=True, timeout=20)
        raw = (r.stdout or "").strip()
        if raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
            if 2000 <= dt.year <= 2100:
                return dt
    except Exception:  # noqa: BLE001
        pass
    return None


def scene_capture_time(source_capture, scene_start_sec: float):
    """Actual wall-clock moment a scene begins (file start + in-file offset)."""
    if source_capture is None:
        return None
    try:
        return source_capture + timedelta(seconds=float(scene_start_sec or 0.0))
    except Exception:  # noqa: BLE001
        return source_capture


def build_trip_labeler(datetimes: list, gap_days: int = 14):
    """Cluster capture dates into TRIPS (gaps > gap_days start a new one) and
    return a labeler fn. Mixed-trip libraries would otherwise yield absurd
    'Day 411' labels; instead: 'Trip 2 · Day 1 · 12-13 15:04'."""
    days = sorted({d.date() for d in datetimes if d is not None})
    trips: list = []          # first day of each trip
    for d in days:
        if not trips or (d - trips[-1][-1]).days > gap_days:
            trips.append([d])
        else:
            trips[-1].append(d)
    starts = [t[0] for t in trips]

    def label(dt) -> str:
        if dt is None or not starts:
            return ""
        ti = 0
        for i, s in enumerate(starts):
            if dt.date() >= s:
                ti = i
        day = max(1, 1 + (dt.date() - starts[ti]).days)
        prefix = f"Trip {ti + 1} · " if len(starts) > 1 else ""
        return f"{prefix}Day {day} · {dt.strftime('%m-%d %H:%M')}"

    return label

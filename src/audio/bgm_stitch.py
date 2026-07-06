"""Standalone BGM stitching — join multiple music tracks into ONE seamless
BGM file, without running the pipeline.

Design (LOGIC.md §16): the stitched output is written into the asset imports
directory as a REGULAR audio file. The pipeline then analyzes and uses it
exactly like any single song — no "virtual music timeline" plumbing needed
downstream; the materialized file IS the timeline.

Seamlessness is measured-first:
- cut points snap to each track's bar grid (facts.bar_sec from the cached
  madmom analysis) so joins land on musical boundaries, not mid-phrase
- segments are loudness-normalized (loudnorm) before joining so no track
  jumps out
- joins are acrossfade over ~2 bars of the outgoing track (1.5-4s), unless
  an explicit crossfade duration is given
"""
from __future__ import annotations

import json
import os
import subprocess
import time


def _track_facts(path: str) -> dict:
    """Cached analysis facts for a track ({} when never analyzed)."""
    try:
        from src.asset_manager.scanner import compute_content_hash
        from src.analyzer import get_analysis_path
        h = compute_content_hash(os.path.abspath(path))
        with open(os.path.join(get_analysis_path(h), "captions.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("facts") or {}
    except Exception:  # noqa: BLE001
        return {}


def _duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def _snap(t: float, bar: float, lo: float, hi: float) -> float:
    """Snap a time to the nearest bar-grid line, clamped to [lo, hi]."""
    if bar and bar > 0.5:
        t = round(t / bar) * bar
    return max(lo, min(hi, t))


def stitch_bgm(tracks: list, out_path: str, crossfade: float = 0.0) -> dict:
    """Join tracks (in order) into one BGM file.

    tracks: [{"path": str, "start": float?, "end": float?}, ...] — start/end
    optional (default whole track); both snap to the track's bar grid.
    crossfade: seconds; 0 = auto (2 bars of the outgoing track, 1.5-4s).

    Returns metadata: {"segments": [...], "joins": [...], "total": float}.
    Raises on ffmpeg failure — callers surface the error, never mask it.
    """
    if len(tracks) < 2:
        raise ValueError("需要至少两首音乐才能拼接")

    segs = []
    for t in tracks:
        p = t.get("path") or ""
        if not os.path.exists(p):
            raise FileNotFoundError(f"音频不存在: {p}")
        dur = _duration(p)
        facts = _track_facts(p)
        bar = float(facts.get("bar_sec") or 0.0)
        s = float(t.get("start") or 0.0)
        e = float(t.get("end") or dur)
        s = _snap(s, bar, 0.0, max(0.0, dur - 5.0))
        e = _snap(e, bar, s + 5.0, dur)
        segs.append({"path": p, "start": round(s, 2), "end": round(e, 2),
                     "duration": round(e - s, 2), "bar_sec": bar or None,
                     "name": os.path.basename(p)})

    # per-join crossfade: ~2 bars of the OUTGOING track (musical breathing),
    # clamped so a slow waltz doesn't smear and a fast track doesn't click
    joins = []
    for k in range(len(segs) - 1):
        if crossfade and crossfade > 0:
            cf = float(crossfade)
        else:
            bar = float(segs[k].get("bar_sec") or 2.0)
            cf = 2.0 * bar
        cf = max(1.5, min(4.0, cf))
        # crossfade must fit inside both neighbors
        cf = min(cf, segs[k]["duration"] / 2.0, segs[k + 1]["duration"] / 2.0)
        joins.append(round(cf, 2))

    cmd = ["ffmpeg", "-y", "-v", "error"]
    for sg in segs:
        cmd += ["-ss", str(sg["start"]), "-t", str(sg["duration"]), "-i", sg["path"]]
    parts = [
        f"[{i}:a]loudnorm=I=-18.0:LRA=11.0:TP=-1.5,aformat=sample_rates=48000:channel_layouts=stereo[n{i}]"
        for i in range(len(segs))
    ]
    prev = "[n0]"
    for k in range(1, len(segs)):
        out = "[aout]" if k == len(segs) - 1 else f"[x{k}]"
        parts.append(f"{prev}[n{k}]acrossfade=d={joins[k - 1]}:c1=tri:c2=tri{out}")
        prev = out
    cmd += ["-filter_complex", ";".join(parts), "-map", "[aout]",
            "-c:a", "libmp3lame", "-b:a", "320k", out_path]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"ffmpeg 拼接失败: {(r.stderr or '')[-400:]}")

    total = _duration(out_path)
    meta = {"segments": segs, "joins": joins, "total": round(total, 2),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        with open(os.path.splitext(out_path)[0] + ".bgmmix.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    return meta

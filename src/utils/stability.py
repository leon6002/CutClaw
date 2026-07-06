"""Measured footage-quality scoring (blur / violent motion).

The VLM judges visual_quality from a handful of STILL frames — it cannot see
the thing that ruins montage picks: violent camera motion (drone course
corrections, whip adjustments, handheld shake). But violent motion leaves a
physical fingerprint on every frame: MOTION BLUR. So we measure that:

- sharpness  = variance of Laplacian at 320×180 gray (blur ⇒ low)
- baseline   = p70 sharpness of 24 frames sampled across the WHOLE source —
               sharpness is scene-dependent (snow is smoother than a forest),
               so a range is judged relative to ITS OWN source's baseline
- disorder   = std of dense optical-flow magnitude (erratic non-uniform
               motion scores high; smooth pans/glides stay low)

score 0–10:  ≥7 crisp, 4–7 usable, <4 visibly degraded (reject-worthy).
Calibrated on real DJI footage: a drone tilt-down correction scored 0.9
(relative sharpness 0.04) while smooth aerials/tracking scored 8.8–10.
"""
import math
import os
import threading

import cv2
import numpy as np

_RANGE_CACHE: dict = {}
_BASELINE_CACHE: dict = {}
_READERS: dict = {}
_LOCK = threading.Lock()


def _reader(video_path: str):
    from decord import VideoReader
    p = os.path.normpath(video_path)
    if p not in _READERS:
        _READERS[p] = VideoReader(p, width=320, height=180, num_threads=2)
    return _READERS[p]


def _sharp(gray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_32F).var())


def _gray(frame):
    return cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)


def _baseline(video_path: str) -> float:
    p = os.path.normpath(video_path)
    with _LOCK:
        if p in _BASELINE_CACHE:
            return _BASELINE_CACHE[p]
        vr = _reader(video_path)
        n = len(vr)
        idx = [int((i + 0.5) * n / 24) for i in range(24)]
        vals = sorted(_sharp(_gray(f)) for f in vr.get_batch(idx).asnumpy())
        base = max(1.0, vals[int(len(vals) * 0.7)])
        _BASELINE_CACHE[p] = base
        return base


def measure_stability(video_path: str, start_sec: float, end_sec: float,
                      samples: int = 5) -> dict:
    """Measured quality of a source-video range.

    Returns {"score": 0-10, "rel_sharp", "disorder", "speed", ...}.
    score=-1 means "could not measure" — callers must NEVER treat that as bad.
    """
    key = (os.path.normpath(video_path or ""), round(float(start_sec), 1),
           round(float(end_sec), 1))
    with _LOCK:
        if key in _RANGE_CACHE:
            return _RANGE_CACHE[key]
    try:
        out = _measure(video_path, float(start_sec), float(end_sec), samples)
    except Exception as e:  # noqa: BLE001
        out = {"score": -1.0, "error": str(e)[:120]}
    with _LOCK:
        _RANGE_CACHE[key] = out
    return out


_HASH_CACHE: dict = {}


def _dhash64(frame) -> int:
    """64-bit difference hash of one RGB frame — a composition fingerprint."""
    g = cv2.resize(_gray(frame), (9, 8), interpolation=cv2.INTER_AREA)
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (1 if int(g[r, c]) > int(g[r, c + 1]) else 0)
    return bits


def visual_hashes(video_path: str, start_sec: float, end_sec: float, n: int = 3) -> list:
    """dHash fingerprints of n frames across a range (hex strings).

    The visual-sameness signature for clustering near-identical moments: a
    slow aerial reads as 'the same photo' even 60s apart, so time distance
    is a useless dedup proxy — composition distance is what the viewer sees."""
    key = ("vh", os.path.normpath(video_path or ""), round(float(start_sec), 1),
           round(float(end_sec), 1), int(n))
    with _LOCK:
        if key in _HASH_CACHE:
            return _HASH_CACHE[key]
        try:
            vr = _reader(video_path)
            fps = float(vr.get_avg_fps() or 24.0)
            nf = len(vr)
            dur = max(0.0, float(end_sec) - float(start_sec))
            idx = sorted({min(nf - 1, max(0, int((float(start_sec) + dur * (i + 0.5) / n) * fps)))
                          for i in range(n)})
            frames = vr.get_batch(idx).asnumpy()
            out = [f"{_dhash64(frames[i]):016x}" for i in range(len(frames))]
        except Exception:  # noqa: BLE001
            out = []
        _HASH_CACHE[key] = out
        return out


def hamming_hex(a: str, b: str) -> int:
    """Bit distance between two hex dHashes (0 identical … 64 unrelated)."""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def _measure(video_path: str, start_sec: float, end_sec: float, samples: int) -> dict:
    base = _baseline(video_path)
    with _LOCK:
        vr = _reader(video_path)
        fps = float(vr.get_avg_fps() or 24.0)
        n = len(vr)
        dur = max(0.0, end_sec - start_sec)
        if dur < 0.3 or n < 10:
            return {"score": -1.0}
        sharps, disorders, speeds, tilts = [], [], [], []
        for i in range(samples):
            t = start_sec + (i + 0.5) * dur / samples
            f0 = min(max(0, int(t * fps)), n - 2)
            fr = vr.get_batch([f0, f0 + 1]).asnumpy()
            g0, g1 = _gray(fr[0]), _gray(fr[1])
            flow = cv2.calcOpticalFlowFarneback(g0, g1, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            mag = np.hypot(flow[..., 0], flow[..., 1])
            speeds.append(float(np.median(mag)) / 320.0 * fps)      # widths/s
            disorders.append(float(np.std(mag)) / 320.0 * fps)
            sharps.append(_sharp(g0))
            # camera roll: near-vertical structures (trees/poles) deviating
            # from vertical = crooked gimbal. Needs enough lines to trust.
            _edges = cv2.Canny(g0, 60, 160)
            _lines = cv2.HoughLinesP(_edges, 1, np.pi / 180, threshold=40,
                                     minLineLength=40, maxLineGap=6)
            if _lines is not None:
                _devs = []
                for _l in _lines[:, 0]:
                    _ang = np.degrees(np.arctan2(float(_l[3] - _l[1]), float(_l[2] - _l[0])))
                    if abs(abs(_ang) - 90) <= 25:
                        _devs.append(abs(abs(_ang) - 90))
                if len(_devs) >= 6:
                    tilts.append(float(np.median(_devs)))

    rel = float(np.median(sharps)) / base
    disorder = float(np.median(disorders))
    speed = float(np.median(speeds))

    # rel ≥ 0.75 → full sharpness marks; decays smoothly below
    sharp_score = 10.0 * min(1.0, rel / 0.75) ** 0.8
    # erratic flow beyond 0.12 w/s eats up to half the score
    disorder_pen = min(1.0, max(0.0, (disorder - 0.12) / 0.25))
    # APPARENT SPEED penalty — user feedback: smooth-but-fast drone sweeps
    # score 10 on sharpness/disorder yet feel dizzy in a calm memory montage.
    # Gentle glides measure ~0.02-0.29 widths/s (keep full marks); beyond
    # 0.30 w/s the motion starts to dominate the frame and gets penalized.
    speed_pen = min(1.0, max(0.0, (speed - 0.30) / 0.45))
    # TILT penalty — user found a shot with a badly crooked gimbal (measured
    # 9.1° vs 1.8° for straight footage). Requires ≥2 frames with enough
    # vertical structure; unmeasurable scenes (open water/sky) are exempt.
    tilt = round(float(np.median(tilts)), 1) if len(tilts) >= 2 else None
    tilt_pen = min(1.0, max(0.0, (tilt - 4.0) / 5.0)) if tilt is not None else 0.0
    score = (sharp_score * (1.0 - 0.5 * disorder_pen)
             * (1.0 - 0.6 * speed_pen) * (1.0 - 0.7 * tilt_pen))
    return {
        "score": round(score, 1),
        "rel_sharp": round(rel, 2),
        "sharp": round(float(np.median(sharps)), 0),
        "baseline": round(base, 0),
        "disorder": round(disorder, 3),
        "speed": round(speed, 3),
        "tilt": tilt,
    }


def quality_per_second(video_path: str, start_sec: float, end_sec: float) -> list:
    """Per-second quality scores across a range — the 'camera adjusting'
    detector. Segment-level medians hide a 1-2s framing wobble inside an
    otherwise fine segment; scanning second by second exposes it so the
    moment can be TRIMMED to its clean core. Windows are range-cached, so
    overlapping moments share the work."""
    out = []
    t = float(start_sec)
    while t < end_sec - 0.4:
        w_end = min(t + 1.0, end_sec)
        st = measure_stability(video_path, t, w_end, samples=1)
        out.append({"t": round(t, 1), "score": st.get("score", -1)})
        t += 1.0
    return out


def longest_clean_run(per_sec: list, floor: float = 3.5) -> tuple:
    """(start_idx, end_idx_exclusive) of the longest contiguous run of seconds
    scoring >= floor (unmeasured seconds count as clean)."""
    best = (0, 0)
    run_start = None
    for i, p in enumerate(per_sec + [{"score": -999}]):
        ok = p["score"] < 0 or p["score"] >= floor
        if i < len(per_sec) and ok:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and i - run_start > best[1] - best[0]:
                best = (run_start, i)
            run_start = None
    return best


def stability_verdict(score: float) -> str:
    """Self-explanatory label the agent can act on without extra prompt text."""
    if score < 0:
        return "unmeasured"
    if score >= 7:
        return "CRISP & STEADY — great pick"
    if score >= 4:
        return "usable"
    return "BLURRY/VIOLENT MOTION — avoid this range"

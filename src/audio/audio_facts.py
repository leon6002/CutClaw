"""Measured audio facts — the signal-analysis layer of audio understanding.

Design principle: anything COMPUTABLE is computed here and treated as ground
truth; LLMs downstream only produce *descriptions* (names, moods, prose) and
are never allowed to emit numbers (timestamps, BPM, energy values).

All functions are defensive: on any failure they return partial/None results
so callers can fall back to legacy behavior instead of crashing the pipeline.
"""

from __future__ import annotations

import statistics


# ── Beat grid ────────────────────────────────────────────────────────────────

def compute_beat_grid(keypoints: list) -> dict:
    """Bar length + felt BPM from madmom downbeat keypoints.

    Downbeats are bar starts; the felt pulse is 240/bar_sec assuming 4/4.
    """
    try:
        db = sorted(kp["time"] for kp in (keypoints or [])
                    if str(kp.get("type", "")).lower().startswith("down"))
        gaps = [b - a for a, b in zip(db, db[1:]) if 0.8 <= b - a <= 8.0]
        if len(gaps) < 4:
            return {}
        bar = statistics.median(gaps)
        return {
            "bar_sec": round(bar, 3),
            "bpm_felt": round(240.0 / bar, 1),
            "downbeat_count": len(db),
            "downbeat_times": [round(t, 3) for t in db],
        }
    except Exception:
        return {}


# ── Energy curve / climax ────────────────────────────────────────────────────

def compute_energy_curve(audio_path: str, hop_s: float = 0.5) -> list | None:
    """Normalized loudness curve [(t, 0..1), ...] via librosa RMS."""
    try:
        import librosa
        import numpy as np
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        hop = max(1, int(hop_s * sr))
        rms = librosa.feature.rms(y=y, frame_length=hop * 2, hop_length=hop)[0]
        if rms.size == 0:
            return None
        lo, hi = float(rms.min()), float(rms.max())
        if hi - lo <= 1e-9:
            return None
        norm = (rms - lo) / (hi - lo)
        times = librosa.frames_to_time(range(len(rms)), sr=sr, hop_length=hop)
        return [(round(float(t), 2), round(float(e), 3)) for t, e in zip(times, norm)]
    except Exception:
        return None


def find_climax(energy_curve: list, smooth_n: int = 8) -> float | None:
    """Time of the global energy peak (moving-average smoothed)."""
    try:
        if not energy_curve or len(energy_curve) < smooth_n * 2:
            return None
        vals = [e for _, e in energy_curve]
        sm = [sum(vals[max(0, i - smooth_n):i + smooth_n]) / len(vals[max(0, i - smooth_n):i + smooth_n])
              for i in range(len(vals))]
        peak_i = max(range(len(sm)), key=lambda i: sm[i])
        return round(energy_curve[peak_i][0], 2)
    except Exception:
        return None


# ── Structure boundaries (novelty segmentation, snapped to downbeats) ───────

def compute_section_boundaries(
    audio_path: str,
    downbeat_times: list,
    duration: float,
    min_len: float = 15.0,
    max_len: float = 45.0,
) -> list | None:
    """Section boundary times from timbre/harmony novelty, snapped to the
    nearest downbeat so section changes land on bar lines.

    Returns [0.0, t1, ..., duration] or None if the analysis is unusable.
    """
    try:
        import librosa
        import numpy as np
        from scipy import signal as _sig

        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        hop = 2048
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
        feats = np.vstack([
            (mfcc - mfcc.mean(axis=1, keepdims=True)) / (mfcc.std(axis=1, keepdims=True) + 1e-9),
            (chroma - chroma.mean(axis=1, keepdims=True)) / (chroma.std(axis=1, keepdims=True) + 1e-9),
        ])
        # smooth features over ~2s to suppress transient noise
        win = max(1, int(2.0 * sr / hop))
        kernel = np.ones(win) / win
        feats = np.apply_along_axis(lambda r: np.convolve(r, kernel, mode="same"), 1, feats)

        # novelty = feature distance across a ±4s window (checkerboard-lite)
        lag = max(1, int(4.0 * sr / hop))
        n = feats.shape[1]
        novelty = np.zeros(n)
        for i in range(lag, n - lag):
            a = feats[:, i - lag:i].mean(axis=1)
            b = feats[:, i:i + lag].mean(axis=1)
            denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9
            novelty[i] = 1.0 - float(np.dot(a, b) / denom)
        if novelty.max() <= 1e-9:
            return None
        novelty /= novelty.max()

        min_dist = max(1, int(min_len * sr / hop))
        peaks, _ = _sig.find_peaks(novelty, distance=min_dist, prominence=0.1)
        times = librosa.frames_to_time(peaks, sr=sr, hop_length=hop)

        # snap to nearest downbeat (section changes should land on bar lines)
        def _snap(t: float) -> float:
            if not downbeat_times:
                return t
            return min(downbeat_times, key=lambda d: abs(d - t))

        bounds = sorted({round(_snap(float(t)), 3) for t in times
                         if min_len * 0.6 < t < duration - min_len * 0.6})
        bounds = [0.0] + bounds + [round(float(duration), 3)]

        # enforce min_len (merge) and max_len (split at interior downbeat)
        merged = [bounds[0]]
        for t in bounds[1:]:
            if t - merged[-1] >= min_len or t == bounds[-1]:
                merged.append(t)
        if len(merged) >= 3 and merged[-1] - merged[-2] < min_len:
            merged.pop(-2)   # absorb a too-short tail section

        final = [merged[0]]
        for t in merged[1:]:
            seg = t - final[-1]
            if seg > max_len:
                n_split = int(seg // max_len) + 1
                step = seg / n_split
                for k in range(1, n_split):
                    ideal = final[-1] + step
                    cand = _snap(ideal)
                    if final[-1] + min_len * 0.6 < cand < t - min_len * 0.6:
                        final.append(round(cand, 3))
                    else:
                        final.append(round(ideal, 3))
            final.append(t)

        return final if len(final) >= 3 else None
    except Exception:
        return None


# ── Per-section statistics ───────────────────────────────────────────────────

def section_stats(boundaries: list, energy_curve: list | None, keypoints: list) -> list:
    """Per-section measured stats: energy mean/max, trend, onset density."""
    out = []
    for i in range(len(boundaries) - 1):
        s, e = boundaries[i], boundaries[i + 1]
        stat = {"start_sec": round(s, 2), "end_sec": round(e, 2),
                "duration_sec": round(e - s, 2)}
        if energy_curve:
            pts = [(t, v) for t, v in energy_curve if s <= t < e]
            if len(pts) >= 3:
                vals = [v for _, v in pts]
                stat["energy_mean"] = round(sum(vals) / len(vals), 3)
                stat["energy_max"] = round(max(vals), 3)
                half = len(vals) // 2
                d = (sum(vals[half:]) / max(1, len(vals[half:]))
                     - sum(vals[:half]) / max(1, len(vals[:half])))
                stat["energy_trend"] = ("building" if d > 0.08
                                        else "fading" if d < -0.08 else "steady")
        n_kp = sum(1 for kp in (keypoints or []) if s <= kp.get("time", -1) < e)
        stat["onset_density_per_10s"] = round(n_kp / max(0.1, (e - s)) * 10.0, 1)
        out.append(stat)
    return out


# ── Assembly ────────────────────────────────────────────────────────────────

def compute_audio_facts(audio_path: str, keypoints: list, duration: float) -> dict:
    """All measured facts for a track. Partial results are fine — every
    consumer must tolerate missing keys."""
    facts: dict = {"version": 1}
    grid = compute_beat_grid(keypoints)
    if grid:
        facts["bar_sec"] = grid["bar_sec"]
        facts["bpm_felt"] = grid["bpm_felt"]
        facts["downbeat_count"] = grid["downbeat_count"]
    curve = compute_energy_curve(audio_path)
    if curve:
        # store a decimated curve (~200 points max) to keep the JSON small
        step = max(1, len(curve) // 200)
        facts["energy_curve"] = curve[::step]
        climax = find_climax(curve)
        if climax is not None:
            facts["climax_sec"] = climax
    bounds = compute_section_boundaries(
        audio_path, grid.get("downbeat_times", []), duration)
    if bounds:
        facts["section_boundaries"] = bounds
        facts["section_stats"] = section_stats(bounds, curve, keypoints)
    return facts

"""Sound-highlight detection — the audio side of "collect the good moments".

The product goal is emotional recall: companions' voices, laughter, ambient
life. Those live in the ORIGINAL audio track, which the montage normally
replaces with BGM. This module finds voice-like segments (speech AND
laughter — both are voiced, pitched sounds) in a source video's audio so:
  1. the editor can prefer ranges that carry them, and
  2. the renderer can duck the BGM and let the real sound through.

Detection is pure signal analysis (project rule: never ask an LLM for a
measurable value). Two stages so wind/water noise can't fool it:
  stage 1 (cheap): voice-band energy ratio high + spectral flatness low
                   + energy above the track's noise floor
  stage 2 (verify): yin f0 periodicity inside candidates — voiced human
                   sound has a stable 70-400 Hz pitch track; wind, rumble
                   and broadband ambience do not.

Output per segment: {"start", "end", "kind": "voice", "strength" 0-1}.
Cached as sound_highlights.json in the video's analysis dir.
"""
import json
import os
import subprocess
import tempfile

import numpy as np

_SR = 16000
_HOP = 0.05          # feature frame hop (s)


def detect_sound_highlights(media_path: str, cache_path: str | None = None) -> list:
    """Detect voice/laughter segments in a media file's audio track.

    Returns a list of segments; [] when the file has no usable audio.
    When cache_path is given, results are read from / written to it.
    """
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f).get("segments", [])
        except Exception:  # noqa: BLE001
            pass
    try:
        segs = _detect(media_path)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [SoundHL] detection failed for {os.path.basename(media_path)}: {str(e)[:150]}")
        return []
    if cache_path:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "segments": segs}, f, ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass
    return segs


def _has_audio_stream(media_path: str) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", media_path],
        capture_output=True, text=True)
    return "audio" in (r.stdout or "")


def _extract_wav(media_path: str) -> str:
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", media_path, "-vn", "-ac", "1", "-ar", str(_SR),
         "-f", "wav", wav],
        capture_output=True, text=True)
    if r.returncode != 0:
        os.unlink(wav)
        raise RuntimeError(f"ffmpeg audio extract failed: {r.stderr[-200:]}")
    return wav


def _detect(media_path: str) -> list:
    import librosa

    # drones (DJI aerials) usually record NO audio at all — that's normal,
    # not an error; voice highlights come from handheld/phone footage
    if not _has_audio_stream(media_path):
        return []

    wav = _extract_wav(media_path)
    try:
        y, sr = librosa.load(wav, sr=_SR, mono=True)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    if y.size < sr:          # under a second of audio
        return []

    hop = int(_HOP * sr)
    n_fft = 1024
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    rms = librosa.feature.rms(S=np.sqrt(S), frame_length=n_fft, hop_length=hop)[0]
    flat = librosa.feature.spectral_flatness(S=np.sqrt(S))[0]
    band = (freqs >= 200) & (freqs <= 3500)
    low = freqs < 150        # wind rumble / handling noise lives here
    total = S.sum(axis=0) + 1e-10
    band_ratio = S[band].sum(axis=0) / total
    low_ratio = S[low].sum(axis=0) / total

    # adaptive energy gate: above the recording's own quiet zone. Phone audio
    # is compressed (whole track loud, tiny spread) — a fixed floor+6dB gate
    # killed everything there, so use whichever is more permissive.
    db = librosa.amplitude_to_db(rms + 1e-10)
    floor = np.percentile(db, 30)
    loud = (db > floor + 4.0) | (db > np.percentile(db, 55))

    # stage 1 is recall-oriented; precision comes from the pitch check below
    cand = loud & (band_ratio > 0.5) & (flat < 0.25) & (low_ratio < 0.6)
    _debug = os.environ.get("CUTCLAW_SHL_DEBUG")

    # frames → merged candidate segments
    times = librosa.frames_to_time(np.arange(len(cand)), sr=sr, hop_length=hop)
    raw = _frames_to_segments(cand, times, min_dur=0.6, max_gap=0.5)
    if _debug:
        print(f"    [SHL debug] cand_frames={int(cand.sum())}/{len(cand)} raw_segments={len(raw)}")
    if not raw:
        return []

    # stage 2: pitch-periodicity verification per candidate
    segs = []
    for s, e in raw:
        a, b = int(s * sr), min(int(e * sr), y.size)
        clip = y[a:b]
        if clip.size < int(0.4 * sr):
            continue
        try:
            f0, voiced_flag, voiced_prob = librosa.pyin(
                clip, fmin=70, fmax=400, sr=sr,
                frame_length=1024, hop_length=hop, fill_na=np.nan)
            voiced = float(np.nanmean(voiced_prob)) if voiced_prob is not None else 0.0
        except Exception:  # noqa: BLE001
            voiced = 0.0
        if _debug:
            print(f"    [SHL debug] cand {s:.1f}-{e:.1f}s voiced_prob={voiced:.2f}")
        # calibration note: mean voiced_prob saturates around 0.3 even for
        # CLEAN speech (only vowel frames are voiced; consonants/pauses
        # dilute the mean). Measured: TTS speech 0.23-0.29, speech dominant
        # in pink noise 0.29-0.34, wind/engine/ambience 0.01-0.06.
        if voiced < 0.18:
            continue          # no stable pitch → wind / broadband ambience
        i0 = max(0, int(s / _HOP))
        i1 = min(len(db), int(e / _HOP))
        strength = float(np.clip(
            0.5 * min(1.0, voiced / 0.30)
            + 0.5 * (np.median(db[i0:i1]) - floor) / 24.0, 0.0, 1.0))
        segs.append({"start": round(float(s), 2), "end": round(float(e), 2),
                     "kind": "voice", "strength": round(strength, 2)})
    return segs


def _frames_to_segments(mask, times, min_dur: float, max_gap: float) -> list:
    out = []
    start = None
    last_true = None
    for m, t in zip(mask, times):
        if m:
            if start is None:
                start = t
            last_true = t
        elif start is not None and t - last_true > max_gap:
            if last_true - start >= min_dur:
                out.append((start, last_true + _HOP))
            start = None
    if start is not None and last_true is not None and last_true - start >= min_dur:
        out.append((start, last_true + _HOP))
    return out


def highlights_in_range(highlights: list, start: float, end: float,
                        min_overlap: float = 0.5) -> list:
    """Highlight segments overlapping [start, end) by at least min_overlap s."""
    hits = []
    for h in highlights or []:
        ov = min(end, h.get("end", 0)) - max(start, h.get("start", 0))
        if ov >= min_overlap:
            hits.append(h)
    return hits

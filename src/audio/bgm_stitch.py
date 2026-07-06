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


def _track_caption(path: str) -> dict:
    """Cached madmom analysis for a track — pipeline cache first, then the
    annotation-track cache ({} when never analyzed)."""
    try:
        from src.asset_manager.scanner import compute_content_hash
        from src.analyzer import get_analysis_path
        h = compute_content_hash(os.path.abspath(path))
        for p in (os.path.join(get_analysis_path(h), "captions.json"),
                  os.path.join("Output", "asset_index", "audio_captions", f"{h}.json")):
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
    except Exception:  # noqa: BLE001
        pass
    return {}


def _track_facts(path: str) -> dict:
    """Measured facts (bar grid / energy / structure) for a track."""
    return _track_caption(path).get("facts") or {}


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


def _measured_segments(path: str) -> dict:
    """Per-track planning data, ALL measured: section spans with energy stats.

    Uses facts.section_stats (signal-derived boundaries + energy) as the
    canonical segment list; the LLM only ever references these by INDEX."""
    cap = _track_caption(path)
    facts = cap.get("facts") or {}
    stats = facts.get("section_stats") or []
    segs = []
    for i, s in enumerate(stats):
        segs.append({
            "idx": i,
            "start": round(float(s.get("start_sec", 0)), 2),
            "end": round(float(s.get("end_sec", 0)), 2),
            "energy": round(float(s.get("energy_mean", 0)), 3),
            "trend": str(s.get("energy_trend", "")),
        })
    return {
        "path": path,
        "name": os.path.basename(path),
        "bpm": facts.get("bpm_felt"),
        "bar_sec": facts.get("bar_sec"),
        "climax_sec": facts.get("climax_sec"),
        "summary": str(cap.get("overall_analysis", {}).get("summary", ""))[:160]
        if isinstance(cap.get("overall_analysis"), dict) else "",
        "segments": segs,
    }


def _fallback_plan(infos: list, target_sec: float) -> list:
    """Deterministic arrangement when the LLM is unavailable/invalid:
    each track contributes its highest-energy contiguous section run of
    ~target/n seconds (greedy expansion around the energy peak). The order
    stays as given. 100%-success rule: never fail if material exists."""
    per = max(20.0, target_sec / max(1, len(infos)))
    picks = []
    for info in infos:
        segs = info["segments"]
        if not segs:
            picks.append({"path": info["path"], "start": None, "end": None})
            continue
        peak = max(range(len(segs)), key=lambda i: segs[i]["energy"])
        lo = hi = peak
        while (segs[hi]["end"] - segs[lo]["start"]) < per:
            left = lo - 1 if lo > 0 else None
            right = hi + 1 if hi < len(segs) - 1 else None
            if left is None and right is None:
                break
            # expand toward the more energetic neighbor
            if right is None or (left is not None
                                 and segs[left]["energy"] >= segs[right]["energy"]):
                lo = left
            else:
                hi = right
        picks.append({"path": info["path"],
                      "start": segs[lo]["start"], "end": segs[hi]["end"]})
    return picks


def plan_bgm_mix(paths: list, target_sec: float = 180.0) -> tuple:
    """AI arrangement over MEASURED segments → ordered picks + rationale.

    The LLM chooses WHICH measured sections of WHICH track fill each role of
    the energy arc (open → build → peak → resolve); every timestamp comes
    from the signal analysis, joins land on bar lines downstream. Any LLM
    failure falls back to a deterministic energy-peak arrangement."""
    infos = [_measured_segments(p) for p in paths]
    usable = [i for i in infos if i["segments"]]
    if len(usable) < 2:
        raise ValueError("至少要有两首已标注(有节奏分析)的歌才能 AI 融合")

    lines = []
    for ti, info in enumerate(usable):
        seg_txt = " | ".join(
            f"S{s['idx']} {s['start']:.0f}-{s['end']:.0f}s e={s['energy']} {s['trend']}"
            for s in info["segments"])
        lines.append(f"Track {ti} 「{info['name']}」 bpm={info['bpm']} bar={info['bar_sec']}s "
                     f"climax@{info['climax_sec']}s\n  {info['summary']}\n  {seg_txt}")

    prompt = (
        "You are a music editor arranging a seamless BGM mix for a travel-memory montage.\n"
        f"Target total duration: about {target_sec:.0f}s (within ±15%).\n\n"
        "Tracks with their MEASURED sections (energy 0-1, trend building/steady/falling):\n"
        + "\n".join(lines) + "\n\n"
        "Arrange 2-4 CONTIGUOUS section runs (each from one track) into an emotional arc: "
        "calm opening → build → peak → gentle resolve.\n"
        "Rules:\n"
        "- Reference sections ONLY by index; never invent times.\n"
        "- Neighboring picks should differ in bpm by <25% when possible.\n"
        "- Prefer joins where the outgoing run ends falling/steady and the incoming starts low "
        "and building — that is where a crossfade disappears.\n"
        "- Use each track at most once.\n"
        'Reply ONLY JSON: {"plan": [{"track": 0, "from": 2, "to": 4, "role": "build"}, ...], '
        '"why": "one short sentence in Chinese"}'
    )

    plan, why = None, ""
    try:
        import litellm
        from src import config as _cfg
        candidates = [
            (getattr(_cfg, "AGENT_LITELLM_MODEL", ""), getattr(_cfg, "AGENT_LITELLM_URL", ""),
             getattr(_cfg, "AGENT_LITELLM_API_KEY", "")),
            (getattr(_cfg, "TRANSLATE_MODEL", ""), getattr(_cfg, "TRANSLATE_ENDPOINT", ""),
             getattr(_cfg, "TRANSLATE_API_KEY", "")),
        ]
        import re as _re
        for model, base, key in candidates:
            if not model:
                continue
            try:
                # reasoning models think BEFORE replying and share the token
                # budget; a small cap truncates the answer to empty
                kwargs = dict(model=model, messages=[{"role": "user", "content": prompt}],
                              temperature=0.4, max_tokens=8000, timeout=120)
                if base:
                    kwargs["api_base"] = base
                if key:
                    kwargs["api_key"] = key
                raw = litellm.completion(**kwargs)
                msg = raw.choices[0].message
                content = (msg.content or "").strip()
                if not content:  # some reasoning models leave the JSON in the thinking text
                    content = str(getattr(msg, "reasoning_content", "") or "").strip()
                if content.startswith("```"):
                    content = _re.sub(r"^```[a-zA-Z]*\s*|\s*```\s*$", "", content)
                m = _re.search(r"\{.*\}", content, _re.DOTALL)
                parsed = json.loads(m.group(0) if m else content)
                if isinstance(parsed, dict) and isinstance(parsed.get("plan"), list):
                    plan, why = parsed["plan"], str(parsed.get("why", ""))[:200]
                    break
            except Exception as e:  # noqa: BLE001
                print(f"[BGMmix] plan via {model} failed: {str(e)[:120]}")
    except Exception:  # noqa: BLE001
        pass

    picks = []
    if plan:
        used = set()
        for item in plan:
            try:
                ti = int(item.get("track"))
                lo, hi = int(item.get("from")), int(item.get("to"))
            except (TypeError, ValueError):
                continue
            if ti in used or not (0 <= ti < len(usable)):
                continue
            segs = usable[ti]["segments"]
            lo = max(0, min(lo, len(segs) - 1))
            hi = max(lo, min(hi, len(segs) - 1))
            used.add(ti)
            picks.append({"path": usable[ti]["path"],
                          "start": segs[lo]["start"], "end": segs[hi]["end"],
                          "role": str(item.get("role", ""))[:20],
                          "track_name": usable[ti]["name"]})
    if len(picks) < 2:
        print("[BGMmix] LLM plan unusable — deterministic energy-peak arrangement")
        picks = _fallback_plan(usable, target_sec)
        for p in picks:
            p["role"] = "auto"
            p["track_name"] = os.path.basename(p["path"])
        why = why or "LLM 编排不可用,按各曲能量峰值段确定性编排"
    return picks, why


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

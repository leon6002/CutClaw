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


def _extract_json_obj(text: str):
    """Last parseable balanced {...} in the text — reasoning models bury the
    final answer after thinking prose that itself contains braces, so a
    greedy first-to-last regex spans garbage."""
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except Exception:  # noqa: BLE001
        pass
    spans = []
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append((start, i + 1))
    for s, e in reversed(spans):
        try:
            v = json.loads(text[s:e])
            if isinstance(v, dict):
                return v
        except Exception:  # noqa: BLE001
            continue
    return None


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
    for k, info in enumerate(infos):
        segs = info["segments"]
        if not segs:
            picks.append({"path": info["path"], "start": None, "end": None})
            continue
        if k == 0:
            # OPENING anchors at the track's own intro (a mid-track excerpt
            # sounds like a radio switched on halfway)
            lo, hi = 0, 0
        elif k == len(infos) - 1:
            # ENDING anchors at the track's final section (natural landing)
            lo = hi = len(segs) - 1
        else:
            lo = hi = max(range(len(segs)), key=lambda i: segs[i]["energy"])
        while (segs[hi]["end"] - segs[lo]["start"]) < per:
            left = lo - 1 if lo > 0 else None
            right = hi + 1 if hi < len(segs) - 1 else None
            if left is None and right is None:
                break
            if k == 0 and right is not None:
                hi = right          # opening grows forward from the intro
            elif k == len(infos) - 1 and left is not None:
                lo = left           # ending grows backward from the outro
            elif right is None or (left is not None
                                   and segs[left]["energy"] >= segs[right]["energy"]):
                lo = left
            else:
                hi = right
        picks.append({"path": info["path"],
                      "start": segs[lo]["start"], "end": segs[hi]["end"]})
    return picks


def footage_brief(video_paths: list) -> str:
    """Compact MEASURED description of the footage a BGM mix must serve:
    moods/tags from annotations, motion mix + voice-moment count + trip span
    from the highlight pools. Empty string when nothing is known."""
    if not video_paths:
        return ""
    try:
        from src.asset_manager.scanner import compute_content_hash
        from src.analyzer import get_analysis_path
        try:
            with open(os.path.join("Output", "asset_index", "annotations.json"),
                      "r", encoding="utf-8") as f:
                idx = json.load(f)
        except Exception:  # noqa: BLE001
            idx = {}
        moods: dict = {}
        motion: dict = {}
        voice_n = 0
        n_clips = 0
        times: list = []
        for p in video_paths:
            try:
                h = compute_content_hash(os.path.abspath(p))
            except Exception:  # noqa: BLE001
                continue
            n_clips += 1
            ann = (idx.get(h) or {}).get("annotation") or {}
            for t in list(ann.get("tags") or []) + list(ann.get("visual_tags") or []) \
                    + ([ann.get("emotion")] if ann.get("emotion") else []):
                k = str(t).strip().lower()
                if k:
                    moods[k] = moods.get(k, 0) + 1
            try:
                with open(os.path.join(get_analysis_path(h), "highlight_pool.json"),
                          "r", encoding="utf-8") as f:
                    for m in json.load(f).get("moments", []):
                        mt = (m.get("motion") or {}).get("type")
                        if mt and mt not in ("unmeasured",):
                            motion[mt] = motion.get(mt, 0) + 1
                        if m.get("sound"):
                            voice_n += 1
                        if m.get("capture_time"):
                            times.append(str(m["capture_time"])[:10])
            except Exception:  # noqa: BLE001
                pass
        if n_clips == 0:
            return ""
        top_moods = ", ".join(k for k, _ in sorted(moods.items(), key=lambda x: -x[1])[:8])
        motion_txt = ", ".join(f"{k}×{v}" for k, v in sorted(motion.items(), key=lambda x: -x[1]))
        span = f"{min(times)} → {max(times)}" if times else "unknown"
        return (
            "\nFOOTAGE THIS MIX MUST SERVE (measured from the selected videos):\n"
            f"- {n_clips} source clips, shot {span}\n"
            + (f"- moods/subjects: {top_moods}\n" if top_moods else "")
            + (f"- measured camera moves across highlight moments: {motion_txt}\n" if motion_txt else "")
            + f"- {voice_n} highlight moment(s) contain REAL voices/laughter — the renderer ducks "
              "the music there, so sparser/softer passages leave room for them\n"
            "Match the musical arc to this footage: calm scenery suits the opening, place the "
            "musical peak where the footage has energy to match, and keep the mix breathable "
            "if voices are plentiful.\n"
        )
    except Exception:  # noqa: BLE001
        return ""


def plan_bgm_mix(paths: list, target_sec: float = 180.0, brief: str = "") -> tuple:
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
        f"Target total duration: about {target_sec:.0f}s (within ±15%).\n"
        + (brief or "") + "\n"
        "Tracks with their MEASURED sections (energy 0-1, trend building/steady/falling):\n"
        + "\n".join(lines) + "\n\n"
        "Arrange CONTIGUOUS section runs (each from one track) into an emotional arc: "
        "calm opening → build → peak → gentle resolve.\n"
        "Rules:\n"
        "- Reference sections ONLY by index; never invent times.\n"
        "- BALANCE: with 4+ tracks provided use AT LEAST 3 of them; no single track may "
        "exceed ~40% of the total — the user picked these songs to HEAR them.\n"
        "- Total duration within ±10% of the target.\n"
        "- OPENING must sound like a real beginning: strongly prefer a run that STARTS at "
        "some track's section 0 (its own intro) — an excerpt from mid-track sounds like a "
        "radio switched on halfway.\n"
        "- ENDING must land: the last run should END on falling/low energy, ideally a track's "
        "final section — never cut off mid-peak.\n"
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
                              temperature=0.4, max_tokens=16000, timeout=180)
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
                parsed = _extract_json_obj(content)
                if parsed is None:
                    raise ValueError(f"no parseable JSON object in reply (len={len(content)})")
                if isinstance(parsed, dict) and isinstance(parsed.get("plan"), list):
                    plan, why = parsed["plan"], str(parsed.get("why", ""))[:200]
                    break
                # parsed but shape wrong — say WHAT came back so failures are diagnosable
                print(f"[BGMmix] {model}: JSON parsed but no plan list "
                      f"(keys={list(parsed)[:6] if isinstance(parsed, dict) else type(parsed).__name__})")
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

    _enforce_bounds(picks, usable, target_sec)
    return picks, why


def _pick_dur(p) -> float:
    return max(0.0, float(p.get("end") or 0) - float(p.get("start") or 0))


def _single_pick_cap(n_picks: int, target_sec: float) -> float:
    """Max seconds one pick may occupy. 45% keeps 4+ track mixes balanced,
    but with few tracks it makes the target INFEASIBLE (2 tracks × 45% = 90%
    of target at best — a 165s ask once shipped 84s): the cap relaxes so the
    picks can always cover the target with headroom."""
    n = max(1, n_picks)
    return max(0.45, 1.0 / n + 0.2) * target_sec


def _enforce_bounds(picks: list, infos: list, target_sec: float) -> None:
    """Deterministic corrections the LLM cannot be trusted with (measured
    boundaries only): total duration in [target−5%, target+12%], and no single
    pick over the per-track cap. Trimming walks whole measured sections off a
    pick — the LAST pick trims from its START (its landing/outro must
    survive), everything else trims from the END. UNDER-length plans (the LLM
    under-picks: asked 165s, planned 84s) are EXTENDED across neighboring
    measured sections — forward for most picks, backward for the last one."""
    segs_by_path = {i["path"]: i["segments"] for i in infos}
    _dur = _pick_dur

    def _trim(p, from_start: bool) -> bool:
        """Drop one measured section from this pick. False when impossible."""
        segs = segs_by_path.get(p["path"]) or []
        inside = [s for s in segs if s["start"] >= p["start"] - 0.5 and s["end"] <= p["end"] + 0.5]
        if len(inside) <= 1:
            return False
        if from_start:
            p["start"] = inside[1]["start"]
        else:
            p["end"] = inside[-2]["end"]
        return True

    def _extend(p, backward: bool) -> float:
        """Widen this pick by one adjacent measured section; returns gained s."""
        segs = segs_by_path.get(p["path"]) or []
        if not segs:
            return 0.0
        if backward:
            prevs = [s for s in segs if s["start"] < float(p["start"]) - 0.01]
            if not prevs:
                return 0.0
            gained = float(p["start"]) - prevs[-1]["start"]
            p["start"] = prevs[-1]["start"]
            return max(0.0, gained)
        nxts = [s for s in segs if s["end"] > float(p["end"]) + 0.01]
        if not nxts:
            return 0.0
        gained = nxts[0]["end"] - float(p["end"])
        p["end"] = nxts[0]["end"]
        return max(0.0, gained)

    cap = _single_pick_cap(len(picks), target_sec)
    for k, p in enumerate(picks):
        while _dur(p) > cap:
            if not _trim(p, from_start=(k == len(picks) - 1)):
                break

    # UNDER-length: extend picks round-robin (shortest first, respecting the
    # cap) until ≥95% of target or the selected tracks' material runs out.
    floor = target_sec * 0.95
    guard = 64
    while sum(_dur(p) for p in picks) < floor and guard > 0:
        guard -= 1
        order = sorted(range(len(picks)), key=lambda i: _dur(picks[i]))
        gained = 0.0
        for k in order:
            if _dur(picks[k]) >= cap:
                continue
            gained = _extend(picks[k], backward=(k == len(picks) - 1))
            if gained > 0:
                break
        if gained <= 0:
            total = sum(_dur(p) for p in picks)
            print(f"⚠️ [BGMmix] 素材不足：编排总长 {total:.0f}s 低于目标 {target_sec:.0f}s，"
                  f"所选曲目的段落已用尽——请增加曲目或降低目标时长", flush=True)
            break

    limit = target_sec * 1.12
    guard = 24
    while sum(_dur(p) for p in picks) > limit and guard > 0:
        guard -= 1
        longest = max(range(len(picks)), key=lambda i: _dur(picks[i]))
        if not _trim(picks[longest], from_start=(longest == len(picks) - 1)):
            break


def stitch_bgm(tracks: list, out_path: str, crossfade: float = 0.0,
               join_sfx: bool = True) -> dict:
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

    # Head/tail polish — the user's ear: a mix that starts mid-phrase or stops
    # at a section boundary feels 没头没尾. When the first pick is NOT the
    # track's own beginning, ease in over ~1 bar; when the last pick is NOT
    # the track's natural ending, breathe out over ~2 bars.
    _first, _last = segs[0], segs[-1]
    fade_in = 0.0
    if _first["start"] > 1.0:
        fade_in = round(max(1.5, min(3.0, float(_first.get("bar_sec") or 2.0))), 2)
    fade_out = 0.0
    _last_dur = _duration(_last["path"])
    if _last_dur and _last["end"] < _last_dur - 1.0:
        fade_out = round(max(3.0, min(6.0, 2.0 * float(_last.get("bar_sec") or 2.0))), 2)

    cmd = ["ffmpeg", "-y", "-v", "error"]
    for sg in segs:
        cmd += ["-ss", str(sg["start"]), "-t", str(sg["duration"]), "-i", sg["path"]]
    parts = []
    for i in range(len(segs)):
        _fx = f",afade=t=in:d={fade_in}" if (i == 0 and fade_in > 0) else ""
        parts.append(
            f"[{i}:a]loudnorm=I=-18.0:LRA=11.0:TP=-1.5,"
            f"aformat=sample_rates=48000:channel_layouts=stereo{_fx}[n{i}]")
    prev = "[n0]"
    for k in range(1, len(segs)):
        out = "[xj]" if k == len(segs) - 1 else f"[x{k}]"
        parts.append(f"{prev}[n{k}]acrossfade=d={joins[k - 1]}:c1=tri:c2=tri{out}")
        prev = out

    # cinematic join sweeteners: a soft pink-noise RISER swelling into each
    # join, ducked well under the music (-22dB-ish). A bare crossfade between
    # two unrelated songs exposes the seam — a swell masks and MOTIVATES it
    # (the standard whoosh/riser trick, synthesized so no sample pack needed).
    join_times = []
    _cum = 0.0
    for i2, sg in enumerate(segs[:-1]):
        _cum += sg["duration"] - joins[i2]
        join_times.append(_cum)
    sfx_inputs = []
    if join_sfx and join_times:
        for j, jt in enumerate(join_times):
            rise = min(3.0, joins[j] + 1.0)          # swell length
            start_at = max(0.0, jt - rise * 0.7)     # peak lands ON the join
            sfx_inputs += ["-f", "lavfi", "-t", f"{rise + 1.0:.2f}",
                           "-i", "anoisesrc=color=pink:sample_rate=48000:amplitude=0.6"]
            parts.append(
                f"[{len(segs) + j}:a]highpass=f=300,lowpass=f=5000,"
                f"afade=t=in:d={rise * 0.7:.2f},afade=t=out:st={rise * 0.7:.2f}:d={rise * 0.3 + 1.0:.2f},"
                f"volume=0.08,aformat=sample_rates=48000:channel_layouts=stereo,"
                f"adelay={int(start_at * 1000)}|{int(start_at * 1000)}[sfx{j}]")
        mix_in = "".join(f"[sfx{j}]" for j in range(len(join_times)))
        parts.append(f"{prev}{mix_in}amix=inputs={1 + len(join_times)}:duration=first:normalize=0[xm]")
        prev = "[xm]"

    if fade_out > 0:
        # expected chain length = segment sum minus crossfade overlaps
        _exp = sum(s["duration"] for s in segs) - sum(joins)
        parts.append(f"{prev}afade=t=out:st={max(0.0, _exp - fade_out):.2f}:d={fade_out}[aout]")
    else:
        parts.append(f"{prev}anull[aout]")
    cmd += sfx_inputs
    cmd += ["-filter_complex", ";".join(parts), "-map", "[aout]",
            "-c:a", "libmp3lame", "-b:a", "320k", out_path]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"ffmpeg 拼接失败: {(r.stderr or '')[-400:]}")

    total = _duration(out_path)
    meta = {"segments": segs, "joins": joins, "total": round(total, 2),
            "fade_in": fade_in, "fade_out": fade_out,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        with open(os.path.splitext(out_path)[0] + ".bgmmix.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    return meta

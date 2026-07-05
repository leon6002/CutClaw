"""Curation-first: the project's HIGHLIGHT POOL — real, measured moments.

The script-first flow had the Screenwriter invent idealized shots and sent
an agent per shot hunting for fiction ("素材可能不符" flags, failures,
fallbacks were all costs of that inversion). For memory montages the
material IS the story: this module enumerates every dense-caption segment
across the selected sources and scores each with MEASURED signals —

    0.40 · VLM content quality (per-segment visual_quality, 1-5)
    0.40 · measured footage quality (blur/violent-motion, stability.py)
    0.15 · sound highlight (real voices/laughter in the original audio)
    0.05 · people presence (companions carry the emotion)

— so the Screenwriter picks from reality instead of imagining it, and
anchored shots need no per-shot agent at all.

Per-source pools cache in the analysis dir (highlight_pool.json, versioned);
the project step only merges them and maps moments onto merged scene indices.
"""
import json
import os

_POOL_VERSION = 5   # v5: camera-roll (tilt) penalty in the measured score

# GLOBAL taste memory — user rejections apply across every project.
REJECTIONS_PATH = os.path.join("Output", "asset_index", "rejections.json")


def load_rejections() -> list:
    try:
        with open(REJECTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def _source_pool(content_hash: str) -> list:
    """Build (or load) the scored moment list for ONE analyzed source."""
    from src.analyzer import get_analysis_path
    cache_dir = get_analysis_path(content_hash)
    pool_path = os.path.join(cache_dir, "highlight_pool.json")
    if os.path.exists(pool_path):
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if int(d.get("version", 0)) >= _POOL_VERSION:
                return d.get("moments", [])
        except Exception:  # noqa: BLE001
            pass

    md_path = os.path.join(cache_dir, "metadata.json")
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            md = json.load(f)
    except Exception:  # noqa: BLE001
        return []
    src_path = md.get("absolute_path") or ""

    # sound highlights (may be absent for pre-feature annotations — fine)
    shl = []
    try:
        with open(os.path.join(cache_dir, "sound_highlights.json"), "r", encoding="utf-8") as f:
            shl = json.load(f).get("segments", [])
    except Exception:  # noqa: BLE001
        pass

    from src.audio.sound_highlights import highlights_in_range
    from src.utils.capture_time import get_capture_time, scene_capture_time
    src_capture = get_capture_time(src_path or md.get("file_name") or "")
    can_measure = bool(src_path and os.path.exists(src_path))
    if can_measure:
        from src.utils.stability import measure_stability

    ckpt_dir = os.path.join(cache_dir, "captions", "ckpt")
    if not os.path.isdir(ckpt_dir):
        return []

    # gather candidate segments first so progress has a real total
    seen = set()
    candidates = []
    for fn in sorted(os.listdir(ckpt_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(ckpt_dir, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        for seg in d.get("dense_segments") or []:
            try:
                s = float(seg.get("start_sec_abs"))
                e = float(seg.get("end_sec_abs"))
            except (TypeError, ValueError):
                continue
            if e - s < 1.6 or (round(s, 1), round(e, 1)) in seen:
                continue
            seen.add((round(s, 1), round(e, 1)))
            candidates.append((s, e, seg))

    # live progress for the UI (polled via the details endpoint). No model
    # calls happen here — VLM scores are read from the annotation cache;
    # footage quality is measured locally (OpenCV), voices via local VAD.
    progress_path = os.path.join(cache_dir, "highlight_pool.progress.json")

    def _progress(done: int, note: str = ""):
        try:
            with open(progress_path, "w", encoding="utf-8") as f:
                json.dump({"done": done, "total": len(candidates), "note": note}, f)
        except Exception:  # noqa: BLE001
            pass

    moments = []
    for _ci, (s, e, seg) in enumerate(candidates):
        _progress(_ci, f"实测画质 {s:.1f}-{e:.1f}s")
        _vq_obj = seg.get("visual_quality") or {}
        vq = _vq_obj.get("score") or 3
        try:
            vq = float(vq)
        except (TypeError, ValueError):
            vq = 3.0
        if vq < 3:
            continue          # VLM already flagged it weak — not pool material
        stab = -1.0
        stab_detail = {}
        trimmed = False
        if can_measure:
            # per-second scan: a segment often contains a 1-2s framing
            # adjustment (violent wobble) that a whole-range median hides.
            # TRIM the moment to its longest clean run instead of averaging
            # the wobble away — this is the user's core ask: keep only the
            # genuinely good part of each take.
            from src.utils.stability import quality_per_second, longest_clean_run
            _ps = quality_per_second(src_path, s, e)
            _i0, _i1 = longest_clean_run(_ps, floor=3.5)
            if _i1 - _i0 <= 0:
                continue      # no clean second at all — pure adjustment take
            _ns, _ne = _ps[_i0]["t"], min(e, _ps[_i1 - 1]["t"] + 1.0)
            if (_ns, _ne) != (s, e):
                trimmed = True
                s, e = _ns, _ne
            if e - s < 1.6:
                continue      # clean core too short to be a usable moment
            _st = measure_stability(src_path, s, e, samples=3)
            stab = float(_st.get("score", -1))
            stab_detail = {"rel_sharp": _st.get("rel_sharp"),
                           "disorder": _st.get("disorder")}
            if 0 <= stab < 3.5:
                continue      # measured blur/violent motion — never a highlight
        voice = highlights_in_range(shl, s, e, min_overlap=0.5)
        cp = seg.get("character_presence") or {}
        people = bool(cp.get("main_character_visible") or cp.get("people_present")
                      or cp.get("other_people_visible"))
        score = (0.40 * (vq / 5.0)
                 + 0.40 * ((stab / 10.0) if stab >= 0 else 0.55)
                 + 0.15 * (1.0 if voice else 0.0)
                 + 0.05 * (1.0 if people else 0.0))
        _ct = scene_capture_time(src_capture, s)
        moments.append({
            "video_path": src_path,
            "source_hash": content_hash,
            "start": round(s, 2),
            "end": round(e, 2),
            "duration": round(e - s, 2),
            "desc": str(seg.get("content_description") or "")[:300],
            "vlm_q": vq,
            # rationale: the VLM's own words on WHY this quality score
            "vlm_notes": str(_vq_obj.get("notes") or "")[:200],
            "stability": stab,
            "stability_detail": stab_detail,
            "sound": bool(voice),
            "people": people,
            "score": round(score, 3),
            "trimmed": trimmed,   # wobbly edges were cut off this moment
            "capture_time": _ct.strftime("%Y-%m-%dT%H:%M:%S") if _ct else None,
        })

    try:
        with open(pool_path, "w", encoding="utf-8") as f:
            json.dump({"version": _POOL_VERSION, "moments": moments}, f,
                      ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass
    try:
        os.remove(progress_path)   # done — the pool file itself signals ready
    except OSError:
        pass
    return moments


def build_highlight_pool(content_hashes: list, merged_scenes_dir: str) -> list:
    """Merged, scene-mapped pool for a project. Moments get stable ids (M0…)
    and a `scene` index pointing into the project's merged scene files."""
    # merged scene windows: (scene_idx, source_hash, start, end)
    windows = []
    try:
        from src.utils.time_format_convert import hhmmss_to_seconds as _to_sec
        for fn in sorted(os.listdir(merged_scenes_dir)):
            if not (fn.startswith("scene_") and fn.endswith(".json")):
                continue
            try:
                idx = int(fn[len("scene_"):-len(".json")])
                with open(os.path.join(merged_scenes_dir, fn), "r", encoding="utf-8") as f:
                    sc = json.load(f)
                tr = sc.get("time_range") or {}
                windows.append((idx, sc.get("_source_hash", ""),
                                _to_sec(str(tr.get("start_seconds", 0))),
                                _to_sec(str(tr.get("end_seconds", 0)))))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass

    pool = []
    for ch in content_hashes:
        for m in _source_pool(ch):
            best, best_ov = None, 0.0
            for idx, src_h, w_s, w_e in windows:
                if src_h != m["source_hash"]:
                    continue
                ov = min(w_e, m["end"]) - max(w_s, m["start"])
                if ov > best_ov:
                    best, best_ov = idx, ov
            if best is None:
                continue          # moment outside every usable merged scene
            mm = dict(m)
            mm["scene"] = best
            pool.append(mm)

    # user taste memory: rejected ranges depress the score (stacking, capped);
    # permanently banned ranges (明确说"不再使用") are excluded outright.
    # Applied at PROJECT-pool time (not the per-source cache) so a fresh
    # rejection takes effect immediately without a pool rebuild.
    rejections = load_rejections()
    if rejections:
        def _normp(p):
            return os.path.normcase(os.path.normpath(p or ""))
        kept = []
        banned_n = 0
        for m in pool:
            pen, reasons, banned = 0.0, [], False
            for r in rejections:
                if _normp(r.get("video_path")) != _normp(m.get("video_path")):
                    continue
                ov = min(m["end"], float(r.get("end", 0))) - max(m["start"], float(r.get("start", 0)))
                if ov <= 0.3:
                    continue
                if r.get("ban"):
                    banned = True
                    break
                pen += 0.15
                if r.get("reason"):
                    reasons.append(str(r["reason"])[:40])
            if banned:
                banned_n += 1
                continue
            if pen > 0:
                m["score"] = round(max(0.0, m["score"] - min(pen, 0.45)), 3)
                m["rejected_overlap"] = reasons[:3]
            kept.append(m)
        pool = kept
        if banned_n:
            print(f"🚫 [Curation] {banned_n} moment(s) excluded by permanent bans (rejections.json)")

    pool.sort(key=lambda m: -m["score"])
    for i, m in enumerate(pool):
        m["id"] = f"M{i}"
    return pool

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

_POOL_VERSION = 1


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

    moments = []
    ckpt_dir = os.path.join(cache_dir, "captions", "ckpt")
    if not os.path.isdir(ckpt_dir):
        return []
    seen = set()
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
            vq = ((seg.get("visual_quality") or {}).get("score") or 3)
            try:
                vq = float(vq)
            except (TypeError, ValueError):
                vq = 3.0
            if vq < 3:
                continue          # VLM already flagged it weak — not pool material
            stab = -1.0
            if can_measure:
                stab = float(measure_stability(src_path, s, e, samples=3).get("score", -1))
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
                "stability": stab,
                "sound": bool(voice),
                "people": people,
                "score": round(score, 3),
                "capture_time": _ct.strftime("%Y-%m-%dT%H:%M:%S") if _ct else None,
            })

    try:
        with open(pool_path, "w", encoding="utf-8") as f:
            json.dump({"version": _POOL_VERSION, "moments": moments}, f,
                      ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
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

    pool.sort(key=lambda m: -m["score"])
    for i, m in enumerate(pool):
        m["id"] = f"M{i}"
    return pool

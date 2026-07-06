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

_POOL_VERSION = 8   # v8: long-take chains — contiguous segments merge into ≤12s
                    # candidates so slow-pacing slots (5-9s) have real supply
                    # (v7: camera-motion signature per moment)

# GLOBAL positive taste memory — user likes apply across every project.
LIKES_PATH = os.path.join("Output", "asset_index", "likes.json")


def load_likes() -> list:
    try:
        with open(LIKES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


# Asset-level ❤️ — "我喜欢这个素材" (coarser than a shot-level 👍 with reason)
HEARTS_PATH = os.path.join("Output", "asset_index", "asset_hearts.json")


def load_hearts() -> set:
    try:
        with open(HEARTS_PATH, "r", encoding="utf-8") as f:
            return {h for h, v in json.load(f).items() if v.get("hearted")}
    except Exception:  # noqa: BLE001
        return set()

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
            # v8 adds long-take CHAIN candidates — they only exist by re-scanning
            # the captioned segments, so older pools (incl. v5-v7) fully rebuild.
            # (The v5/v6 signature-backfill shortcut is retired: it would stamp
            # a chain-less pool as current.)
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

    # gather candidate segments first so progress has a real total.
    # LONG-TAKE CHAINS (pool v8): contiguous captioned segments WITHIN one
    # detected shot merge into longer candidates (≤12s). Single segments cap
    # at their own span (median ~4s in practice), so slow-pacing slots (5-9s)
    # had NO supply — 25/29 anchors once came up shorter than their slot and
    # the film drifted off the beat grid. Chains never cross ckpt files: a
    # file boundary IS a detected hard cut, and a "moment" must not contain
    # one. The per-second clean-run scan below still trims wobble inside a
    # chain; description comes from the chain's first segment.
    _CHAIN_CAP = 12.0
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
        file_cands = []
        for seg in d.get("dense_segments") or []:
            try:
                s = float(seg.get("start_sec_abs"))
                e = float(seg.get("end_sec_abs"))
            except (TypeError, ValueError):
                continue
            if e - s < 1.6 or (round(s, 1), round(e, 1)) in seen:
                continue
            seen.add((round(s, 1), round(e, 1)))
            file_cands.append((s, e, seg))
        file_cands.sort(key=lambda c: c[0])
        chains = []
        for i, (s0, e0, seg0) in enumerate(file_cands):
            # only chain from HEAD segments (nothing contiguous right before)
            if any(0.0 <= s0 - e2 <= 0.25 for (_s2, e2, _g) in file_cands if e2 <= s0):
                continue
            cur_e = e0
            for (s1, e1, _g) in file_cands[i + 1:]:
                if s1 - cur_e > 0.25 or e1 - s0 > _CHAIN_CAP:
                    break
                cur_e = max(cur_e, e1)
            if cur_e - e0 > 0.5:                   # chain actually adds length
                key = (round(s0, 1), round(cur_e, 1))
                if key not in seen:
                    seen.add(key)
                    chains.append((s0, cur_e, seg0))
        candidates.extend(file_cands + chains)

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
            _motion = _st.get("motion") or {"type": "unmeasured"}
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
        _ph = []
        if can_measure:
            from src.utils.stability import visual_hashes
            _ph = visual_hashes(src_path, s, e)
        moments.append({
            "phash": _ph,
            "motion": _motion if can_measure else {"type": "unmeasured"},
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

    # Positive taste memory: liked ranges get a boost (symmetric to the
    # rejection penalty), so praised material floats to the menu's top.
    likes = load_likes()
    if likes:
        def _normp2(p):
            return os.path.normcase(os.path.normpath(p or ""))
        for m in pool:
            bonus, why = 0.0, []
            for lk in likes:
                if _normp2(lk.get("video_path")) != _normp2(m.get("video_path")):
                    continue
                ov = min(m["end"], float(lk.get("end", 0))) - max(m["start"], float(lk.get("start", 0)))
                if ov <= 0.3:
                    continue
                bonus += 0.15
                if lk.get("reason"):
                    why.append(str(lk["reason"])[:40])
            if bonus > 0:
                m["score"] = round(m["score"] + min(bonus, 0.45), 3)
                m["liked_overlap"] = why[:3]

    # Asset hearts: a modest whole-source boost (coarser signal than a
    # shot-level like, so a smaller bonus — the user asked for "稍稍多一点")
    _hearts = load_hearts()
    if _hearts:
        for m in pool:
            if m.get("source_hash") in _hearts:
                m["score"] = round(m["score"] + 0.05, 3)
                m["hearted_source"] = True

    # Blogger-gem bonus (user-calibrated): a steady, coherent camera move
    # (glide/pan/push — NOT chaotic) at pro travel-reel length. Upper bound
    # follows the v8 chains: a clean 8s glide is exactly what slow-pacing
    # slots (5-9s) need surfaced first.
    for m in pool:
        _mo = m.get("motion") or {}
        if (float(m.get("stability", -1)) >= 7.0
                and _mo.get("type") not in (None, "chaotic", "unmeasured")
                and 3.0 <= float(m.get("duration", 0)) <= 9.0):
            m["score"] = round(m["score"] + 0.06, 3)
            m["gem"] = True

    # ── Visual clustering (§14): near-identical compositions become one
    # "look" — the dedup unit the viewer actually perceives. Applied at
    # project-pool time so cross-clip (and cross-source) sameness is caught.
    _cluster_pool(pool)

    pool.sort(key=lambda m: -m["score"])

    # Per-look retention: a visually homogeneous source (slow aerial) floods
    # the pool with near-identical high scorers. Keep cap+1 per cluster (one
    # spare for assembly-time repair) — front-loaded quality control, so no
    # downstream stage ever has to reject-and-retry over sameness.
    from src import config
    _keep = int(getattr(config, "VISUAL_CLUSTER_MAX_USES", 2)) + 1
    _counts: dict = {}
    _kept, _dropped = [], 0
    for m in pool:
        c = m.get("cluster")
        if c is None:
            _kept.append(m)
            continue
        _counts[c] = _counts.get(c, 0) + 1
        if _counts[c] <= _keep:
            _kept.append(m)
        else:
            _dropped += 1
    if _dropped:
        print(f"👁️  [Curation] {_dropped} near-duplicate moment(s) folded away "
              f"({len(_counts)} distinct looks, keeping top {_keep} per look)")
    pool = _kept

    for i, m in enumerate(pool):
        m["id"] = f"M{i}"
    return pool


def build_anchor_budget(pool: list, n_slots: int,
                        cluster_max: int | None = None,
                        source_max: int | None = None) -> list:
    """Deterministic pre-allocation: the anchor MENU the Screenwriter sees.

    Quotas are consumed HERE, not policed after the pick — any subset of a
    quota-satisfying menu also satisfies the quotas, so the LLM literally
    cannot choose wrong (front-loaded quality control, §14 in LOGIC.md).

    Voice moments are seated first (the emotional core, and scarce). When the
    material can't fill the slots under the default caps, caps relax in a
    FIXED order (cluster +1, then source +1, repeat) with a log line each
    step — the only legal failure is running out of material entirely."""
    from src import config
    cmax = int(cluster_max or getattr(config, "VISUAL_CLUSTER_MAX_USES", 2))
    smax = int(source_max or getattr(config, "SOURCE_VIDEO_MAX_USES", 4))
    want = max(int(n_slots * 1.5), n_slots + 5)
    # voices first within the greedy order; score decides the rest
    ordered = sorted(pool, key=lambda m: (-int(bool(m.get("sound"))), -m.get("score", 0)))

    _hearts = load_hearts()   # hearted sources earn one extra menu slot

    def _select(cm: int, sm: int) -> list:
        by_c: dict = {}
        by_s: dict = {}
        chosen = []
        for m in ordered:
            c, s = m.get("cluster"), m.get("source_hash")
            if c is not None and by_c.get(c, 0) >= cm:
                continue
            if by_s.get(s, 0) >= (sm + 1 if s in _hearts else sm):
                continue
            chosen.append(m)
            if c is not None:
                by_c[c] = by_c.get(c, 0) + 1
            by_s[s] = by_s.get(s, 0) + 1
            if len(chosen) >= want:
                break
        return chosen

    relax = 0
    while True:
        chosen = _select(cmax, smax)
        if len(chosen) >= min(want, len(pool)) or len(chosen) >= n_slots:
            break
        if relax % 2 == 0:
            cmax += 1
        else:
            smax += 1
        relax += 1
        print(f"📉 [Curation] anchor budget short ({len(chosen)}/{n_slots} slots) — "
              f"relaxing caps to look≤{cmax}, source≤{smax}")
        if relax > 12:   # pathological pool — hand over whatever exists
            chosen = list(ordered[:want])
            break

    chosen.sort(key=lambda m: -m.get("score", 0))
    return chosen


def _cluster_pool(pool: list) -> None:
    """Greedy leader clustering on dHash — assigns m['cluster'] ('C0', …).

    Distance between two moments = min pairwise hamming across their sampled
    frame hashes; ≤ VISUAL_CLUSTER_HAMMING joins the leader's cluster.
    Moments without hashes get cluster None (never quota-capped)."""
    from src import config
    from src.utils.stability import hamming_hex
    hmax = int(getattr(config, "VISUAL_CLUSTER_HAMMING", 12))
    leaders: list = []   # (cluster_id, leader hash list)
    for m in sorted(pool, key=lambda x: -x.get("score", 0)):
        hs = m.get("phash") or []
        if not hs:
            m["cluster"] = None
            continue
        cid = None
        for lid, lh in leaders:
            d = min(hamming_hex(a, b) for a in hs for b in lh)
            if d <= hmax:
                cid = lid
                break
        if cid is None:
            cid = f"C{len(leaders)}"
            leaders.append((cid, hs))
        m["cluster"] = cid

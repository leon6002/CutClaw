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
import re

_POOL_VERSION = 9   # v9: 最近轴倾斜测量(旧版 ±25° 窗口对 45° 歪斜全盲)+ tilt 入 detail
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
                           "disorder": _st.get("disorder"),
                           "tilt": _st.get("tilt"),
                           "tilt_min": _st.get("tilt_min")}
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
        # 标注时的逐片段细评(fine_review.json)随行进项目池
        try:
            from src.fine_review import load_fine_review
            _fr = load_fine_review(ch)
        except Exception:  # noqa: BLE001
            _fr = {}
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
            _fk = f"{float(m.get('start') or 0):.1f}:{float(m.get('end') or 0):.1f}"
            if _fk in _fr:
                from src.fine_review import apply_measured_caps
                mm["fine"] = apply_measured_caps(_fr[_fk], mm)
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

    # ── 评分 v9(§17):百分位归一 + 事件性 + 稀缺度 + 空镜否决 ──────────
    # 旧公式绝对值相加,白山项目 top15 全部饱和在 0.92——系统丧失区分度;
    # 且"匀速滑过的空镜"稳定+清晰就能拿高分,用户点名"镜头无意义"。
    # 全部吃 moment 里已存的字段,在项目池层重算,无需重建任何缓存。
    _score_reform_v9(pool)

    # L3 审美精评:VLM 对 top 时刻分组排序(结论全局缓存,重跑不重付)
    _l3_aesthetic_rescore(pool)

    pool.sort(key=lambda m: -m["score"])
    # L3 合入后分数变了 → 按最终排名重发 tier(强制分布)
    for _rank, m in enumerate(pool):
        _q = _rank / max(1, len(pool))
        m["tier"] = "S" if _q < 0.10 else "A" if _q < 0.30 else "B" if _q < 0.70 else "C"

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


# 事件动词:画面里"有事发生"(转身/挥手/笑/摔/指/停下)vs 匀速滑过。
# 挖的是密集描述文本(英文),不花一分钱。
_EVENT_STRONG = re.compile(
    r"\b(jump|leap|laugh|smil\w*|wave|wav(es|ing)|hug|fall\w*|tumbl\w*|spray|splash|"
    r"looks? back|look(s|ing) (at|toward)s? the camera|point(s|ing)|gestur\w*|"
    r"celebrat\w*|high.five|danc\w*|stands? up|sits? down|stops? (and|to)|"
    r"turn(s|ing)? (his|her|their) head)\b", re.I)
_EVENT_MILD = re.compile(
    r"\b(turn(s|ing)|approach\w*|pass(es|ing) (close|by)|reveal(s|ing)|"
    r"child\w*|kid s?|runs?|walking toward)\b", re.I)


def _event_strength(desc: str) -> float:
    """1.0 = 明确的瞬间/动作;0.5 = 有变化;0 = 匀速无事件。"""
    t = desc or ""
    if _EVENT_STRONG.search(t):
        return 1.0
    if _EVENT_MILD.search(t):
        return 0.5
    return 0.0


def _score_reform_v9(pool: list) -> None:
    """项目池层重打分(LOGIC.md §17):

    - content/stability/rarity 按**项目内百分位**归一(排名/N)——绝对分
      饱和的数学根治,永远有区分度;
    - event(动词挖掘)与 rarity(1/√簇规模)是新维度:第 15 个同款跟拍
      自动掉价,"有事发生"的瞬间升值;
    - 人脸小权重(用户:风景为主人脸少)、口味加成忽略不计但保留增量;
    - 空镜否决:无人+无事件+无人声 → 乘 0.6 并标记(封不进菜单前列);
    - 旧 score 里的口味/宝石/红心加成以 delta 形式原样保留。
    """
    if len(pool) < 4:
        return
    # 簇规模(rarity 的分母);cluster None = 独一无二
    _csize: dict = {}
    for m in pool:
        c = m.get("cluster")
        if c is not None:
            _csize[c] = _csize.get(c, 0) + 1

    def _pct(values: list) -> dict:
        order = sorted(range(len(values)), key=lambda i: values[i])
        n = max(1, len(values) - 1)
        out = {}
        for rank, i in enumerate(order):
            out[i] = rank / n
        return out

    # content 维度:有细评用细评均分(0-10,严苛量表),否则用标注 VLM 分(1-5 → ×2)
    content_raw = [float((m.get("fine") or {}).get("avg") or 0) or float(m.get("vlm_q") or 3) * 2
                   for m in pool]
    stab_raw = [float(m.get("stability", -1)) if float(m.get("stability", -1)) >= 0 else 5.5
                for m in pool]
    rar_raw = [1.0 / (_csize.get(m.get("cluster"), 1) ** 0.5) for m in pool]
    c_pct, s_pct, r_pct = _pct(content_raw), _pct(stab_raw), _pct(rar_raw)

    empties = 0
    for i, m in enumerate(pool):
        base_old = (0.40 * (float(m.get("vlm_q") or 3) / 5.0)
                    + 0.40 * ((float(m.get("stability", -1)) / 10.0)
                              if float(m.get("stability", -1)) >= 0 else 0.55)
                    + 0.15 * (1.0 if m.get("sound") else 0.0)
                    + 0.05 * (1.0 if m.get("people") else 0.0))
        delta = float(m.get("score") or 0) - base_old   # 口味/宝石/红心加成
        ev = _event_strength(m.get("desc") or "")
        comp = (0.30 * c_pct[i] + 0.25 * s_pct[i] + 0.15 * ev
                + 0.15 * r_pct[i]
                + 0.10 * (1.0 if m.get("sound") else 0.0)
                + 0.05 * (1.0 if m.get("people") else 0.0))
        if not (m.get("people") or m.get("sound") or ev > 0):
            comp *= 0.6
            m["empty_shot"] = True   # 无人无事无声的空镜——呼吸位可用,前列免谈
            empties += 1
        _fine = m.get("fine") or {}
        if _fine.get("tier") in _L3_TIER_SCORE:
            # 标注时细评的 tier 在此合入(同 L3 配方),并占用 grade 展示通道;
            # 这些时刻不再进 L3 排序(省钱,见 _l3_aesthetic_rescore 的过滤)
            comp = 0.65 * comp + 0.35 * _L3_TIER_SCORE[_fine["tier"]]
            m["l3_tier"] = _fine["tier"]
            m["l3_why"] = _fine.get("critique", "")
        m["event"] = ev
        m["rarity"] = round(rar_raw[i], 3)
        m["score"] = round(max(0.0, comp + delta), 3)
    # 分层标签(菜单/UI 展示用):强制分布,"都很好"被制度性禁止
    ranked = sorted(pool, key=lambda m: -m["score"])
    for rank, m in enumerate(ranked):
        q = rank / max(1, len(ranked))
        m["tier"] = "S" if q < 0.10 else "A" if q < 0.30 else "B" if q < 0.70 else "C"
    print(f"🏷️  [Curation] v9 rescore: {len(pool)} moments · "
          f"{sum(1 for m in pool if m.get('event') == 1.0)} strong-event · "
          f"{empties} empty shots demoted · S={sum(1 for m in pool if m['tier'] == 'S')}")


_L3_VERSION = 1
_L3_TIER_SCORE = {"S": 1.0, "A": 0.7, "B": 0.4, "C": 0.15}


def _l3_cache_path() -> str:
    return os.path.join("Output", "asset_index", "aesthetic_scores.json")


def _l3_key(m: dict) -> str:
    return f"{m.get('source_hash')}:{float(m.get('start') or 0):.1f}:{float(m.get('end') or 0):.1f}:v{_L3_VERSION}"


def _l3_frame_b64(m: dict) -> str | None:
    """时刻中间帧 → 640px JPEG base64(给 VLM 排序看的)。"""
    import base64
    import subprocess
    import tempfile
    mid = (float(m.get("start") or 0) + float(m.get("end") or 0)) / 2
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        out = tf.name
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{mid:.2f}", "-i", m["video_path"],
             "-frames:v", "1", "-vf", "scale=640:-2", out],
            capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.getsize(out):
            return None
        with open(out, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def _l3_aesthetic_rescore(pool: list) -> None:
    """L3 审美精评(LOGIC.md §17):VLM **排序而不是打分**。

    绝对打分挤在 3-4 分(v8 饱和的根源之一);6 帧一组、组内强制分层
    (最多 1 个 S、至少 1 个 C)的 listwise 排序区分度好一个量级。只评
    v9 分数 top L3_RESCORE_TOP 的时刻;结论按 (素材指纹+区间) 全局缓存
    (aesthetic_scores.json)——重跑/跨项目复用素材都不重付。失败的组
    保持 v9 分,不重试(重试纪律)。最终分 = 0.65×v9 + 0.35×L3。
    """
    from src import config
    if not getattr(config, "L3_RESCORE", True) or len(pool) < 12:
        return
    top_n = int(getattr(config, "L3_RESCORE_TOP", 120))
    ranked = sorted(pool, key=lambda m: -m["score"])[:top_n]

    cache = {}
    try:
        with open(_l3_cache_path(), "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:  # noqa: BLE001
        cache = {}

    todo = [m for m in ranked if _l3_key(m) not in cache and not m.get("fine")]
    if todo:
        try:
            from src.utils.llm_logger import set_llm_stage
            set_llm_stage("aesthetic_rescore")
        except Exception:  # noqa: BLE001
            pass
        import litellm
        import random
        import threading
        from concurrent.futures import ThreadPoolExecutor
        rng = random.Random(42)
        rng.shuffle(todo)          # 混源分组:同组尽量来自不同文件,对照才有意义
        groups = [todo[i:i + 6] for i in range(0, len(todo), 6)]
        print(f"🎨 [Curation] L3 精评: {len(todo)} 个时刻未缓存 → {len(groups)} 组 VLM 排序"
              f"(每组 6 帧,计费:视觉模型 {len(groups)} 次调用,4 路并行)")
        _lock = threading.Lock()

        def _flush():
            # 每组即时落盘——付费结论一条都不能因中断丢失
            try:
                os.makedirs(os.path.dirname(_l3_cache_path()), exist_ok=True)
                with open(_l3_cache_path(), "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=1)
            except Exception:  # noqa: BLE001
                pass

        def _judge(gi_grp):
            gi, grp = gi_grp
            frames = [(m, _l3_frame_b64(m)) for m in grp]
            frames = [(m, b) for m, b in frames if b]
            if len(frames) < 3:
                return
            content = [{"type": "text", "text":
                        "你是旅拍回忆混剪的选片师。下面是同一批素材里的 %d 个候选画面(编号 1-%d,按顺序)。"
                        "从「值得进成片」的角度综合判断:构图、光影、瞬间感(有没有事发生)、情绪价值。"
                        "把它们分层,**必须拉开档次:最多 1 个 S,至少 1 个 C**,不许全部同档。"
                        "只输出 JSON 数组:[{\"idx\": <1-%d>, \"tier\": \"S|A|B|C\", \"why\": \"<15字内理由>\"}]"
                        % (len(frames), len(frames), len(frames))}]
            for _mi, (_m, b64) in enumerate(frames):
                content.append({"type": "text", "text": f"画面 {_mi + 1}:"})
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            kwargs = dict(model=config.VIDEO_ANALYSIS_MODEL,
                          messages=[{"role": "user", "content": content}],
                          max_tokens=2048, temperature=0.0, timeout=150)
            if getattr(config, "VIDEO_ANALYSIS_ENDPOINT", ""):
                kwargs["api_base"] = config.VIDEO_ANALYSIS_ENDPOINT
            if getattr(config, "VIDEO_ANALYSIS_API_KEY", ""):
                kwargs["api_key"] = config.VIDEO_ANALYSIS_API_KEY
            try:
                raw = litellm.completion(**kwargs)
                txt = (raw.choices[0].message.content or "").strip()
                m_arr = re.search(r"\[.*\]", txt, re.S)
                verdicts = json.loads(m_arr.group(0)) if m_arr else []
            except Exception as e:  # noqa: BLE001
                print(f"⚠️  [Curation] L3 组 {gi + 1}/{len(groups)} 失败(保持 v9 分,不重试): {str(e)[:120]}")
                return
            with _lock:
                for v in verdicts:
                    try:
                        mi = int(v.get("idx")) - 1
                        tier = str(v.get("tier", "")).strip().upper()
                    except Exception:  # noqa: BLE001
                        continue
                    if 0 <= mi < len(frames) and tier in _L3_TIER_SCORE:
                        cache[_l3_key(frames[mi][0])] = {
                            "tier": tier, "why": str(v.get("why") or "")[:60]}
                _flush()
                print(f"🎨 [Curation] L3 组 {gi + 1}/{len(groups)} ✓")

        _workers = max(1, int(getattr(config, "L3_WORKERS", 2)))  # 4 路曾触发 Gemini 503
        with ThreadPoolExecutor(max_workers=_workers) as ex:
            list(ex.map(_judge, enumerate(groups)))

    hit = 0
    for m in ranked:
        if m.get("fine"):
            continue          # 细评 tier 已在 v9 阶段合入,不双重混入
        v = cache.get(_l3_key(m))
        if not v:
            continue
        hit += 1
        m["l3_tier"] = v["tier"]
        m["l3_why"] = v.get("why", "")
        m["score"] = round(0.65 * m["score"] + 0.35 * _L3_TIER_SCORE[v["tier"]], 3)
    if hit:
        _tc = {}
        for m in ranked:
            if m.get("l3_tier"):
                _tc[m["l3_tier"]] = _tc.get(m["l3_tier"], 0) + 1
        print(f"🎨 [Curation] L3 精评合入 {hit}/{len(ranked)} 个时刻: {_tc}")


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
        by_iv: dict = {}   # source_hash -> [(start,end)] 已选区间
        chosen = []
        for m in ordered:
            c, s = m.get("cluster"), m.get("source_hash")
            if c is not None and by_c.get(c, 0) >= cm:
                continue
            if by_s.get(s, 0) >= (sm + 1 if s in _hearts else sm):
                continue
            # 同源区间互斥:精华时刻和长镜链常覆盖同一段画面(如 134-138s 与
            # 134-146s),分数排序下先到者赢,重叠>30%(按较短者算)的后来者
            # 不进菜单——否则同一段素材以两个锚点出现,成片里就是"重复镜头"
            # (白山项目 #7/#21、#10/#16 均由此而来)。
            a, b = float(m.get("start") or 0), float(m.get("end") or 0)
            _dup = False
            for pa, pb in by_iv.get(s, ()):
                ov = min(b, pb) - max(a, pa)
                if ov > 0.3 * max(0.1, min(b - a, pb - pa)):
                    _dup = True
                    break
            if _dup:
                continue
            chosen.append(m)
            if c is not None:
                by_c[c] = by_c.get(c, 0) + 1
            by_s[s] = by_s.get(s, 0) + 1
            by_iv.setdefault(s, []).append((a, b))
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

    # 呼吸镜头保底:评分偏爱运镜(稳定运镜/航拍精华都加分),静止固定机位被
    # 系统性挤出菜单——白山成片 26 镜头 0 static,全片没有一次"停下来"。
    # 菜单里保证至少 N 个高分静止时刻(仍守同源区间互斥,不吃簇配额)。
    breath_min = int(getattr(config, "STATIC_BREATH_MIN", 3))
    _static = lambda m: ((m.get("motion") or {}).get("type") == "static")  # noqa: E731
    _have = sum(1 for m in chosen if _static(m))
    if _have < breath_min:
        _ids = {id(m) for m in chosen}
        _ivs: dict = {}
        for m in chosen:
            _ivs.setdefault(m.get("source_hash"), []).append(
                (float(m.get("start") or 0), float(m.get("end") or 0)))
        for m in sorted((x for x in pool if _static(x) and id(x) not in _ids),
                        key=lambda x: -x.get("score", 0)):
            a, b = float(m.get("start") or 0), float(m.get("end") or 0)
            if any(min(b, pb) - max(a, pa) > 0.3 * max(0.1, min(b - a, pb - pa))
                   for pa, pb in _ivs.get(m.get("source_hash"), ())):
                continue
            chosen.append(m)
            _ivs.setdefault(m.get("source_hash"), []).append((a, b))
            _have += 1
            if _have >= breath_min:
                break
        print(f"🍃 [Curation] breathing shots: {_have} static moment(s) guaranteed in menu"
              + ("" if _have >= breath_min else f" (pool only has {_have})"))

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

"""Tab 1: Asset Manager — scan, annotate, browse, auto-select."""

import json
import os
import queue
import threading

import streamlit as st

from ..helpers import PROJECT_ROOT, cfg, save_config


def _load_analysis_details(content_hash: str) -> dict | None:
    """Load per-clip and per-scene analysis from the analyzed cache."""
    from src.analyzer import get_analysis_path
    cache_dir = get_analysis_path(content_hash)
    if not os.path.isdir(cache_dir):
        return None
    result = {}
    # Per-clip captions
    ckpt_dir = os.path.join(cache_dir, "captions", "ckpt")
    if os.path.isdir(ckpt_dir):
        clips = []
        for fn in sorted(os.listdir(ckpt_dir)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(ckpt_dir, fn), "r", encoding="utf-8") as f:
                        clip = json.load(f)
                    clips.append(clip)
                except Exception:
                    pass
        result["clips"] = clips
    # Scene summaries
    summaries_dir = os.path.join(cache_dir, "captions", "scene_summaries_video")
    if os.path.isdir(summaries_dir):
        scenes = []
        for fn in sorted(os.listdir(summaries_dir)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(summaries_dir, fn), "r", encoding="utf-8") as f:
                        scenes.append(json.load(f))
                except Exception:
                    pass
        result["scenes"] = scenes
    # Metadata
    meta_path = os.path.join(cache_dir, "metadata.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                result["metadata"] = json.load(f)
        except Exception:
            pass
    return result if result else None


def _render_asset_card(_ann, _idx0, asset_root):
    """Render a single asset card with media preview and annotations."""
    _a = _ann.annotation
    _meta = _ann.metadata
    _atype = _ann.asset_type
    _score = getattr(_a, 'quality_score', 5.0)
    _abs_path = getattr(_meta, 'absolute_path', '')
    _fn = os.path.basename(_ann.file_path)
    _dur = getattr(_meta, 'duration_sec', 0) if hasattr(_meta, 'duration_sec') else 0
    _res = f"{getattr(_meta, 'width', 0)}x{getattr(_meta, 'height', 0)}" if hasattr(_meta, 'width') else ""
    _size = getattr(_meta, 'file_size_mb', 0)

    # Media preview
    _seek_key = f"seek_{_ann.content_hash[:12]}"
    _seek_time = st.session_state.get(_seek_key, 0)
    if _abs_path and os.path.exists(_abs_path):
        if _atype == "video":
            if _seek_time > 0:
                st.video(_abs_path, start_time=_seek_time)
                if st.button("⏹ Stop seeking", key=f"stop_{_seek_key}"):
                    st.session_state[_seek_key] = 0
                    st.rerun()
            else:
                st.video(_abs_path)
        elif _atype == "image":
            st.image(_abs_path, use_container_width=True)
        elif _atype == "audio":
            st.audio(_abs_path)

    # Header
    st.markdown(f"**{_fn}**  `Q:{_score:.1f}`  {_dur:.0f}s  {_res}  {_size:.1f}MB")

    # Annotation summary fields
    _fields_shown = []
    for _field, _label in [
        ("summary", "Summary"), ("emotion", "Emotion"),
        ("tags", "Tags"), ("visual_tags", "Visual"),
        ("scene_types", "Scene"), ("camera_movement", "Camera"),
        ("time_of_day", "Time"), ("key_colors", "Colors"),
        ("suggested_use", "Use"), ("has_people", "People"),
        ("genre", "Genre"), ("energy_level", "Energy"), ("bpm", "BPM"),
    ]:
        _val = getattr(_a, _field, None)
        if _val is None or _val == "" or _val == [] or _val == 0.0:
            continue
        if isinstance(_val, list):
            _val = ", ".join(str(v) for v in _val)
        elif isinstance(_val, bool):
            _val = "Yes" if _val else "No"
        elif isinstance(_val, float):
            _val = f"{_val:.1f}"
        _fields_shown.append(f"**{_label}:** {_val}")
    if _fields_shown:
        st.caption("  \n".join(_fields_shown))

    # Show any stored error/warning for this asset
    _err_key = f"err_{_ann.content_hash[:12]}"
    if st.session_state.get(_err_key):
        st.error(st.session_state[_err_key])
        if st.button("✕ Dismiss", key=f"dismiss_{_ann.content_hash[:12]}"):
            del st.session_state[_err_key]
            st.rerun()

    # Re-annotate button (kicks off background thread, progress shows inline below)
    _reann_key = f"reannotating_{_ann.content_hash[:12]}"
    if st.button("🔄 Re-annotate", key=f"reann_{_ann.content_hash[:12]}", help="Force re-analysis of this asset"):
        st.session_state[_reann_key] = True
        if st.session_state.get(_err_key):
            del st.session_state[_err_key]
        # Use subprocess (like the pipeline) so all conda deps are available
        st.session_state[f"seek_{_ann.content_hash[:12]}"] = 0
        st.rerun()

    # Show inline progress if this asset is being re-annotated (via subprocess)
    if st.session_state.get(_reann_key):
        _proc_key = f"reann_proc_{_ann.content_hash[:12]}"
        # Start subprocess if not already running
        if _proc_key not in st.session_state:
            import subprocess as _sp, sys as _sys, tempfile, os as _os2

            # Find the right Python (prefer CutClaw conda env which has decord/torch)
            _python_exe = _sys.executable
            for _cand in [
                os.path.join(os.path.dirname(_sys.prefix), "envs", "CutClaw", "python.exe"),
                os.path.join(_sys.prefix, "..", "..", "envs", "CutClaw", "python.exe"),
            ]:
                _cand = os.path.abspath(_cand)
                if os.path.exists(_cand):
                    _python_exe = _cand
                    break
            try:
                import decord  # noqa: F401
            except ImportError:
                pass  # _python_exe already set to conda Python above if available

            script = f"""
import sys; sys.path.insert(0, '.')
# Fix Windows GBK console encoding
for s in (sys.stdout, sys.stderr):
    if hasattr(s, 'reconfigure'):
        try: s.reconfigure(encoding='utf-8', errors='replace')
        except Exception: pass
from src.analyzer import analyze_video, analyze_audio, get_analysis_path
from src.asset_manager.annotator import annotate_asset
from src.asset_manager.index_store import upsert_annotations
from src.asset_manager.scanner import probe_video_metadata, probe_audio_metadata, compute_content_hash
import os, json

abs_path = {repr(_abs_path)}
force = True
print('[stage] shot_detection start')
ch = compute_content_hash(abs_path)
fs = os.path.getsize(abs_path)
if abs_path.lower().endswith(('.mp4','.mkv','.mov','.avi','.webm','.m4v')):
    meta = probe_video_metadata(abs_path, fs)
    analyze_video(abs_path, force=force)
else:
    meta = probe_audio_metadata(abs_path, fs)
    analyze_audio(abs_path, force=force)
meta.file_path = os.path.basename(abs_path)
meta.content_hash = ch
print('[stage] done')
new_ann = annotate_asset(meta)
upsert_annotations([new_ann])
ckpt_dir = os.path.join(get_analysis_path(ch), 'captions', 'ckpt')
cc = len([f for f in os.listdir(ckpt_dir) if f.endswith('.json')]) if os.path.isdir(ckpt_dir) else 0
print(f'CLIPS:{{cc}}')
print('DONE')
"""
            # Write to temp file to avoid shell escaping issues
            _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
            _tmp.write(script)
            _tmp.close()
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            try:
                proc = _sp.Popen(
                    [_python_exe, _tmp.name],
                    stdout=_sp.PIPE, stderr=_sp.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    bufsize=1, cwd=PROJECT_ROOT, env=env,
                )
                st.session_state[_proc_key] = proc
                st.session_state[f"reann_tmp_{_ann.content_hash[:12]}"] = _tmp.name
                st.session_state[f"reann_lines_{_ann.content_hash[:12]}"] = []
            except Exception as e:
                st.session_state[_err_key] = str(e)
                st.session_state[_reann_key] = False
                _os2.unlink(_tmp.name)
                st.rerun()

        @st.fragment(run_every=0.5)
        def _reann_progress():
            proc = st.session_state.get(_proc_key)
            lines_key = f"reann_lines_{_ann.content_hash[:12]}"
            lines = st.session_state.get(lines_key, [])
            if proc:
                # Read new lines
                new_lines = []
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    new_lines.append(line.rstrip())
                lines.extend(new_lines)
                st.session_state[lines_key] = lines

                # Parse stages
                _stages: dict[str, dict] = {}
                _stage_map = {
                    "shot_detection": "🎞️ Shot Detect",
                    "captioning": "🎬 Captioning",
                    "scene_merge": "🧩 Merge",
                    "scene_analysis": "🔍 Analysis",
                }
                for line in lines:
                    if "shot_detection start" in line:
                        _stages["shot_detection"] = {"status": "start", "detail": "running..."}
                    elif "shot_detection done" in line:
                        _stages["shot_detection"] = {"status": "done", "detail": ""}
                    elif "captioning start" in line:
                        _stages["captioning"] = {"status": "start", "detail": line.split("start", 1)[-1].strip()}
                    elif "captioning done" in line:
                        _stages["captioning"] = {"status": "done", "detail": line.split("done", 1)[-1].strip()}
                    elif "captioning skip" in line:
                        _stages["captioning"] = {"status": "skip", "detail": "cached"}
                    elif "scene_merge start" in line:
                        _stages["scene_merge"] = {"status": "start", "detail": ""}
                    elif "scene_merge done" in line:
                        _stages["scene_merge"] = {"status": "done", "detail": line.split("done", 1)[-1].strip()}
                    elif "scene_merge skip" in line:
                        _stages["scene_merge"] = {"status": "skip", "detail": "cached"}
                    elif "scene_analysis start" in line:
                        _stages["scene_analysis"] = {"status": "start", "detail": line.split("start", 1)[-1].strip()}
                    elif "scene_analysis done" in line:
                        _stages["scene_analysis"] = {"status": "done", "detail": line.split("done", 1)[-1].strip()}
                    elif "scene_analysis skip" in line:
                        _stages["scene_analysis"] = {"status": "skip", "detail": "cached"}

                # Render stage columns
                _cols = st.columns(4)
                for i, (sk, sl) in enumerate(_stage_map.items()):
                    s = _stages.get(sk, {})
                    sts = s.get("status", "pending")
                    detail = s.get("detail", "")
                    emoji = {"done": "✅", "start": "🔄", "skip": "⏭️"}.get(sts, "⏳")
                    with _cols[i]:
                        st.caption(f"{emoji} {sl}")
                        if detail:
                            st.caption(f"_{detail[:60]}_")

                # Show latest few log lines
                if lines:
                    with st.expander("📋 Log", expanded=False):
                        st.caption("\n".join(lines[-10:]))

                # Check for completion
                if proc.poll() is not None:
                    all_text = "\n".join(lines)
                    import re as _re2
                    clips_match = _re2.search(r'CLIPS:(\d+)', all_text)
                    clip_count = int(clips_match.group(1)) if clips_match else -1
                    if proc.returncode == 0:
                        if clip_count == 0:
                            st.warning("Analysis completed but 0 clips produced. VLM may have timed out.")
                        else:
                            st.success(f"✅ Done — {clip_count} clips detected!")
                    else:
                        st.session_state[_err_key] = f"Exit code {proc.returncode}\n\n{all_text[-1000:]}"
                    # Cleanup
                    del st.session_state[_proc_key]
                    del st.session_state[lines_key]
                    _tmpf = st.session_state.pop(f"reann_tmp_{_ann.content_hash[:12]}", None)
                    if _tmpf and os.path.exists(_tmpf):
                        try: os.unlink(_tmpf)
                        except Exception: pass
                    st.session_state[_reann_key] = False
                    st.rerun()

        _reann_progress()

    # Detailed per-clip / per-scene analysis
    _details = _load_analysis_details(_ann.content_hash)
    if _details:
        clips = _details.get("clips", [])
        if not clips:
            st.warning("No clips were detected in this video. Try re-annotating.")
        else:
            with st.expander(f"📋 {len(clips)} clips detected", expanded=False):
                for ci, clip in enumerate(clips):
                    dur = clip.get("duration", {})
                    start = dur.get("clip_start_time", "?") if isinstance(dur, dict) else "?"
                    end = dur.get("clip_end_time", "?") if isinstance(dur, dict) else "?"
                    action = clip.get("action_atoms", {})
                    narrative = clip.get("narrative_analysis", {})
                    cine = clip.get("cinematography", {})
                    event = action.get("event_summary", "") if isinstance(action, dict) else ""
                    mood = narrative.get("mood", "") if isinstance(narrative, dict) else ""
                    shot = cine.get("shot_scale", "") if isinstance(cine, dict) else ""
                    cam = cine.get("camera_movement", "") if isinstance(cine, dict) else ""
                    # Parse start time to seconds for seek
                    _start_sec = 0.0
                    try:
                        _parts = str(start).split(":")
                        if len(_parts) == 3:
                            _start_sec = int(_parts[0]) * 3600 + int(_parts[1]) * 60 + float(_parts[2])
                        elif len(_parts) == 2:
                            _start_sec = int(_parts[0]) * 60 + float(_parts[1])
                    except Exception:
                        pass

                    c_col1, c_col2 = st.columns([0.5, 9.5])
                    with c_col1:
                        if st.button("▶", key=f"seek_{_ann.content_hash[:12]}_{ci}", help=f"Play from {start}"):
                            st.session_state[_seek_key] = int(_start_sec)
                            st.rerun()
                    with c_col2:
                        st.caption(
                            f"**Clip {ci + 1}** ({start} → {end})"
                            + (f" | {shot}" if shot else "")
                            + (f" | {cam}" if cam else "")
                            + (f" | 🎭 {mood}" if mood else "")
                        )
                        if event:
                            st.caption(f"  {event[:200]}")
                        dense = clip.get("dense_segments", [])
                        if dense:
                            st.caption(f"⏱️ {len(dense)} time segments:")
                            for seg in dense:
                                    ts = seg.get("timestamp", "")
                                    ts_abs = seg.get("timestamp_absolute", "")
                                    desc = seg.get("content_description", "")
                                    vq = seg.get("visual_quality", {})
                                    qs = vq.get("score", "") if isinstance(vq, dict) else ""
                                    em = seg.get("emotion", {})
                                    mood = em.get("mood", "") if isinstance(em, dict) else ""
                                    rec = seg.get("editor_recommendation", "")
                                    st.caption(f"**{ts}**" + (f"  ({ts_abs})" if ts_abs else ""))
                                    if qs: st.caption(f"  Quality: {qs}" + (f" | {mood}" if mood else ""))
                                    if desc: st.caption(f"  {desc[:200]}")
                                    if rec: st.caption(f"  💡 {rec[:150]}")
                                    st.markdown("---")
        scenes = _details.get("scenes", [])
        if scenes:
            with st.expander(f"🎬 {len(scenes)} scenes", expanded=False):
                for si, scene in enumerate(scenes):
                    va = scene.get("video_analysis", {}).get("scene_caption", {})
                    sc = va.get("scene_summary", {}) or va.get("visual_analysis", {}) or {}
                    summary = sc.get("summary", sc.get("narrative", "")) if isinstance(sc, dict) else ""
                    cls_info = va.get("scene_classification", {})
                    usable = cls_info.get("is_usable", True) if isinstance(cls_info, dict) else True
                    importance = cls_info.get("importance_score", 5) if isinstance(cls_info, dict) else 5
                    time_range = scene.get("time_range", {})
                    st.caption(f"**Scene {si + 1}** ({time_range.get('start_seconds','?')} → {time_range.get('end_seconds','?')})  usable={usable}  importance={importance}")
                    if summary:
                        st.caption(f"  {str(summary)[:300]}")


def _render_unannotated_card(meta, asset_root):
    """Render a card for an unannotated asset."""
    _abs_path = getattr(meta, 'absolute_path', '')
    _fn = getattr(meta, 'file_name', os.path.basename(getattr(meta, 'file_path', '')))
    _dur = getattr(meta, 'duration_sec', 0) if hasattr(meta, 'duration_sec') else 0
    _res = f"{getattr(meta, 'width', 0)}x{getattr(meta, 'height', 0)}" if hasattr(meta, 'width') else ""
    _size = getattr(meta, 'file_size_mb', 0)
    _atype = getattr(meta, 'asset_type', 'video')

    if _abs_path and os.path.exists(_abs_path):
        if _atype == "video":
            st.video(_abs_path)
        elif _atype == "image":
            st.image(_abs_path, use_container_width=True)
        elif _atype == "audio":
            st.audio(_abs_path)

    st.markdown(f"**{_fn}**  `Not annotated`  {_dur:.0f}s  {_res}  {_size:.1f}MB")
    if st.button("🏷️ Generate", key=f"gen_{meta.content_hash[:12]}", use_container_width=True):
        _annotate_single(meta)
        st.rerun()


def _annotate_single(meta):
    """Annotate a single asset synchronously."""
    from src.asset_manager.annotator import annotate_asset
    from src.asset_manager.index_store import upsert_annotations
    with st.spinner(f"Analyzing {getattr(meta, 'file_name', '')}..."):
        ann = annotate_asset(meta)
        upsert_annotations([ann])


def render_tab_assets():
    st.markdown("### 📁 Asset Manager")

    asset_root = st.text_input("Asset Folder", value=st.session_state.asset_root_dir, key="si_asset_root2")
    st.session_state.asset_root_dir = asset_root

    # Buttons row
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        scan_clicked = st.button("🔍 Scan", key="btn_scan2", use_container_width=True)
    with c2:
        disabled_gen = not st.session_state.asset_scanned
        gen_all_clicked = st.button("🏷️ Generate All", key="btn_gen_all", use_container_width=True, disabled=disabled_gen)
    with c3:
        from src.asset_manager.index_store import load_index
        idx = load_index()
        sel_clicked = st.button("✨ Auto-Select", key="btn_auto2", use_container_width=True, disabled=not bool(idx))
    with c4:
        if st.button("🗑️ Clear Selection", key="btn_clear2", use_container_width=True):
            st.session_state.asset_selection = None
            st.rerun()

    # Scan
    if scan_clicked:
        abs_root = os.path.join(PROJECT_ROOT, asset_root) if not os.path.isabs(asset_root) else asset_root
        if os.path.isdir(abs_root):
            with st.spinner("Scanning..."):
                from src.asset_manager.scanner import scan_asset_directory
                st.session_state.scanned_assets = scan_asset_directory(abs_root)
                st.session_state.asset_scanned = True
                st.session_state.asset_selection = None
            st.rerun()
        else:
            st.warning(f"Folder not found: {abs_root}")

    # Generate All
    if gen_all_clicked and st.session_state.asset_scanned:
        _run_batch_annotation()
        st.rerun()

    # Auto-Select
    if sel_clicked:
        with st.spinner("Agent selecting..."):
            from src.asset_manager.selector import select_assets
            target_dur = float(cfg("AUDIO_SEGMENT_MAX_DURATION_SEC", "20.0")) - 5.0
            _instr = cfg("INSTRUCTION", "").strip()
            selection = select_assets(instruction=_instr if _instr else "travel montage", target_duration_sec=max(15.0, target_dur))
            st.session_state.asset_selection = selection
            abs_root2 = os.path.join(PROJECT_ROOT, asset_root) if not os.path.isabs(asset_root) else asset_root
            abs_videos = [os.path.join(abs_root2, p) if not os.path.isabs(p) else p for p in selection.selected_videos]
            if selection.selected_images:
                from src.utils.video_concat import create_slideshow_video
                ss = create_slideshow_video(
                    [os.path.join(abs_root2, p) if not os.path.isabs(p) else p for p in selection.selected_images],
                    duration_per_image=float(cfg("ASSET_IMAGE_DURATION_SEC", "3.0")))
                if ss: abs_videos.append(ss)
            if abs_videos: save_config("VIDEO_PATH", "||".join(abs_videos))
            if selection.selected_audio:
                a = selection.selected_audio[0]
                save_config("AUDIO_PATH", os.path.join(abs_root2, a) if not os.path.isabs(a) else a)
            st.rerun()

    # Selection display
    sel = st.session_state.asset_selection
    if sel and (sel.selected_videos or sel.selected_audio):
        with st.expander("📋 Current Selection", expanded=True):
            for v in sel.selected_videos[:5]: st.caption(f"📹 {os.path.basename(v)}")
            for img in sel.selected_images[:5]: st.caption(f"🖼️ {os.path.basename(img)}")
            for a in sel.selected_audio[:1]: st.caption(f"🎵 {os.path.basename(a)}")
            if sel.rationale: st.caption(f"💡 {sel.rationale[:300]}")

    # ── Annotation progress (background, auto-refreshing) ──
    if st.session_state.get("annotation_running"):
        @st.fragment(run_every=0.5)
        def _annotation_progress_fragment():
            _render_annotation_progress()
        _annotation_progress_fragment()
        if st.session_state.get("annotation_done"):
            st.session_state.annotation_done = False
            st.rerun()

    # ── Asset list ──
    if not st.session_state.asset_scanned or not st.session_state.scanned_assets:
        st.info("👆 Click **Scan** to discover media files in the asset folder.")
        return

    scanned = st.session_state.scanned_assets
    from src.asset_manager.index_store import load_index, find_new_assets, count_by_type

    idx = load_index()
    new_assets = find_new_assets(scanned)
    counts = count_by_type()

    # Summary bar
    videos = [a for a in scanned if a.asset_type == "video"]
    images = [a for a in scanned if a.asset_type == "image"]
    audios = [a for a in scanned if a.asset_type == "audio"]
    cached_total = counts.get('video', 0) + counts.get('image', 0) + counts.get('audio', 0)
    st.info(f"📹 {len(videos)} videos · 🖼️ {len(images)} images · 🎵 {len(audios)} audio | 💾 {cached_total} annotated · 🆕 {len(new_assets)} new")

    # Tabs by type
    tabs = st.tabs([f"📹 Videos ({len(videos)})", f"🖼️ Images ({len(images)})", f"🎵 Audio ({len(audios)})"])

    for ti, atype in enumerate(["video", "image", "audio"]):
        with tabs[ti]:
            type_assets = [a for a in scanned if a.asset_type == atype]
            if not type_assets:
                st.caption(f"No {atype} assets found.")
                continue

            # Separate annotated vs unannotated
            annotated = {}
            unannotated = []
            for meta in type_assets:
                if meta.content_hash in idx:
                    annotated[meta.content_hash] = idx[meta.content_hash]
                else:
                    unannotated.append(meta)

            # Annotated first (sorted by quality)
            if annotated:
                st.markdown("##### ✅ Annotated")
                for h, ann in sorted(annotated.items(), key=lambda kv: getattr(kv[1].annotation, 'quality_score', 0), reverse=True):
                    with st.container(border=True):
                        _render_asset_card(ann, idx, asset_root)

            # Unannotated
            if unannotated:
                st.markdown("##### ⏳ Needs Annotation")
                for meta in unannotated:
                    with st.container(border=True):
                        _render_unannotated_card(meta, asset_root)


def _run_batch_annotation():
    """Start background annotation of all new assets."""
    from src.asset_manager.index_store import find_new_assets
    new_a = find_new_assets(st.session_state.scanned_assets)
    if not new_a:
        st.info("All assets already annotated.")
        return
    st.session_state.annotation_running = True
    st.session_state.annotation_queue = queue.Queue()

    def _run(new_assets, q):
        def _stage_cb(stage, status, detail):
            q.put({"type": "stage", "stage": stage, "status": status, "detail": detail})
        def _file_cb(current, total, filename):
            q.put({"type": "file", "current": current, "total": total, "filename": filename})
        try:
            from src.asset_manager.annotator import batch_annotate
            from src.analyzer import get_analysis_path
            import os as _os2
            results = batch_annotate(new_assets, progress_callback=_file_cb, stage_callback=_stage_cb)
            # Check for assets that produced 0 clips
            empty_clips = []
            for r in results:
                cache_dir = get_analysis_path(r.content_hash) if r else None
                if cache_dir:
                    ckpt = _os2.path.join(cache_dir, "captions", "ckpt")
                    if _os2.path.isdir(ckpt) and not any(f.endswith(".json") for f in _os2.listdir(ckpt)):
                        empty_clips.append(os.path.basename(r.file_path))
            if empty_clips:
                q.put({"type": "warning", "warning": f"{len(empty_clips)} assets produced 0 clips (VLM may have timed out): {', '.join(empty_clips[:3])}"})
            q.put({"type": "done", "results": results})
        except Exception as e:
            import traceback as _tb2
            q.put({"type": "error", "error": f"{e}\n\n{_tb2.format_exc()[-500:]}"})

    threading.Thread(target=_run, args=(new_a, st.session_state.annotation_queue), daemon=True).start()


def _render_annotation_progress():
    """Live annotation progress display."""
    _q = st.session_state.annotation_queue
    _current_file, _file_progress = "", (0, 1)
    _stages: dict[str, dict] = {}

    while not _q.empty():
        try:
            msg = _q.get_nowait()
        except queue.Empty:
            break
        if msg["type"] == "file":
            _current_file = msg["filename"]
            _file_progress = (msg["current"], msg["total"])
        elif msg["type"] == "stage":
            _stages[msg["stage"]] = {"status": msg["status"], "detail": msg["detail"]}
        elif msg["type"] == "warning":
            st.warning(msg["warning"])
        elif msg["type"] == "done":
            from src.asset_manager.index_store import upsert_annotations
            upsert_annotations(msg["results"])
            st.session_state.annotation_running = False
            st.session_state.annotation_queue = None
            st.session_state.annotation_done = True
            st.success(f"✅ {len(msg['results'])} assets annotated!")
        elif msg["type"] == "error":
            st.session_state.annotation_running = False
            st.session_state.annotation_done = True
            st.session_state._batch_error = msg["error"]
            st.error(f"❌ Failed: {msg['error']}")

    _fc, _ft = _file_progress
    st.progress(_fc / max(_ft, 1), text=f"📹 {_current_file} ({_fc}/{_ft})" if _current_file else f"Processing {_fc}/{_ft}")

    _stage_order = ["shot_detection", "captioning", "scene_merge", "scene_analysis"]
    _stage_labels = {"shot_detection": "🎞️ Shot Detect", "captioning": "🎬 Captioning",
                     "scene_merge": "🧩 Merge", "scene_analysis": "🔍 Analysis"}
    _cols = st.columns(len(_stage_order))
    for i, sk in enumerate(_stage_order):
        s = _stages.get(sk, {})
        sts = s.get("status", "pending")
        detail = s.get("detail", "")
        emoji = {"done": "✅", "start": "🔄", "skip": "⏭️", "progress": "📡"}.get(sts, "⏳")
        with _cols[i]:
            st.caption(f"{emoji} {_stage_labels[sk]}")
            if detail:
                for line in detail.split(" · "):
                    st.caption(f"_{line.strip()}_")

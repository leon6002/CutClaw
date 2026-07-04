"""Tab 2: Project Editor — video/audio selection, params, Run/Stop, pipeline status."""

import os

import streamlit as st

from ..helpers import (PROJECT_ROOT, cfg, save_config, _derive_target_shot_length_from_config,
                        _persist_target_shot_length, _persist_target_output_length,
                        format_log_line, resolve_hook_subtitle_path, MIN_TARGET_SHOT_LENGTH_SEC)
from ..components import build_graph_html, render_live_status
from ..pipeline import start_pipeline, stop_pipeline, parse_stage_from_line
from ...utils.video_concat import plan_effective_video_path


def render_tab_editor(live_status_ph=None, error_banner_ph=None):
    col_left, col_right = st.columns([2, 1])
    with col_left:
        st.markdown("### ✂️ Project Settings")

        _VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
        def _scan_files(folder, exts):
            base = os.path.join(PROJECT_ROOT, folder)
            if not os.path.isdir(base): return []
            return sorted(os.path.join(folder, f) for f in os.listdir(base) if os.path.splitext(f)[1].lower() in exts)

        _video_files = _scan_files("resource/video", _VIDEO_EXTS)
        _saved_video_raw = cfg("VIDEO_PATH", "")
        _saved_videos = [p for p in _saved_video_raw.split("||") if p] if _saved_video_raw else []
        for _v in _saved_videos:
            if _v and _v not in _video_files: _video_files = [_v] + _video_files

        if _video_files:
            _default = [_v for _v in _saved_videos if _v in _video_files]
            video_paths = st.multiselect("🎬 Videos", _video_files, default=_default, key="si_video_paths2")
        else:
            _txt = st.text_input("🎬 Video Path", value=(_saved_videos[0] if _saved_videos else ""), key="si_video_path2")
            video_paths = [_txt] if _txt.strip() else []

        _joined = "||".join(video_paths)
        if _joined != _saved_video_raw: save_config("VIDEO_PATH", _joined)
        video_path = plan_effective_video_path(video_paths)

        _AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}
        _audio_files = _scan_files("resource/audio", _AUDIO_EXTS)
        _saved_audio = cfg("AUDIO_PATH", "")
        if _saved_audio and _saved_audio not in _audio_files: _audio_files = [_saved_audio] + _audio_files
        if _audio_files:
            _ai = _audio_files.index(_saved_audio) if _saved_audio in _audio_files else 0
            audio_path = st.selectbox("🎵 Audio", _audio_files, index=_ai, key="si_audio_path2")
        else:
            audio_path = st.text_input("🎵 Audio Path", value=_saved_audio, key="si_audio_path2")
        if audio_path != _saved_audio: save_config("AUDIO_PATH", audio_path)

        instruction = st.text_area("📝 Instruction", value=cfg("INSTRUCTION", ""),
                                   placeholder="Describe the edit you want...", height=80, key="si_instruction2")
        if instruction != cfg("INSTRUCTION", ""): save_config("INSTRUCTION", instruction)

    with col_right:
        st.markdown("### ⚙️ Parameters")
        has_dialogue = st.checkbox("🎙️ Has dialogue", value=False, key="si_has_dialogue2")
        st.session_state["has_dialogue"] = has_dialogue
        video_type = "film" if has_dialogue else "vlog"

        main_character = ""
        if has_dialogue:
            main_character = st.text_input("Main Character", value=cfg("MAIN_CHARACTER_NAME", ""), key="si_main_character2")
            if main_character != cfg("MAIN_CHARACTER_NAME", ""): save_config("MAIN_CHARACTER_NAME", main_character)

        target_length = st.number_input("Target Length (s)", min_value=10.0, max_value=300.0,
                                        value=float(cfg("AUDIO_SEGMENT_MAX_DURATION_SEC", "35.0")) - 5.0,
                                        step=5.0, key="si_target_length2")
        if target_length != float(cfg("AUDIO_SEGMENT_MAX_DURATION_SEC", "35.0")) - 5.0:
            _persist_target_output_length(target_length)

        shot_length = st.number_input("Shot Length (s)", min_value=MIN_TARGET_SHOT_LENGTH_SEC, max_value=30.0,
                                      value=_derive_target_shot_length_from_config(), step=0.1, key="si_shot_length2")
        if shot_length != _derive_target_shot_length_from_config(): _persist_target_shot_length(shot_length)

        with st.expander("📝 SRT (optional)"):
            _SRT_EXTS = {".srt", ".vtt", ".ass", ".ssa"}
            _srt_files = _scan_files("resource/subtitle", _SRT_EXTS)
            _saved_srt = cfg("SRT_PATH", "")
            if _saved_srt and _saved_srt not in _srt_files: _srt_files = [_saved_srt] + _srt_files
            if _srt_files:
                _sri = ([""] + _srt_files).index(_saved_srt) if _saved_srt in _srt_files else 0
                srt_path = st.selectbox("SRT File", [""] + _srt_files, index=_sri, key="si_srt_path2")
            else:
                srt_path = st.text_input("SRT Path", value=_saved_srt, key="si_srt_path2")
            if srt_path != _saved_srt: save_config("SRT_PATH", srt_path)

    # Run / Stop
    st.markdown("---")
    col_run, col_stop = st.columns(2)
    with col_run:
        run_clicked = st.button("▶ Run Pipeline", disabled=st.session_state.running, use_container_width=True, key="btn_run2")
    with col_stop:
        stop_clicked = st.button("■ Stop", disabled=not st.session_state.running, use_container_width=True, key="btn_stop2")

    if run_clicked and not st.session_state.running:
        err = start_pipeline(
            video_paths, audio_path, instruction, video_type, main_character, srt_path, target_length, shot_length,
            cfg("VIDEO_ANALYSIS_MODEL", ""), cfg("VIDEO_ANALYSIS_ENDPOINT", ""), cfg("VIDEO_ANALYSIS_API_KEY", ""),
            cfg("AUDIO_LITELLM_MODEL", ""), cfg("AUDIO_LITELLM_BASE_URL", ""), cfg("AUDIO_LITELLM_API_KEY", ""),
            cfg("AGENT_LITELLM_MODEL", ""), cfg("AGENT_LITELLM_URL", ""), cfg("AGENT_LITELLM_API_KEY", ""),
        )
        if err: st.session_state["start_error"] = err
        st.rerun()

    if stop_clicked and st.session_state.running:
        stop_pipeline()
        st.rerun()

    # Pipeline status
    if st.session_state.get("start_error"):
        st.error(f"Failed to start pipeline: {st.session_state.start_error}")
        st.session_state.start_error = None

    graph_placeholder = st.empty()
    _log_expanded = st.session_state.running or st.session_state.pipeline_failed
    with st.expander("🖥️ Pipeline Logs", expanded=_log_expanded):
        log_placeholder = st.empty()

    def render_graph():
        graph_placeholder.markdown(
            build_graph_html(st.session_state.stage_status, st.session_state.stage_times),
            unsafe_allow_html=True)

    def render_log():
        lines = "<br>".join(format_log_line(l) for l in st.session_state.log_lines[-500:])
        log_placeholder.markdown(
            f'<div class="vca-log" style="display:flex;flex-direction:column-reverse"><div>{lines}</div></div>',
            unsafe_allow_html=True)

    @st.fragment(run_every=0.5)
    def pipeline_monitor():
        if st.session_state.running:
            q = st.session_state.log_queue
            proc = st.session_state.process
            while True:
                try:
                    line = q.get_nowait()
                except queue.Empty:
                    break
                if line is None:
                    proc.wait()
                    st.session_state.running = False
                    if proc.returncode != 0: st.session_state.pipeline_failed = True
                    st.rerun()
                    break
                st.session_state.log_lines.append(line)
                parse_stage_from_line(line, st.session_state.stage_status, st.session_state.stage_times)
        from ..components import render_live_status as _rls
        _rls(live_status_ph, error_banner_ph)
        render_log()
        render_graph()

    pipeline_monitor()

    return video_path, audio_path, instruction, video_type, main_character, srt_path

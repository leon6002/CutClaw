"""CutClaw — AI-powered video editing pipeline. Streamlit frontend."""

import importlib
import os

import streamlit as st

from src.ui.styles import inject_styles
from src.ui.helpers import (PROJECT_ROOT, cfg, save_config, status_badge,
                             resolve_hook_subtitle_path)
from src.ui.components import render_model_card
from src.ui.tabs.tab_assets import render_tab_assets
from src.ui.tabs.tab_editor import render_tab_editor
from src.ui.tabs.tab_render import render_tab_render

# ── Page config & styles ──────────────────────────────────────────
inject_styles()

# ── Session state defaults ────────────────────────────────────────
_STAGE_NAMES = ["shot_detection", "asr", "video_captioning", "audio_analysis", "screenwriter", "editor"]
_DEFAULTS = {
    "running": False, "process": None, "log_lines": [], "log_queue": None,
    "result_shot_json": None, "pipeline_failed": False, "start_error": None,
    "stage_status": {s: "pending" for s in _STAGE_NAMES}, "stage_times": {},
    "pipeline_start_time": None,
    "asset_root_dir": os.path.join(PROJECT_ROOT, cfg("ASSET_ROOT_DIR", "resource/imports/")),
    "asset_scanned": False, "scanned_assets": [], "asset_selection": None,
    "annotation_running": False,
}
for _k, _v in _DEFAULTS.items():
    st.session_state.setdefault(_k, _v)


# ── Hook dialogue re-select (used by tab_render) ──────────────────
def rerun_hook_dialogue_selection(shot_plan_path, video_path, instruction, main_character, srt_path):
    subtitle_path = resolve_hook_subtitle_path(video_path, srt_path)
    if not subtitle_path:
        raise FileNotFoundError("No subtitle file available for hook dialogue selection.")
    if not os.path.exists(shot_plan_path):
        raise FileNotFoundError(f"Shot plan not found: {shot_plan_path}")
    import src.config as runtime_config
    runtime_config = importlib.reload(runtime_config)
    import src.Screenwriter_scene_short as sw
    sw = importlib.reload(sw)
    sw.refresh_hook_dialogue_in_shot_plan(
        shot_plan_path=shot_plan_path, subtitle_path=subtitle_path,
        instruction=instruction, main_character=main_character.strip() or None,
        prompt_window_mode="random_window", random_window_attempts=4)
    return subtitle_path


# ── Sidebar: model config ─────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎬 CutClaw")
    st.markdown("---")
    with st.expander("⚙️ Model Settings", expanded=False):
        import json as _json2
        _pool_path = os.path.join(PROJECT_ROOT, "src", "api_pool.json")
        _api_pool: list[dict] = []
        if os.path.exists(_pool_path):
            try:
                with open(_pool_path, "r", encoding="utf-8") as _f:
                    _api_pool = _json2.load(_f)
            except Exception:
                _api_pool = []
        _pool_names = [e.get("name", e.get("model", "?")) for e in _api_pool]
        _pool_by_name = {e.get("name", ""): e for e in _api_pool}
        _mm_pool = [e for e in _api_pool if e.get("multimodal", False)]
        _mm_names = [e.get("name", e["model"]) for e in _mm_pool]

        render_model_card("Vision", "🖼️", "VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY",
                          _mm_names, _pool_by_name, "va")
        render_model_card("Audio", "🎵", "AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY",
                          _pool_names, _pool_by_name, "al")
        render_model_card("Agent", "🧠", "AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY",
                          _pool_names, _pool_by_name, "ag")

# ── Main area ─────────────────────────────────────────────────────
st.markdown(
    f'<h1 style="font-size:2rem;font-weight:700;margin-bottom:0.2rem">🎬 CutClaw &nbsp; {status_badge()}</h1>',
    unsafe_allow_html=True)

live_status_placeholder = st.empty()
error_banner_placeholder = st.empty()

tab1, tab2, tab3 = st.tabs(["📁 素材库", "✂️ 项目编辑", "🎬 渲染导出"])

with tab1:
    render_tab_assets()

with tab2:
    _result = render_tab_editor(live_status_placeholder, error_banner_placeholder)
    video_path, audio_path, instruction, video_type, main_character, srt_path = _result

with tab3:
    render_tab_render(video_path, audio_path, instruction, video_type, main_character, srt_path)

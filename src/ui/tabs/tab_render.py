"""Tab 3: Render & Export — ratio selection, ending toggle, render buttons, preview."""

import os
import subprocess

import streamlit as st

from ..helpers import PROJECT_ROOT, cfg, resolve_hook_subtitle_path


def render_tab_render(video_path, audio_path, instruction, video_type, main_character, srt_path):
    if st.session_state.pipeline_failed:
        st.error("Pipeline failed. Check logs in Tab 2.")
        return
    if st.session_state.running:
        st.info("⏳ Pipeline is running — render options will appear here when complete.")
        return
    if not st.session_state.result_shot_json:
        st.info("👈 Run the pipeline in Tab 2 first, then render here.")
        return

    shot_json = st.session_state.result_shot_json
    abs_shot_json = os.path.join(PROJECT_ROOT, shot_json)
    abs_shot_plan = abs_shot_json.replace("shot_point_", "shot_plan_")
    output_dir = os.path.dirname(abs_shot_json)

    st.markdown("### 🎬 Render Video")
    RATIOS = ["9:16", "16:9", "1:1"]

    def output_path(ratio):
        return os.path.join(output_dir, f"output_{ratio.replace(':', 'x')}.mp4")

    ending_video = os.path.join(PROJECT_ROOT, "resource", "ending", "ending.mp4")
    dialogue_font = os.path.join(PROJECT_ROOT, "resource", "font", "Pulp Fiction Italic M54.ttf")
    add_ending = False
    if os.path.exists(ending_video):
        add_ending = st.checkbox("🎬 Append ending video", value=False, key="cb_add_ending2")

    def run_render(ratio):
        out = output_path(ratio)
        cmd = [
            "python", "render/render_video.py",
            "--shot-plan", abs_shot_plan, "--shot-json", abs_shot_json,
            "--video", video_path, "--audio", audio_path,
            "--output", out, "--crop-ratio", ratio, "--no-labels",
        ]
        if st.session_state.get("has_dialogue", True): cmd += ["--render-hook-dialogue"]
        if add_ending and os.path.exists(ending_video): cmd += ["--ending-video", ending_video]
        if os.path.exists(dialogue_font): cmd += ["--dialogue-font", dialogue_font]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        subprocess.run(cmd, cwd=PROJECT_ROOT, env=env)

    cols = st.columns(3)
    for i, ratio in enumerate(RATIOS):
        with cols[i]:
            if st.button(f"▶ Render {ratio}", key=f"render_{ratio}2", use_container_width=True):
                with st.spinner(f"Rendering {ratio}…"): run_render(ratio)
                st.rerun()

    rendered = [(r, output_path(r)) for r in RATIOS if os.path.exists(output_path(r))]
    if rendered:
        st.markdown("#### Preview")
        _w = {"9:16": 1, "16:9": 3, "1:1": 2}
        for ratio, path in rendered:
            w = _w.get(ratio, 2)
            pad = (6 - w) // 2 if w < 6 else 0
            cols = st.columns([pad, w, pad] if pad > 0 else [w])
            with cols[1] if pad > 0 else cols[0]:
                st.caption(ratio)
                st.video(path)

    # Hook dialogue re-select
    if os.path.exists(abs_shot_plan) and st.session_state.get("has_dialogue", False):
        sub_path = resolve_hook_subtitle_path(video_path, srt_path)
        if sub_path:
            with st.expander("🔄 Re-select Hook Dialogue"):
                if st.button("Re-select", key="reselect_hook2"):
                    try:
                        with st.spinner("Re-selecting..."):
                            from ...app import rerun_hook_dialogue_selection
                            rerun_hook_dialogue_selection(
                                shot_plan_path=abs_shot_plan, video_path=video_path,
                                instruction=instruction, main_character=main_character, srt_path=srt_path)
                        st.success("Hook dialogue updated!")
                    except Exception as exc:
                        st.error(f"Failed: {exc}")

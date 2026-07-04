"""Pipeline management: start, stop, and stage parsing."""

import os
import queue
import re
import signal
import subprocess
import threading
import time

import streamlit as st

from .helpers import (PROJECT_ROOT, cfg, _derive_shot_duration_bounds,
                       derive_shot_point_path, MIN_TARGET_SHOT_LENGTH_SEC,
                       MIN_SEGMENT_DURATION_FLOOR_SEC, SHOT_LENGTH_RANGE_CAP_SEC)
from .helpers import _derive_target_shot_length_from_config, _persist_target_shot_length
from .helpers import _persist_target_output_length
from ..utils.video_concat import plan_effective_video_path

_STAGE_NAMES = ["shot_detection", "asr", "video_captioning", "audio_analysis", "screenwriter", "editor"]
_STAGE_STARTS = {
    "shot_detection": "[Step 1] Extracting video frames",
    "asr": "[Thread A: ASR]",
    "video_captioning": "[Thread B: Video]",
    "audio_analysis": "[Thread C: Audio]",
    "screenwriter": "Running Screenwriter",
    "editor": "Running EditorCoreAgent",
}
_STAGE_ENDS = {
    "shot_detection": "[Step 1] Shot detection completed",
    "asr": "[Thread A] ✨ Completed",
    "video_captioning": "[Thread B] ✨ Completed",
    "audio_analysis": "[Thread C] ✨ Completed",
    "screenwriter": "Shot plan generated successfully",
    "editor": "Video clip selection completed",
}
_STAGE_ERRORS = {
    "asr": "[Thread A] ❌",
    "video_captioning": "[Thread B] ❌",
    "audio_analysis": "[Thread C] ❌",
}
_TIME_RE = re.compile(r"[Cc]ompleted in ([\d.]+)s")


def parse_stage_from_line(line: str, stage_status: dict, stage_times: dict):
    for stage, kw in _STAGE_STARTS.items():
        if kw in line and stage_status.get(stage) == "pending":
            stage_status[stage] = "running"
    for stage, kw in _STAGE_ENDS.items():
        if kw in line and stage_status.get(stage) == "running":
            stage_status[stage] = "done"
            m = _TIME_RE.search(line)
            if m: stage_times[stage] = float(m.group(1))
    for stage, kw in _STAGE_ERRORS.items():
        if kw in line: stage_status[stage] = "error"
    if "❌ Pipeline stage" in line:
        for stage, status in stage_status.items():
            if status == "running": stage_status[stage] = "error"


def _read_stdout(proc, q):
    try:
        for line in proc.stdout:
            q.put(line.rstrip())
    finally:
        q.put(None)


def start_pipeline(video_paths, audio_path, instruction, video_type, main_character, srt_path,
                   target_length, shot_length,
                   va_model, va_endpoint, va_key,
                   al_model, al_endpoint, al_key,
                   ag_model, ag_endpoint, ag_key):
    if isinstance(video_paths, str):
        video_paths = [video_paths] if video_paths.strip() else []
    video_paths = [p for p in video_paths if p and str(p).strip()]
    min_duration = max(5.0, target_length - 5.0)
    max_duration = target_length + 5.0
    min_seg_duration, max_seg_duration = _derive_shot_duration_bounds(shot_length)
    cmd = [
        "python", "local_run.py",
        "--Video_Path", *video_paths,
        "--Audio_Path", audio_path,
        "--Instruction", instruction,
        "--type", video_type,
        "--instruction_type", "object",
        "--config.AUDIO_SEGMENT_MIN_DURATION_SEC", str(min_duration),
        "--config.AUDIO_SEGMENT_MAX_DURATION_SEC", str(max_duration),
        "--config.AUDIO_MIN_SEGMENT_DURATION", str(min_seg_duration),
        "--config.AUDIO_MAX_SEGMENT_DURATION", str(max_seg_duration),
        "--config.VIDEO_ANALYSIS_MODEL", va_model,
        "--config.VIDEO_ANALYSIS_ENDPOINT", va_endpoint,
        "--config.VIDEO_ANALYSIS_API_KEY", va_key,
        "--config.AUDIO_LITELLM_MODEL", al_model,
        "--config.AUDIO_LITELLM_BASE_URL", al_endpoint,
        "--config.AUDIO_LITELLM_API_KEY", al_key,
        "--config.AGENT_LITELLM_MODEL", ag_model,
        "--config.AGENT_LITELLM_URL", ag_endpoint,
        "--config.AGENT_LITELLM_API_KEY", ag_key,
    ]
    if main_character.strip(): cmd += ["--config.MAIN_CHARACTER_NAME", main_character.strip()]
    if srt_path.strip(): cmd += ["--SRT_Path", srt_path.strip()]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1, cwd=PROJECT_ROOT, env=env, start_new_session=True)
    except Exception as e:
        return str(e)
    q = queue.Queue()
    threading.Thread(target=_read_stdout, args=(proc, q), daemon=True).start()
    st.session_state.process = proc
    st.session_state.log_queue = q
    st.session_state.log_lines = []
    st.session_state.running = True
    st.session_state.pipeline_failed = False
    st.session_state.stage_status = {s: "pending" for s in _STAGE_NAMES}
    st.session_state.stage_times = {}
    st.session_state.pipeline_start_time = time.time()
    st.session_state.result_shot_json = derive_shot_point_path(
        plan_effective_video_path(video_paths), audio_path, instruction)
    return None


def stop_pipeline():
    proc = st.session_state.process
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    st.session_state.running = False

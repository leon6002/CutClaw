import os

# ==============================================================================
# CutClaw global configuration (beginner-friendly version)
# ------------------------------------------------------------------------------
# This file controls the full editing pipeline:
# 1) Video preprocessing (frame sampling, shot detection, scene analysis)
# 2) Audio analysis (beat/energy/structure)
# 3) Agent generation (Screenwriter + Editor + Reviewer)
#
# Recommended usage:
# - Prefer CLI overrides for experiments instead of changing defaults directly:
#   python local_run.py ... --config.PARAM_NAME VALUE
# - Example: --config.VIDEO_FPS 1 --config.AUDIO_TOTAL_SHOTS 80
# ==============================================================================

# ------------------ UI Remembered Inputs ------------------ #
# These are saved automatically by the app when you change sidebar fields.

VIDEO_PATH = "E:\\apps\\CutClaw\\resource\\imports\\DJI_20260615120557_0642_D.MP4||E:\\apps\\CutClaw\\resource\\imports\\DJI_20260615164736_0674_D.MP4||E:\\apps\\CutClaw\\resource\\imports\\DJI_20260615165651_0680_D.MP4||E:\\apps\\CutClaw\\resource\\imports\\DJI_20260615211535_0709_D.MP4||E:\\apps\\CutClaw\\resource\\imports\\DJI_20260619162056_0786_D.MP4||E:\\apps\\CutClaw\\resource\\imports\\DJI_20260621145043_0819_D.MP4"
AUDIO_PATH = "E:\\apps\\CutClaw\\resource\\imports\\马也_Crabbit - 海屿你.mp3"
INSTRUCTION = "将栈道漫步、营地航拍和花海奔跑串联，以动态镜头语言呈现一段前往旷野的青春旅行记录。"
SRT_PATH = ""


# ------------------ Video Preprocess ------------------ #
# These parameters control how video is sampled, where outputs are written,
# and how much content is processed.


VIDEO_DATABASE_FOLDER = "./Output/"
# Root folder for all intermediate artifacts and output JSON files.
# Changing this affects read/write paths across the whole project.

VIDEO_RESOLUTION = 240
# Target short-side resolution during frame extraction (aspect ratio preserved).
# - Smaller: faster and lower memory usage, but less detail
# - Larger: better detail, but slower

VIDEO_FPS = 2 
# Frame sampling rate during preprocessing (frames per second).
# Typical range: 1~3. Higher FPS gives finer analysis but increases cost/time.

VIDEO_MAX_MINUTES = None
# Maximum video duration to process (minutes). None means full video.
# For quick debugging, setting this to 3~10 is often helpful.

VIDEO_MAX_FRAMES = None if VIDEO_MAX_MINUTES is None else int(VIDEO_MAX_MINUTES * 60 * VIDEO_FPS)
# Auto-computed frame cap when VIDEO_MAX_MINUTES is set.

VIDEO_SAVE_DEBUG_FRAMES = False
# If True, sampled debug frames are written to disk for troubleshooting.
# This increases disk I/O.


# ------------------ Shot Detection ------------------ #
# These parameters determine where shot boundaries are detected.

SHOT_DETECTION_FPS = 2.0
# Sampling FPS specifically for shot detection (independent from VIDEO_FPS).

VIDEO_TYPE = "film"
# Global video type: "film" or "vlog".
# Note: local_run.py overrides this with CLI --type.

if VIDEO_TYPE == "film":
    SHOT_DETECTION_THRESHOLD = 3.0
    SHOT_DETECTION_MIN_SCENE_LEN = 3
elif VIDEO_TYPE == "vlog":
    SHOT_DETECTION_THRESHOLD = 1.5
    SHOT_DETECTION_MIN_SCENE_LEN = 45
else:
    # Fallback defaults: use film settings.
        SHOT_DETECTION_THRESHOLD = 3.0
    # For scenedetect: lower = more cuts, higher = more conservative.
        SHOT_DETECTION_MIN_SCENE_LEN = 3
    # Minimum shot length (in frames), used by some detectors.

SHOT_DETECTION_SCENES_PATH = "shot_scenes.txt"
# Output filename for shot boundary results (usually no need to change).

SHOT_DETECTION_MODEL = "scenedetect"
# Options: "autoshot", "transnetv2", "Qwen3VL", "scenedetect".
# Beginners should start with the default: scenedetect.



CLIP_SECS = 30
# Maximum length (seconds) of a single candidate clip.

MERGE_SHORT_SCENES = True
# If True, consecutive short scenes are merged to reduce fragmentation.

SCENE_MERGE_METHOD = "min_length"
# Scene merging strategy:
# - min_length: prioritize avoiding overly short scenes
# - max_length: prioritize not exceeding max scene duration

SCENE_MIN_LENGTH_SECS = 3
# Minimum scene length in seconds when SCENE_MERGE_METHOD="min_length".

SCENE_SIMILARITY_THRESHOLD = 0.5
# Similarity threshold for scene segmentation:
# - Lower: harder to split (longer scenes)
# - Higher: easier to split (more fragmented scenes)

MAX_SCENE_DURATION_SECS = 300
# Maximum allowed scene duration before forced split.

MIN_SCENE_DURATION_SECS = 30.0
# Scenes shorter than this are candidates for merging into neighboring scenes.

WHOLE_VIDEO_SUMMARY_BATCH_SIZE = 50
# Number of clips per batch for whole-video summarization.
# Affects parallelism and throughput.


# ═══════════════════════════════════════════════════════════════════════════════
# ASR (Speech Recognition)
# Converts dialogue to subtitles and can optionally run speaker diarization.
# Recommendation: use one backend at a time (local whisper_cpp or cloud litellm).
# ═══════════════════════════════════════════════════════════════════════════════

ASR_BACKEND = "litellm"
# Options: "whisper_cpp" (local) | "litellm" (cloud).
# - Local: lower cost/offline, speed depends on hardware
# - Cloud: easier setup, but incurs API cost

ASR_LANGUAGE = "English"
# Recognition language. Example values: "English", "Chinese", "en", "zh".
# Set None for auto-detection.

# ───────────────────────────────────────────────────────────────────────────────
# Option 1: whisper.cpp (local)
# ───────────────────────────────────────────────────────────────────────────────

ASR_DEVICE = "cuda:0"  # default; actual detection deferred to asr.py runtime
# Device for local ASR. Uses cuda:0 when available, otherwise cpu.

ASR_WHISPER_CPP_MODEL = "base.en"
# whisper.cpp model name or local ggml model path (e.g., "base.en", "large-v3").

ASR_WHISPER_CPP_N_THREADS = 8
# CPU inference thread count (relevant mainly for CPU runs).

ASR_ENABLE_DIARIZATION = True
# Enable speaker diarization (who is speaking).
# Improves dialogue understanding for films but adds runtime.

ASR_DIARIZATION_MODEL_PATH = "pyannote/speaker-diarization-community-1"
# HuggingFace model name/path for diarization.

ASR_MERGE_SAME_SPEAKER = True
# Merge adjacent subtitle segments from the same speaker.

ASR_MERGE_GAP = 1.0
# Maximum gap (seconds) to merge adjacent segments from same speaker.

# ───────────────────────────────────────────────────────────────────────────────
# Option 2: LiteLLM (cloud ASR)
# ───────────────────────────────────────────────────────────────────────────────

ASR_LITELLM_MAX_SEGMENT_MB = 1.0
# Max size (MB) per uploaded audio segment for cloud ASR.
# Helps avoid oversized requests.

ASR_LITELLM_BATCH_SIZE = 128
# Number of audio segments per request batch.
# Larger batches improve throughput but may increase rate-limit risk.

# ------------------ Video Understanding Model ------------------ #

SCENE_PROMPT_TYPE = VIDEO_TYPE
# Prompt style for scene analysis. Usually kept consistent with VIDEO_TYPE.

VIDEO_ANALYSIS_MODEL_MAX_TOKEN = 16384 
# Max output token count for the video analysis model.

VIDEO_ANALYSIS_MODEL = "openai/[L]gemini-3-flash-preview"
# Video semantic analysis model name (called via OpenAI-compatible endpoint).

VIDEO_ANALYSIS_ENDPOINT = "https://bboluo.com/v1"
# API base URL for the video analysis model.

VIDEO_ANALYSIS_API_KEY = "sk-y0kMdDUN7Ns13P8KDBMGB42hO7bjAb8LtlrVXAThHGGwFapL"
# API key for the video analysis model.

CAPTION_BATCH_SIZE = 64
# Batch size for parallel clip captioning/analysis.

SCENE_ANALYSIS_MIN_FRAMES = 6
# Minimum number of sampled frames per scene.
# Higher values may improve stability but increase runtime.


# ------------------ Audio Model ------------------ #
# Analyzes musical beat/energy/structure and outputs editing keypoints.

AUDIO_LITELLM_MODEL = "deepseek/deepseek-v4-pro"
# Cloud model used for audio captioning and structure analysis.

AUDIO_LITELLM_API_KEY = "sk-11bedec4d36c44e6b374baa3c55a83dc"
# API key for the audio model.

AUDIO_LITELLM_BASE_URL = "https://api.deepseek.com/v1"
# API base URL for the audio model.

AUDIO_DETECTION_METHODS = ["downbeat", "pitch", "mel_energy"]
# Keypoint detection methods (single or combined):
# - downbeat: rhythm/beat structure
# - pitch: melodic variation
# - mel_energy: energy peaks

# ----- Downbeat -----
AUDIO_BEATS_PER_BAR = 4
# Beats per bar (e.g., 4 for 4/4 time).

AUDIO_DBN_THRESHOLD = 0.05
# Activation threshold for downbeat tracking.

AUDIO_MIN_BPM = 55.0
AUDIO_MAX_BPM = 215.0
# BPM search range preset (may be weakly used in some paths).

# ----- Pitch -----
AUDIO_PITCH_TOLERANCE = 0.8
# Pitch matching tolerance.

AUDIO_PITCH_THRESHOLD = 0.8
# Confidence threshold for keeping pitch keypoint candidates.
# Higher value = stricter filtering.

AUDIO_PITCH_MIN_DISTANCE = 0.3
# Minimum spacing (seconds) between pitch keypoints.

AUDIO_PITCH_NMS_METHOD = "basic"
# Non-maximum suppression strategy: "basic" / "adaptive" / "window".

AUDIO_PITCH_MAX_POINTS = 50
# Maximum number of retained pitch keypoints.

# ----- Mel Energy -----
AUDIO_MEL_WIN_S = 512
# FFT window size.

AUDIO_MEL_N_FILTERS = 40
# Number of mel filters.

AUDIO_MEL_THRESHOLD_RATIO = 0.3
# Threshold ratio for energy peak detection.

AUDIO_MEL_MIN_DISTANCE = 0.3
# Minimum spacing (seconds) between mel-energy keypoints.

AUDIO_MEL_NMS_METHOD = "basic"
# Non-maximum suppression strategy: "basic" / "adaptive" / "window".

AUDIO_MEL_MAX_POINTS = 50
# Maximum number of retained mel-energy keypoints.

# ----- Keypoint post-processing (denoising/sparsification) -----
AUDIO_MERGE_CLOSE = 0
# [May be unused in main path] Merge very close keypoints.

AUDIO_MIN_INTERVAL = 0
# Global minimum keypoint interval (seconds).

AUDIO_TOP_K = 0
# Keep only top-K strongest keypoints. 0 means unlimited.

AUDIO_ENERGY_PERCENTILE = 0
# Keep only keypoints above this energy percentile (0~100).

AUDIO_SILENCE_THRESHOLD_DB = -45.0
# Silence filtering threshold (dB).
# Segments below this level are treated as too quiet and filtered.

# ----- Shot-length perceptual bounds (NOT a user setting) -----
# Shot length is decided automatically per song: cut density follows the music's
# energy relative to the whole track (energetic sections cut fast, calm sections
# hold long). These two values are only the PERCEPTUAL EXTREMES the algorithm is
# allowed to reach — editing-craft constants, like frame rate, not knobs a novice
# user should set. The self-calibrating pacing lives in audio_caption_madmom.py.
AUDIO_MIN_SEGMENT_DURATION = 1.2
# Fastest cut (seconds) — reached at the track's most energetic moments.
# Below ~1s a shot is too brief to read.

AUDIO_MAX_SEGMENT_DURATION = 7.0
# Longest hold (seconds) — reached at the track's calmest moments.
# Above ~8s a montage shot starts to drag.

DENSE_CAPTION_MAX_SEGMENT_SEC = 6.0
# Internal: longest a single pre-annotation dense-caption segment may span. Longer
# uniform segments are split into equal anchors (same description) so the editor's
# cache lookup always finds coverage and never needs a fresh model call. Not a UI knob.

SHOT_MIN_GAP_SEC = 2.0
# Minimum spacing (seconds) between two clips taken from the SAME source video.
# Adjacent slices of one continuous shot look like duplicates; the orchestrator
# reruns picks closer than this to spread them out (or push onto a different
# source). It never drops a shot over spacing — if a crowded/single source can't
# satisfy the gap, the close pick is committed anyway. Set 0 to disable.

# ----- Music structure analysis (Level-1) -----
AUDIO_STRUCTURE_TEMPERATURE = 0.7
AUDIO_STRUCTURE_TOP_P = 0.95
AUDIO_STRUCTURE_MAX_TOKENS = 4096
# Controls generation style and token limit for structure analysis.

# ----- Section-aware shot allocation -----
AUDIO_USE_STAGE1_SECTIONS = True
# Use Level-1 structural sections to guide keypoint filtering.

AUDIO_SECTION_MIN_INTERVAL = AUDIO_MIN_SEGMENT_DURATION
# Global minimum keypoint interval across sections.

AUDIO_TOTAL_SHOTS = 200
# Target total shot count, allocated proportionally by sections.
# For quick debugging, try reducing this to 30~80.

# ----- Multi-feature fusion weights -----
AUDIO_WEIGHT_DOWNBEAT = 1.0
AUDIO_WEIGHT_PITCH = 1.0
AUDIO_WEIGHT_MEL_ENERGY = 1.0
# Fusion weights for downbeat/pitch/mel-energy signals.
# Defaults use equal weighting.

# ----- Keypoint caption analysis (Level-2) -----
AUDIO_BATCH_SIZE = 8
# Batch size for parallel audio-segment inference.
# Larger values increase throughput but use more resources.

AUDIO_KEYPOINT_TEMPERATURE = 0.7
AUDIO_KEYPOINT_TOP_P = 0.95
AUDIO_KEYPOINT_MAX_TOKENS = 4096
# Controls style and maximum length for keypoint caption generation.

# ------------------ Agent Runtime ------------------ #

AGENT_MODEL_MAX_TOKEN = 8192
# Maximum generated tokens per agent response (not total context size).

AGENT_MAX_ITERATIONS = 3
# Max tool-use iterations per shot in the editor agent loop. Each iteration is
# one LLM round-trip carrying the full (growing) message history, so this is the
# single biggest lever on token cost. Minimal decisive path is: retrieve →
# (one inspect) → commit. Kept tight to force decisiveness; if too many shots
# fail before committing, bump to 4-5. Works together with AGENT_MAX_FINE_GRAINED.

AGENT_MAX_FINE_GRAINED = 2
# Max DISTINCT fine_grained_shot_trimming (inspection) calls per shot. The agent
# tends to over-analyze — it will call this 9+ times, burning the whole iteration
# budget before it ever commits. After this many candidate inspections it is told
# to stop and commit its best pick.

AGENT_MODEL_MAX_RETRIES = 2
# Max retries per agent step when model calls fail. Each retry re-sends the whole
# history, so fewer retries also reduces wasted tokens on flaky relay errors.

AGENT_RATE_LIMIT_BACKOFF_BASE = 1.0
AGENT_RATE_LIMIT_MAX_BACKOFF = 8.0
# Backoff timing (seconds) when rate limits occur.

AUDIO_SEGMENT_MIN_DURATION_SEC = 55.0
AUDIO_SEGMENT_MAX_DURATION_SEC = 65.0
# Allowed music-span duration range for short-video planning.

AUDIO_SEGMENT_SELECTION_MAX_RETRIES = 3
# Retry count when selected music span is invalid.

AUDIO_SEGMENT_TIME_TOLERANCE_SEC = 0.25
# Allowed timestamp drift (seconds) when validating selected spans.

ENABLE_TRIM_SHOT_CHARACTER_ANALYSIS = True
# Enable VLM character analysis during trim_shot.

CORE_MAX_FRAMES = 60
# Maximum sampled frames per clip for core + reviewer analysis.

AGENT_LITELLM_URL = "https://api.deepseek.com/v1"
# API base URL for the agent LLM.

AGENT_LITELLM_API_KEY = "sk-11bedec4d36c44e6b374baa3c55a83dc"
# API key for the agent LLM.

AGENT_LITELLM_MODEL = "deepseek/deepseek-v4-pro"
# Primary model for the agent.

PARALLEL_SHOT_ENABLED = True
# Whether to enable parallel shot selection (ParallelShotOrchestrator) in film mode.

PARALLEL_SHOT_MAX_WORKERS = 4
# Number of parallel workers.

PARALLEL_SHOT_MAX_RERUNS = 1
# Maximum rerun rounds for conflicted shots. Each rerun re-runs a full agent loop
# for every losing shot, so keeping this at 1 avoids a second expensive round.



# ------------------ Reviewer (Quality Checks) ------------------ #

ENABLE_REVIEWER = True
# Master switch for the Reviewer agent. Set to False to skip all review checks (face quality, duplicate detection, etc.).

ENABLE_FACE_QUALITY_CHECK = True
# Enable face/protagonist quality checks before finalizing shot selection.

VLM_FACE_LOG_EACH_FRAME = False
# Print per-frame protagonist detection logs (very verbose; debug only).

ENABLE_AESTHETIC_QUALITY_CHECK = True
# Toggle aesthetic quality checks for vlog mode (may not be fully implemented).

FACE_QUALITY_CHECK_METHOD = "vlm"
# Quality check method (currently only "vlm" is supported).

# ------------------ Protagonist Presence Constraints ------------------ #

MAIN_CHARACTER_NAME = ""
# Main character / target subject name (comma-separated for multiple roles).
# This is one of the highest-impact parameters in object mode.

MIN_PROTAGONIST_RATIO = 0.7
# Minimum ratio (0~1) of frames where the protagonist should be the main focus.

VLM_MIN_BOX_SIZE = 100
# Minimum protagonist bounding-box size in pixels. Smaller detections are ignored.


# ------------------ Shot Selection Constraints ------------------ #
# These parameters define fallback behavior when perfect matches are unavailable.

MIN_ACCEPTABLE_SHOT_DURATION = 2.0
# Minimum acceptable final shot duration (seconds).
# Smaller values increase match rate but may produce more fragmented edits.

ALLOW_DURATION_TOLERANCE = 1.0
# Allow duration deviation of ±N seconds from target.

ALLOW_CONTENT_MISMATCH = True
# Allow semantically similar (not exact) content matches.

ENABLE_FALLBACK_STRATEGY = True
# Enable multi-level fallback strategy to reduce hard failures.

SCENE_EXPLORATION_RANGE = 3
# Extra exploration range around recommended scenes (±N scenes).
# Example: if recommended scene is 8 and range=3, search scene 5~11.
# Set to 0 to strictly limit selection to recommended scenes only.

# ═══════════════════════════════════════════════════════════════════════════════
# Asset Manager — automated media scanning, annotation, and selection
# ═══════════════════════════════════════════════════════════════════════════════

ASSET_ROOT_DIR = "resource/imports/"
# Default directory scanned for media assets (videos / images / audio).

ASSET_ANNOTATION_MODEL = VIDEO_ANALYSIS_MODEL
# Model used for asset annotation. Defaults to same as video analysis model.
# Set to a cheaper/faster model for cataloging to save costs.

ASSET_SELECTOR_MODEL = AGENT_LITELLM_MODEL
# Model used by the selector Agent. Defaults to the same Agent model.

ASSET_MAX_VIDEOS_TO_SELECT = 5
# Maximum number of videos the selector can pick.

ASSET_MAX_IMAGES_TO_SELECT = 10
# Maximum number of images the selector can pick (converted to slideshow).

ASSET_IMAGE_DURATION_SEC = 3.0
# Duration per still image when converting to slideshow video.



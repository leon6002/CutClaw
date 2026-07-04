import src.config as config
import os
import json
import argparse
import time
import threading
import sys

from src.video.preprocess.asr import run_asr, assign_speakers_to_srt
from src.video.deconstruction.get_character import analyze_subtitles

# import src.config as config


def _configure_console_encoding() -> None:
    """Avoid UnicodeEncodeError on Windows GBK consoles when printing emojis."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _ensure_conda_bin_on_path() -> None:
    """Make ffmpeg and other conda-provided binaries discoverable.

    When this script is launched via the env's python.exe without activating the
    conda environment, the env's ``Library\\bin`` (which contains ffmpeg.exe) is
    not on PATH, causing ASR/audio ffmpeg subprocess calls to fail with WinError 2.
    Derive the paths from ``sys.prefix`` so this stays portable across machines.

    A bundled static ffmpeg under ``<project>/tools/ffmpeg`` (which ships with
    libmp3lame) is placed FIRST so MP3 encoding works; the conda ffmpeg on
    Windows lacks libmp3lame and falls back to the broken ``mp3_mf`` encoder.
    """
    project_ffmpeg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "ffmpeg")
    candidates = [
        project_ffmpeg,
        os.path.join(sys.prefix, "Library", "bin"),
        os.path.join(sys.prefix, "Library", "mingw-w64", "bin"),
        os.path.join(sys.prefix, "Library", "usr", "bin"),
        os.path.join(sys.prefix, "Scripts"),
        sys.prefix,
    ]
    existing = os.environ.get("PATH", "")
    parts = existing.split(os.pathsep)
    prepend = [p for p in candidates if os.path.isdir(p) and p not in parts]
    if prepend:
        os.environ["PATH"] = os.pathsep.join(prepend + parts)


def parse_config_overrides(unknown_args):
    """
    Parse config override arguments in the format --config.PARAM_NAME value

    Args:
        unknown_args: List of unknown arguments from argparse

    Returns:
        None (modifies config module in place)
    """
    i = 0
    while i < len(unknown_args):
        arg = unknown_args[i]
        if arg.startswith('--config.'):
            param_name = arg[9:]  # Remove '--config.' prefix
            if i + 1 < len(unknown_args) and not unknown_args[i + 1].startswith('--'):
                value_str = unknown_args[i + 1]

                # Auto-detect type based on existing config value or infer from string
                if hasattr(config, param_name):
                    original_value = getattr(config, param_name)
                    # Preserve original type
                    if isinstance(original_value, bool):
                        value = value_str.lower() in ('true', '1', 'yes')
                    elif isinstance(original_value, int):
                        value = int(value_str)
                    elif isinstance(original_value, float):
                        value = float(value_str)
                    else:
                        value = value_str
                else:
                    # Infer type from string
                    try:
                        if '.' in value_str:
                            value = float(value_str)
                        else:
                            value = int(value_str)
                    except ValueError:
                        if value_str.lower() in ('true', 'false'):
                            value = value_str.lower() == 'true'
                        else:
                            value = value_str

                setattr(config, param_name, value)
                print(f"✅ Config override: {param_name} = {value} (type: {type(value).__name__})")
                i += 2
            else:
                print(f"⚠️ Warning: --config.{param_name} specified but no value provided")
                i += 1
        else:
            print(f"⚠️ Warning: Unknown argument '{arg}' ignored")
            i += 1

def main():
    _configure_console_encoding()
    _ensure_conda_bin_on_path()

    # Register the global LLM request/response logger as early as possible so
    # every downstream litellm call is recorded.
    from src.utils.llm_logger import setup_llm_logging
    setup_llm_logging()

    parser = argparse.ArgumentParser(description="Run VideoCaptioningAgent on a video.")
    parser.add_argument("--Video_Path", nargs="+", help="One or more source videos. Multiple videos are automatically concatenated into a single timeline before editing.", default=["Dataset/Video/Movie/La_La_Land.mkv"])
    parser.add_argument("--Audio_Path", help="The URL of the video to process.", default="Dataset/Audio/Norman_fucking_rockwell.mp3")
    parser.add_argument("--Instruction", help="The Instruction to cutting the video.", default="Mia and Sebastian's relationship evolves through sweet to break moments.")
    parser.add_argument("--instruction_type", help="Type of instruction: 'object' for Object-centric or 'narrative' for Narrative-driven", default="object", choices=["object", "narrative"])
    parser.add_argument("--type", help="film or vlog", default="film")
    parser.add_argument("--SRT_Path", type=str,
                        help="Path to existing SRT file. Skips ASR transcription; diarization still runs to assign speakers.")

    # Parse known args and capture unknown args for config overrides
    args, unknown = parser.parse_known_args()

    # Apply config overrides
    parse_config_overrides(unknown)

    config.VIDEO_TYPE = args.type

    _video_inputs = args.Video_Path if isinstance(args.Video_Path, list) else [args.Video_Path]
    _video_inputs = [p for p in _video_inputs if p and str(p).strip()]
    if not _video_inputs:
        print("❌ No video files provided.")
        sys.exit(1)
    Audio_Path = args.Audio_Path
    Instruction = args.Instruction
    instruction_type = args.instruction_type

    if not (Instruction or "").strip():
        print("❌ Instruction is empty — please provide an editing instruction (剪辑指令为空).")
        sys.exit(1)

    # Per-file analysis: each source video is analyzed independently and cached
    # by content hash. Merging is deferred to render time.
    from src.analyzer import analyze_video, analyze_audio, merge_scene_summaries, get_scene_summaries_dir, get_analysis_path

    video_hashes: list[str] = []
    for vp in _video_inputs:
        vh = analyze_video(vp, video_type=config.VIDEO_TYPE)
        video_hashes.append(vh)

    audio_hash: str = ""
    if Audio_Path:
        audio_hash = analyze_audio(Audio_Path)

    # Use the first video's hash as the primary project identifier
    primary_hash = video_hashes[0][:12] if video_hashes else "unknown"
    audio_id = os.path.splitext(os.path.basename(Audio_Path))[0].replace('.', '_').replace(' ', '_') if Audio_Path else "no_audio"

    # Generate a safe filename from instruction
    import re
    import hashlib
    # Create a short hash of the instruction for uniqueness
    instruction_hash = hashlib.md5(Instruction.encode('utf-8')).hexdigest()[:8]
    # Create a more readable version (up to 50 characters, sanitized)
    instruction_safe = re.sub(r'[^\w\s-]', '', Instruction)[:50].strip().replace(' ', '_')
    # If instruction is too long or empty, use a more informative format
    if len(instruction_safe) > 0:
        instruction_id = f"{instruction_safe}_{instruction_hash}"
    else:
        instruction_id = f"instruction_{instruction_hash}"

    # ===== All Path Definitions =====
    # Project ID is based on the primary video hash + audio
    project_id = f"{primary_hash}_{audio_id}"

    # Use the first video as primary for ASR/diarization
    primary_video = _video_inputs[0]
    primary_hash_full = video_hashes[0]

    # Audio paths (audio analysis is cached per-file by analyze_audio)
    audio_captions_dir = os.path.join(config.VIDEO_DATABASE_FOLDER, 'Audio', audio_id, "captions")
    audio_caption_file = os.path.join(audio_captions_dir, "captions.json")

    # Bridge: per-file cache (Output/analyzed/{hash}/captions.json) → legacy
    # project path expected by Screenwriter/Editor. Without this, cached audio
    # analysis silently never reaches the agents. Refresh when cache is newer.
    if Audio_Path and audio_hash:
        _src_caption = os.path.join(get_analysis_path(audio_hash), "captions.json")
        if os.path.exists(_src_caption):
            os.makedirs(audio_captions_dir, exist_ok=True)
            if (not os.path.exists(audio_caption_file)
                    or os.path.getmtime(_src_caption) > os.path.getmtime(audio_caption_file)):
                import shutil as _sh
                _sh.copy(_src_caption, audio_caption_file)
                print(f"🔗 [Audio] Synced captions cache → {audio_caption_file}")

    # Build merged scene summaries dir for Screenwriter + Editor
    merged_scenes_dir = os.path.join(
        config.VIDEO_DATABASE_FOLDER, 'Output', project_id, "merged_scenes"
    )
    os.makedirs(merged_scenes_dir, exist_ok=True)

    # Merge scene summaries from all analyzed videos into the project dir
    all_scenes = merge_scene_summaries(video_hashes)
    scene_index = 0
    for scene in all_scenes:
        out_path = os.path.join(merged_scenes_dir, f"scene_{scene_index}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(scene, f, ensure_ascii=False, indent=2)
        scene_index += 1
    print(f"🧩 [Project] Merged {scene_index} scenes from {len(video_hashes)} videos → {merged_scenes_dir}")

    # Output paths
    shot_plan_output_path = os.path.join(
        config.VIDEO_DATABASE_FOLDER,
        'Output',
        project_id,
        f"shot_plan_{instruction_id}.json"
    )
    shot_point_output_path = os.path.join(
        config.VIDEO_DATABASE_FOLDER,
        'Output',
        project_id,
        f"shot_point_{instruction_id}.json"
    )
    start_time = time.time()
    stage_times = {}

    print(f"\n{'='*80}")
    print(f"🎬 Starting VideoCuttingAgent Pipeline")
    print(f"📽️  Videos ({len(_video_inputs)}): {', '.join(os.path.basename(v) for v in _video_inputs)}")
    print(f"🎵 Audio: {Audio_Path}")
    print(f"📝 Instruction: {Instruction}")
    print(f"{'='*80}\n")

    # Step 1 is done: per-file analysis is cached by analyze_video() above.
    # Step 2: ASR + audio run in parallel threads (unchanged logic).
    # Build ASR paths using the primary video for project-scoped subtitles.
    video_id_for_asr = os.path.splitext(os.path.basename(primary_video))[0].replace('.', '_').replace(' ', '_')
    frames_dir_for_asr = os.path.join(config.VIDEO_DATABASE_FOLDER, 'Video', video_id_for_asr, "frames")
    srt_path = os.path.join(config.VIDEO_DATABASE_FOLDER, 'Video', video_id_for_asr, "subtitles.srt")
    srt_with_characters = os.path.join(config.VIDEO_DATABASE_FOLDER, 'Video', video_id_for_asr, "subtitles_with_characters.srt")
    character_info_path = os.path.join(config.VIDEO_DATABASE_FOLDER, 'Video', video_id_for_asr, "character_info.json")
    stage_times['shot_detection'] = 0.0  # Already done in analyze_video


    thread_errors = {}

    def run_asr_and_character_id():
        """Thread A: ASR + Character ID. Uses primary video for audio extraction."""
        try:
            t0 = time.time()
            if args.type != "vlog":
                if args.SRT_Path is not None:
                    print(f"🔤 [Thread A: ASR] External SRT provided, skipping ASR transcription: {args.SRT_Path}")
                    if not os.path.exists(srt_path):
                        enable_diarization = getattr(config, 'ASR_ENABLE_DIARIZATION', False)
                        if enable_diarization:
                            from src.video.preprocess.asr import extract_audio_mp3_16k
                            extracted_audio_path = os.path.join(frames_dir_for_asr, "audio_16k_mono.mp3")
                            os.makedirs(frames_dir_for_asr, exist_ok=True)
                            if not os.path.exists(extracted_audio_path):
                                print("[Thread A: ASR] 🔊 Extracting audio for diarization...")
                                extract_audio_mp3_16k(primary_video, extracted_audio_path)
                            assign_speakers_to_srt(
                                srt_path=args.SRT_Path,
                                audio_path=extracted_audio_path,
                                output_srt_path=srt_path,
                                device=config.ASR_DEVICE,
                            )
                        else:
                            import shutil
                            shutil.copy(args.SRT_Path, srt_path)
                            print(f"[Thread A: ASR] 📋 Diarization disabled, copied SRT to {srt_path}")
                    else:
                        print(f"[Thread A: ASR] ⏭️ SRT already exists at {srt_path}, skipping.")
                else:
                    print("[Thread A: ASR] 🎙️ Running ASR to generate subtitles...")
                    os.makedirs(frames_dir_for_asr, exist_ok=True)
                    run_asr(
                        video_path=primary_video,
                        output_dir=frames_dir_for_asr,
                        srt_path=srt_path,
                        backend=config.ASR_BACKEND,
                        asr_device=config.ASR_DEVICE,
                        asr_language=config.ASR_LANGUAGE,
                        whisper_cpp_model_name=getattr(config, 'ASR_WHISPER_CPP_MODEL', 'base.en'),
                        whisper_cpp_n_threads=getattr(config, 'ASR_WHISPER_CPP_N_THREADS', 4),
                        litellm_model=getattr(config, 'ASR_LITELLM_MODEL', None),
                        litellm_api_key=getattr(config, 'ASR_LITELLM_API_KEY', None),
                        litellm_api_base=getattr(config, 'ASR_LITELLM_API_BASE', None),
                        litellm_max_segment_mb=getattr(config, 'ASR_LITELLM_MAX_SEGMENT_MB', 25.0),
                        litellm_batch_size=getattr(config, 'ASR_LITELLM_BATCH_SIZE', 8),
                        litellm_debug_dir=os.path.join(config.VIDEO_DATABASE_FOLDER, 'Video', video_id_for_asr, "subtitles_segments"),
                    )
                print("[Thread A: ASR] ✅ ASR/SRT step completed.")
                if os.path.exists(srt_path) and not os.path.exists(character_info_path):
                    print("[Thread A: CharID] 👥 Analyzing subtitles to identify characters...")
                    video_name = video_id_for_asr.replace('_', ' ')
                    speaker_mapping, _character_info = analyze_subtitles(
                        srt_path=srt_path,
                        movie_name=video_name,
                        output_dir=os.path.join(config.VIDEO_DATABASE_FOLDER, 'Video', video_id_for_asr),
                        use_full_subtitles=True,
                        model=config.VIDEO_ANALYSIS_MODEL,
                        api_base=config.VIDEO_ANALYSIS_ENDPOINT,
                        api_key=config.VIDEO_ANALYSIS_API_KEY,
                        max_tokens=config.VIDEO_ANALYSIS_MODEL_MAX_TOKEN,
                    )
                    print(f"[Thread A: CharID] ✅ Character identification completed. Found {len(speaker_mapping)} characters.")
                elif os.path.exists(character_info_path):
                    print(f"[Thread A: CharID] ⏭️ Character info already exists at {character_info_path}.")
                else:
                    print(f"[Thread A: CharID] ⚠️ Subtitle file not found at {srt_path}, skipping character identification.")
            else:
                print("[Thread A] ⏭️ Skipping ASR/character ID for vlog type.")
            stage_times['asr_character_id'] = time.time() - t0
            print(f"[Thread A] ✨ Completed in {stage_times['asr_character_id']:.1f}s")
        except Exception as e:
            thread_errors['asr'] = e
            print(f"[Thread A] ❌ ERROR: {e}")

    # Thread B (Video Captioning) is no longer needed — per-file analysis
    # already runs shot detection → captions → scene merge → scene analysis
    # in analyze_video() with content-hash caching.
    stage_times['video_captioning'] = 0.0
    print("[Thread B] ⏭️  Skipped — per-file analysis already cached by analyze_video().")

    def run_audio_analysis():
        """Thread C: Audio Analysis — uses cached analyze_audio() result."""
        try:
            t0 = time.time()
            if Audio_Path and not os.path.exists(audio_caption_file):
                # analyze_audio() was already called above — results are cached.
                # If the file still doesn't exist, run inline.
                print("[Thread C: Audio] 🎵 Audio captions missing, running analysis...")
                from src.analyzer import analyze_audio as _analyze_audio_cached
                _analyze_audio_cached(Audio_Path)
            elif Audio_Path:
                print(f"[Thread C: Audio] ⏭️ Audio captions already exist at {audio_caption_file}.")
            else:
                print("[Thread C: Audio] ⏭️ No audio file provided.")
            stage_times['audio_analysis'] = time.time() - t0
            print(f"[Thread C] ✨ Completed in {stage_times['audio_analysis']:.1f}s")
        except Exception as e:
            thread_errors['audio'] = e
            print(f"[Thread C] ❌ ERROR: {e}")

    # Launch threads (Thread B removed)
    thread_a = threading.Thread(target=run_asr_and_character_id, name="ASR-CharID", daemon=False)
    thread_c = threading.Thread(target=run_audio_analysis, name="AudioAnalysis", daemon=False)

    thread_a.start()
    thread_c.start()

    thread_a.join()
    thread_c.join()

    if thread_errors:
        for name, err in thread_errors.items():
            print(f"❌ Pipeline stage '{name}' failed: {err}")
        raise RuntimeError(f"Pipeline failed in stages: {list(thread_errors.keys())}")

    print("\n🚀 All parallel stages completed.")

    end_time = time.time()
    print(f"\n{'='*60}")
    print(f"⏱️  Stage Timing Summary:")
    for stage, elapsed in stage_times.items():
        print(f"  {stage:<30} {elapsed:>8.1f}s")
    print(f"  {'total (wall clock)':<30} {end_time - start_time:>8.1f}s")
    print(f"{'='*60}\n")

    



    # Step 5: Run Screenwriter to generate shot plan
    if os.path.isdir(merged_scenes_dir) and os.path.exists(audio_caption_file):
        print("\n" + "="*80)
        if os.path.exists(shot_plan_output_path):
            print("✍️  Running Screenwriter to validate/complete existing shot plan...")
            print(f"📄 Existing shot plan detected: {shot_plan_output_path}")
        else:
            print("✍️  Running Screenwriter to generate shot plan...")
        print("="*80)

        from src.Screenwriter_scene_short import Screenwriter

        os.makedirs(os.path.dirname(shot_plan_output_path), exist_ok=True)

        screenwriter = Screenwriter(
            video_scene_path=merged_scenes_dir,
            audio_caption_path=audio_caption_file,
            output_path=shot_plan_output_path,
            video_path=primary_video,
            subtitle_path=srt_with_characters if config.VIDEO_TYPE == "film" and os.path.exists(srt_with_characters) else None,
            main_character=config.MAIN_CHARACTER_NAME if config.MAIN_CHARACTER_NAME else None,
            max_iterations=20,
        )

        print(f"📝 Instruction: '{Instruction}'")
        t0 = time.time()
        _shot_plan = screenwriter.run(Instruction)
        stage_times['screenwriter'] = time.time() - t0

        print(f"\n{'='*80}")
        print(f"✅ Shot plan generated successfully in {stage_times['screenwriter']:.1f}s!")
        print(f"💾 Output saved to: {shot_plan_output_path}")
        print(f"{'='*80}\n")

    # Step 6: Run EditorCoreAgent to select video clips based on shot plan
    if os.path.isdir(merged_scenes_dir) and os.path.exists(audio_caption_file) and os.path.exists(shot_plan_output_path):
        print("\n" + "="*80)
        print("✂️  Running EditorCoreAgent to select video clips...")
        print("="*80)

        if config.VIDEO_TYPE == "film":
            from src.core import EditorCoreAgent, ParallelShotOrchestrator
        elif config.VIDEO_TYPE == "vlog":
            try:
                from src.core_vlog import EditorCoreAgent  # optional specialization
                from src.core import ParallelShotOrchestrator
            except ImportError:
                print("ℹ️  src.core_vlog not found — using the standard editor core for vlog mode.")
                from src.core import EditorCoreAgent, ParallelShotOrchestrator

        os.makedirs(os.path.dirname(shot_point_output_path), exist_ok=True)

        max_iterations = config.AGENT_MAX_ITERATIONS if hasattr(config, 'AGENT_MAX_ITERATIONS') else 20
        use_parallel_shot = (
            getattr(config, "PARALLEL_SHOT_ENABLED", True)
        )

        print(f"🚀 Running editor agent with instruction: '{Instruction}'")
        print(f"📂 Using shot plan from: {shot_plan_output_path}")
        print(f"📂 Using merged scenes from: {merged_scenes_dir}")

        # Use the first analyzed video's captions.json as the primary caption path
        primary_caption_file = os.path.join(get_analysis_path(primary_hash_full), "captions", "captions.json")

        if use_parallel_shot:
            max_workers = getattr(config, "PARALLEL_SHOT_MAX_WORKERS", 4)
            max_reruns = getattr(config, "PARALLEL_SHOT_MAX_RERUNS", 2)
            print(f"⚡ Parallel mode enabled (workers: {max_workers}, max_reruns: {max_reruns})")
            orchestrator = ParallelShotOrchestrator(
                video_caption_path=primary_caption_file,
                video_scene_path=merged_scenes_dir,
                audio_caption_path=audio_caption_file,
                output_path=shot_point_output_path,
                max_iterations=max_iterations,
                video_path=primary_video,
                frame_folder_path="",
                transcript_path=srt_with_characters if os.path.exists(srt_with_characters) else srt_path,
                max_workers=max_workers,
                max_reruns=max_reruns,
            )
            _results = orchestrator.run_parallel(shot_plan_path=shot_plan_output_path)
            print(f"✅ Parallel mode completed, selected {len(_results)} shots.")
        else:
            print("🚶 Sequential mode enabled (EditorCoreAgent.run).")
            editor_agent = EditorCoreAgent(
                video_caption_path=primary_caption_file,
                video_scene_path=merged_scenes_dir,
                audio_caption_path=audio_caption_file,
                output_path=shot_point_output_path,
                max_iterations=max_iterations,
                video_path=primary_video,
                video_reader=None,
                frame_folder_path="",
                transcript_path=srt_with_characters if os.path.exists(srt_with_characters) else srt_path
            )
            _messages = editor_agent.run(shot_plan_path=shot_plan_output_path)

        print(f"\n{'='*80}")
        print(f"🎉 Video clip selection completed!")
        print(f"💾 Output saved to: {shot_point_output_path}")
        print(f"{'='*80}\n")
    else:
        print("\n" + "="*80)
        print("❌ Cannot run EditorCoreAgent - missing required files:")
        if not os.path.isdir(merged_scenes_dir):
            print(f"  ❌ Scene summaries directory not found at {merged_scenes_dir}")
        if not os.path.exists(audio_caption_file):
            print(f"  ❌ Audio caption file not found at {audio_caption_file}")
        if not os.path.exists(shot_plan_output_path):
            print(f"  ❌ Shot plan file not found at {shot_plan_output_path}")
        print("="*80 + "\n")


if __name__ == "__main__":
    from src.utils.llm_logger import print_llm_summary
    try:
        main()
    except KeyboardInterrupt:
        print("\n⏸️  Interrupted. Progress up to the last completed step is saved.")
        print("↻ Re-run the SAME command to resume from where it stopped (cached steps are skipped).")
        print_llm_summary()
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ Pipeline stopped due to an error: {e}")
        print("💾 Progress is saved incrementally: shot detection, video/audio captions, scene "
              "summaries, the shot plan, and each already-selected shot are all cached on disk.")
        print("↻ Re-run the SAME command to resume from where it stopped — cached steps are skipped, "
              "so you won't re-spend tokens on completed work.")
        print_llm_summary()
        sys.exit(1)
    else:
        print_llm_summary()

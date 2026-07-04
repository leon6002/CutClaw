"""CutClaw web backend — FastAPI wrapper around the existing pipeline.

Run:  python server/main.py   (from project root, inside the cutclaw env)
Serves the built React UI from web/dist at http://127.0.0.1:8765
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

CONFIG_PATH = os.path.join(PROJECT_ROOT, "src", "config.py")

app = FastAPI(title="CutClaw API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Config helpers (regex-based, same as Streamlit UI) ─────────────────────

def _read_config() -> dict:
    vals = {}
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with open(CONFIG_PATH, "r", encoding=enc) as f:
                for line in f:
                    m = re.match(r'^([A-Z_][A-Z0-9_]*)\s*=\s*(.+)', line)
                    if m:
                        vals[m.group(1)] = m.group(2).strip()
            return vals
        except UnicodeDecodeError:
            vals.clear()
    return vals


def cfg(key: str, fallback: str = "") -> str:
    raw = _read_config().get(key, fallback)
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def save_config(key: str, value: str):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        float(value)
        new_val = value
    except ValueError:
        new_val = json.dumps(value, ensure_ascii=False)
    pattern = rf'^({re.escape(key)}\s*=\s*).*'
    content = re.sub(pattern, lambda m: f"{m.group(1)}{new_val}", content, flags=re.MULTILINE)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def _resolve(path: str) -> str:
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


# ── Job registry (annotate / pipeline / render run as jobs) ────────────────

class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.lines: list[str] = []
        self.status = "running"          # running | done | error
        self.returncode: int | None = None
        self.meta: dict = {}
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def add(self, line: str):
        with self.lock:
            self.lines.append(line)

    def to_dict(self, since: int = 0):
        with self.lock:
            return {
                "id": self.id, "kind": self.kind, "status": self.status,
                "returncode": self.returncode, "meta": dict(self.meta),
                "lines": self.lines[since:], "total": len(self.lines),
            }


JOBS: dict[str, Job] = {}
PIPELINE_JOB_ID: str | None = None


def _reader_thread(job: Job, on_line=None):
    try:
        for line in job.proc.stdout:
            line = line.rstrip()
            job.add(line)
            if on_line:
                on_line(job, line)
    finally:
        job.proc.wait()
        job.returncode = job.proc.returncode
        job.status = "done" if job.proc.returncode == 0 else "error"


def _spawn(job: Job, cmd: list[str], on_line=None):
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    job.proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        bufsize=1, cwd=PROJECT_ROOT, env=env, **kwargs)
    threading.Thread(target=_reader_thread, args=(job, on_line), daemon=True).start()
    JOBS[job.id] = job


def _kill(job: Job):
    proc = job.proc
    if proc and proc.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
    job.status = "error"
    job.add("■ Stopped by user.")


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, since: int = 0):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.to_dict(since)


# ── Config endpoints ────────────────────────────────────────────────────────

CONFIG_KEYS = [
    "VIDEO_PATH", "AUDIO_PATH", "INSTRUCTION", "SRT_PATH", "MAIN_CHARACTER_NAME",
    "ASSET_ROOT_DIR", "ASSET_IMAGE_DURATION_SEC",
    "AUDIO_SEGMENT_MIN_DURATION_SEC", "AUDIO_SEGMENT_MAX_DURATION_SEC",
    "AUDIO_MIN_SEGMENT_DURATION", "AUDIO_MAX_SEGMENT_DURATION",
    "VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY",
    "AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY",
    "AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY",
]


@app.get("/api/config")
def get_config():
    return {k: cfg(k, "") for k in CONFIG_KEYS}


class ConfigUpdate(BaseModel):
    values: dict[str, str]


@app.put("/api/config")
def put_config(body: ConfigUpdate):
    for k, v in body.values.items():
        if k in CONFIG_KEYS:
            save_config(k, v)
    return {"ok": True}


@app.get("/api/api-pool")
def get_api_pool():
    p = os.path.join(PROJECT_ROOT, "src", "api_pool.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


# ── Resource file listing ───────────────────────────────────────────────────

_EXTS = {
    "video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"},
    "audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"},
    "srt": {".srt", ".vtt", ".ass", ".ssa"},
}
_DIRS = {"video": "resource/video", "audio": "resource/audio", "srt": "resource/subtitle"}


@app.get("/api/files")
def list_files(kind: str = Query(..., pattern="^(video|audio|srt)$")):
    base = os.path.join(PROJECT_ROOT, _DIRS[kind])
    if not os.path.isdir(base):
        return []
    return sorted(
        os.path.join(_DIRS[kind], f).replace("\\", "/")
        for f in os.listdir(base)
        if os.path.splitext(f)[1].lower() in _EXTS[kind])


# ── Media preview ───────────────────────────────────────────────────────────

@app.get("/api/media")
def media(path: str):
    abs_path = _resolve(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(404, f"not found: {path}")
    return FileResponse(abs_path, filename=os.path.basename(abs_path))


# ── Assets ──────────────────────────────────────────────────────────────────

def _dump(model) -> dict:
    for attr in ("model_dump", "dict"):
        fn = getattr(model, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return {k: v for k, v in vars(model).items() if not k.startswith("_")}


class ScanRequest(BaseModel):
    root: str = ""


SCANNED: dict[str, list] = {"assets": [], "root": ""}


@app.post("/api/assets/scan")
def scan_assets(body: ScanRequest):
    from src.asset_manager.scanner import scan_asset_directory
    from src.asset_manager.index_store import load_index
    root = body.root.strip() or cfg("ASSET_ROOT_DIR", "resource/imports/")
    abs_root = _resolve(root)
    if not os.path.isdir(abs_root):
        raise HTTPException(400, f"Folder not found: {abs_root}")
    assets = scan_asset_directory(abs_root)
    SCANNED["assets"] = assets
    SCANNED["root"] = abs_root
    idx = load_index()
    out = []
    for meta in assets:
        d = _dump(meta)
        h = getattr(meta, "content_hash", "")
        ann = idx.get(h)
        d["annotated"] = ann is not None
        if ann is not None:
            d["annotation"] = _dump(ann.annotation)
        out.append(d)
    return {"root": abs_root, "assets": out}


def _analysis_details(content_hash: str) -> dict:
    """Per-clip captions + scene summaries from the analyzed cache."""
    from src.analyzer import get_analysis_path
    cache_dir = get_analysis_path(content_hash)
    result = {"clips": [], "scenes": []}
    ckpt = os.path.join(cache_dir, "captions", "ckpt")
    if os.path.isdir(ckpt):
        for fn in sorted(os.listdir(ckpt)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(ckpt, fn), "r", encoding="utf-8") as f:
                        result["clips"].append(json.load(f))
                except Exception:
                    pass
    sm = os.path.join(cache_dir, "captions", "scene_summaries_video")
    if os.path.isdir(sm):
        for fn in sorted(os.listdir(sm)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(sm, fn), "r", encoding="utf-8") as f:
                        result["scenes"].append(json.load(f))
                except Exception:
                    pass
    return result


@app.get("/api/assets/{content_hash}/details")
def asset_details(content_hash: str):
    return _analysis_details(content_hash)


class AnnotateRequest(BaseModel):
    content_hashes: list[str] = []   # empty → all new assets
    force: bool = False


@app.post("/api/assets/annotate")
def annotate(body: AnnotateRequest):
    """Annotate assets in a background thread (in-process, like Streamlit batch)."""
    from src.asset_manager.index_store import find_new_assets
    if not SCANNED["assets"]:
        raise HTTPException(400, "Scan first.")
    if body.content_hashes:
        targets = [a for a in SCANNED["assets"] if a.content_hash in set(body.content_hashes)]
    else:
        targets = find_new_assets(SCANNED["assets"])
    if not targets:
        return {"job_id": None, "message": "All assets already annotated."}

    job = Job("annotate")
    job.meta.update({"current": 0, "total": len(targets), "filename": ""})
    JOBS[job.id] = job

    def _run():
        try:
            from src.asset_manager.annotator import batch_annotate, annotate_asset
            from src.asset_manager.index_store import upsert_annotations
            from src.analyzer import analyze_video, analyze_audio

            if body.force:
                results = []
                for i, meta in enumerate(targets, 1):
                    job.meta.update({"current": i, "total": len(targets),
                                     "filename": getattr(meta, "file_name", "")})
                    job.add(f"[file] {i}/{len(targets)} {getattr(meta, 'file_name', '')}")
                    ap = getattr(meta, "absolute_path", "")
                    if getattr(meta, "asset_type", "video") == "video":
                        analyze_video(ap, force=True)
                    elif getattr(meta, "asset_type", "") == "audio":
                        analyze_audio(ap, force=True)
                    results.append(annotate_asset(meta))
                upsert_annotations(results)
            else:
                def _stage_cb(stage, status, detail):
                    job.add(f"[stage] {stage} {status} {detail or ''}".rstrip())
                def _file_cb(current, total, filename):
                    job.meta.update({"current": current, "total": total, "filename": filename})
                    job.add(f"[file] {current}/{total} {filename}")
                results = batch_annotate(targets, progress_callback=_file_cb, stage_callback=_stage_cb)
                upsert_annotations(results)
            job.add(f"DONE — {len(results)} assets annotated")
            job.status = "done"
        except Exception as e:
            import traceback
            job.add(f"ERROR: {e}\n{traceback.format_exc()[-800:]}")
            job.status = "error"

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job.id}


class SelectRequest(BaseModel):
    instruction: str = ""


@app.post("/api/assets/auto-select")
def auto_select(body: SelectRequest):
    from src.asset_manager.selector import select_assets
    from src.utils.video_concat import create_slideshow_video
    root = SCANNED["root"] or _resolve(cfg("ASSET_ROOT_DIR", "resource/imports/"))
    instr = body.instruction.strip() or cfg("INSTRUCTION", "").strip() or "travel montage"
    target_dur = float(cfg("AUDIO_SEGMENT_MAX_DURATION_SEC", "20.0")) - 5.0
    sel = select_assets(instruction=instr, target_duration_sec=max(15.0, target_dur))

    def _abs(p):
        return p if os.path.isabs(p) else os.path.join(root, p)

    abs_videos = [_abs(p) for p in sel.selected_videos]
    if sel.selected_images:
        ss = create_slideshow_video(
            [_abs(p) for p in sel.selected_images],
            duration_per_image=float(cfg("ASSET_IMAGE_DURATION_SEC", "3.0")))
        if ss:
            abs_videos.append(ss)
    if abs_videos:
        save_config("VIDEO_PATH", "||".join(abs_videos))
    if sel.selected_audio:
        save_config("AUDIO_PATH", _abs(sel.selected_audio[0]))
    return {"selection": _dump(sel), "videos": abs_videos,
            "audio": _abs(sel.selected_audio[0]) if sel.selected_audio else ""}


# ── Pipeline ────────────────────────────────────────────────────────────────

_STAGE_NAMES = ["shot_detection", "asr", "video_captioning", "audio_analysis", "screenwriter", "editor"]
_STAGE_STARTS = {
    "shot_detection": "[Step 1] Extracting video frames",
    "asr": "[Thread A: ASR]", "video_captioning": "[Thread B: Video]",
    "audio_analysis": "[Thread C: Audio]",
    "screenwriter": "Running Screenwriter", "editor": "Running EditorCoreAgent",
}
_STAGE_ENDS = {
    "shot_detection": "[Step 1] Shot detection completed",
    "asr": "[Thread A] ✨ Completed", "video_captioning": "[Thread B] ✨ Completed",
    "audio_analysis": "[Thread C] ✨ Completed",
    "screenwriter": "Shot plan generated successfully", "editor": "Video clip selection completed",
}
_STAGE_ERRORS = {"asr": "[Thread A] ❌", "video_captioning": "[Thread B] ❌", "audio_analysis": "[Thread C] ❌"}
_TIME_RE = re.compile(r"[Cc]ompleted in ([\d.]+)s")


def _parse_stage(job: Job, line: str):
    ss = job.meta["stages"]
    times = job.meta["stage_times"]
    for stage, kw in _STAGE_STARTS.items():
        if kw in line and ss.get(stage) == "pending":
            ss[stage] = "running"
    for stage, kw in _STAGE_ENDS.items():
        if kw in line and ss.get(stage) == "running":
            ss[stage] = "done"
            m = _TIME_RE.search(line)
            if m:
                times[stage] = float(m.group(1))
    for stage, kw in _STAGE_ERRORS.items():
        if kw in line:
            ss[stage] = "error"
    if "❌ Pipeline stage" in line:
        for stage, status in ss.items():
            if status == "running":
                ss[stage] = "error"


def _derive_shot_point_path(video_path: str, audio_path: str, instruction: str) -> str:
    import src.config as config
    video_id = os.path.splitext(os.path.basename(video_path))[0].replace('.', '_').replace(' ', '_')
    audio_id = os.path.splitext(os.path.basename(audio_path))[0].replace('.', '_').replace(' ', '_')
    ih = hashlib.md5(instruction.encode("utf-8")).hexdigest()[:8]
    safe = re.sub(r'[^\w\s-]', '', instruction)[:50].strip().replace(' ', '_')
    iid = f"{safe}_{ih}" if safe else f"instruction_{ih}"
    return os.path.join(config.VIDEO_DATABASE_FOLDER, 'Output', f"{video_id}_{audio_id}", f"shot_point_{iid}.json")


class PipelineRequest(BaseModel):
    video_paths: list[str]
    audio_path: str
    instruction: str
    has_dialogue: bool = False
    main_character: str = ""
    srt_path: str = ""
    target_length: float = 30.0
    shot_length: float = 4.0


MIN_SHOT = 0.2
SEG_FLOOR = 0.1
SHOT_RANGE_CAP = 1.0


def _shot_bounds(shot_length: float):
    shot_length = max(MIN_SHOT, float(shot_length))
    r = min(SHOT_RANGE_CAP, max(SEG_FLOOR, shot_length))
    return round(max(SEG_FLOOR, shot_length - r), 3), round(shot_length + r, 3)


@app.post("/api/pipeline/start")
def pipeline_start(body: PipelineRequest):
    global PIPELINE_JOB_ID
    if PIPELINE_JOB_ID and JOBS.get(PIPELINE_JOB_ID) and JOBS[PIPELINE_JOB_ID].status == "running":
        raise HTTPException(409, "Pipeline already running.")
    videos = [p for p in body.video_paths if p and p.strip()]
    if not videos:
        raise HTTPException(400, "No videos selected.")
    video_type = "film" if body.has_dialogue else "vlog"
    min_d = max(5.0, body.target_length - 5.0)
    max_d = body.target_length + 5.0
    min_s, max_s = _shot_bounds(body.shot_length)

    # persist UI choices like the Streamlit app does
    save_config("VIDEO_PATH", "||".join(videos))
    save_config("AUDIO_PATH", body.audio_path)
    save_config("INSTRUCTION", body.instruction)
    save_config("AUDIO_SEGMENT_MIN_DURATION_SEC", str(min_d))
    save_config("AUDIO_SEGMENT_MAX_DURATION_SEC", str(max_d))
    save_config("AUDIO_MIN_SEGMENT_DURATION", str(min_s))
    save_config("AUDIO_MAX_SEGMENT_DURATION", str(max_s))

    cmd = [
        sys.executable, "local_run.py",
        "--Video_Path", *[_resolve(v) for v in videos],
        "--Audio_Path", _resolve(body.audio_path),
        "--Instruction", body.instruction,
        "--type", video_type, "--instruction_type", "object",
        "--config.AUDIO_SEGMENT_MIN_DURATION_SEC", str(min_d),
        "--config.AUDIO_SEGMENT_MAX_DURATION_SEC", str(max_d),
        "--config.AUDIO_MIN_SEGMENT_DURATION", str(min_s),
        "--config.AUDIO_MAX_SEGMENT_DURATION", str(max_s),
    ]
    for key in ("VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY",
                "AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY",
                "AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY"):
        v = cfg(key, "")
        if v:
            cmd += [f"--config.{key}", v]
    if body.main_character.strip():
        cmd += ["--config.MAIN_CHARACTER_NAME", body.main_character.strip()]
    if body.srt_path.strip():
        cmd += ["--SRT_Path", _resolve(body.srt_path.strip())]

    from src.utils.video_concat import plan_effective_video_path
    effective = plan_effective_video_path([_resolve(v) for v in videos])
    shot_point = _derive_shot_point_path(effective, body.audio_path, body.instruction)

    job = Job("pipeline")
    job.meta.update({
        "stages": {s: "pending" for s in _STAGE_NAMES},
        "stage_times": {}, "start_time": time.time(),
        "shot_point": shot_point, "effective_video": effective,
    })
    try:
        _spawn(job, cmd, on_line=_parse_stage)
    except Exception as e:
        raise HTTPException(500, str(e))
    PIPELINE_JOB_ID = job.id
    return {"job_id": job.id, "shot_point": shot_point, "effective_video": effective}


@app.post("/api/pipeline/stop")
def pipeline_stop():
    job = JOBS.get(PIPELINE_JOB_ID or "")
    if not job or job.status != "running":
        return {"ok": False, "message": "Not running."}
    _kill(job)
    return {"ok": True}


@app.get("/api/pipeline/current")
def pipeline_current(since: int = 0):
    job = JOBS.get(PIPELINE_JOB_ID or "")
    if not job:
        return {"job": None}
    return {"job": job.to_dict(since)}


@app.get("/api/project/recent")
def project_recent(limit: int = 10):
    """Most recent shot_point results on disk — works regardless of id scheme."""
    import src.config as config
    base = os.path.join(_resolve(config.VIDEO_DATABASE_FOLDER), "Output")
    found = []
    if os.path.isdir(base):
        for proj in os.listdir(base):
            pdir = os.path.join(base, proj)
            if not os.path.isdir(pdir):
                continue
            for fn in os.listdir(pdir):
                if fn.startswith("shot_point_") and fn.endswith(".json"):
                    fp = os.path.join(pdir, fn)
                    found.append({
                        "shot_point": fp, "project": proj,
                        "instruction_id": fn[len("shot_point_"):-len(".json")],
                        "mtime": os.path.getmtime(fp),
                    })
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return found[:limit]


# ── Render ──────────────────────────────────────────────────────────────────

class RenderRequest(BaseModel):
    shot_point: str
    video_path: str
    audio_path: str
    ratio: str = "9:16"
    add_ending: bool = False
    has_dialogue: bool = False


@app.post("/api/render")
def render(body: RenderRequest):
    abs_point = _resolve(body.shot_point)
    abs_plan = abs_point.replace("shot_point_", "shot_plan_")
    if not os.path.exists(abs_point):
        raise HTTPException(400, f"shot_point not found: {abs_point}")
    out = os.path.join(os.path.dirname(abs_point), f"output_{body.ratio.replace(':', 'x')}.mp4")
    ending = os.path.join(PROJECT_ROOT, "resource", "ending", "ending.mp4")
    font = os.path.join(PROJECT_ROOT, "resource", "font", "Pulp Fiction Italic M54.ttf")
    cmd = [
        sys.executable, "render/render_video.py",
        "--shot-plan", abs_plan, "--shot-json", abs_point,
        "--video", _resolve(body.video_path), "--audio", _resolve(body.audio_path),
        "--output", out, "--crop-ratio", body.ratio, "--no-labels",
    ]
    if body.has_dialogue:
        cmd += ["--render-hook-dialogue"]
    if body.add_ending and os.path.exists(ending):
        cmd += ["--ending-video", ending]
    if os.path.exists(font):
        cmd += ["--dialogue-font", font]
    job = Job("render")
    job.meta.update({"output": out, "ratio": body.ratio})
    try:
        _spawn(job, cmd)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"job_id": job.id, "output": out}


@app.get("/api/render/outputs")
def render_outputs(shot_point: str):
    abs_point = _resolve(shot_point)
    d = os.path.dirname(abs_point)
    out = []
    for ratio in ("9:16", "16:9", "1:1"):
        p = os.path.join(d, f"output_{ratio.replace(':', 'x')}.mp4")
        if os.path.exists(p):
            out.append({"ratio": ratio, "path": p, "size_mb": round(os.path.getsize(p) / 1e6, 1),
                        "mtime": os.path.getmtime(p)})
    return {"outputs": out, "shot_point_exists": os.path.exists(abs_point),
            "has_ending_video": os.path.exists(os.path.join(PROJECT_ROOT, "resource", "ending", "ending.mp4"))}


# ── Static frontend (web/dist) ──────────────────────────────────────────────

DIST = os.path.join(PROJECT_ROOT, "web", "dist")
if os.path.isdir(DIST):
    app.mount("/", StaticFiles(directory=DIST, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    # keep ffmpeg discoverable, same trick as local_run.py
    tools_ffmpeg = os.path.join(PROJECT_ROOT, "tools", "ffmpeg")
    if os.path.isdir(tools_ffmpeg):
        os.environ["PATH"] = tools_ffmpeg + os.pathsep + os.environ.get("PATH", "")
    uvicorn.run(app, host="127.0.0.1", port=8765)

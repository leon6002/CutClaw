"""CutClaw web backend — FastAPI wrapper around the existing pipeline.

Run:  python server/main.py   (from project root, inside the cutclaw env)
Serves the built React UI from web/dist at http://127.0.0.1:8765
"""

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

# Force UTF-8 stdout/stderr so the server's own print() never crashes on non-GBK
# characters (ø/é/中文 in paths, project names) on a Windows GBK console.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

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
    if re.search(pattern, content, flags=re.MULTILINE):
        content = re.sub(pattern, lambda m: f"{m.group(1)}{new_val}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n\n{key} = {new_val}\n"
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def _resolve(path: str) -> str:
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def _reload_runtime_config():
    """Reload config-derived modules so in-process jobs pick up model switches.

    save_config() edits config.py on disk, but already-imported modules cache
    values (src.config attributes, litellm_client's AUDIO_* constants). Reload
    mutates the module objects in place, so every existing reference updates.
    """
    import importlib
    try:
        import src.config as _c
        importlib.reload(_c)
    except Exception:
        pass
    try:
        import src.audio.litellm_client as _lc
        importlib.reload(_lc)
    except Exception:
        pass


# ── Job registry (annotate / pipeline / render run as jobs) ────────────────

JOBS_DIR = os.path.join(PROJECT_ROOT, "Output", "jobs")


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
        self._last_save = 0.0

    def add(self, line: str):
        with self.lock:
            self.lines.append(line)
        self.save()

    def save(self, force: bool = False):
        """Persist a snapshot so job views survive backend restarts (throttled)."""
        now = time.time()
        if not force and now - self._last_save < 3.0:
            return
        self._last_save = now
        try:
            os.makedirs(JOBS_DIR, exist_ok=True)
            with self.lock:
                data = {
                    "id": self.id, "kind": self.kind, "status": self.status,
                    "returncode": self.returncode, "meta": self.meta,
                    "lines": self.lines[-3000:], "saved_at": now,
                }
                payload = json.dumps(data, ensure_ascii=False)
            with open(os.path.join(JOBS_DIR, f"{self.id}.json"), "w", encoding="utf-8") as f:
                f.write(payload)
        except Exception:
            pass

    def to_dict(self, since: int = 0):
        import copy
        with self.lock:
            # traces (full agent step details) are fetched on demand, not polled
            meta_light = {k: v for k, v in self.meta.items() if k != "traces"}
            return {
                "id": self.id, "kind": self.kind, "status": self.status,
                "returncode": self.returncode, "meta": copy.deepcopy(meta_light),
                "lines": self.lines[since:], "total": len(self.lines),
            }


JOBS: dict[str, Job] = {}
PIPELINE_JOB_ID: str | None = None


def _load_persisted_jobs():
    """Restore job snapshots after a backend restart (views survive; keep last 12)."""
    global PIPELINE_JOB_ID
    if not os.path.isdir(JOBS_DIR):
        return
    files = []
    for fn in os.listdir(JOBS_DIR):
        if fn.endswith(".json"):
            fp = os.path.join(JOBS_DIR, fn)
            try:
                files.append((os.path.getmtime(fp), fp))
            except OSError:
                pass
    files.sort()
    for _, fp in files[:-12]:   # prune old snapshots
        try:
            os.remove(fp)
        except OSError:
            pass
    for _, fp in files[-12:]:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            job = Job(d.get("kind", "job"))
            job.id = d.get("id") or job.id
            job.lines = d.get("lines", [])
            job.meta = d.get("meta", {})
            job.returncode = d.get("returncode")
            st = d.get("status", "done")
            if st == "running":
                st = "error"
                job.lines.append(
                    "[服务重启] 任务跟踪中断 — 子进程可能仍在后台完成并把结果写盘；重跑会走缓存跳过已完成部分。")
            job.status = st
            JOBS[job.id] = job
            if job.kind == "pipeline":
                PIPELINE_JOB_ID = job.id   # files sorted old→new, last wins
        except Exception:
            pass


_load_persisted_jobs()


def _reader_thread(job: Job, on_line=None):
    try:
        for line in job.proc.stdout:
            line = line.rstrip()
            if on_line:
                on_line(job, line)
            if "@@PROGRESS" not in line:   # progress events go to meta, not the log
                job.add(line)
    finally:
        job.proc.wait()
        job.returncode = job.proc.returncode
        job.status = "done" if job.proc.returncode == 0 else "error"
        pid = job.meta.get("project_id")
        if pid:
            try:
                p = _load_project(pid)
                p["last_run_status"] = job.status
                _save_project(p)
            except Exception:
                pass
        job.save(force=True)


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


@app.get("/api/jobs/current/{kind}")
def current_job_of_kind(kind: str):
    """Latest running job of a kind — lets the UI reattach after a page refresh."""
    for job in reversed(list(JOBS.values())):
        if job.kind == kind and job.status == "running":
            return {"job": {"id": job.id, "kind": job.kind, "status": job.status}}
    return {"job": None}


@app.get("/api/jobs/latest/{kind}")
def latest_job_of_kind(kind: str):
    """Most recent job of a kind regardless of status — surfaces batches that
    were killed by a backend restart (their assets silently stay 未标注
    otherwise). For annotate jobs, include how many files never finished."""
    for job in reversed(list(JOBS.values())):
        if job.kind != kind:
            continue
        info: dict = {"id": job.id, "kind": job.kind, "status": job.status}
        files = (job.meta or {}).get("files") or {}
        if files:
            info["unfinished"] = sum(1 for v in files.values() if v in ("p", "r"))
            info["total"] = len(files)
        return {"job": info}
    return {"job": None}


@app.get("/api/jobs/{job_id}/traces")
def get_job_traces(job_id: str):
    """ALL unit traces, slimmed for the canvas (one poll instead of N).

    Heavy fields (args/reply) are stripped to flags + snippets; the canvas
    fetches full detail for a single unit via /trace when a node expands.
    """
    import copy
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    out: dict = {}
    with job.lock:
        for task, units in (job.meta.get("traces", {}) or {}).items():
            tu: dict = {}
            for idx, steps in units.items():
                slim = []
                for s in steps:
                    t = {k: s.get(k) for k in ("phase", "iter", "max_iter", "elapsed", "tool", "verdict", "note") if s.get(k) is not None}
                    r = s.get("result")
                    if r:
                        t["result"] = str(r)[:160]
                    if s.get("args"):
                        t["has_args"] = True
                    if s.get("reply"):
                        t["has_reply"] = True
                    slim.append(t)
                tu[idx] = slim
            out[task] = tu
        out = copy.deepcopy(out)
    return {"traces": out}


@app.get("/api/jobs/{job_id}/trace")
def get_job_trace(job_id: str, task: str, idx: int):
    """Full agent step trace for one work unit (model replies, tool args).

    Checkpoint-skipped units have no steps in the current run — fall back to
    the most recent earlier job that traced the same unit.
    """
    import copy
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    with job.lock:
        steps = copy.deepcopy(job.meta.get("traces", {}).get(task, {}).get(str(idx), []))
    if steps:
        return {"steps": steps, "from_previous_run": False}

    cur_sp = job.meta.get("shot_point")
    for other in reversed(list(JOBS.values())):
        if other.id == job_id or other.kind != job.kind:
            continue
        # same pipeline result target (when known) — avoids cross-project mixups
        if cur_sp and other.meta.get("shot_point") and other.meta.get("shot_point") != cur_sp:
            continue
        with other.lock:
            s2 = copy.deepcopy(other.meta.get("traces", {}).get(task, {}).get(str(idx), []))
        if s2:
            return {"steps": s2, "from_previous_run": True}
    return {"steps": [], "from_previous_run": False}


# ── Config endpoints ────────────────────────────────────────────────────────

CONFIG_KEYS = [
    "VISION_POOL_REF", "AUDIO_POOL_REF", "AGENT_POOL_REF",
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


class PoolSave(BaseModel):
    pool: list


_ROLE_CONFIG_KEYS = [
    ("VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY"),
    ("AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY"),
    ("AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY"),
]


@app.put("/api/api-pool")
def save_api_pool(body: PoolSave):
    p = os.path.join(PROJECT_ROOT, "src", "api_pool.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(body.pool, f, ensure_ascii=False, indent=2)

    # The role→entry REFERENCE is authoritative; the config triplets are just a
    # materialized cache for subprocess/legacy readers. Re-materialize them from
    # the (possibly edited) pool on every save so they can never drift.
    _ROLE_REFS = [
        ("VISION_POOL_REF", "VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY"),
        ("AUDIO_POOL_REF", "AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY"),
        ("AGENT_POOL_REF", "AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY"),
    ]
    by_name = {(e.get("name") or e.get("model") or ""): e for e in body.pool}
    synced = []
    for rk, mk, ek, kk in _ROLE_REFS:
        e = by_name.get(cfg(rk, ""))
        if not e:
            # legacy fallback: no ref stored yet — match by model+endpoint
            cur_m, cur_e = cfg(mk, ""), cfg(ek, "")
            e = next((x for x in body.pool
                      if (x.get("model") or "") == cur_m
                      and (x.get("endpoint") or x.get("api_base") or "") == cur_e), None)
            if not e:
                continue
        vals = {
            mk: e.get("model", "") or "",
            ek: e.get("endpoint") or e.get("api_base") or "",
            kk: e.get("api_key", "") or "",
        }
        for k, v in vals.items():
            if cfg(k, "") != v:
                save_config(k, v)
                if rk not in synced:
                    synced.append(rk)
    return {"ok": True, "count": len(body.pool), "synced_roles": synced}


class PoolTestRequest(BaseModel):
    model: str
    endpoint: str = ""
    api_key: str = ""


@app.post("/api/api-pool/test")
def api_pool_test(body: PoolTestRequest):
    """Fire a tiny completion at the given model config and report latency."""
    import time as _t
    import litellm

    if not body.model.strip():
        raise HTTPException(400, "model 不能为空")
    kwargs: dict = dict(
        model=body.model,
        messages=[{"role": "user", "content": "Reply with exactly one word: pong"}],
        max_tokens=1024,   # reasoning models need headroom for thought tokens
        timeout=30,
    )
    if body.endpoint.strip():
        kwargs["api_base"] = body.endpoint.strip()
    if body.api_key.strip():
        kwargs["api_key"] = body.api_key.strip()

    t0 = _t.time()
    try:
        r = litellm.completion(**kwargs)
        msg = r.choices[0].message
        content = (msg.content or getattr(msg, "reasoning_content", None) or "").strip()
        usage = getattr(r, "usage", None)
        return {
            "ok": True,
            "latency_s": round(_t.time() - t0, 2),
            "reply": content[:200],
            "tokens": getattr(usage, "total_tokens", None) if usage else None,
        }
    except Exception as e:
        return {"ok": False, "latency_s": round(_t.time() - t0, 2), "error": str(e)[:300]}


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


@app.get("/api/assets/thumb")
def asset_thumb(hash: str, path: str = ""):
    """Poster thumbnail for a video asset — extracted once with ffmpeg and
    cached on disk, so the grid never embeds heavyweight <video> elements."""
    if not hash or any(c in hash for c in "\\/.:"):
        raise HTTPException(400, "bad hash")
    tdir = os.path.join(PROJECT_ROOT, "Output", "asset_index", "thumbs")
    os.makedirs(tdir, exist_ok=True)
    tp = os.path.join(tdir, f"{hash}.jpg")
    if not os.path.exists(tp):
        src = _resolve(path)
        if not (src and os.path.isfile(src)):
            raise HTTPException(404, "source not found")
        import subprocess
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", src, "-frames:v", "1",
             "-vf", "scale=480:-2", "-q:v", "4", tp],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0 or not os.path.exists(tp):
            # very short clips: retry from 0s
            subprocess.run(
                ["ffmpeg", "-y", "-i", src, "-frames:v", "1",
                 "-vf", "scale=480:-2", "-q:v", "4", tp],
                capture_output=True, timeout=30,
            )
        if not os.path.exists(tp):
            raise HTTPException(500, "thumbnail extraction failed")
    return FileResponse(tp, media_type="image/jpeg")


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
    # per-asset states keyed by content hash — the ONLY reliable way for the
    # UI to mark cards (filename matching breaks: the completion callback
    # reports the file that just FINISHED, and names aren't stable keys)
    _files_state = {t.content_hash: "p" for t in targets}          # p/r/d
    _name_to_hash = {getattr(t, "file_name", ""): t.content_hash for t in targets}
    job.meta.update({"current": 0, "total": len(targets), "filename": "",
                     "files": _files_state,
                     "names": {t.content_hash: getattr(t, "file_name", "") for t in targets},
                     "file_stages": {}})
    JOBS[job.id] = job

    def _reset_file_view():
        """Segment grids & stage stepper are scoped to the CURRENT file — old
        files' units (and their failures) must not bleed into the new one."""
        job.meta["tasks"] = {}
        job.meta["file_stages"] = {}
        job.meta["stage"] = ""
        job.meta["stage_detail"] = ""

    def _run():
        _reload_runtime_config()
        # feed fine-grained segment events (e.g. audio captioning) into this job
        from src.utils import progress as _progress
        _progress.HOOK = lambda ev: (_apply_progress_ev(job, ev), job.save())
        try:
            from src.asset_manager.annotator import batch_annotate, annotate_asset
            from src.asset_manager.index_store import upsert_annotations
            from src.analyzer import analyze_video, analyze_audio

            def _stage_cb(stage, status, detail, filename=None):
                # per-card stage line ("镜头检测 42% / 片段理解 …") — surfaced live
                # on the running asset's card + the stage stepper.
                # filename is set when videos annotate in PARALLEL processes:
                # stages route per-hash instead of the single global stepper.
                if filename:
                    h = _name_to_hash.get(filename)
                    if h:
                        if stage == "annotate" and status == "fail":
                            # analysis failed but is RESUMABLE — mark the file
                            # red; the asset stays 未标注 so the next batch run
                            # picks it up and resumes from the checkpoints
                            _files_state[h] = "f"
                            (job.meta.get("stage_by_hash") or {}).pop(h, None)
                        else:
                            _sbh = job.meta.setdefault("stage_by_hash", {})
                            if status in ("start", "progress"):
                                _sbh[h] = {"stage": stage, "detail": str(detail or "")[:60]}
                            else:
                                _sbh[h] = {"stage": "", "detail": ""}
                    job.add(f"[stage] {filename} · {stage} {status} {str(detail or '')[:80]}".rstrip())
                    return
                _fs = job.meta.setdefault("file_stages", {})
                if status in ("start", "progress"):
                    _fs[stage] = "running"
                    job.meta.update({"stage": stage,
                                     "stage_detail": str(detail or "")[:60]})
                elif status in ("done", "skip"):
                    _fs[stage] = "done" if status == "done" else "skip"
                    job.meta.update({"stage": "", "stage_detail": ""})
                job.add(f"[stage] {stage} {status} {detail or ''}".rstrip())

            if body.force:
                results = []
                for i, meta in enumerate(targets, 1):
                    _files_state[meta.content_hash] = "r"
                    _reset_file_view()
                    job.meta.update({"current": i - 1, "total": len(targets),
                                     "filename": getattr(meta, "file_name", "")})
                    job.add(f"[file] {i}/{len(targets)} {getattr(meta, 'file_name', '')}")
                    ap = getattr(meta, "absolute_path", "")
                    if getattr(meta, "asset_type", "video") == "video":
                        analyze_video(ap, force=True, progress_callback=_stage_cb)
                    elif getattr(meta, "asset_type", "") == "audio":
                        analyze_audio(ap, force=True)
                    results.append(annotate_asset(meta))
                    _files_state[meta.content_hash] = "d"
                    job.meta.update({"current": i, "stage": ""})
                upsert_annotations(results)
            else:
                def _start_cb(filename):
                    h = _name_to_hash.get(filename)
                    if h:
                        _files_state[h] = "r"
                    _reset_file_view()
                    job.meta.update({"filename": filename})
                    job.add(f"[file] start {filename}")
                def _file_cb(current, total, filename):
                    h = _name_to_hash.get(filename)
                    if h:
                        _files_state[h] = "d"
                        (job.meta.get("stage_by_hash") or {}).pop(h, None)
                    job.meta.update({"current": current, "total": total})
                    job.add(f"[file] {current}/{total} {filename} done")
                results = batch_annotate(targets, progress_callback=_file_cb,
                                         stage_callback=_stage_cb, start_callback=_start_cb)
                upsert_annotations(results)
            job.add(f"DONE — {len(results)} assets annotated")
            job.status = "done"
        except Exception as e:
            import traceback
            job.add(f"ERROR: {e}\n{traceback.format_exc()[-800:]}")
            job.status = "error"
        finally:
            _progress.HOOK = None
            job.save(force=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job.id}


class SelectRequest(BaseModel):
    instruction: str = ""
    project_id: str = ""
    target_length: float = 0.0


@app.post("/api/assets/auto-select")
def auto_select(body: SelectRequest):
    """Agent asset selection as a staged background job (poll via /api/jobs)."""
    root = SCANNED["root"] or _resolve(cfg("ASSET_ROOT_DIR", "resource/imports/"))
    instr = body.instruction.strip() or cfg("INSTRUCTION", "").strip() or "travel montage"
    target_dur = body.target_length or (float(cfg("AUDIO_SEGMENT_MAX_DURATION_SEC", "20.0")) - 5.0)

    job = Job("select")
    job.meta["stages"] = {}
    JOBS[job.id] = job

    _STATUS_MAP = {"start": "running"}

    def _st(stage: str, status: str, detail: str = ""):
        status = _STATUS_MAP.get(status, status)
        job.meta["stages"] = {**job.meta.get("stages", {}), stage: {"status": status, "detail": detail}}
        job.add(f"[{stage}] {status} {detail}".rstrip())

    def _run():
        _reload_runtime_config()
        try:
            from src.asset_manager.selector import select_assets
            from src.utils.video_concat import create_slideshow_video

            sel = select_assets(
                instruction=instr,
                target_duration_sec=max(15.0, target_dur),
                stage_callback=_st,
            )

            if not (sel.selected_videos or sel.selected_images or sel.selected_audio):
                job.add(f"选材失败：{sel.rationale}")
                job.meta["selection"] = _dump(sel)
                job.status = "error"
                return

            def _abs(p):
                return p if os.path.isabs(p) else os.path.join(root, p)

            abs_videos = [_abs(p) for p in sel.selected_videos]

            if sel.selected_images:
                _st("slideshow", "running", f"{len(sel.selected_images)} 张图片")
                ss = create_slideshow_video(
                    [_abs(p) for p in sel.selected_images],
                    duration_per_image=float(cfg("ASSET_IMAGE_DURATION_SEC", "3.0")))
                if ss:
                    abs_videos.append(ss)
                    _st("slideshow", "done", "Ken Burns 幻灯片已生成")
                else:
                    _st("slideshow", "error", "幻灯片生成失败")
            else:
                _st("slideshow", "skip", "没有选中图片")

            _st("apply", "running")
            if abs_videos:
                save_config("VIDEO_PATH", "||".join(abs_videos))
            audio_abs = _abs(sel.selected_audio[0]) if sel.selected_audio else ""
            if audio_abs:
                save_config("AUDIO_PATH", audio_abs)
            if body.project_id:
                try:
                    p = _load_project(body.project_id)
                    if abs_videos:
                        p["videos"] = abs_videos
                    if audio_abs:
                        p["audio"] = audio_abs
                    p["selection_rationale"] = sel.rationale or ""
                    _save_project(p)
                except Exception:
                    pass
            _st("apply", "done", "已写入当前项目")

            job.meta["selection"] = _dump(sel)
            job.meta["videos"] = abs_videos
            job.meta["audio"] = audio_abs
            job.status = "done"
        except Exception as e:
            import traceback
            job.add(f"ERROR: {e}\n{traceback.format_exc()[-600:]}")
            job.status = "error"
        finally:
            job.save(force=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job.id}


# ── AI instruction suggestions ─────────────────────────────────────────────

class SuggestRequest(BaseModel):
    project_id: str = ""


@app.post("/api/instruction/suggestions")
def instruction_suggestions(body: SuggestRequest):
    """Generate a few diverse editing-instruction suggestions from asset annotations."""
    import re as _re
    import litellm
    from src.asset_manager.index_store import get_all_summaries

    summaries = get_all_summaries()
    if summaries.startswith("(No annotated"):
        raise HTTPException(400, "没有已标注素材 — 先在素材库扫描并标注。")

    project_ctx = ""
    if body.project_id:
        try:
            p = _load_project(body.project_id)
            names = [os.path.basename(v) for v in p.get("videos", [])]
            if names:
                project_ctx = "当前项目已选视频素材：" + "、".join(names)
            if p.get("audio"):
                project_ctx += f"；音乐：{os.path.basename(p['audio'])}"
        except HTTPException:
            pass

    prompt = (
        "你是短视频混剪的创意顾问。根据下面的素材标注摘要"
        + ("和当前项目已选素材" if project_ctx else "")
        + "，提出 4 条风格差异明显的剪辑指令建议"
        "（每条一句话、20~40 个字、中文；风格覆盖如：情感叙事、快节奏卡点、氛围沉浸、旅行记录等）。\n\n"
        + (project_ctx + "\n\n" if project_ctx else "")
        + f"素材库摘要：\n{summaries[:6000]}\n\n"
        '只返回 JSON 数组，例如 ["建议一","建议二","建议三","建议四"]，不要其他内容。'
    )

    kwargs: dict = dict(
        model=cfg("AGENT_LITELLM_MODEL", ""),
        messages=[{"role": "user", "content": prompt}],
        # reasoning models (e.g. gemini-flash thinking) burn budget on thought
        # tokens first — a small max_tokens yields EMPTY content
        temperature=0.9, max_tokens=8192,
    )
    if cfg("AGENT_LITELLM_URL", ""):
        kwargs["api_base"] = cfg("AGENT_LITELLM_URL", "")
    if cfg("AGENT_LITELLM_API_KEY", ""):
        kwargs["api_key"] = cfg("AGENT_LITELLM_API_KEY", "")

    finish_reason = ""
    try:
        raw = litellm.completion(**kwargs)
        msg = raw.choices[0].message
        finish_reason = str(getattr(raw.choices[0], "finish_reason", "") or "")
        content = msg.content or getattr(msg, "reasoning_content", None) or ""
    except Exception as e:
        raise HTTPException(502, f"LLM 调用失败：{str(e)[:200]}")

    text = content.strip()
    m = _re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, _re.DOTALL)
    if m:
        text = m.group(1).strip()
    arr: list = []
    try:
        arr = json.loads(text)
    except Exception:
        s, e2 = text.find("["), text.rfind("]")
        if s >= 0 and e2 > s:
            try:
                arr = json.loads(text[s:e2 + 1])
            except Exception:
                arr = []
    arr = [str(x).strip() for x in arr if str(x).strip()][:6]
    if not arr:
        raise HTTPException(
            502,
            f"无法解析模型返回（finish_reason={finish_reason or '?'}，长度={len(content)}）：{content[:150]}")
    return {"suggestions": arr}


@app.post("/api/params/suggestions")
def params_suggestions(body: SuggestRequest):
    """Suggest target_length + shot_length from the project's assets + instruction."""
    import re as _re
    import litellm
    from src.asset_manager.index_store import load_index

    _reload_runtime_config()
    if not body.project_id:
        raise HTTPException(400, "缺少 project_id")
    p = _load_project(body.project_id)
    instr = (p.get("instruction") or "").strip()

    idx = load_index()
    by_path: dict = {}
    for ann in idx.values():
        ap = getattr(ann.metadata, "absolute_path", "") or ""
        if ap:
            by_path[os.path.normcase(os.path.abspath(ap))] = ann
            by_path[os.path.basename(ap)] = ann

    def _find(path: str):
        if not path:
            return None
        return (by_path.get(os.path.normcase(os.path.abspath(_resolve(path))))
                or by_path.get(os.path.basename(path)))

    video_lines = []
    total_video_dur = 0.0
    for v in p.get("videos", []):
        a = _find(v)
        d = float(getattr(a.metadata, "duration_sec", 0) or 0) if a else 0.0
        total_video_dur += d
        summ = str(getattr(a.annotation, "summary", "") or "")[:80] if a else ""
        video_lines.append(f"- {os.path.basename(v)}（{d:.0f}s）{summ}")

    audio_desc = "（未选择音乐）"
    audio_dur = 0.0
    aa = _find(p.get("audio", ""))
    # MEASURED tempo beats any annotation: annotation.bpm historically came from
    # an LLM guess (it would answer genre clichés like "128" for a 70 BPM song).
    # Derive the felt pulse from the madmom downbeat grid cached in the audio's
    # analyzed captions — works for old caches too.
    measured_bpm = None
    try:
        _ap = os.path.normcase(os.path.abspath(_resolve(p.get("audio", "") or "")))
        if _ap:
            for _md in glob.glob(os.path.join(PROJECT_ROOT, "Output", "analyzed", "*", "metadata.json")):
                try:
                    with open(_md, "r", encoding="utf-8") as fh:
                        _meta = json.load(fh)
                except Exception:
                    continue
                if os.path.normcase(str(_meta.get("absolute_path", ""))) != _ap:
                    continue
                _cj = os.path.join(os.path.dirname(_md), "captions.json")
                if os.path.exists(_cj):
                    with open(_cj, "r", encoding="utf-8") as fh:
                        _cap = json.load(fh)
                    measured_bpm = ((_cap.get("facts") or {}).get("bpm_felt")
                                    or (_cap.get("measured_tempo") or {}).get("bpm_felt"))
                    if not measured_bpm:  # older caches: derive from downbeat gaps
                        _db = sorted(k["time"] for k in (_cap.get("_keypoints_detail") or [])
                                     if k.get("type") == "Downbeat")
                        _gaps = sorted(b - a for a, b in zip(_db, _db[1:]) if 0.8 <= b - a <= 8.0)
                        if len(_gaps) >= 4:
                            _bar = _gaps[len(_gaps) // 2]
                            measured_bpm = round(240.0 / _bar, 1)
                break
    except Exception:
        measured_bpm = None
    if aa:
        audio_dur = float(getattr(aa.metadata, "duration_sec", 0) or 0)
        bpm = measured_bpm or getattr(aa.annotation, "bpm", None)
        bpm_tag = "（实测律动）" if measured_bpm else "（估计，未实测）"
        energy = getattr(aa.annotation, "energy_level", None)
        genre = getattr(aa.annotation, "genre", None)
        audio_desc = (f"时长 {audio_dur:.0f}s"
                      + (f"，BPM {bpm}{bpm_tag}" if bpm else "")
                      + (f"，能量 {energy}" if energy else "")
                      + (f"，曲风 {genre}" if genre else ""))
    elif p.get("audio"):
        audio_desc = f"{os.path.basename(p['audio'])}（未标注，无节奏信息）"

    prompt = (
        "你是短视频混剪参数顾问。根据剪辑指令和素材情况，推荐两项参数：\n"
        "1. target_length_sec：成片目标时长（秒，10~120 之间，不得超过音乐时长的 90%，"
        "也要考虑视频素材总量是否足够填满）\n"
        "2. shot_length_sec：单镜头平均长度（秒，0.5~8 之间。快节奏卡点应对齐节拍——"
        "例如 1 拍 = 60/BPM 秒、1 小节 = 240/BPM 秒；情感叙事可以更长）\n\n"
        f"剪辑指令：{instr or '（未填写）'}\n"
        f"音乐：{audio_desc}\n"
        f"视频素材（共 {total_video_dur:.0f}s）：\n" + ("\n".join(video_lines) or "（未选择视频）") + "\n\n"
        '只返回 JSON，例如 {"target_length_sec": 35, "shot_length_sec": 2.0, "rationale": "一句话理由"}'
    )

    kwargs: dict = dict(
        model=cfg("AGENT_LITELLM_MODEL", ""),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4, max_tokens=8192,
    )
    if cfg("AGENT_LITELLM_URL", ""):
        kwargs["api_base"] = cfg("AGENT_LITELLM_URL", "")
    if cfg("AGENT_LITELLM_API_KEY", ""):
        kwargs["api_key"] = cfg("AGENT_LITELLM_API_KEY", "")

    try:
        raw = litellm.completion(**kwargs)
        msg = raw.choices[0].message
        content = msg.content or getattr(msg, "reasoning_content", None) or ""
    except Exception as e:
        raise HTTPException(502, f"LLM 调用失败：{str(e)[:200]}")

    text = content.strip()
    m = _re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, _re.DOTALL)
    if m:
        text = m.group(1).strip()
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        s, e2 = text.find("{"), text.rfind("}")
        if s >= 0 and e2 > s:
            try:
                parsed = json.loads(text[s:e2 + 1])
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        raise HTTPException(502, f"无法解析模型返回：{content[:150]}")

    try:
        target = float(parsed.get("target_length_sec", 30))
        shot = float(parsed.get("shot_length_sec", 3))
    except Exception:
        raise HTTPException(502, f"模型返回的参数不是数字：{parsed}")

    # clamp to sane / feasible bounds
    target = max(10.0, min(300.0, target))
    if audio_dur > 5:
        target = min(target, round(audio_dur * 0.9, 1))
    shot = max(0.3, min(15.0, round(shot, 2)))

    return {
        "target_length": round(target, 1),
        "shot_length": shot,
        "rationale": str(parsed.get("rationale", ""))[:300],
    }


# ── Analysis data (for chart visualizations) ───────────────────────────────

@app.get("/api/json")
def get_json_file(path: str):
    """Serve a JSON artifact (shot_point / captions / …) under Output/."""
    abs_path = os.path.abspath(_resolve(path))
    out_root = os.path.abspath(os.path.join(PROJECT_ROOT, "Output"))
    if not abs_path.startswith(out_root):
        raise HTTPException(403, "only files under Output/ are allowed")
    if not os.path.isfile(abs_path):
        raise HTTPException(404, f"not found: {path}")
    with open(abs_path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/audio/keypoints")
def audio_keypoints(path: str):
    """Raw madmom keypoints for an audio file (from the shared keypoint cache)."""
    abs_path = _resolve(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(404, f"not found: {path}")
    from src.asset_manager.scanner import compute_content_hash
    import src.config as config
    ch = compute_content_hash(abs_path)
    kp_dir = os.path.join(_resolve(getattr(config, "VIDEO_DATABASE_FOLDER", "./Output/")),
                          "analyzed", "keypoints")
    out: dict = {}
    if os.path.isdir(kp_dir):
        for fn in os.listdir(kp_dir):
            if fn.startswith(ch[:16]) and fn.endswith(".json"):
                parts = fn[:-5].split("_")
                method = parts[1] if len(parts) > 1 else fn
                try:
                    with open(os.path.join(kp_dir, fn), "r", encoding="utf-8") as f:
                        out[method] = json.load(f)
                except Exception:
                    pass
    return out


# ── Projects ────────────────────────────────────────────────────────────────
# A project = one edit: selected assets, instruction, params, pipeline result.
# Asset annotations stay asset-level (hash-cached), independent of projects.

PROJECTS_DIR = os.path.join(PROJECT_ROOT, "Output", "projects")

_PROJECT_DEFAULTS = {
    "name": "",
    "videos": [], "audio": "", "instruction": "",
    "has_dialogue": False, "main_character": "", "srt": "",
    "target_length": 30.0, "shot_length": 4.0,
    "selection_rationale": "",
    "shot_point": "", "effective_video": "",
    "last_run_at": None, "last_run_status": "",
}


def _project_file(pid: str) -> str:
    return os.path.join(PROJECTS_DIR, pid, "project.json")


def _load_project(pid: str) -> dict:
    fp = _project_file(pid)
    if not os.path.exists(fp):
        raise HTTPException(404, f"project not found: {pid}")
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_project(p: dict) -> dict:
    p["updated_at"] = time.time()
    os.makedirs(os.path.dirname(_project_file(p["id"])), exist_ok=True)
    with open(_project_file(p["id"]), "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    return p


@app.get("/api/projects")
def list_projects():
    out = []
    if os.path.isdir(PROJECTS_DIR):
        for pid in os.listdir(PROJECTS_DIR):
            fp = _project_file(pid)
            if os.path.exists(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        out.append(json.load(f))
                except Exception:
                    pass
    out.sort(key=lambda p: p.get("updated_at", 0), reverse=True)
    return out


class ProjectCreate(BaseModel):
    name: str = ""
    from_config: bool = False   # seed with legacy config.py values


@app.post("/api/projects")
def create_project(body: ProjectCreate):
    pid = "p_" + uuid.uuid4().hex[:8]
    p = dict(_PROJECT_DEFAULTS)
    p["id"] = pid
    p["name"] = body.name.strip() or f"项目 {time.strftime('%m-%d %H:%M')}"
    p["created_at"] = time.time()
    if body.from_config:
        p["videos"] = [v for v in cfg("VIDEO_PATH", "").split("||") if v]
        p["audio"] = cfg("AUDIO_PATH", "")
        p["instruction"] = cfg("INSTRUCTION", "")
    return _save_project(p)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return _load_project(pid)


class ProjectPatch(BaseModel):
    patch: dict


@app.put("/api/projects/{pid}")
def update_project(pid: str, body: ProjectPatch):
    p = _load_project(pid)
    allowed = set(_PROJECT_DEFAULTS.keys())
    for k, v in body.patch.items():
        if k in allowed:
            p[k] = v
    return _save_project(p)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    fp = _project_file(pid)
    if os.path.exists(fp):
        os.remove(fp)
        try:
            os.rmdir(os.path.dirname(fp))
        except OSError:
            pass
    return {"ok": True}


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


_EVENT_STATE = {"start": "r", "done": "d", "fail": "f"}


def _apply_progress_ev(job: Job, ev: dict):
    """Fold one fine-grained progress event into job.meta['tasks']."""
    with job.lock:
        tasks = job.meta.setdefault("tasks", {})
        name = str(ev.get("task", "task"))
        t = tasks.setdefault(name, {"total": 0, "states": {}})
        total = ev.get("total")
        if isinstance(total, int) and total > 0:
            t["total"] = total
        idx = ev.get("idx", -1)
        event = ev.get("event", "")
        if event == "step" and isinstance(idx, int) and idx >= 0:
            # detailed agent iteration step — stored in traces, fetched on demand
            traces = job.meta.setdefault("traces", {}).setdefault(name, {})
            lst = traces.setdefault(str(idx), [])
            lst.append({k: ev[k] for k in ("phase", "iter", "max_iter", "elapsed", "tool", "args", "reply", "verdict", "result", "note") if k in ev})
            if len(lst) > 80:
                del lst[:len(lst) - 80]
            t.setdefault("iters", {})[str(idx)] = f"{ev.get('iter', '?')}/{ev.get('max_iter', '?')}"
            return
        if event == "reset":
            t["states"] = {}
            t["labels"] = {}
        if isinstance(idx, int) and idx >= 0:
            if "label" in ev:
                t.setdefault("labels", {})[str(idx)] = str(ev["label"])[:60]
            if event == "retry":
                t["states"].pop(str(idx), None)   # back to pending
            elif event in _EVENT_STATE:
                t["states"][str(idx)] = _EVENT_STATE[event]
        t["done"] = sum(1 for v in t["states"].values() if v == "d")
        t["fail"] = sum(1 for v in t["states"].values() if v == "f")
        for k in ("avg", "eta"):
            if k in ev:
                t[k] = ev[k]


def _parse_progress_line(job: Job, line: str):
    pos = line.find("@@PROGRESS ")
    if pos < 0:
        return
    try:
        ev = json.loads(line[pos + len("@@PROGRESS "):])
    except Exception:
        return
    _apply_progress_ev(job, ev)
    job.save()


def _on_pipeline_line(job: Job, line: str):
    _parse_stage(job, line)
    _parse_progress_line(job, line)


def _derive_shot_point_path(video_paths: list, audio_path: str, instruction: str) -> str:
    """Mirror local_run.py exactly: project dir = {primary_hash[:12]}_{audio_id}."""
    import src.config as config
    audio_id = os.path.splitext(os.path.basename(audio_path))[0].replace('.', '_').replace(' ', '_') if audio_path else "no_audio"
    ih = hashlib.md5(instruction.encode("utf-8")).hexdigest()[:8]
    # Hash-only id (must match local_run.py exactly): always ASCII, no sanitizing.
    iid = f"instruction_{ih}"
    primary_hash = ""
    try:
        from src.asset_manager.scanner import compute_content_hash
        primary_hash = compute_content_hash(_resolve(video_paths[0]))[:12]
    except Exception:
        pass
    proj = f"{primary_hash or 'unknown'}_{audio_id}"
    return os.path.join(config.VIDEO_DATABASE_FOLDER, 'Output', proj, f"shot_point_{iid}.json")


class PipelineRequest(BaseModel):
    video_paths: list[str]
    audio_path: str
    instruction: str
    has_dialogue: bool = False
    main_character: str = ""
    srt_path: str = ""
    target_length: float = 30.0
    shot_length: float = 4.0
    project_id: str = ""


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
    if not body.instruction.strip():
        raise HTTPException(400, "剪辑指令为空 — 请先填写你想要的剪辑效果。")
    video_type = "film" if body.has_dialogue else "vlog"
    min_d = max(5.0, body.target_length - 5.0)
    max_d = body.target_length + 5.0
    # Shot length is no longer a user setting — pacing is decided automatically
    # from the music's energy (self-calibrating). AUDIO_MIN/MAX_SEGMENT_DURATION
    # stay at their config defaults (the perceptual fast/slow bounds) and are NOT
    # overridden here.

    # persist UI choices like the Streamlit app does
    save_config("VIDEO_PATH", "||".join(videos))
    save_config("AUDIO_PATH", body.audio_path)
    save_config("INSTRUCTION", body.instruction)
    save_config("AUDIO_SEGMENT_MIN_DURATION_SEC", str(min_d))
    save_config("AUDIO_SEGMENT_MAX_DURATION_SEC", str(max_d))

    cmd = [
        sys.executable, "local_run.py",
        "--Video_Path", *[_resolve(v) for v in videos],
        "--Audio_Path", _resolve(body.audio_path),
        "--Instruction", body.instruction,
        "--type", video_type, "--instruction_type", "object",
        "--config.AUDIO_SEGMENT_MIN_DURATION_SEC", str(min_d),
        "--config.AUDIO_SEGMENT_MAX_DURATION_SEC", str(max_d),
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

    # "effective video" for downstream use = the primary (first) REAL video;
    # multi-source projects never pre-merge, so a planned merged path is a lie
    effective = _resolve(videos[0])
    shot_point = _derive_shot_point_path(videos, body.audio_path, body.instruction)

    job = Job("pipeline")
    job.meta.update({
        "stages": {s: "pending" for s in _STAGE_NAMES},
        "stage_times": {}, "start_time": time.time(),
        "shot_point": shot_point, "effective_video": effective,
        "project_id": body.project_id,
    })
    # snapshot settings + result location into the project record
    if body.project_id:
        try:
            p = _load_project(body.project_id)
            p.update(
                videos=videos, audio=body.audio_path, instruction=body.instruction,
                has_dialogue=body.has_dialogue, main_character=body.main_character,
                srt=body.srt_path, target_length=body.target_length,
                shot_length=body.shot_length, shot_point=shot_point,
                effective_video=effective,
                last_run_at=time.time(), last_run_status="running",
            )
            _save_project(p)
        except HTTPException:
            pass
    try:
        _spawn(job, cmd, on_line=_on_pipeline_line)
    except Exception as e:
        raise HTTPException(500, str(e))
    PIPELINE_JOB_ID = job.id
    return {"job_id": job.id, "shot_point": shot_point, "effective_video": effective}


class RetryShotRequest(BaseModel):
    project_id: str
    section_idx: int
    shot_idx: int


def _remove_shot_from_point(shot_point_path: str, section_idx: int, shot_idx: int) -> bool:
    """Drop one shot's committed pick so the next run re-selects it. True if removed."""
    if not shot_point_path or not os.path.exists(shot_point_path):
        return False
    try:
        with open(shot_point_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    if not isinstance(data, list):
        return False
    kept = [r for r in data if not (
        isinstance(r, dict)
        and r.get("section_idx") == section_idx
        and r.get("shot_idx") == shot_idx
    )]
    if len(kept) == len(data):
        return False
    with open(shot_point_path, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)
    return True


@app.post("/api/pipeline/retry_shot")
def retry_shot(body: RetryShotRequest):
    """Re-generate a single shot: drop its committed pick from shot_point, then
    relaunch the pipeline with the project's saved params. Analysis + shot plan
    are cached, so the run resumes and only the dropped shot is re-selected. For
    a shot that FAILED (never committed) this is just a resume — nothing to drop."""
    if PIPELINE_JOB_ID and JOBS.get(PIPELINE_JOB_ID) and JOBS[PIPELINE_JOB_ID].status == "running":
        raise HTTPException(409, "流水线正在运行，请先停止再重试单个镜头。")
    p = _load_project(body.project_id)
    removed = _remove_shot_from_point(p.get("shot_point", ""), body.section_idx, body.shot_idx)
    preq = PipelineRequest(
        video_paths=p.get("videos", []) or [],
        audio_path=p.get("audio", "") or "",
        instruction=p.get("instruction", "") or "",
        has_dialogue=bool(p.get("has_dialogue", False)),
        main_character=p.get("main_character", "") or "",
        srt_path=p.get("srt", "") or "",
        target_length=float(p.get("target_length", 30.0) or 30.0),
        shot_length=float(p.get("shot_length", 4.0) or 4.0),
        project_id=body.project_id,
    )
    result = pipeline_start(preq)
    return {**result, "removed": removed,
            "section_idx": body.section_idx, "shot_idx": body.shot_idx}


@app.post("/api/pipeline/stop")
def pipeline_stop():
    job = JOBS.get(PIPELINE_JOB_ID or "")
    if not job or job.status != "running":
        return {"ok": False, "message": "Not running."}
    _kill(job)
    return {"ok": True}


@app.get("/api/pipeline/current")
def pipeline_current(since: int = 0, project_id: str = ""):
    if project_id:
        # latest pipeline job belonging to THIS project (running or finished)
        for job in reversed(list(JOBS.values())):
            if job.kind == "pipeline" and job.meta.get("project_id") == project_id:
                return {"job": job.to_dict(since)}
        return {"job": None}
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
    transition: float = 0.0   # crossfade seconds between clips (0 = hard cuts)
    transition_mode: str = ""  # "" | "uniform" | "ai" (LLM picks per-cut transitions)


@app.post("/api/render")
def render(body: RenderRequest):
    abs_point = _resolve(body.shot_point)
    abs_plan = abs_point.replace("shot_point_", "shot_plan_")
    if not os.path.exists(abs_point):
        raise HTTPException(400, f"shot_point not found: {abs_point}")
    # Isolate the render per shot_point (≈ per project/instruction). The project
    # DIR is keyed only by primary-video-hash + audio, so different instructions
    # share it; without a per-instruction tag every project overwrote the same
    # output_{ratio}.mp4 and saw each other's renders. Tag = hash of shot_point name.
    _sp_tag = hashlib.md5(os.path.basename(abs_point).encode("utf-8")).hexdigest()[:8]
    out = os.path.join(os.path.dirname(abs_point), f"output_{body.ratio.replace(':', 'x')}_{_sp_tag}.mp4")
    ending = os.path.join(PROJECT_ROOT, "resource", "ending", "ending.mp4")
    font = os.path.join(PROJECT_ROOT, "resource", "font", "Pulp Fiction Italic M54.ttf")

    # --video is only a fallback in multi-source mode (clips carry their own
    # video_path). Never pass a non-existent path (e.g. a planned-but-never-
    # created merged file) — resolve a real source from the shot_point instead.
    vid = _resolve(body.video_path)
    if not vid or not os.path.exists(vid):
        try:
            with open(abs_point, "r", encoding="utf-8") as f:
                _data = json.load(f)
            for _r in (_data if isinstance(_data, list) else []):
                for _c in ([_r] + (_r.get("clips") or [])):
                    vp = _c.get("video_path") or ""
                    if vp and os.path.exists(vp):
                        vid = vp
                        break
                if vid and os.path.exists(vid):
                    break
        except Exception:
            pass
    if not vid or not os.path.exists(vid):
        raise HTTPException(400, "找不到任何存在的源视频（项目视频与 shot_point 中的 clip 路径均无效）")

    cmd = [
        sys.executable, "render/render_video.py",
        "--shot-plan", abs_plan, "--shot-json", abs_point,
        "--video", vid, "--audio", _resolve(body.audio_path),
        "--output", out, "--crop-ratio", body.ratio, "--no-labels",
    ]
    if body.has_dialogue:
        cmd += ["--render-hook-dialogue"]
    if body.transition_mode == "ai":
        cmd += ["--transition-mode", "ai"]
    elif body.transition and body.transition > 0:
        cmd += ["--transition", str(body.transition)]
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


def _ts_to_sec(v) -> float:
    """Accept '00:00:13.2' | '13.2' | 13.2 → seconds."""
    if v is None:
        return float("nan")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        if ":" in s:
            parts = [float(x) for x in s.split(":")]
            mult = [1, 60, 3600]
            return sum(p * m for p, m in zip(reversed(parts), mult))
        return float(s)
    except (ValueError, TypeError):
        return float("nan")


@app.get("/api/render/clip_map")
def render_clip_map(shot_point: str):
    """Map the finished-video timeline → each source clip + the AI description
    that drove its selection. Joins shot_point.json (coordinates the renderer
    actually uses) with shot_plan.json (Screenwriter's per-shot intent)."""
    abs_point = _resolve(shot_point)
    if not os.path.exists(abs_point):
        return {"clips": [], "total": 0.0, "error": "shot_point not found"}
    try:
        entries = json.load(open(abs_point, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"clips": [], "total": 0.0, "error": f"parse: {e}"}
    if not isinstance(entries, list):
        entries = entries.get("shots") or entries.get("clips") or []

    # sibling shot_plan.json (same instruction id) → descriptions
    plan_path = abs_point.replace("shot_point_", "shot_plan_")
    plan_shots = {}  # (section_idx, shot_idx) -> desc dict
    try:
        plan = json.load(open(plan_path, encoding="utf-8"))
        for si, sec in enumerate(plan.get("video_structure", []) or []):
            for shi, shot in enumerate((sec.get("shot_plan") or {}).get("shots", []) or []):
                plan_shots[(si, shi)] = shot
    except Exception:  # noqa: BLE001
        pass

    # basename → dense_segments (what the VLM actually saw, from the analysis cache).
    # This is the ground truth the editor selected against; comparing it to the
    # Screenwriter's intent shows whether a clip's pick actually matches its slot.
    dense_by_video: dict = {}
    analyzed_root = os.path.join(PROJECT_ROOT, "Output", "analyzed")
    for md in glob.glob(os.path.join(analyzed_root, "*", "metadata.json")):
        try:
            meta = json.load(open(md, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        name = meta.get("file_name")
        if not name:
            continue
        ck = os.path.join(os.path.dirname(md), "captions", "ckpt")
        if not os.path.isdir(ck):
            continue
        segs: dict = {}  # (s,e) -> content, dedup across ckpt files
        for f in os.listdir(ck):
            if not f.endswith(".json"):
                continue
            try:
                dd = json.load(open(os.path.join(ck, f), encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for sg in dd.get("dense_segments") or []:
                s, e = sg.get("start_sec_abs"), sg.get("end_sec_abs")
                if s is None or e is None:
                    continue
                segs[(round(float(s), 1), round(float(e), 1))] = sg.get("content_description", "")
        if segs:
            dense_by_video[name] = sorted(
                ([s, e, txt] for (s, e), txt in segs.items()), key=lambda x: x[0])

    def _analysis_at(video_name: str, a: float, b: float) -> str:
        """VLM descriptions of segments overlapping [a,b] in the source video."""
        segs = dense_by_video.get(video_name)
        if not segs or a != a or b != b:  # NaN guard
            return ""
        parts = []
        for s, e, txt in segs:
            if e > a and s < b and txt:  # overlap
                parts.append(f"[{s:g}-{e:g}s] {txt}")
        return "  ".join(parts)

    out, cursor = [], 0.0
    for e in entries:
        if not isinstance(e, dict):
            continue
        sec_i = e.get("section_idx", 0)
        shot_i = e.get("shot_idx", 0)
        desc = plan_shots.get((sec_i, shot_i), {})
        for c in e.get("clips", []) or []:
            src_s = _ts_to_sec(c.get("start"))
            src_e = _ts_to_sec(c.get("end"))
            dur = c.get("duration")
            dur = float(dur) if isinstance(dur, (int, float)) else (src_e - src_s)
            if not (dur > 0):
                continue
            out.append({
                "out_start": round(cursor, 2),
                "out_end": round(cursor + dur, 2),
                "duration": round(dur, 2),
                "video": os.path.basename(str(c.get("video_path") or e.get("video_path") or "")),
                "src_start": round(src_s, 2) if src_s == src_s else None,
                "src_end": round(src_e, 2) if src_e == src_e else None,
                "section_idx": sec_i,
                "shot_idx": shot_i,
                "content": desc.get("content", ""),
                "visuals": desc.get("visuals", ""),
                "emotion": desc.get("emotion", ""),
                "visual_beat": desc.get("visual_beat", ""),
                # ground truth: what the VLM actually saw at this source range
                "analysis": _analysis_at(
                    os.path.basename(str(c.get("video_path") or e.get("video_path") or "")),
                    src_s, src_e),
            })
            cursor += dur
    return {"clips": out, "total": round(cursor, 2)}


@app.get("/api/render/outputs")
def render_outputs(shot_point: str):
    abs_point = _resolve(shot_point)
    d = os.path.dirname(abs_point)
    _sp_tag = hashlib.md5(os.path.basename(abs_point).encode("utf-8")).hexdigest()[:8]
    out = []
    for ratio in ("9:16", "16:9", "1:1"):
        p = os.path.join(d, f"output_{ratio.replace(':', 'x')}_{_sp_tag}.mp4")
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
    if "--reload" in sys.argv:
        # dev mode: auto-restart on backend code changes
        # (in-memory jobs are lost on reload — don't use mid-pipeline)
        uvicorn.run("server.main:app", host="127.0.0.1", port=8765,
                    reload=True, reload_dirs=[os.path.join(PROJECT_ROOT, "server")])
    else:
        uvicorn.run(app, host="127.0.0.1", port=8765)

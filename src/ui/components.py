"""Reusable UI components: pipeline graph, live status, error banner, model card."""

import os
import time

import streamlit as st

from .helpers import cfg, save_config

_STAGE_NAMES = ["shot_detection", "asr", "video_captioning", "audio_analysis", "screenwriter", "editor"]


# ── Pipeline graph ─────────────────────────────────────────────────────────

def build_graph_html(stage_status: dict, stage_times: dict) -> str:
    done_any = any(v in ("done", "running") for v in stage_status.values())
    input_state = "done" if done_any else "pending"
    output_state = "done" if stage_status.get("editor") == "done" else "pending"

    def node(stage, label, state_override=None):
        state = state_override or stage_status.get(stage, "pending")
        t = stage_times.get(stage)
        time_html = f'<div class="nt">{int(t)}s</div>' if t else ""
        badge = {"done": "✓", "error": "✗", "running": "●"}.get(state, "")
        badge_html = f'<div class="nb">{badge}</div>' if badge else ""
        return f'<div class="node node-{state}"><div class="ni">{badge_html}<div class="nl">{label}</div>{time_html}</div></div>'

    def arrow():
        return '<div class="arr">→</div>'

    par_nodes = (
        '<div class="par-col">'
        f'{node("asr", "ASR")}{node("video_captioning", "Video")}{node("audio_analysis", "Audio")}'
        '</div>'
    )

    css = """<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:transparent}
.pipeline{display:flex;align-items:center;justify-content:center;font-family:"Inter","SF Pro Display",-apple-system,BlinkMacSystemFont,sans-serif;padding:16px 8px;gap:0;flex-wrap:nowrap;}
.node{display:flex;align-items:center;justify-content:center;width:80px;height:64px;border-radius:10px;border:1px solid #e5e7eb;background:#fafbfc;transition:all 0.3s ease;flex-shrink:0;}
.ni{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;width:100%;padding:0 4px;}
.nb{font-size:0.7rem;line-height:1;color:#6b7280}
.nl{font-size:0.65rem;font-weight:600;text-align:center;line-height:1.3;color:#374151}
.nt{font-size:0.55rem;color:#9ca3af;font-weight:400}
.node-pending{border-color:#e5e7eb;background:#f9fafb;color:#9ca3af}
.node-running{border-color:#93c5fd;background:#eff6ff;color:#1d4ed8;box-shadow:0 0 0 3px rgba(59,130,246,0.12);animation:gpulse 2s ease-in-out infinite}
.node-done{border-color:#a7f3d0;background:#ecfdf5;color:#065f46}
.node-error{border-color:#fecaca;background:#fef2f2;color:#991b1b}
.arr{color:#d1d5db;font-size:0.9rem;padding:0 4px;flex-shrink:0;align-self:center;line-height:1}
.par-wrap{display:flex;align-items:center;flex-shrink:0}
.par-col{display:flex;flex-direction:column;gap:4px;flex-shrink:0}
@keyframes gpulse{0%,100%{box-shadow:0 0 0 3px rgba(59,130,246,0.12)}50%{box-shadow:0 0 0 6px rgba(59,130,246,0.06)}}
</style>"""

    fork_join = f"""
<div class="par-wrap">
  <svg width="28" height="110" viewBox="0 0 28 110" fill="none"><line x1="0" y1="55" x2="14" y2="55" stroke="#d1d5db" stroke-width="1.5"/><line x1="14" y1="18" x2="14" y2="92" stroke="#d1d5db" stroke-width="1.5"/><line x1="14" y1="18" x2="28" y2="18" stroke="#d1d5db" stroke-width="1.5"/><line x1="14" y1="55" x2="28" y2="55" stroke="#d1d5db" stroke-width="1.5"/><line x1="14" y1="92" x2="28" y2="92" stroke="#d1d5db" stroke-width="1.5"/></svg>
  {par_nodes}
  <svg width="28" height="110" viewBox="0 0 28 110" fill="none"><line x1="28" y1="55" x2="14" y2="55" stroke="#d1d5db" stroke-width="1.5"/><line x1="14" y1="18" x2="14" y2="92" stroke="#d1d5db" stroke-width="1.5"/><line x1="14" y1="18" x2="0" y2="18" stroke="#d1d5db" stroke-width="1.5"/><line x1="14" y1="55" x2="0" y2="55" stroke="#d1d5db" stroke-width="1.5"/><line x1="14" y1="92" x2="0" y2="92" stroke="#d1d5db" stroke-width="1.5"/></svg>
</div>"""

    html = css + '<div class="pipeline">'
    html += node("_input", "Input", state_override=input_state) + arrow()
    html += node("shot_detection", "Shot Detect") + fork_join
    html += node("screenwriter", "Screenwriter") + arrow()
    html += node("editor", "Editor") + arrow()
    html += node("_output", "Output", state_override=output_state)
    html += '</div>'
    return html


# ── Live status strip ──────────────────────────────────────────────────────

def _current_stage_label() -> str:
    ss = st.session_state.stage_status
    running = [s for s in _STAGE_NAMES if ss.get(s) == "running"]
    if running:
        labels = {"shot_detection": "🎞️ Detecting shots…", "asr": "🔤 Transcribing…",
                  "video_captioning": "🎬 Analyzing scenes…", "audio_analysis": "🎵 Analyzing audio…",
                  "screenwriter": "✍️ Writing shot plan…", "editor": "✂️ Selecting clips…"}
        return ", ".join(labels.get(s, s) for s in running)
    done = [s for s in _STAGE_NAMES if ss.get(s) == "done"]
    return "⚙️ Working…" if done else "🚀 Starting pipeline…"


def _extract_error_lines(log_lines: list, tail: int = 8) -> list:
    error_keywords = ("error", "traceback", "exception", "❌", "keyerror", "attributeerror",
                      "typeerror", "valueerror", "filenotfounderror", "runtimeerror")
    err_lines, in_tb = [], False
    for line in reversed(log_lines):
        lower = line.lower()
        if any(k in lower for k in error_keywords) or in_tb:
            err_lines.append(line)
            in_tb = True
            if "traceback" in lower:
                in_tb = True
        if len(err_lines) >= tail:
            break
    return list(reversed(err_lines))


def render_live_status(placeholder, error_placeholder):
    if not st.session_state.running and not st.session_state.pipeline_failed:
        placeholder.empty(); error_placeholder.empty(); return
    elapsed = time.time() - (st.session_state.pipeline_start_time or time.time())
    mm, ss = int(elapsed // 60), int(elapsed % 60)
    stage_label = _current_stage_label()
    last_line = ""
    for line in reversed(st.session_state.log_lines):
        if line.strip():
            last_line = line.strip()[:120] + ("…" if len(line.strip()) > 120 else ""); break
    if st.session_state.pipeline_failed:
        placeholder.markdown(f'<div class="vca-card" style="border-left:4px solid #ef4444"><span style="color:#ef4444;font-weight:700">❌ Failed</span><span style="color:#94a3b8;margin-left:1rem">after {mm}m {ss}s</span></div>', unsafe_allow_html=True)
    else:
        placeholder.markdown(f'<div class="vca-card" style="border-left:4px solid #155eef"><div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:0.35rem"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#155eef;animation:gpulse 2s ease-in-out infinite"></span><span style="font-weight:600;color:#1e40af">{stage_label}</span><span style="color:#94a3b8;font-size:0.85rem">⏱ {mm}m {ss}s</span></div><div style="color:#94a3b8;font-size:0.75rem;font-family:JetBrains Mono,SF Mono,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{last_line or "Waiting…"}</div></div>', unsafe_allow_html=True)
    if st.session_state.pipeline_failed:
        err = _extract_error_lines(st.session_state.log_lines, 12) or st.session_state.log_lines[-6:]
        if err:
            _joined = "\n".join(err)
            error_placeholder.markdown(f'<div class="vca-card" style="border:1px solid rgba(239,68,68,0.4);background:rgba(239,68,68,0.04)"><div style="color:#ef4444;font-weight:700;margin-bottom:0.5rem">📋 Last error:</div><pre style="color:#1e293b;font-size:0.78rem;line-height:1.5;white-space:pre-wrap;word-break:break-all;margin:0;font-family:JetBrains Mono,SF Mono,monospace">{_joined}</pre></div>', unsafe_allow_html=True)
    else:
        error_placeholder.empty()


# ── Model card ──────────────────────────────────────────────────────────────

def _test_model_connection(model: str, endpoint: str, api_key: str) -> tuple[bool, str]:
    import litellm
    try:
        kwargs = dict(model=model, messages=[{"role": "user", "content": "Reply with just 'OK'."}], temperature=0.0, max_tokens=10, timeout=15)
        if endpoint: kwargs["api_base"] = endpoint
        if api_key: kwargs["api_key"] = api_key
        raw = litellm.completion(**kwargs)
        text = raw.choices[0].message.content or ""
        return (True, "✅ Connected") if "OK" in text else (True, f"✅ {text[:40]}")
    except Exception as e:
        return False, f"❌ {str(e)[:200]}"


def render_model_card(label, icon, model_key, endpoint_key, api_key_key, pool_names, pool_by_name, card_key):
    model_val = cfg(model_key, "")
    ep_val = cfg(endpoint_key, "")
    key_val = cfg(api_key_key, "")
    short = model_val.split("/")[-1] if "/" in model_val else model_val
    if len(short) > 28:
        short = short[:25] + "..."
    cols = st.columns([3, 1, 1])
    with cols[0]:
        st.caption(f"{icon} **{label}**: `{short or '(not set)'}` {'🟢' if model_val else '⚪'}")
    with cols[1]:
        if st.button("🔌", key=f"test_{card_key}", help=f"Test {label}"):
            ok, msg = _test_model_connection(model_val, ep_val, key_val)
            if ok: st.success(msg)
            else: st.error(msg)
    with cols[2]:
        edit = st.checkbox("Edit", key=f"edit_{card_key}", value=False)
    if edit:
        with st.container():
            nm = st.text_input("Model", value=model_val, key=f"{card_key}_model", label_visibility="collapsed", placeholder="e.g. openai/gpt-4o")
            if nm != model_val: save_config(model_key, nm)
            c1, c2 = st.columns(2)
            with c1:
                ne = st.text_input("Endpoint", value=ep_val, key=f"{card_key}_ep", label_visibility="collapsed", placeholder="https://...")
                if ne != ep_val: save_config(endpoint_key, ne)
            with c2:
                nk = st.text_input("Key", value=key_val, type="password", key=f"{card_key}_key", label_visibility="collapsed", placeholder="sk-...")
                if nk != key_val: save_config(api_key_key, nk)
            sel = st.selectbox("Pool", ["—"] + pool_names, key=f"{card_key}_pool")
            if sel != "—" and st.button("Apply from pool", key=f"apply_{card_key}"):
                e = pool_by_name[sel]
                if e.get("model"): save_config(model_key, e["model"])
                if e.get("endpoint"): save_config(endpoint_key, e["endpoint"])
                if e.get("api_key"): save_config(api_key_key, e["api_key"])
                st.rerun()

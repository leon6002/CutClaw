"""CSS styles for the CutClaw Streamlit app — white minimalist Dify-style theme."""

import streamlit as st

STYLES = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "SF Pro Display", sans-serif !important;
    color: #1e293b;
}

/* Buttons */
div.stButton > button {
    border-radius: 8px; font-weight: 500; font-size: 0.875rem;
    padding: 0.4rem 1rem; border: 1px solid #d1d5db;
    background: #fff; color: #374151;
    transition: all 0.15s ease; box-shadow: none; min-height: 2.25rem;
}
div.stButton > button:hover { border-color: #155eef; color: #155eef; background: #f0f5ff; }
div.stButton > button:first-child { background: #155eef; color: #fff !important; border: 1px solid #155eef; font-weight: 600; }
div.stButton > button:first-child:hover { background: #1d4ed8; border-color: #1d4ed8; color: #fff !important; }
div.stButton > button:disabled { opacity: 0.4; cursor: not-allowed; }

/* Inputs */
div[data-baseweb="input"] input, div[data-baseweb="textarea"] textarea {
    border-radius: 8px !important; border-color: #d1d5db !important;
    font-size: 0.875rem !important; padding: 0.5rem 0.75rem !important;
}
div[data-baseweb="input"] input:focus, div[data-baseweb="textarea"] textarea:focus {
    border-color: #155eef !important; box-shadow: 0 0 0 3px rgba(21,94,239,0.12) !important;
}

/* Cards */
.vca-card { border-radius: 12px; padding: 1rem 1.25rem; background: #fff; border: 1px solid #e5e7eb; margin-bottom: 0.75rem; }

/* Log panel */
.vca-log {
    background: #1e293b; color: #e2e8f0;
    font-family: "JetBrains Mono", "SF Mono", monospace;
    font-size: 0.8rem; line-height: 1.6; border-radius: 8px;
    padding: 1rem; height: 400px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-all; border: 1px solid #334155;
}
.vca-stage { color: #60a5fa; font-weight: 600; }
.vca-error { color: #f87171; font-weight: 600; }
.vca-success { color: #4ade80; font-weight: 500; }

/* Model card */
.model-card { border: 1px solid #e5e7eb; border-radius: 10px; padding: 0.75rem 1rem; margin-bottom: 0.75rem; background: #fafbfc; }
.model-card-header { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.model-name { font-weight: 600; font-size: 0.85rem; color: #1e293b; }
.model-tag { display: inline-block; font-size: 0.65rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 4px; background: #e0e7ff; color: #3730a3; }
.model-tag.mm { background: #fce7f3; color: #9d174d; }
.model-endpoint { font-size: 0.7rem; color: #94a3b8; margin-top: 0.15rem; }

/* Status badge */
.vca-badge { display: inline-block; border-radius: 6px; padding: 0.2rem 0.6rem; font-size: 0.72rem; font-weight: 600; }
.vca-badge-idle    { background: #f1f5f9; color: #64748b; border: 1px solid #e2e8f0; }
.vca-badge-running { background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
.vca-badge-done    { background: #ecfdf5; color: #065f46; border: 1px solid #a7f3d0; }
.vca-badge-error   { background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }
</style>
"""


def inject_styles():
    """Inject the CutClaw CSS into the Streamlit page."""
    st.set_page_config(page_title="CutClaw", page_icon="🎬", layout="wide", initial_sidebar_state="expanded")
    st.markdown(STYLES, unsafe_allow_html=True)

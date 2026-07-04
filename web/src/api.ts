import { useEffect, useRef, useState } from "react";

export const BASE = "";

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

export const mediaUrl = (p: string) => `${BASE}/api/media?path=${encodeURIComponent(p)}`;

export interface JobState {
  lines: string[];
  status: "idle" | "running" | "done" | "error";
  meta: Record<string, any>;
}

/** Poll a job's incremental log + metadata until it finishes. */
export function useJob(jobId: string | null): JobState {
  const [state, setState] = useState<JobState>({ lines: [], status: "idle", meta: {} });
  const cursor = useRef(0);

  useEffect(() => {
    cursor.current = 0;
    if (!jobId) {
      setState({ lines: [], status: "idle", meta: {} });
      return;
    }
    setState({ lines: [], status: "running", meta: {} });
    let stopped = false;
    let timer: number;

    const tick = async () => {
      try {
        const j = await api<any>(`/api/jobs/${jobId}?since=${cursor.current}`);
        if (stopped) return;
        cursor.current = j.total;
        setState((s) => ({
          lines: j.lines.length ? [...s.lines, ...j.lines] : s.lines,
          status: j.status,
          meta: j.meta,
        }));
        if (j.status === "running") timer = window.setTimeout(tick, 700);
      } catch {
        if (!stopped) timer = window.setTimeout(tick, 2000);
      }
    };
    tick();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [jobId]);

  return state;
}

export function fmtDuration(sec: number): string {
  if (!sec || sec <= 0) return "";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m > 0 ? `${m}m${s.toString().padStart(2, "0")}s` : `${s}s`;
}

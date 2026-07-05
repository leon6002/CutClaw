/** Shared trace data layer: types, polling hook, grouping, reasoning splitter. */
import { useEffect, useState, type ReactNode } from "react";
import { AudioWaveform, FileVideo, Film, Layers, PenLine, ScanText, Scissors } from "lucide-react";
import { api } from "../api";

export const TASK_LABEL: Record<string, ReactNode> = {
  video_analysis: <><FileVideo className="mr-1.5 inline h-3.5 w-3.5 text-slate-400" />逐文件分析调度</>,
  audio_analysis_asset: <><AudioWaveform className="mr-1.5 inline h-3.5 w-3.5 text-violet-400" />音频分析（素材）</>,
  audio_segments: <><AudioWaveform className="mr-1.5 inline h-3.5 w-3.5 text-violet-400" />音频片段描述</>,
  editor_shots: <><Scissors className="mr-1.5 inline h-3.5 w-3.5 text-cyan-400" />镜头选择 Agent</>,
  video_clips: <><Film className="mr-1.5 inline h-3.5 w-3.5 text-sky-400" />视频片段理解</>,
  video_dense: <><ScanText className="mr-1.5 inline h-3.5 w-3.5 text-fuchsia-400" />密集片段描述</>,
  video_scenes: <><Layers className="mr-1.5 inline h-3.5 w-3.5 text-emerald-400" />场景分析</>,
  screenwriter_llm: <><PenLine className="mr-1.5 inline h-3.5 w-3.5 text-amber-400" />AI 编剧（提示词 / 回复）</>,
};

/** File-level scheduler grids track states only — LLM call traces live in the
 *  inner per-clip/per-scene grids. Explain that instead of the checkpoint blurb. */
export function emptyTraceMsg(task: string): string {
  if (task === "video_analysis" || task === "audio_analysis_asset")
    return "这一层是文件级调度单元，只记录 开始/完成 状态，不承载模型调用轨迹——视频理解的调用细节（提示词 / 模型回复）在下方「视频片段理解 / 密集片段描述 / 场景分析」网格里，点开对应单元查看。";
  return "本轮没有执行该单元（尚未开始，或先前运行已完成、走检查点跳过），历史任务中也没有找到它的轨迹。";
}

/** "3/12" when a max exists, "#3" for unbounded call counters. */
export function fmtIter(iter?: number, maxIter?: number): string {
  if (iter === undefined) return "";
  return maxIter ? `${iter}/${maxIter}` : `#${iter}`;
}

export interface TaskInfo {
  total: number;
  states: Record<string, string>;
  labels?: Record<string, string>;
  iters?: Record<string, string>;
  done?: number;
  fail?: number;
  avg?: number;
  eta?: number;
}

export interface TraceStep {
  phase: "calling" | "action" | "result" | "round" | string;
  iter?: number;
  max_iter?: number;
  elapsed?: number;
  tool?: string;
  args?: string;
  reply?: string;
  verdict?: "ok" | "warn" | "fail" | "info" | string;
  result?: string;
  note?: string;
  /** screenwriter sub-step this call belongs to (选择音乐段落 / 生成分镜脚本 / …) */
  stage?: string;
}

export const VERDICT_META: Record<string, {
  cls: string; node: string; dot: string; banner: string; label: string;
}> = {
  ok:   { cls: "text-emerald-400", node: "border-emerald-500/60 bg-emerald-500/15 text-emerald-400", dot: "bg-emerald-400", banner: "border-emerald-500 bg-emerald-500/10", label: "通过" },
  warn: { cls: "text-amber-300",  node: "border-amber-500/60 bg-amber-500/15 text-amber-300",   dot: "bg-amber-400",  banner: "border-amber-500 bg-amber-500/10",  label: "警告" },
  fail: { cls: "text-red-400",    node: "border-red-500/60 bg-red-500/15 text-red-400",         dot: "bg-red-400",    banner: "border-red-500 bg-red-500/10",      label: "被拒/错误" },
  info: { cls: "text-slate-300",  node: "border-white/20 bg-white/[0.06] text-slate-400",       dot: "bg-slate-400",  banner: "border-white/20 bg-white/[0.04]",   label: "结果" },
};

export const STATE_TXT: Record<string, string> = { d: "已完成", r: "处理中", f: "失败", p: "等待中" };
export const STATE_DOT: Record<string, string> = {
  d: "bg-emerald-400", r: "bg-cyan-400 animate-pulse", f: "bg-red-400", p: "bg-white/15",
};

export function tryPretty(s: string): string {
  try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; }
}

export function useTrace(jobId: string, task: string, idx: number, enabled: boolean = true) {
  const [steps, setSteps] = useState<TraceStep[] | null>(null);
  const [fromPrev, setFromPrev] = useState(false);

  useEffect(() => {
    setSteps(null); setFromPrev(false);
    if (!enabled) return;
    let stop = false;
    let timer = 0;
    const tick = async () => {
      try {
        const r = await api<{ steps: TraceStep[]; from_previous_run?: boolean }>(
          `/api/jobs/${jobId}/trace?task=${encodeURIComponent(task)}&idx=${idx}`);
        if (!stop) { setSteps(r.steps); setFromPrev(!!r.from_previous_run); }
      } catch { /* ignore */ }
      if (!stop) timer = window.setTimeout(tick, 1000);
    };
    tick();
    return () => { stop = true; window.clearTimeout(timer); };
  }, [jobId, task, idx, enabled]);

  return { steps, fromPrev };
}

/** One logical iteration: the model's action + the system's responses to it. */
export interface IterEntry {
  iter?: number;
  max_iter?: number;
  elapsed?: number;
  calling?: boolean;
  action?: TraceStep;
  results: TraceStep[];
  /** round divider marker (new conversation: initial run / conflict rerun) */
  round?: { note?: string; n: number };
  /** screenwriter sub-step this iteration belongs to */
  stage?: string;
}

export function groupSteps(steps: TraceStep[]): IterEntry[] {
  const out: IterEntry[] = [];
  let roundN = 0;
  steps.forEach((s, i) => {
    if (s.phase === "round") {
      roundN += 1;
      out.push({ round: { note: s.note, n: roundN }, max_iter: s.max_iter, results: [] });
      return;
    }
    if (s.phase === "calling") {
      if (i === steps.length - 1) {
        out.push({ iter: s.iter, max_iter: s.max_iter, elapsed: s.elapsed, calling: true, results: [], stage: s.stage });
      }
      return;
    }
    if (s.phase === "action") {
      out.push({ iter: s.iter, max_iter: s.max_iter, elapsed: s.elapsed, action: s, results: [], stage: s.stage });
      return;
    }
    if (s.phase === "result") {
      const last = out[out.length - 1];
      if (last && !last.calling) last.results.push(s);
      else out.push({ iter: s.iter, max_iter: s.max_iter, results: [s] });
    }
  });
  return out;
}

export function entryWorstVerdict(e: IterEntry): string {
  if (e.results.some((r) => r.verdict === "fail")) return "fail";
  if (e.results.some((r) => r.verdict === "warn")) return "warn";
  if (e.results.some((r) => r.verdict === "ok")) return "ok";
  return "info";
}

/** Split a long reasoning blob into visually-anchored logical blocks. */
export interface ReasoningBlock { kind: "note" | "conclusion" | "plain"; text: string }

const CONCLUSION_RE = /(therefore|thus|conclusion|final(ly)?|i('| wi)ll (commit|go with|select|choose)|best (choice|option|candidate)|decided|perfect|great[,.! ]|\[shot|结论|因此|所以|最终|综上|决定|选定)/i;
const NOTE_RE = /^(let me|let's|i need|i will|i should|i want|first|next|now|then|looking|search|explor|check|analyz|review|我先|我需要|接下来|现在|首先|然后|让我)/i;

export function splitReasoning(text: string): ReasoningBlock[] {
  let paras = text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  // one giant paragraph → break by sentence groups (~3 sentences each)
  if (paras.length === 1 && paras[0].length > 700) {
    const sentences = paras[0].split(/(?<=[.!?。！？])\s+/);
    const grouped: string[] = [];
    for (let i = 0; i < sentences.length; i += 3) {
      grouped.push(sentences.slice(i, i + 3).join(" "));
    }
    paras = grouped;
  }
  return paras.map((p) => ({
    kind: CONCLUSION_RE.test(p) ? "conclusion" : NOTE_RE.test(p) ? "note" : "plain",
    text: p,
  }));
}

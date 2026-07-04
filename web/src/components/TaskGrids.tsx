import { useMemo, type ReactNode } from "react";
import { motion } from "framer-motion";
import { AudioWaveform, Film, Layers, Scissors, Zap } from "lucide-react";

const TASK_LABEL: Record<string, ReactNode> = {
  audio_segments: <><AudioWaveform className="mr-1.5 inline h-3.5 w-3.5 text-violet-400" />音频片段描述</>,
  editor_shots: <><Scissors className="mr-1.5 inline h-3.5 w-3.5 text-cyan-400" />镜头选择 Agent</>,
  video_clips: <><Film className="mr-1.5 inline h-3.5 w-3.5 text-sky-400" />视频片段理解</>,
  video_scenes: <><Layers className="mr-1.5 inline h-3.5 w-3.5 text-emerald-400" />场景分析</>,
};

interface TaskInfo {
  total: number;
  states: Record<string, string>; // idx → "r" | "d" | "f"
  labels?: Record<string, string>;
  done?: number;
  fail?: number;
  avg?: number;
  eta?: number;
}

function SegGrid({ name, t }: { name: string; t: TaskInfo }) {
  const total = t.total ?? 0;
  const states = t.states ?? {};

  const cells = useMemo(
    () => Array.from({ length: total }, (_, i) => states[String(i)] ?? "p"),
    [total, states],
  );
  const running = cells.filter((s) => s === "r").length;
  const done = t.done ?? cells.filter((s) => s === "d").length;
  const fail = t.fail ?? 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  if (total === 0) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="my-3"
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-[13px] font-semibold text-slate-200">{TASK_LABEL[name] ?? name}</span>
        <span className="text-xs text-slate-400">
          {done}/{total} 完成（{pct}%）
          {running > 0 && (
            <span className="font-semibold text-cyan-300">
              {" "}· <Zap className="inline h-3 w-3 -translate-y-px" /> {running} 并行中
            </span>
          )}
          {fail > 0 && <span className="text-red-400"> · {fail} 失败</span>}
          {t.avg ? ` · 平均 ${t.avg}s/个` : ""}
          {t.eta && running > 0 ? ` · 预计还需 ${(t.eta / 60).toFixed(1)} 分钟` : ""}
        </span>
      </div>
      <div className="flex flex-wrap gap-[3px]">
        {cells.map((s, i) => {
          const lab = t.labels?.[String(i)];
          const st = s === "d" ? "已完成" : s === "r" ? "处理中" : s === "f" ? "失败" : "等待中";
          return <div key={i} className={`seg seg-${s}`} title={`#${i + 1}${lab ? ` ${lab}` : ""} · ${st}`} />;
        })}
      </div>
    </motion.div>
  );
}

/**
 * Fine-grained execution monitor: one cell per work unit. Grey = pending,
 * pulsing cyan = in flight (parallel workers), emerald = done, red = failed.
 */
export default function TaskGrids({ tasks }: { tasks: Record<string, TaskInfo> }) {
  const entries = Object.entries(tasks ?? {});
  if (entries.length === 0) return null;
  return (
    <div className="my-1">
      {entries.map(([name, t]) => <SegGrid key={name} name={name} t={t} />)}
    </div>
  );
}

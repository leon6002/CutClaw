import { useEffect, useMemo, useState, type ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  AudioWaveform, ChevronDown, Film, Layers, Loader2, Maximize2,
  MessageSquareText, RotateCcw, Scissors, Wrench, X, Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { api } from "../api";

const TASK_LABEL: Record<string, ReactNode> = {
  audio_segments: <><AudioWaveform className="mr-1.5 inline h-3.5 w-3.5 text-violet-400" />音频片段描述</>,
  editor_shots: <><Scissors className="mr-1.5 inline h-3.5 w-3.5 text-cyan-400" />镜头选择 Agent</>,
  video_clips: <><Film className="mr-1.5 inline h-3.5 w-3.5 text-sky-400" />视频片段理解</>,
  video_scenes: <><Layers className="mr-1.5 inline h-3.5 w-3.5 text-emerald-400" />场景分析</>,
};

interface TaskInfo {
  total: number;
  states: Record<string, string>;
  labels?: Record<string, string>;
  iters?: Record<string, string>;
  done?: number;
  fail?: number;
  avg?: number;
  eta?: number;
}

interface TraceStep {
  phase: "calling" | "action" | string;
  iter?: number;
  max_iter?: number;
  elapsed?: number;
  tool?: string;
  args?: string;
  reply?: string;
}

function tryPretty(s: string): string {
  try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; }
}

const STATE_TXT: Record<string, string> = { d: "已完成", r: "处理中", f: "失败", p: "等待中" };
const STATE_DOT: Record<string, string> = {
  d: "bg-emerald-400", r: "bg-cyan-400 animate-pulse", f: "bg-red-400", p: "bg-white/15",
};

/** Live per-unit agent trace. `tall` fills the workbench; default is compact inline. */
function ShotTrace({
  jobId, task, idx, label, iters, onClose, tall = false,
}: {
  jobId: string; task: string; idx: number;
  label?: string; iters?: string; onClose?: () => void; tall?: boolean;
}) {
  const [steps, setSteps] = useState<TraceStep[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    setSteps(null); setOpen(null);
    let stop = false;
    let timer = 0;
    const tick = async () => {
      try {
        const r = await api<{ steps: TraceStep[] }>(
          `/api/jobs/${jobId}/trace?task=${encodeURIComponent(task)}&idx=${idx}`);
        if (!stop) setSteps(r.steps);
      } catch { /* ignore */ }
      if (!stop) timer = window.setTimeout(tick, 1000);
    };
    tick();
    return () => { stop = true; window.clearTimeout(timer); };
  }, [jobId, task, idx]);

  const visible = useMemo(() => {
    if (!steps) return [];
    const out: TraceStep[] = [];
    steps.forEach((s, i) => {
      if (s.phase === "calling" && i < steps.length - 1) return;
      out.push(s);
    });
    return out;
  }, [steps]);

  return (
    <div className={cn(
      "flex flex-col rounded-xl border border-cyan-500/20 bg-black/40",
      tall ? "h-full min-h-0" : "mt-2",
    )}>
      <div className="flex shrink-0 items-center gap-2 border-b border-white/[0.07] px-3 py-2">
        <span className="text-xs font-semibold text-cyan-300">
          单元 #{idx + 1}{label ? ` · ${label}` : ""}
        </span>
        {iters && <Badge variant="outline" className="h-4 border-white/15 bg-white/[0.06] px-1.5 text-[10px] text-slate-300">迭代 {iters}</Badge>}
        <span className="text-[11px] text-slate-500">{steps?.length ?? 0} 步 · 点击步骤展开完整回复</span>
        {onClose && (
          <button className="ml-auto text-slate-500 hover:text-slate-300" onClick={onClose}>
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      <ScrollArea className={tall ? "min-h-0 flex-1" : "max-h-[300px]"}>
        <div className="px-3 py-2">
          {steps === null ? (
            <div className="py-3 text-xs text-slate-500">加载轨迹…</div>
          ) : visible.length === 0 ? (
            <div className="py-3 text-xs text-slate-500">还没有步骤记录（该单元可能尚未开始或来自缓存）</div>
          ) : (
            <AnimatePresence initial={false}>
              {visible.map((s, i) => {
                const isCalling = s.phase === "calling";
                const expanded = tall || open === i;   // workbench: always expanded
                return (
                  <motion.div
                    key={i}
                    layout="position"
                    initial={{ y: 14, opacity: 0 }}
                    animate={{ y: 0, opacity: 1 }}
                    className="border-b border-white/[0.05] py-1.5 last:border-0"
                  >
                    <div
                      className={cn(
                        "flex flex-wrap items-center gap-1.5 text-xs",
                        !isCalling && !tall && "cursor-pointer hover:bg-white/[0.03]",
                      )}
                      onClick={() => !isCalling && !tall && setOpen(expanded ? null : i)}
                    >
                      <Badge variant="outline" className="h-4 border-white/15 bg-white/[0.06] px-1.5 font-mono text-[10px] text-slate-400">
                        iter {s.iter}/{s.max_iter}
                      </Badge>
                      {isCalling ? (
                        <span className="flex items-center gap-1.5 text-cyan-300">
                          <Loader2 className="h-3 w-3 animate-spin" /> 模型思考中…
                        </span>
                      ) : s.tool ? (
                        <span className="flex items-center gap-1 font-mono text-emerald-400">
                          <Wrench className="h-3 w-3" />{s.tool}
                        </span>
                      ) : (
                        <span className="flex items-center gap-1 text-slate-300">
                          <MessageSquareText className="h-3 w-3" />（无工具调用）
                        </span>
                      )}
                      {s.elapsed !== undefined && <span className="text-slate-500">+{s.elapsed}s</span>}
                      {!isCalling && !tall && (
                        <ChevronDown className={cn("h-3 w-3 text-slate-600 transition-transform", expanded && "rotate-180")} />
                      )}
                      {!isCalling && !expanded && (s.args || s.reply) && (
                        <span className="max-w-[380px] truncate text-slate-500">
                          {s.args || s.reply}
                        </span>
                      )}
                    </div>
                    {expanded && !isCalling && (
                      <div>
                        {s.args && (
                          <div className="mt-1.5">
                            <div className="mb-0.5 text-[10px] font-semibold tracking-wider text-emerald-500/80 uppercase">工具参数</div>
                            <pre className={cn("rawjson", tall ? "!max-h-none" : "!max-h-[160px]")}>{tryPretty(s.args)}</pre>
                          </div>
                        )}
                        {s.reply && (
                          <div className="mt-1.5">
                            <div className="mb-0.5 text-[10px] font-semibold tracking-wider text-sky-500/80 uppercase">模型回复</div>
                            <pre className={cn("rawjson", tall ? "!max-h-none" : "!max-h-[220px]")}>{s.reply}</pre>
                          </div>
                        )}
                        {!s.args && !s.reply && (
                          <div className="mt-1.5 text-[11px] text-slate-500">该步骤没有记录详情</div>
                        )}
                      </div>
                    )}
                  </motion.div>
                );
              })}
            </AnimatePresence>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}

/** Near-fullscreen workbench: unit list on the left, full-height trace on the right. */
function Workbench({
  name, t, jobId, onClose,
}: { name: string; t: TaskInfo; jobId: string; onClose: () => void }) {
  const total = t.total ?? 0;
  const states = t.states ?? {};
  const firstActive = useMemo(() => {
    for (let i = 0; i < total; i++) if (states[String(i)] === "r") return i;
    return 0;
  }, []);
  const [idx, setIdx] = useState(firstActive);

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="flex h-[86vh] w-[94vw] flex-col border-white/10 bg-slate-950/95 backdrop-blur-xl sm:max-w-[1200px]">
        <DialogHeader className="shrink-0">
          <DialogTitle className="text-sm">{TASK_LABEL[name] ?? name} — Agent 工作台</DialogTitle>
        </DialogHeader>
        <div className="flex min-h-0 flex-1 gap-3">
          {/* unit list */}
          <ScrollArea className="w-60 shrink-0 rounded-xl border border-white/[0.08] bg-white/[0.02]">
            <div className="p-1.5">
              {Array.from({ length: total }, (_, i) => {
                const st = states[String(i)] ?? "p";
                const lab = t.labels?.[String(i)];
                const iter = t.iters?.[String(i)];
                return (
                  <button
                    key={i}
                    onClick={() => setIdx(i)}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors",
                      idx === i ? "bg-cyan-500/15 text-cyan-300" : "text-slate-400 hover:bg-white/[0.04] hover:text-slate-200",
                    )}
                  >
                    <span className={cn("h-2 w-2 shrink-0 rounded-full", STATE_DOT[st])} />
                    <span className="shrink-0 font-mono">#{i + 1}</span>
                    <span className="truncate">{lab || STATE_TXT[st]}</span>
                    {iter && <span className="ml-auto shrink-0 font-mono text-[10px] text-slate-600">{iter}</span>}
                  </button>
                );
              })}
            </div>
          </ScrollArea>
          {/* full-height trace */}
          <div className="min-h-0 min-w-0 flex-1">
            <ShotTrace
              key={idx} tall
              jobId={jobId} task={name} idx={idx}
              label={t.labels?.[String(idx)]} iters={t.iters?.[String(idx)]}
            />
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function SegGrid({
  name, t, jobId, jobRunning, sel, onSelect, onRetryFailed,
}: {
  name: string; t: TaskInfo; jobId?: string | null; jobRunning?: boolean;
  sel: number | null; onSelect: (idx: number | null) => void;
  onRetryFailed?: () => void;
}) {
  const total = t.total ?? 0;
  const states = t.states ?? {};
  const [workbench, setWorkbench] = useState(false);

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
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="my-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <span className="flex items-center gap-2 text-[13px] font-semibold text-slate-200">
          {TASK_LABEL[name] ?? name}
          {jobId && (
            <Button variant="outline" size="sm"
              className="h-5 gap-1 border-cyan-500/25 bg-cyan-500/[0.06] px-1.5 text-[10px] text-cyan-300 hover:bg-cyan-500/15"
              onClick={() => setWorkbench(true)}>
              <Maximize2 className="h-2.5 w-2.5" /> 工作台
            </Button>
          )}
          {onRetryFailed && !jobRunning && fail > 0 && (
            <Button variant="outline" size="sm"
              className="h-5 gap-1 border-amber-500/25 bg-amber-500/[0.06] px-1.5 text-[10px] text-amber-300 hover:bg-amber-500/15"
              onClick={onRetryFailed}
              title="重跑流水线：已完成镜头走检查点跳过，只重试失败/缺失的">
              <RotateCcw className="h-2.5 w-2.5" /> 重试失败 ({fail})
            </Button>
          )}
        </span>
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
      {total <= 24 ? (
        /* few units → informative chips: what each agent is assigned to */
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 xl:grid-cols-3">
          {cells.map((s, i) => {
            const lab = t.labels?.[String(i)];
            const iter = t.iters?.[String(i)];
            return (
              <button
                key={i}
                className={cn("seg-chip", `chip-${s}`, sel === i && "chip-sel", !jobId && "cursor-default")}
                title={`#${i + 1}${lab ? ` ${lab}` : ""} · ${STATE_TXT[s]}${jobId ? "（点击查看轨迹）" : ""}`}
                onClick={jobId ? () => onSelect(sel === i ? null : i) : undefined}
              >
                <span className={cn("h-2 w-2 shrink-0 rounded-full", STATE_DOT[s])} />
                <span className="shrink-0 font-mono text-[10px] text-slate-500">#{i + 1}</span>
                <span className="min-w-0 flex-1 truncate text-left">{lab || STATE_TXT[s]}</span>
                {s === "r" && <Loader2 className="h-3 w-3 shrink-0 animate-spin text-cyan-400" />}
                {iter && <span className="shrink-0 font-mono text-[10px] text-slate-500">{iter}</span>}
              </button>
            );
          })}
        </div>
      ) : (
        /* many units → dense squares */
        <div className="flex flex-wrap gap-[3px]">
          {cells.map((s, i) => {
            const lab = t.labels?.[String(i)];
            const iter = t.iters?.[String(i)];
            return (
              <div
                key={i}
                className={cn("seg", `seg-${s}`, jobId && "cursor-pointer", sel === i && "seg-sel")}
                title={`#${i + 1}${lab ? ` ${lab}` : ""}${iter ? ` · iter ${iter}` : ""} · ${STATE_TXT[s]}${jobId ? "（点击查看轨迹）" : ""}`}
                onClick={jobId ? () => onSelect(sel === i ? null : i) : undefined}
              />
            );
          })}
        </div>
      )}
      <AnimatePresence>
        {jobId && sel !== null && (
          <motion.div
            key={`${name}-${sel}`}
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <ShotTrace
              jobId={jobId} task={name} idx={sel}
              label={t.labels?.[String(sel)]} iters={t.iters?.[String(sel)]}
              onClose={() => onSelect(null)}
            />
          </motion.div>
        )}
      </AnimatePresence>
      {workbench && jobId && (
        <Workbench name={name} t={t} jobId={jobId} onClose={() => setWorkbench(false)} />
      )}
    </motion.div>
  );
}

/**
 * Fine-grained execution monitor. Click a cell for its inline trace, or open
 * the near-fullscreen workbench (unit list + full-height expanded trace).
 */
export default function TaskGrids({
  tasks, jobId, jobRunning, onRetryFailed,
}: {
  tasks: Record<string, TaskInfo>;
  jobId?: string | null;
  jobRunning?: boolean;
  onRetryFailed?: (task: string) => void;
}) {
  const [sel, setSel] = useState<{ task: string; idx: number } | null>(null);
  const entries = Object.entries(tasks ?? {});
  if (entries.length === 0) return null;
  return (
    <div className="my-1">
      {entries.map(([name, t]) => (
        <SegGrid
          key={name} name={name} t={t} jobId={jobId} jobRunning={jobRunning}
          sel={sel?.task === name ? sel.idx : null}
          onSelect={(idx) => setSel(idx === null ? null : { task: name, idx })}
          onRetryFailed={onRetryFailed ? () => onRetryFailed(name) : undefined}
        />
      ))}
    </div>
  );
}

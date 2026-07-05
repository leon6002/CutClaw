import { useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Loader2, Maximize2, RotateCcw, X, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import {
  TASK_LABEL, STATE_DOT, STATE_TXT, VERDICT_META,
  entryWorstVerdict, fmtIter, groupSteps, useTrace, type TaskInfo,
} from "./trace";

// ── compact inline glance (click a cell) ────────────────────────────────────

function InlineTrace({
  jobId, task, idx, label, iters, onClose, onOpenWorkbench,
}: {
  jobId: string; task: string; idx: number;
  label?: string; iters?: string; onClose: () => void; onOpenWorkbench: () => void;
}) {
  const { steps, fromPrev } = useTrace(jobId, task, idx);
  const entries = useMemo(() => groupSteps(steps ?? []), [steps]);

  return (
    <div className="mt-2 rounded-xl border border-cyan-500/20 bg-black/40">
      <div className="flex flex-wrap items-center gap-2 border-b border-white/[0.07] px-3 py-2">
        <span className="text-xs font-semibold text-cyan-300">单元 #{idx + 1}{label ? ` · ${label}` : ""}</span>
        {iters && <Badge variant="outline" className="h-4 border-white/15 bg-white/[0.06] px-1.5 text-[10px] text-slate-300">迭代 {iters}</Badge>}
        {fromPrev && (
          <Badge variant="outline" className="h-4 border-amber-500/30 bg-amber-500/10 px-1.5 text-[10px] text-amber-300">
            来自先前运行
          </Badge>
        )}
        <Button variant="outline" size="sm"
          className="ml-auto h-6 gap-1 border-cyan-500/25 bg-cyan-500/[0.06] px-2 text-[10px] text-cyan-300 hover:bg-cyan-500/15"
          onClick={onOpenWorkbench}>
          <Maximize2 className="h-2.5 w-2.5" /> 工作台查看详情
        </Button>
        <button className="text-slate-500 hover:text-slate-300" onClick={onClose}>
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <ScrollArea className="max-h-[200px]">
        <div className="px-3 py-2">
          {steps === null ? (
            <div className="py-2 text-xs text-slate-500">加载轨迹…</div>
          ) : entries.length === 0 ? (
            <div className="py-2 text-xs text-slate-500">
              本轮没有执行该单元（尚未开始，或先前运行已完成、走检查点跳过），历史任务中也没有找到它的轨迹。
            </div>
          ) : (
            entries.map((e, i) => {
              if (e.round) {
                const rerun = e.round.note === "conflict_rerun";
                return (
                  <div key={i} className="flex items-center gap-2 py-1.5">
                    <div className="h-px flex-1 bg-white/10" />
                    <span className={cn("text-[9.5px] font-semibold tracking-wider uppercase", rerun ? "text-amber-400/90" : "text-slate-600")}>
                      第 {e.round.n} 轮{rerun ? " · 冲突重跑" : ""}
                    </span>
                    <div className="h-px flex-1 bg-white/10" />
                  </div>
                );
              }
              const vm = VERDICT_META[entryWorstVerdict(e)];
              return (
                <div key={i} className="flex flex-wrap items-center gap-1.5 border-b border-white/[0.05] py-1.5 text-xs last:border-0">
                  <Badge variant="outline" className="h-4 border-white/15 bg-white/[0.06] px-1.5 font-mono text-[10px] text-slate-400">
                    {fmtIter(e.iter, e.max_iter)}
                  </Badge>
                  {e.calling ? (
                    <span className="flex items-center gap-1.5 text-cyan-300">
                      <Loader2 className="h-3 w-3 animate-spin" />模型思考中…
                    </span>
                  ) : (
                    <span className="font-mono text-emerald-400">{e.action?.tool || "（无工具调用）"}</span>
                  )}
                  {e.results.map((r, j) => (
                    <span key={j} className={cn("h-2 w-2 rounded-full", (VERDICT_META[r.verdict ?? "info"] ?? VERDICT_META.info).dot)} />
                  ))}
                  {e.elapsed !== undefined && <span className="text-slate-600">+{e.elapsed}s</span>}
                  {e.results[0]?.result && (
                    <span className={cn("max-w-[320px] truncate", vm.cls)}>{e.results[0].result.split("\n")[0]}</span>
                  )}
                </div>
              );
            })
          )}
        </div>
      </ScrollArea>
    </div>
  );
}

// ── segment grid ────────────────────────────────────────────────────────────

function SegGrid({
  name, t, jobId, jobRunning, sel, onSelect, onRetryFailed, onOpenWorkbench,
}: {
  name: string; t: TaskInfo; jobId?: string | null; jobRunning?: boolean;
  sel: number | null; onSelect: (idx: number | null) => void;
  onRetryFailed?: () => void;
  onOpenWorkbench?: (idx?: number) => void;
}) {
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
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="my-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <span className="flex items-center gap-2 text-[13px] font-semibold text-slate-200">
          {TASK_LABEL[name] ?? name}
          {jobId && onOpenWorkbench && (
            <Button variant="outline" size="sm"
              className="h-5 gap-1 border-cyan-500/25 bg-cyan-500/[0.06] px-1.5 text-[10px] text-cyan-300 hover:bg-cyan-500/15"
              onClick={() => onOpenWorkbench()}>
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
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 xl:grid-cols-3">
          {cells.map((s, i) => {
            const lab = prettyLabel(t.labels?.[String(i)]);
            const iter = t.iters?.[String(i)];
            return (
              <button
                key={i}
                className={cn("seg-chip", `chip-${s}`, sel === i && "chip-sel", (!jobId || s === "p") && "cursor-default")}
                title={`#${i + 1}${lab ? ` ${lab}` : ""} · ${STATE_TXT[s]}${jobId && s !== "p" ? "（点击查看轨迹）" : ""}`}
                onClick={jobId && s !== "p" ? () => onSelect(sel === i ? null : i) : undefined}
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
        <div className="flex flex-wrap gap-[3px]">
          {cells.map((s, i) => {
            const lab = prettyLabel(t.labels?.[String(i)]);
            const iter = t.iters?.[String(i)];
            return (
              <div
                key={i}
                className={cn("seg", `seg-${s}`, jobId && s !== "p" && "cursor-pointer", sel === i && "seg-sel")}
                title={`#${i + 1}${lab ? ` ${lab}` : ""}${iter ? ` · iter ${iter}` : ""} · ${STATE_TXT[s]}${jobId && s !== "p" ? "（点击查看轨迹）" : ""}`}
                onClick={jobId && s !== "p" ? () => onSelect(sel === i ? null : i) : undefined}
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
            <InlineTrace
              jobId={jobId} task={name} idx={sel}
              label={prettyLabel(t.labels?.[String(sel)])} iters={t.iters?.[String(sel)]}
              onClose={() => onSelect(null)}
              onOpenWorkbench={() => onOpenWorkbench?.(sel)}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

/**
 * Fine-grained execution monitor. Cells open a quick inline glance; the full
 * immersive workbench is owned by the parent view (onOpenWorkbench).
 */
/** Humanize internal unit labels: "0_30_shot0_sub0" -> "0–30s", "scene_3.json" -> "场景 4" */
function prettyLabel(lab?: string): string | undefined {
  if (!lab) return lab;
  let m = /^(\d+)_(\d+)_shot\d+(?:_sub\d+)?/.exec(lab);
  if (m) return `${m[1]}–${m[2]}s`;
  m = /^scene_(\d+)\.json$/.exec(lab);
  if (m) return `场景 ${Number(m[1]) + 1}`;
  return lab;
}

export default function TaskGrids({
  tasks, jobId, jobRunning, onRetryFailed, onOpenWorkbench,
}: {
  tasks: Record<string, TaskInfo>;
  jobId?: string | null;
  jobRunning?: boolean;
  onRetryFailed?: (task: string) => void;
  onOpenWorkbench?: (task: string, idx?: number) => void;
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
          onOpenWorkbench={onOpenWorkbench ? (idx) => onOpenWorkbench(name, idx) : undefined}
        />
      ))}
    </div>
  );
}

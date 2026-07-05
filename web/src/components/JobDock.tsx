/**
 * Global job dock — fixed bottom-LEFT, above every overlay (sheets, dialogs).
 *
 * Problem it solves: task progress used to live inside whichever view started
 * the task, so triggering e.g. re-annotation from inside the asset detail
 * sheet left the progress invisible behind the sheet's backdrop. The dock is
 * mounted at App level with a z-index above all overlays: any running job
 * (annotate / select / pipeline / render) is always visible, from anywhere.
 */
import { useEffect, useRef, useState } from "react";
import {
  Check, ChevronDown, ChevronUp, Loader2, Scissors, Sparkles, Tags, Video, X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api, useJob } from "../api";

const KINDS = [
  { kind: "annotate", label: "标注", Icon: Tags },
  { kind: "select", label: "智能选材", Icon: Sparkles },
  { kind: "pipeline", label: "流水线", Icon: Scissors },
  { kind: "render", label: "渲染", Icon: Video },
] as const;

function JobChip({
  label, Icon, jobId, onDismiss,
}: {
  label: string; Icon: any; jobId: string; onDismiss: () => void;
}) {
  const job = useJob(jobId);
  const [open, setOpen] = useState(false);
  const m: any = job.meta ?? {};
  const running = job.status === "running";
  const pct = typeof m.total === "number" && m.total > 0
    ? Math.min(100, Math.round(((m.current ?? 0) / m.total) * 100))
    : null;

  // auto-dismiss successful jobs after a short linger
  useEffect(() => {
    if (job.status === "done") {
      const t = window.setTimeout(onDismiss, 12000);
      return () => window.clearTimeout(t);
    }
  }, [job.status]);

  if (job.status === "idle") return null;

  return (
    <div className={cn(
      "pointer-events-auto w-[300px] overflow-hidden rounded-xl border shadow-[0_8px_32px_rgba(0,0,0,0.55)] backdrop-blur-xl",
      running ? "border-cyan-500/40 bg-slate-950/95"
        : job.status === "done" ? "border-emerald-500/40 bg-slate-950/95"
          : "border-red-500/50 bg-slate-950/95",
    )}>
      <div className="flex items-center gap-2 px-3 py-2">
        {running
          ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-cyan-400" />
          : job.status === "done"
            ? <Check className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
            : <X className="h-3.5 w-3.5 shrink-0 text-red-400" />}
        <Icon className="h-3.5 w-3.5 shrink-0 text-slate-400" />
        <span className="text-xs font-semibold text-slate-200">{label}</span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-slate-500">
          {running
            ? (m.filename || m.stage || "运行中…")
            : job.status === "done" ? "完成" : "失败 — 展开看日志"}
        </span>
        {pct !== null && running && (
          <span className="shrink-0 font-mono text-[11px] text-cyan-300">
            {m.current ?? 0}/{m.total}
          </span>
        )}
        <button className="shrink-0 text-slate-500 hover:text-slate-300"
          onClick={() => setOpen((o) => !o)} title="展开日志">
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronUp className="h-3.5 w-3.5" />}
        </button>
        {!running && (
          <button className="shrink-0 text-slate-500 hover:text-slate-300" onClick={onDismiss} title="关闭">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      {running && (
        <div className="h-0.5 w-full bg-white/[0.06]">
          <div
            className={cn("h-full bg-cyan-400 transition-all", pct === null && "animate-pulse")}
            style={{ width: pct !== null ? `${pct}%` : "40%" }}
          />
        </div>
      )}
      {open && (
        <pre className="max-h-44 overflow-y-auto border-t border-white/[0.07] bg-black/40 px-3 py-2 font-mono text-[10.5px] leading-relaxed whitespace-pre-wrap text-slate-400">
          {job.lines.slice(-40).join("\n") || "（暂无日志）"}
        </pre>
      )}
    </div>
  );
}

export default function JobDock() {
  const [ids, setIds] = useState<Record<string, string>>({});
  const dismissed = useRef<Set<string>>(new Set());
  const [, force] = useState(0);

  useEffect(() => {
    let stop = false;
    let timer = 0;
    const tick = async () => {
      for (const k of KINDS) {
        try {
          const r = await api<any>(`/api/jobs/current/${k.kind}`);
          if (stop) return;
          const id = r.job?.id;
          if (id) {
            setIds((s) => (s[k.kind] === id ? s : { ...s, [k.kind]: id }));
          }
        } catch { /* server absent — ignore */ }
      }
      if (!stop) timer = window.setTimeout(tick, 2500);
    };
    tick();
    return () => { stop = true; window.clearTimeout(timer); };
  }, []);

  const chips = KINDS
    .map((k) => ({ ...k, jobId: ids[k.kind] }))
    .filter((k) => k.jobId && !dismissed.current.has(k.jobId));

  if (chips.length === 0) return null;

  return (
    <div className="pointer-events-none fixed bottom-6 left-6 z-[300] flex flex-col gap-2">
      {chips.map((k) => (
        <JobChip
          key={k.jobId}
          label={k.label} Icon={k.Icon} jobId={k.jobId!}
          onDismiss={() => { dismissed.current.add(k.jobId!); force((n) => n + 1); }}
        />
      ))}
    </div>
  );
}

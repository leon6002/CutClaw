/** Custom React Flow nodes for the agent workflow canvas. */
import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import {
  Check, GitMerge, Loader2, MessageSquareText, PenLine,
  RotateCcw, Scissors, TriangleAlert, X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { VERDICT_META, entryWorstVerdict, splitReasoning, tryPretty, type IterEntry } from "../trace";
import { CheckCircle2, Lightbulb } from "lucide-react";

// ── shared bits ─────────────────────────────────────────────────────────────

const H = { t: Position.Top, b: Position.Bottom, l: Position.Left, r: Position.Right };

function handleCls() {
  return "!h-2 !w-2 !border-white/30 !bg-slate-600";
}

export const STATE_RING: Record<string, string> = {
  d: "border-emerald-500/50",
  r: "border-cyan-400/70 node-breathe",
  f: "border-red-500/60",
  p: "border-white/10",
};

// ── A. Shot Root Node ───────────────────────────────────────────────────────

export interface ShotRootData {
  idx: number;
  label: string;
  state: string; // p/r/d/f
  iters?: string;
  onRetry?: () => void;   // present only when the shot can be re-generated
  [key: string]: unknown;
}

export const ShotRootNode = memo(({ data }: NodeProps) => {
  const d = data as ShotRootData;
  return (
    <div className={cn(
      "w-[230px] rounded-xl border bg-slate-900/90 shadow-lg",
      "border-l-4 border-l-cyan-400/80",
      STATE_RING[d.state] ?? STATE_RING.p,
    )}>
      <div className="flex items-center gap-2 px-3 py-2">
        <span className={cn(
          "h-2 w-2 shrink-0 rounded-full",
          d.state === "d" ? "bg-emerald-400" : d.state === "r" ? "animate-pulse bg-cyan-400"
          : d.state === "f" ? "bg-red-400" : "bg-white/20",
        )} />
        <span className="shrink-0 font-mono text-[10px] text-slate-500">#{d.idx + 1}</span>
        {d.state === "r" && <Loader2 className="h-3 w-3 shrink-0 animate-spin text-cyan-400" />}
        {d.onRetry && (
          <button
            onClick={(ev) => { ev.stopPropagation(); d.onRetry!(); }}
            title={d.state === "f" ? "重试这个失败的镜头" : "重新生成这个镜头"}
            className="ml-auto flex shrink-0 items-center gap-0.5 rounded-md border border-cyan-500/30 bg-cyan-500/10 px-1.5 py-0.5 text-[9px] text-cyan-300 hover:bg-cyan-500/20"
          >
            <RotateCcw className="h-2.5 w-2.5" />{d.state === "f" ? "重试" : "换一个"}
          </button>
        )}
        {d.iters && <span className={cn("shrink-0 font-mono text-[9px] text-slate-600", !d.onRetry && "ml-auto")}>{d.iters}</span>}
      </div>
      <div className="px-3 pb-2 text-[11px] leading-snug text-slate-300" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
        {d.label}
      </div>
      <Handle type="source" position={H.r} className={handleCls()} />
      <Handle type="target" position={H.l} className={handleCls()} />
    </div>
  );
});

// ── B. Shot Lane Node (a shot's iterations collapsed into verdict pills) ─────
// One node per shot round-segment instead of one node per iteration — cuts the
// node count ~10× so the canvas stays smooth. Click a pill to expand that
// iteration's tool args / feedback / reasoning inline.

function VIcon({ v, className }: { v: string; className?: string }) {
  const c = cn("h-3 w-3", className);
  if (v === "ok") return <Check className={c} />;
  if (v === "fail") return <X className={c} />;
  if (v === "warn") return <TriangleAlert className={c} />;
  return <MessageSquareText className={c} />;
}

/** Expanded detail for a single iteration (tool params / feedback / reasoning). */
function StepDetail({ e }: { e: IterEntry }) {
  return (
    <div className="max-h-[380px] space-y-2 overflow-y-auto border-t border-white/[0.06] px-3 py-2" onClick={(ev) => ev.stopPropagation()}>
      {e.action?.args && (
        <div>
          <div className="mb-1 text-[9px] font-semibold tracking-wider text-emerald-500/90 uppercase">Tool Parameters</div>
          <pre className="max-h-[130px] overflow-y-auto rounded-lg border border-white/[0.06] bg-black/60 p-2 font-mono text-[10px] leading-relaxed whitespace-pre-wrap text-cyan-100/90">
            {tryPretty(e.action.args)}
          </pre>
        </div>
      )}
      {e.results.map((r, i) => {
        const rvm = VERDICT_META[r.verdict ?? "info"] ?? VERDICT_META.info;
        return (
          <div key={i} className={cn("rounded-md border-l-2 px-2 py-1.5", rvm.banner)}>
            <div className={cn("mb-0.5 flex items-center gap-1 text-[10px] font-semibold", rvm.cls)}>
              <VIcon v={r.verdict ?? "info"} className="h-2.5 w-2.5" />{rvm.label}
            </div>
            <div className="max-h-[110px] overflow-y-auto text-[10.5px] leading-relaxed whitespace-pre-wrap text-slate-300">
              {r.result}
            </div>
          </div>
        );
      })}
      {e.action?.reply && (
        <div>
          <div className="mb-1 text-[9px] font-semibold tracking-wider text-sky-500/90 uppercase">Reasoning</div>
          <div className="max-h-[170px] space-y-1.5 overflow-y-auto pr-1">
            {splitReasoning(e.action.reply).map((b, j) => (
              <div key={j} className="flex gap-1.5">
                {b.kind === "note" ? <Lightbulb className="mt-0.5 h-3 w-3 shrink-0 text-amber-400" />
                  : b.kind === "conclusion" ? <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-emerald-400" />
                  : <span className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-slate-600" />}
                <p className="text-[10.5px] leading-relaxed whitespace-pre-wrap text-slate-300">{b.text}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export interface ShotLaneData {
  idx: number;
  state: string;                 // p/r/d/f
  entries: IterEntry[];          // this segment's iterations (slim)
  ordBase: number;               // global ord of entries[0]
  selectedLocal: number | null;  // which pill (local index) is expanded, if any
  fullEntry?: IterEntry;         // full detail for the selected pill
  running: boolean;
  onPill: (globalOrd: number) => void;
  [key: string]: unknown;
}

export const ShotLaneNode = memo(({ data }: NodeProps) => {
  const d = data as ShotLaneData;
  const sel = d.selectedLocal !== null ? (d.fullEntry ?? d.entries[d.selectedLocal]) : null;
  return (
    <div className={cn(
      "rounded-xl border bg-slate-900/90 shadow-lg transition-[width]",
      sel ? "w-[460px]" : "w-[250px]",
      "border-l-4 border-l-cyan-400/80",
      STATE_RING[d.state] ?? STATE_RING.p,
    )}>
      <div className="flex items-center gap-2 px-3 pt-2 pb-1">
        <span className={cn("h-2 w-2 shrink-0 rounded-full",
          d.state === "d" ? "bg-emerald-400" : d.state === "r" ? "animate-pulse bg-cyan-400"
          : d.state === "f" ? "bg-red-400" : "bg-white/20")} />
        <span className="shrink-0 font-mono text-[10px] text-slate-500">#{d.idx + 1}</span>
        <span className="text-[10px] text-slate-600">{d.entries.length} 次迭代</span>
        {d.running && <Loader2 className="ml-auto h-3 w-3 shrink-0 animate-spin text-cyan-400" />}
      </div>
      {/* iteration pills */}
      <div className="flex flex-wrap gap-1 px-3 pb-2">
        {d.entries.map((e, k) => {
          if (e.calling) {
            return (
              <span key={k} title={`Iter #${e.iter} 思考中`}
                className="flex h-5 items-center gap-0.5 rounded-md border border-cyan-400/60 bg-cyan-500/15 px-1 font-mono text-[9px] text-cyan-300 node-breathe">
                <Loader2 className="h-2.5 w-2.5 animate-spin" />{e.iter}
              </span>
            );
          }
          const vm = VERDICT_META[entryWorstVerdict(e)] ?? VERDICT_META.info;
          const isSel = d.selectedLocal === k;
          return (
            <button key={k} title={`Iter #${e.iter} · ${e.action?.tool || "无工具"} · ${vm.label}`}
              onClick={(ev) => { ev.stopPropagation(); d.onPill(d.ordBase + k); }}
              className={cn(
                "flex h-5 min-w-[20px] items-center justify-center rounded-md border px-1 font-mono text-[9px] transition-transform hover:scale-110",
                vm.node, isSel && "ring-2 ring-white/70",
              )}>
              {e.iter}
            </button>
          );
        })}
      </div>
      {sel && <StepDetail e={sel} />}
      {d.selectedLocal !== null && !d.fullEntry && (
        <div className="px-3 pb-2 text-[10px] text-slate-500">加载完整内容…</div>
      )}
      <Handle type="target" position={H.l} className={handleCls()} />
      <Handle type="source" position={H.r} className={handleCls()} />
    </div>
  );
});

// ── C. Screenwriter Node ────────────────────────────────────────────────────

export interface ScreenwriterData {
  calls: number;
  state: string;
  running: boolean;
  onOpen?: () => void;
  [key: string]: unknown;
}

export const ScreenwriterNode = memo(({ data }: NodeProps) => {
  const d = data as ScreenwriterData;
  return (
    <div
      className={cn(
        "w-[210px] cursor-pointer rounded-xl border bg-slate-900/90 px-3 py-2.5 shadow-lg",
        d.running ? "border-amber-400/70 node-breathe" : d.state === "f" ? "border-red-500/50" : "border-amber-500/40",
      )}
      onClick={(ev) => { ev.stopPropagation(); d.onOpen?.(); }}
      title="点击查看编剧的提示词与回复"
    >
      <div className="flex items-center gap-1.5 text-[12px] font-semibold text-amber-300">
        <PenLine className="h-3.5 w-3.5" />AI 编剧
        {d.running && <Loader2 className="ml-auto h-3 w-3 animate-spin" />}
      </div>
      <div className="mt-0.5 text-[10px] text-slate-500">{d.calls} 次 LLM 调用 · 点击查看提示词/回复</div>
      <Handle type="source" position={H.r} className={handleCls()} />
    </div>
  );
});

// ── D. Orchestrator Node (conflict check / merge) ───────────────────────────

export interface OrchestratorData {
  kind: "conflict" | "merge" | "editor";
  title: string;
  detail?: string;
  state: string; // p/r/d/f
  [key: string]: unknown;
}

export const OrchestratorNode = memo(({ data }: NodeProps) => {
  const d = data as OrchestratorData;
  const isEditor = d.kind === "editor";
  const Icon = isEditor ? Scissors : GitMerge;
  return (
    <div className={cn(
      "w-[190px] rounded-xl border bg-slate-900/95 px-3 py-2.5 shadow-lg",
      isEditor
        ? (d.state === "r" ? "border-cyan-400/70 node-breathe" : "border-cyan-500/50")
        : (d.state === "r" ? "border-violet-400/70 node-breathe" : "border-violet-500/40"),
    )}>
      <div className={cn("flex items-center gap-1.5 text-[11.5px] font-semibold",
        isEditor ? "text-cyan-300" : "text-violet-300")}>
        <Icon className="h-3.5 w-3.5" />{d.title}
      </div>
      {d.detail && <div className="mt-0.5 text-[10px] text-slate-500">{d.detail}</div>}
      <Handle type="target" position={H.l} className={handleCls()} />
      <Handle type="source" position={H.r} className={handleCls()} />
    </div>
  );
});

export const nodeTypes = {
  shotRoot: ShotRootNode,
  shotLane: ShotLaneNode,
  screenwriter: ScreenwriterNode,
  orchestrator: OrchestratorNode,
};

import { Fragment, type ReactNode } from "react";
import { motion } from "framer-motion";
import { Check, Loader2, Minus, X } from "lucide-react";
import { cn } from "@/lib/utils";

export interface FlowStep {
  key: string;
  label: string;
  icon: ReactNode;
}

export interface StageInfo {
  status: "pending" | "running" | "done" | "error" | "skip" | "next" | string;
  detail?: string;
}

function stageOf(stages: Record<string, any>, key: string): StageInfo {
  const v = stages?.[key];
  if (!v) return { status: "pending" };
  if (typeof v === "string") return { status: v };
  return { status: v.status ?? "pending", detail: v.detail };
}

const DOT_STYLE: Record<string, string> = {
  pending: "border-white/10 text-slate-600 bg-white/[0.02]",
  running: "border-cyan-400/80 text-cyan-300 bg-cyan-500/10",
  done: "border-emerald-500/60 text-emerald-400 bg-emerald-500/10",
  error: "border-red-500/70 text-red-400 bg-red-500/10",
  skip: "border-white/10 text-slate-600 bg-white/[0.02] opacity-40",
  next: "border-dashed border-cyan-400/70 text-cyan-300 bg-cyan-500/5",
};

function Dot({ status, icon }: { status: string; icon: ReactNode }) {
  const inner =
    status === "running" ? <Loader2 className="h-4 w-4 animate-spin" /> :
    status === "done" ? <Check className="h-4 w-4" /> :
    status === "error" ? <X className="h-4 w-4" /> :
    status === "skip" ? <Minus className="h-4 w-4" /> :
    icon;

  return (
    <motion.div
      className={cn(
        "mx-auto flex h-11 w-11 items-center justify-center rounded-full border-2 transition-colors duration-300",
        DOT_STYLE[status] ?? DOT_STYLE.pending,
      )}
      animate={
        status === "running" ? {
          boxShadow: [
            "0 0 0px rgba(34,211,238,0)",
            "0 0 22px rgba(34,211,238,0.55)",
            "0 0 0px rgba(34,211,238,0)",
          ],
        } : status === "next" ? {
          boxShadow: [
            "0 0 0px rgba(34,211,238,0)",
            "0 0 14px rgba(34,211,238,0.35)",
            "0 0 0px rgba(34,211,238,0)",
          ],
        } : status === "done" ? { scale: [0.7, 1.08, 1] } : {}
      }
      transition={
        status === "running" || status === "next"
          ? { duration: 1.8, repeat: Infinity, ease: "easeInOut" }
          : { duration: 0.35 }
      }
    >
      {inner}
    </motion.div>
  );
}

/** Connector with a flowing light-particle when data streams toward the next node. */
function Connector({ state }: { state: string }) {
  return (
    <div className="relative mt-[21px] h-px min-w-7 flex-1 overflow-visible">
      <div className={cn(
        "absolute inset-0 rounded",
        state === "done" ? "bg-emerald-500/35"
        : state === "error" ? "bg-red-500/35"
        : "bg-white/10",
      )} />
      {state === "active" && (
        <>
          <div className="absolute inset-0 rounded bg-cyan-500/15" />
          <motion.div
            className="absolute top-1/2 h-[3px] w-10 -translate-y-1/2 rounded-full bg-gradient-to-r from-transparent via-cyan-300 to-transparent shadow-[0_0_10px_#22d3ee]"
            animate={{ left: ["-2.5rem", "100%"] }}
            transition={{ duration: 1.1, repeat: Infinity, ease: "linear" }}
          />
        </>
      )}
    </div>
  );
}

/**
 * Animated left-to-right agent flow. Running nodes breathe with a neon glow,
 * connectors stream light particles toward the active node, "next" nodes get
 * a dashed accent ring marking what the user should do next.
 */
export default function AgentFlow({
  steps, stages, onStepClick,
}: {
  steps: FlowStep[];
  stages: Record<string, any>;
  onStepClick?: (key: string) => void;
}) {
  return (
    <div className="flex items-start overflow-x-auto px-0.5 py-1">
      {steps.map((s, i) => {
        const st = stageOf(stages, s.key);
        const prev = i > 0 ? stageOf(stages, steps[i - 1].key) : null;
        const prevDone = prev && (prev.status === "done" || prev.status === "skip");
        const connState =
          st.status === "running" || st.status === "next" ? "active"
          : st.status === "done" || st.status === "skip" ? "done"
          : st.status === "error" ? "error"
          : prevDone ? "done"
          : "";
        return (
          <Fragment key={s.key}>
            {i > 0 && <Connector state={connState} />}
            <div
              className={cn(
                "w-[118px] shrink-0 text-center",
                onStepClick && "cursor-pointer rounded-xl px-0.5 py-1 hover:bg-white/[0.04]",
                st.status === "skip" && "opacity-45",
              )}
              onClick={onStepClick ? () => onStepClick(s.key) : undefined}
            >
              <Dot status={st.status} icon={s.icon} />
              <div className={cn(
                "mt-1.5 text-xs font-semibold",
                st.status === "pending" ? "text-slate-500"
                : st.status === "next" || st.status === "running" ? "text-cyan-300"
                : st.status === "error" ? "text-red-400"
                : "text-slate-200",
              )}>
                {s.label}
                {st.status === "next" && (
                  <span className="ml-1 rounded-full bg-cyan-500/15 px-1.5 py-px align-[1px] text-[10px] font-bold text-cyan-300">
                    下一步
                  </span>
                )}
              </div>
              <div
                className={cn(
                  "mx-auto mt-0.5 line-clamp-2 min-h-[15px] max-w-[116px] text-[11px] leading-snug",
                  st.status === "error" ? "text-red-400/90" : "text-slate-400",
                )}
                title={st.detail}
              >
                {st.detail || " "}
              </div>
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}

import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { GripHorizontal, Minus, TerminalSquare, X } from "lucide-react";
import { cn } from "@/lib/utils";
import JobLog from "./JobLog";

/**
 * Draggable floating terminal for the pipeline log.
 * Collapsed: a pill button bottom-right (pulses while running).
 * Open: a draggable window; drag by the header grip.
 */
export default function FloatingTerminal({
  lines, running, visible,
}: { lines: string[]; running: boolean; visible: boolean }) {
  const [open, setOpen] = useState(false);

  if (!visible) return null;

  return (
    <>
      <AnimatePresence>
        {open && (
          <motion.div
            key="term-window"
            drag dragMomentum={false}
            initial={{ opacity: 0, y: 24, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 24, scale: 0.96 }}
            className="fixed right-6 bottom-6 z-50 w-[620px] max-w-[92vw] overflow-hidden rounded-xl border border-white/15 bg-slate-950/95 shadow-[0_12px_48px_rgba(0,0,0,0.6)] backdrop-blur-xl"
          >
            <div className="flex cursor-grab items-center gap-2 border-b border-white/10 bg-white/[0.04] px-3 py-2 active:cursor-grabbing">
              <GripHorizontal className="h-3.5 w-3.5 text-slate-600" />
              <TerminalSquare className="h-3.5 w-3.5 text-cyan-400" />
              <span className="font-mono text-[11px] text-slate-400">pipeline — 流水线终端</span>
              {running && (
                <span className="flex items-center gap-1 text-[10px] font-semibold text-cyan-300">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-400" />运行中
                </span>
              )}
              <span className="ml-auto font-mono text-[10px] text-slate-600">{lines.length} lines</span>
              <button className="text-slate-500 hover:text-slate-300" onClick={() => setOpen(false)} title="最小化">
                <Minus className="h-3.5 w-3.5" />
              </button>
            </div>
            {/* -mt-3 cancels JobLog's own top margin */}
            <div className="-mt-3 px-2 pb-2">
              <JobLog lines={lines} height={360} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {!open && (
          <motion.button
            key="term-pill"
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 16 }}
            onClick={() => setOpen(true)}
            className={cn(
              "fixed right-6 bottom-6 z-50 flex items-center gap-2 rounded-full border px-4 py-2.5 text-xs font-semibold shadow-lg backdrop-blur-xl transition-colors",
              running
                ? "border-cyan-500/40 bg-cyan-500/15 text-cyan-300 shadow-[0_0_18px_rgba(34,211,238,0.25)]"
                : "border-white/15 bg-slate-900/85 text-slate-300 hover:border-white/30",
            )}
          >
            <TerminalSquare className="h-4 w-4" />
            流水线终端
            {running && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-400" />}
          </motion.button>
        )}
      </AnimatePresence>
    </>
  );
}

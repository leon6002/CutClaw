import { useEffect, useRef } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { TriangleAlert } from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const STAGE_KW = ["[Thread A]", "[Thread B]", "[Thread C]", "Shot detection", "ASR", "Captioning",
  "Scene", "Audio", "Screenwriter", "Editor", "Processing", "[stage]", "[file]", "[Parallel]"];

type Kind = "error" | "warn" | "ok" | "stage" | "plain";

function classify(line: string): Kind {
  const lower = line.toLowerCase();
  if (lower.includes("traceback") || lower.includes("exception") || line.includes("❌") ||
      (lower.includes("error") && !lower.includes("0 error"))) return "error";
  if (lower.includes("warn") || lower.includes("duplicate") || lower.includes("retry") || line.includes("⚠")) return "warn";
  if (lower.includes("complete") || lower.includes("done") || lower.includes("success") ||
      lower.includes("finished") || line.includes("✅") || line.includes("✓") || line.includes("♻")) return "ok";
  if (STAGE_KW.some((k) => line.includes(k))) return "stage";
  return "plain";
}

const KIND_CLS: Record<Kind, string> = {
  error: "text-red-400",
  warn: "text-amber-300",
  ok: "glow-green",
  stage: "glow-cyan",
  plain: "text-slate-300",
};

/** Split out [S1-Shot2]-style tags so they render as translucent badges. */
function LineContent({ line }: { line: string }) {
  const kind = classify(line);
  const parts = line.split(/(\[[^\[\]\n]{1,28}\])/g);
  return (
    <span className={cn("break-all whitespace-pre-wrap", KIND_CLS[kind])}>
      {kind === "warn" && (
        <TriangleAlert className="warn-flash mr-1 inline h-3 w-3 -translate-y-px text-amber-400" />
      )}
      {parts.map((p, i) =>
        p.startsWith("[") && p.endsWith("]") ? (
          <Badge
            key={i}
            variant="outline"
            className="mx-0.5 h-4 border-white/15 bg-white/[0.06] px-1.5 py-0 align-[1px] font-mono text-[10px] font-medium text-slate-300"
          >
            {p.slice(1, -1)}
          </Badge>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </span>
  );
}

/** Pseudo-terminal: monospace, syntax-highlighted agent log with slide-in lines. */
export default function JobLog({ lines, height = 320 }: { lines: string[]; height?: number }) {
  const areaRef = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  const shown = lines.slice(-250);
  const startIdx = lines.length - shown.length;

  useEffect(() => {
    const viewport = areaRef.current?.querySelector<HTMLElement>("[data-radix-scroll-area-viewport]");
    if (viewport && pinned.current) viewport.scrollTop = viewport.scrollHeight;
  }, [lines.length]);

  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-white/10 bg-black/70 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
      {/* terminal chrome */}
      <div className="flex items-center gap-1.5 border-b border-white/10 bg-white/[0.03] px-3 py-2">
        <span className="h-2.5 w-2.5 rounded-full bg-red-500/70" />
        <span className="h-2.5 w-2.5 rounded-full bg-amber-400/70" />
        <span className="h-2.5 w-2.5 rounded-full bg-emerald-500/70" />
        <span className="ml-2 font-mono text-[11px] text-slate-500">
          agent@cutclaw — pipeline.log
        </span>
        <span className="ml-auto font-mono text-[10px] text-slate-600">{lines.length} lines</span>
      </div>

      <ScrollArea
        ref={areaRef} style={{ height }}
        onScrollCapture={(e) => {
          const el = (e.target as HTMLElement);
          if (!el?.scrollHeight) return;
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
        }}
      >
        <div className="px-3 py-2 font-mono text-xs">
          {shown.length === 0 ? (
            <div className="text-slate-600">
              <span className="text-emerald-500">❯</span> waiting for agent output
              <span className="term-caret" />
            </div>
          ) : (
            <AnimatePresence initial={false}>
              {shown.map((l, i) => (
                <motion.div
                  key={startIdx + i}
                  layout="position"
                  initial={{ y: 20, opacity: 0 }}
                  animate={{ y: 0, opacity: 1 }}
                  transition={{ duration: 0.22, ease: "easeOut" }}
                  className="term-line"
                >
                  <span className="mr-1.5 select-none text-slate-700">❯</span>
                  <LineContent line={l} />
                </motion.div>
              ))}
            </AnimatePresence>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}

/**
 * Immersive in-page Agent workbench (replaces the old cramped dialog).
 * Left: Shot Navigator. Right: Agent Thought Stream — a vertical timeline where
 * finished iterations collapse to one line and the focused one expands with a
 * cyan glow, structured into Header / Tool Parameters / Feedback / Reasoning.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Check, CheckCircle2, ChevronDown, ChevronRight, ChevronUp, Code2, Lightbulb,
  Loader2, MessageSquareText, TriangleAlert, Wrench, X,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  Collapsible, CollapsibleContent, CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import {
  TASK_LABEL, STATE_DOT, STATE_TXT, VERDICT_META,
  entryWorstVerdict, fmtIter, groupSteps, splitReasoning, tryPretty, useTrace,
  type IterEntry, type TaskInfo,
} from "./trace";

// ── structured feedback renderer ────────────────────────────────────────────

const TIME_TOKEN_RE = /(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*(?:to|→|~|–)\s*\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)?)/g;

/** Inline text with time ranges rendered as cyan mono badges. */
function InlineRich({ s }: { s: string }) {
  const parts = s.split(TIME_TOKEN_RE);
  return (
    <>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <Badge key={i} variant="outline"
            className="mx-0.5 h-[17px] border-cyan-500/30 bg-cyan-500/10 px-1.5 align-[-1px] font-mono text-[10.5px] font-medium text-cyan-300">
            {p}
          </Badge>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

// ── recursive JSON field tree ───────────────────────────────────────────────

const isPrim = (v: any) => v === null || ["string", "number", "boolean"].includes(typeof v);

function JsonValue({ v }: { v: any }) {
  if (v === null || v === undefined) return <span className="font-mono text-[11px] text-slate-600">null</span>;
  if (typeof v === "number") return <span className="font-mono text-[12px] text-amber-300">{String(v)}</span>;
  if (typeof v === "boolean") return <span className="font-mono text-[12px] text-violet-300">{String(v)}</span>;
  return <span className="text-[12px] text-slate-200"><InlineRich s={String(v)} /></span>;
}

function JsonNode({ k, v, depth }: { k?: string; v: any; depth: number }) {
  const [open, setOpen] = useState(depth < 2);
  const keyEl = k !== undefined ? (
    <span className="shrink-0 font-mono text-[11px] font-medium text-sky-300/85">{k}</span>
  ) : null;

  if (isPrim(v)) {
    return (
      <div className="flex flex-wrap items-baseline gap-1.5 py-[3px]">
        {keyEl}{k !== undefined && <span className="text-slate-600">:</span>}
        <span className="min-w-0 leading-relaxed break-words"><JsonValue v={v} /></span>
      </div>
    );
  }

  const isArr = Array.isArray(v);
  const entries: [string, any][] = isArr
    ? (v as any[]).map((x, i) => [String(i), x])
    : Object.entries(v ?? {});

  // short primitive arrays → inline chips
  if (isArr && entries.length <= 8 && (v as any[]).every(isPrim)) {
    return (
      <div className="flex flex-wrap items-center gap-1 py-[3px]">
        {keyEl}{k !== undefined && <span className="text-slate-600">:</span>}
        {(v as any[]).map((x, i) => (
          <span key={i} className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[11px] text-slate-300">
            {String(x)}
          </span>
        ))}
        {entries.length === 0 && <span className="font-mono text-[11px] text-slate-600">[]</span>}
      </div>
    );
  }

  return (
    <div className="py-[3px]">
      <button onClick={() => setOpen(!open)} className="flex items-center gap-1 text-left">
        <ChevronRight className={cn("h-3 w-3 shrink-0 text-slate-500 transition-transform", open && "rotate-90")} />
        {keyEl}
        <span className="font-mono text-[10px] text-slate-600">
          {isArr ? `[${entries.length}]` : `{${entries.length}}`}
        </span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="ml-[5px] overflow-hidden border-l border-white/[0.07] pl-3"
          >
            {entries.map(([ck, cv]) => <JsonNode key={ck} k={ck} v={cv} depth={depth + 1} />)}
            {entries.length === 0 && <span className="font-mono text-[11px] text-slate-600">（空）</span>}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

type FBlock =
  | { type: "header" | "bullet" | "text"; text: string }
  | { type: "json"; text: string; value: any };

function parseFeedback(text: string): FBlock[] {
  const blocks: FBlock[] = [];
  const lines = text.split("\n");
  let jsonBuf: string[] | null = null;
  let depth = 0;

  const flushJson = () => {
    if (!jsonBuf) return;
    const raw = jsonBuf.join("\n");
    try {
      const value = JSON.parse(raw);
      blocks.push({ type: "json", text: JSON.stringify(value, null, 2), value });
    } catch {
      raw.split("\n").forEach((l) => l.trim() && blocks.push({ type: "text", text: l }));
    }
    jsonBuf = null; depth = 0;
  };

  for (const line of lines) {
    const tr = line.trim();
    if (jsonBuf) {
      jsonBuf.push(line);
      depth += (tr.match(/[{[]/g) ?? []).length - (tr.match(/[}\]]/g) ?? []).length;
      if (depth <= 0) flushJson();
      continue;
    }
    if (!tr) continue;
    if (/^[{[]/.test(tr)) {
      depth = (tr.match(/[{[]/g) ?? []).length - (tr.match(/[}\]]/g) ?? []).length;
      if (depth <= 0) {
        try {
          const value = JSON.parse(tr);
          blocks.push({ type: "json", text: JSON.stringify(value, null, 2), value });
          continue;
        } catch { blocks.push({ type: "text", text: tr }); continue; }
      }
      jsonBuf = [line];
      continue;
    }
    const clean = tr.replace(/^[✅❌⚠️♻️]+\s*/, "");
    if (/^[-•*]\s+/.test(clean) || /^\d+[.)]\s+/.test(clean)) {
      blocks.push({ type: "bullet", text: clean.replace(/^[-•*]\s+/, "").replace(/^\d+[.)]\s+/, "") });
    } else if (
      (/^(here are|available shots|scene \d|shot \d|clip \d|review (passed|failed)|suggestions|note[:：]|warning|error)/i.test(clean)
        || (clean.endsWith(":") && clean.length < 80))
    ) {
      blocks.push({ type: "header", text: clean });
    } else {
      blocks.push({ type: "text", text: clean });
    }
  }
  flushJson();
  return blocks;
}

/** Graphical rendering of a system-feedback payload; raw content collapsible. */
function FeedbackView({ text }: { text: string }) {
  const blocks = useMemo(() => parseFeedback(text), [text]);
  const jsonCount = blocks.filter((b) => b.type === "json").length;

  return (
    <div>
      <div className="space-y-1.5">
        {blocks.map((b, i) => {
          if (b.type === "json") {
            return (
              <div key={i} className="rounded-lg border border-white/[0.06] bg-black/40 px-3 py-2">
                {/* field-by-field tree, expanded to 2 levels by default */}
                <JsonNode v={b.value} depth={0} />
                <Collapsible>
                  <CollapsibleTrigger className="group mt-1 flex items-center gap-1.5 text-[10.5px] text-slate-500 hover:text-slate-300">
                    <ChevronRight className="h-3 w-3 transition-transform group-data-[state=open]:rotate-90" />
                    <Code2 className="h-3 w-3" />
                    原始 JSON
                  </CollapsibleTrigger>
                  <CollapsibleContent>
                    <pre className="mt-1 rounded-lg border border-white/[0.06] bg-black/60 p-2.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-slate-400">
                      {b.text}
                    </pre>
                  </CollapsibleContent>
                </Collapsible>
              </div>
            );
          }
          if (b.type === "header") {
            return (
              <div key={i} className="pt-1 text-[12.5px] font-semibold text-slate-200">
                <InlineRich s={b.text} />
              </div>
            );
          }
          if (b.type === "bullet") {
            return (
              <div key={i} className="flex gap-2 pl-1">
                <ChevronRight className="mt-[3px] h-3 w-3 shrink-0 text-slate-500" />
                <span className="text-[12.5px] leading-relaxed text-slate-300">
                  <InlineRich s={b.text} />
                </span>
              </div>
            );
          }
          return (
            <p key={i} className="text-[12.5px] leading-relaxed text-slate-300">
              <InlineRich s={b.text} />
            </p>
          );
        })}
      </div>

      {/* full raw payload, collapsed by default */}
      <Collapsible>
        <CollapsibleTrigger className="group mt-2 flex items-center gap-1.5 text-[10.5px] text-slate-500 hover:text-slate-300">
          <ChevronRight className="h-3 w-3 transition-transform group-data-[state=open]:rotate-90" />
          查看原始内容{jsonCount > 0 ? `（含 ${jsonCount} 个 JSON 块）` : ""}
        </CollapsibleTrigger>
        <CollapsibleContent>
          <pre className="mt-1 rounded-lg border border-white/[0.06] bg-black/60 p-2.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-slate-400">
            {text}
          </pre>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}

// ── expanded node: structured content ───────────────────────────────────────

function VerdictIcon({ v, className }: { v: string; className?: string }) {
  const c = cn("h-3.5 w-3.5", className);
  if (v === "ok") return <Check className={c} />;
  if (v === "fail") return <X className={c} />;
  if (v === "warn") return <TriangleAlert className={c} />;
  return <MessageSquareText className={c} />;
}

function NodeCard({ e, onCollapse }: { e: IterEntry; onCollapse: () => void }) {
  const a = e.action;
  const blocks = useMemo(() => (a?.reply ? splitReasoning(a.reply) : []), [a?.reply]);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-xl border border-cyan-500/40 bg-slate-900/60 shadow-[0_0_28px_rgba(34,211,238,0.10)]"
    >
      {/* Header 区 */}
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <Badge variant="outline" className="border-white/15 bg-white/[0.06] font-mono text-[11px] text-slate-300">
          Iter {fmtIter(e.iter, e.max_iter)}
        </Badge>
        {a?.tool ? (
          <span className="flex items-center gap-1.5 font-mono text-[13px] font-semibold text-cyan-300">
            <Wrench className="h-3.5 w-3.5" />{a.tool}
          </span>
        ) : (
          <span className="text-[13px] text-slate-400">（无工具调用）</span>
        )}
        {e.elapsed !== undefined && <span className="text-xs text-slate-500">+{e.elapsed}s</span>}
        <button className="ml-auto text-slate-500 hover:text-slate-300" onClick={onCollapse} title="折叠">
          <ChevronUp className="h-4 w-4" />
        </button>
      </div>
      <Separator className="bg-white/[0.06]" />

      <div className="space-y-3 px-4 py-3">
        {/* 参数区：字段树优先，原始文本兜底 */}
        {a?.args && (
          <Collapsible defaultOpen>
            <CollapsibleTrigger className="group flex items-center gap-1.5 text-[11px] font-semibold tracking-wider text-emerald-500/90 uppercase">
              <ChevronDown className="h-3 w-3 transition-transform group-data-[state=closed]:-rotate-90" />
              Tool Parameters
            </CollapsibleTrigger>
            <CollapsibleContent>
              {(() => {
                try {
                  const parsed = JSON.parse(a.args!);
                  return (
                    <div className="mt-1.5 rounded-lg border border-white/[0.06] bg-black/40 px-3 py-2">
                      <JsonNode v={parsed} depth={0} />
                    </div>
                  );
                } catch {
                  return (
                    <pre className="mt-1.5 rounded-lg border border-white/[0.06] bg-black/60 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-slate-300">
                      {tryPretty(a.args!)}
                    </pre>
                  );
                }
              })()}
            </CollapsibleContent>
          </Collapsible>
        )}

        {/* 系统反馈横幅 */}
        {e.results.map((r, i) => {
          const vm = VERDICT_META[r.verdict ?? "info"] ?? VERDICT_META.info;
          return (
            <div key={i} className={cn("rounded-lg border-l-4 px-3.5 py-2.5", vm.banner)}>
              <div className={cn("mb-1.5 flex items-center gap-1.5 text-xs font-semibold", vm.cls)}>
                <VerdictIcon v={r.verdict ?? "info"} />系统反馈 · {vm.label}
              </div>
              {r.result ? <FeedbackView text={r.result} /> : (
                <div className="text-[12.5px] text-slate-500">（无内容）</div>
              )}
            </div>
          );
        })}

        {/* 思考过程区 — 切块 + 视觉锚点 */}
        {blocks.length > 0 && (
          <div>
            <div className="mb-2 text-[11px] font-semibold tracking-wider text-sky-500/90 uppercase">Reasoning</div>
            <div className="space-y-2.5">
              {blocks.map((b, j) => (
                <div key={j} className="flex gap-2.5">
                  {b.kind === "note" ? (
                    <Lightbulb className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400" />
                  ) : b.kind === "conclusion" ? (
                    <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-400" />
                  ) : (
                    <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-slate-600" />
                  )}
                  <p className={cn(
                    "text-[12.5px] leading-relaxed whitespace-pre-wrap",
                    b.kind === "conclusion" ? "text-slate-200" : "text-slate-300",
                  )}>
                    {b.text}
                  </p>
                </div>
              ))}
            </div>
          </div>
        )}

        {!a?.args && !a?.reply && e.results.length === 0 && (
          <div className="text-xs text-slate-500">该步骤没有记录详情</div>
        )}
      </div>
    </motion.div>
  );
}

// ── the workbench ───────────────────────────────────────────────────────────

export default function AgentWorkbench({
  name, t, jobId, initialIdx, onClose, embedded = false,
}: {
  name: string; t: TaskInfo; jobId: string;
  initialIdx?: number; onClose: () => void;
  /** render as an in-canvas overlay panel (fills parent, opaque bg) */
  embedded?: boolean;
}) {
  const total = t.total ?? 0;
  const states = t.states ?? {};
  const firstActive = useMemo(() => {
    if (initialIdx !== undefined) return initialIdx;
    for (let i = 0; i < total; i++) if (states[String(i)] === "r") return i;
    return 0;
  }, []);
  const [idx, setIdx] = useState(firstActive);
  const { steps, fromPrev } = useTrace(jobId, name, idx);
  const entries = useMemo(() => groupSteps(steps ?? []), [steps]);
  const [openNode, setOpenNode] = useState<number | null>(null);
  const follow = useRef(true);

  // auto-expand the newest non-divider node until the user picks an older one
  useEffect(() => {
    if (follow.current && entries.length > 0) {
      for (let i = entries.length - 1; i >= 0; i--) {
        if (!entries[i].round) { setOpenNode(i); return; }
      }
    }
  }, [entries.length]);
  useEffect(() => { follow.current = true; setOpenNode(null); }, [idx]);

  return (
    <motion.div
      layout={!embedded}
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "overflow-hidden rounded-2xl border border-cyan-500/25",
        embedded
          ? "flex h-full w-full flex-col bg-slate-950/95 shadow-[0_8px_40px_rgba(0,0,0,0.6)]"
          : "mt-4 bg-slate-950/70",
      )}
    >
      {/* title bar */}
      <div className="flex items-center gap-2 border-b border-white/[0.07] bg-white/[0.03] px-4 py-2.5">
        <span className="text-[13px] font-semibold text-slate-200">{TASK_LABEL[name] ?? name} — Agent 工作台</span>
        {fromPrev && (
          <Badge variant="outline" className="h-4 border-amber-500/30 bg-amber-500/10 px-1.5 text-[10px] text-amber-300">
            来自先前运行
          </Badge>
        )}
        <button className="ml-auto text-slate-500 hover:text-slate-300" onClick={onClose}>
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className={cn("flex", embedded ? "min-h-0 flex-1" : "h-[68vh] min-h-[420px]")}>
        {/* ── Shot Navigator ── */}
        <ScrollArea className={cn("shrink-0 border-r border-white/[0.07]", embedded ? "w-48" : "w-72")}>
          <div className="p-2">
            {Array.from({ length: total }, (_, i) => {
              const st = states[String(i)] ?? "p";
              const lab = t.labels?.[String(i)];
              const active = idx === i;
              return (
                <button
                  key={i}
                  onClick={() => setIdx(i)}
                  className={cn(
                    "flex w-full items-center gap-2.5 rounded-lg border-l-2 px-3 py-2.5 text-left text-xs transition-colors",
                    active
                      ? "border-l-cyan-400 bg-cyan-500/10 text-cyan-200"
                      : "border-l-transparent text-slate-400 hover:bg-white/[0.04] hover:text-slate-200",
                  )}
                >
                  <span className={cn("h-2 w-2 shrink-0 rounded-full", STATE_DOT[st])} />
                  <span className="shrink-0 font-mono text-[11px]">#{i + 1}</span>
                  <span className="min-w-0 flex-1 truncate">{lab || STATE_TXT[st]}</span>
                  {t.iters?.[String(i)] && (
                    <span className="shrink-0 font-mono text-[10px] text-slate-600">{t.iters[String(i)]}</span>
                  )}
                </button>
              );
            })}
          </div>
        </ScrollArea>

        {/* ── Agent Thought Stream ── */}
        <ScrollArea className="min-w-0 flex-1">
          <div className="relative p-4 pl-8">
            {/* timeline rail */}
            <div className="absolute top-6 bottom-6 left-[19px] w-px bg-white/10" />

            {steps === null ? (
              <div className="p-4 text-sm text-slate-500">加载轨迹…</div>
            ) : entries.length === 0 ? (
              <div className="p-4 text-sm leading-relaxed text-slate-500">
                本轮没有执行该单元（先前运行已完成、走检查点跳过），历史任务中也没有找到它的轨迹。
              </div>
            ) : (
              <AnimatePresence initial={false}>
                {entries.map((e, i) => {
                  const worst = entryWorstVerdict(e);
                  const vm = VERDICT_META[worst];
                  const expanded = openNode === i;

                  // round divider: a new conversation begins here
                  if (e.round) {
                    const rerun = e.round.note === "conflict_rerun";
                    return (
                      <motion.div key={i} layout className="relative my-3 flex items-center gap-2.5">
                        <div className="h-px flex-1 bg-gradient-to-r from-transparent via-white/15 to-white/15" />
                        <span className={cn(
                          "flex items-center gap-1 text-[10px] font-semibold tracking-wider uppercase",
                          rerun ? "text-amber-400/90" : "text-slate-500",
                        )}>
                          {rerun && <TriangleAlert className="h-3 w-3" />}
                          第 {e.round.n} 轮{rerun ? " · 冲突重跑（对话重新开始）" : ""}
                        </span>
                        <div className="h-px flex-1 bg-gradient-to-l from-transparent via-white/15 to-white/15" />
                      </motion.div>
                    );
                  }

                  return (
                    <motion.div key={i} layout className="relative mb-2">
                      {/* rail node */}
                      <span className={cn(
                        "absolute top-[11px] -left-[21px] z-10 flex h-4 w-4 items-center justify-center rounded-full border bg-slate-950",
                        e.calling ? "border-cyan-400/70 text-cyan-300" : vm.node,
                      )}>
                        {e.calling
                          ? <Loader2 className="h-2.5 w-2.5 animate-spin" />
                          : <VerdictIcon v={worst} className="h-2.5 w-2.5" />}
                      </span>

                      {e.calling ? (
                        /* 进行中节点 */
                        <motion.div
                          layout
                          className="flex items-center gap-3 rounded-xl border border-cyan-500/40 bg-cyan-500/[0.06] px-4 py-3 shadow-[0_0_22px_rgba(34,211,238,0.12)]"
                        >
                          <Loader2 className="h-4 w-4 animate-spin text-cyan-300" />
                          <span className="text-[13px] text-cyan-300">
                            模型思考中… Iter {fmtIter(e.iter, e.max_iter)}
                            {e.elapsed !== undefined ? ` · 已 ${e.elapsed}s` : ""}
                          </span>
                        </motion.div>
                      ) : expanded ? (
                        <NodeCard e={e} onCollapse={() => { setOpenNode(null); follow.current = false; }} />
                      ) : (
                        /* 历史节点：折叠为一行 */
                        <motion.button
                          layout
                          onClick={() => { setOpenNode(i); follow.current = i === entries.length - 1; }}
                          className="group flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left transition-colors hover:bg-white/[0.03]"
                        >
                          <span className="font-mono text-[11px] text-slate-600">Iter {e.iter}</span>
                          <span className={cn("truncate font-mono text-xs", worst === "fail" ? "text-red-400/80" : worst === "warn" ? "text-amber-300/70" : "text-slate-500 group-hover:text-slate-300")}>
                            {e.action?.tool || "（无工具）"}
                          </span>
                          {e.results.map((r, j) => (
                            <span key={j} className={cn("h-1.5 w-1.5 shrink-0 rounded-full", (VERDICT_META[r.verdict ?? "info"] ?? VERDICT_META.info).dot)} />
                          ))}
                          {e.elapsed !== undefined && <span className="text-[10px] text-slate-700">+{e.elapsed}s</span>}
                          <ChevronDown className="ml-auto h-3.5 w-3.5 shrink-0 text-slate-700 group-hover:text-slate-400" />
                        </motion.button>
                      )}
                    </motion.div>
                  );
                })}
              </AnimatePresence>
            )}
          </div>
        </ScrollArea>
      </div>
    </motion.div>
  );
}

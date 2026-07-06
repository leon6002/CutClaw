/** Custom React Flow nodes for the agent workflow canvas. */
import { memo, useState, type ReactNode } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import {
  AudioWaveform, Check, FileVideo, Film, GitMerge, Layers, Loader2,
  MessageSquareText, Music2, PenLine, Play, RotateCcw, ScanText, Scissors,
  TriangleAlert, X,
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

// ── B2. Asset Node (source video / audio + its analysis annotation) ─────────

export interface AssetNodeData {
  path: string;
  fileName: string;
  assetType: "video" | "image" | "audio";
  annotated: boolean;
  annotation?: Record<string, any>;
  contentHash?: string;
  liveState?: string;   // live pipeline analysis state: "r" 分析中 / "d" 已分析
  onOpen?: () => void;
  [key: string]: unknown;
}

const fmtQ = (q: any) => (typeof q === "number" ? (q % 1 ? q.toFixed(1) : String(q)) : String(q));

/** Source asset: a cheap ffmpeg poster thumbnail (never a live <video>) for
 *  videos, a waveform for audio — click to open the full annotation + player. */
export const AssetNode = memo(({ data }: NodeProps) => {
  const d = data as AssetNodeData;
  const isAudio = d.assetType === "audio";
  const q = (d.annotation ?? {}).quality_score;
  const live = d.liveState;   // "r" 分析中 / "d" 已分析
  const [thumbFailed, setThumbFailed] = useState(false);
  const thumbUrl = `/api/assets/thumb?hash=${encodeURIComponent(d.contentHash || "")}&path=${encodeURIComponent(d.path)}`;
  return (
    <div
      className={cn(
        "group w-[236px] cursor-pointer overflow-hidden rounded-xl border bg-slate-900/85 shadow-lg transition-colors",
        live === "r" ? "border-sky-400/70 node-breathe"
          : isAudio ? "border-violet-500/30 hover:border-violet-400/60"
            : "border-sky-500/30 hover:border-sky-400/60",
      )}
      onClick={(ev) => { ev.stopPropagation(); d.onOpen?.(); }}
      title="点击查看该素材的标注与预览"
    >
      {/* poster: cheap cached JPEG for video, decorative waveform for audio */}
      <div className="relative flex aspect-video w-full items-center justify-center overflow-hidden bg-gradient-to-br from-slate-800 to-slate-950">
        {isAudio
          ? <Waveform seed={d.path} />
          : !thumbFailed
            ? <img src={thumbUrl} loading="lazy" className="h-full w-full object-cover" onError={() => setThumbFailed(true)} />
            : <Film className="h-8 w-8 text-slate-700" />}
        {/* analyzing pulse overlay */}
        {live === "r" && <span className="pointer-events-none absolute inset-0 animate-pulse bg-sky-500/15" />}
        {/* quality / status badge */}
        <span className="absolute top-1.5 right-1.5 rounded bg-black/65 px-1.5 py-0.5 text-[10px] font-semibold backdrop-blur-sm">
          {live === "r"
            ? <span className="flex items-center gap-1 text-sky-300"><Loader2 className="h-2.5 w-2.5 animate-spin" />分析中</span>
            : d.annotated ? <span className="text-emerald-300">Q {fmtQ(q)}</span>
              : live === "d" ? <span className="text-sky-300">已分析</span>
                : <span className="text-amber-300">未标注</span>}
        </span>
        {/* stage tag */}
        <span className={cn("absolute bottom-1.5 left-1.5 rounded px-1.5 py-0.5 text-[9px] backdrop-blur-sm",
          isAudio ? "bg-violet-500/25 text-violet-200" : "bg-sky-500/25 text-sky-100")}>
          {isAudio ? "音乐分析" : "视频理解"}
        </span>
        {/* hover play hint */}
        <span className="absolute inset-0 flex items-center justify-center bg-black/0 opacity-0 transition-opacity group-hover:bg-black/25 group-hover:opacity-100">
          <span className="flex h-8 w-8 items-center justify-center rounded-full border border-white/25 bg-black/60">
            <Play className="ml-0.5 h-3.5 w-3.5 text-white" />
          </span>
        </span>
      </div>
      {/* filename */}
      <div className="flex items-center gap-2 px-2.5 py-1.5">
        <span className={cn("flex h-4 w-4 shrink-0 items-center justify-center rounded",
          isAudio ? "bg-violet-500/15 text-violet-300" : "bg-sky-500/15 text-sky-300")}>
          {isAudio ? <Music2 className="h-2.5 w-2.5" /> : <Film className="h-2.5 w-2.5" />}
        </span>
        <span className="truncate text-[11.5px] font-medium text-slate-200" title={d.fileName}>{d.fileName}</span>
      </div>
      {/* source → screenwriter; right target = faint back-links from clip result
          nodes; left target = original music tracks feeding a BGM mix node */}
      <Handle type="target" position={H.r} className={handleCls()} />
      <Handle id="in-l" type="target" position={H.l} className={handleCls()} />
      <Handle type="source" position={H.r} className={handleCls()} />
    </div>
  );
});

/** Decorative deterministic waveform for audio posters (seeded by path). */
function Waveform({ seed }: { seed: string }) {
  const bars = Array.from({ length: 32 }, (_, i) => {
    const c = seed.charCodeAt((i * 7) % Math.max(1, seed.length)) || 60;
    return 18 + ((c * 31 + i * 17) % 62);
  });
  return (
    <div className="flex h-full w-full items-center justify-center gap-[2px] px-5">
      {bars.map((h, i) => (
        <span key={i} className="w-[3px] rounded-full bg-violet-500/45" style={{ height: `${h}%` }} />
      ))}
    </div>
  );
}

// ── B3. Clip result node (final selected source + time slice, previewable) ──

export interface ClipInfo { video_path: string; start: string; end: string; duration: number }
export interface ClipNodeData {
  clips: ClipInfo[];
  fallback?: boolean;
  state: string;   // shot state p/r/d/f
  onOpen?: () => void;
  [key: string]: unknown;
}

const shortTime = (t?: string) => (t || "").replace(/^00:/, "");

export const ClipNode = memo(({ data }: NodeProps) => {
  const d = data as ClipNodeData;
  const clips = d.clips ?? [];
  const first = clips[0];
  const name = first ? (first.video_path.split(/[\\/]/).pop() || first.video_path) : "—";
  return (
    <div
      className={cn(
        "w-[212px] cursor-pointer rounded-xl border bg-slate-900/85 px-2.5 py-2 shadow-lg transition-colors",
        d.state === "f" ? "border-red-500/40 hover:border-red-400/60" : "border-teal-500/30 hover:border-teal-400/60",
      )}
      onClick={(ev) => { ev.stopPropagation(); d.onOpen?.(); }}
      title="点击预览这个镜头"
    >
      <div className="flex items-center gap-1.5">
        <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded bg-teal-500/15 text-teal-300">
          <Play className="ml-px h-2.5 w-2.5" />
        </span>
        <span className="truncate text-[11.5px] font-medium text-teal-100" title={first?.video_path}>{name}</span>
        {d.fallback && (
          <span className="shrink-0 rounded bg-amber-500/15 px-1 py-0.5 text-[8.5px] text-amber-300/90" title="确定性兜底选取">兜底</span>
        )}
      </div>
      {clips.map((c, i) => (
        <div key={i} className="mt-1 flex items-center gap-1.5 font-mono text-[10px]">
          {clips.length > 1 && <span className="text-slate-600">{i + 1}</span>}
          <span className="text-slate-300">{shortTime(c.start)}–{shortTime(c.end)}</span>
          {c.duration != null && <span className="text-slate-600">{Number(c.duration).toFixed(1)}s</span>}
        </div>
      ))}
      {/* in ← shot lane, out → merge, back → source asset (faint) */}
      <Handle id="in" type="target" position={H.l} className={handleCls()} />
      <Handle id="out" type="source" position={H.r} className={handleCls()} />
      <Handle id="back" type="source" position={H.l} className="!h-1.5 !w-1.5 !border-0 !bg-slate-600/50" />
    </div>
  );
});

// ── C. Screenwriter stage — a self-drawn vertical timeline (single node) ─────
// One node renders the whole "AI 编剧" phase as an elegant station-and-rail
// timeline: each sub-step is a station with its own call data, connected by a
// rail whose active segment glows and flows. Drawn internally (CSS) rather than
// with React-Flow child nodes + edges, so there are no exposed handle dots.

// ordered sub-steps of the screenwriter phase. Labels MUST match _sw_stage()
// in src/Screenwriter_scene_short.py (order drives done/running/pending).
export const SW_STEPS = ["选择音乐段落", "生成结构提案", "生成分镜脚本", "挑选开场对白", "保存分镜脚本"];

export interface SwStep { label: string; state: string; calls: number; elapsed: number; }
export interface ScreenwriterData {
  title: string;
  state: string;      // p/r/d/f (whole stage)
  running?: boolean;
  started?: boolean;  // false → dim "待开始" placeholder before the phase begins
  calls: number;      // total LLM calls
  steps: SwStep[];
  onOpen?: () => void;
  [key: string]: unknown;
}

/** True once the pipeline has reached station i (done / running / failed). */
const reached = (steps: SwStep[], i: number) =>
  i >= 0 && i < steps.length && ["d", "r", "f"].includes(steps[i].state);

export const ScreenwriterNode = memo(({ data }: NodeProps) => {
  const d = data as ScreenwriterData;
  const steps = d.steps ?? [];
  const notStarted = d.started === false;
  return (
    <div className={cn(
      "w-[300px] overflow-hidden rounded-2xl border bg-gradient-to-b from-slate-900/95 to-slate-950/95 shadow-[0_10px_34px_rgba(0,0,0,0.45)] transition-opacity",
      d.running ? "border-amber-400/50 node-breathe-amber"
        : d.state === "f" ? "border-red-500/40" : "border-amber-500/25",
      notStarted && "opacity-70",
    )}>
      {/* header */}
      <div
        className="flex cursor-pointer items-center gap-2 border-b border-white/[0.06] bg-gradient-to-r from-amber-500/[0.16] via-amber-500/[0.05] to-transparent px-3.5 py-2.5"
        onClick={(ev) => { ev.stopPropagation(); d.onOpen?.(); }}
        title="点击查看编剧的提示词与回复"
      >
        <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-amber-500/15 text-amber-300 ring-1 ring-amber-400/20">
          <PenLine className="h-3.5 w-3.5" />
        </span>
        <span className="text-[13px] font-semibold tracking-wide text-amber-200">{d.title}</span>
        {d.running && <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-300/90" />}
        <span className="ml-auto font-mono text-[10px] text-slate-500">
          {d.calls > 0 ? `${d.calls} 次调用` : d.running ? "运行中" : notStarted ? "待开始" : "已完成"}
        </span>
      </div>

      {/* timeline */}
      <div className="px-3 py-1.5">
        {steps.map((s, i) => {
          const active = s.state === "r", done = s.state === "d", fail = s.state === "f";
          const last = i === steps.length - 1;
          const topFilled = reached(steps, i);
          const botFilled = reached(steps, i + 1);
          const flowing = done && steps[i + 1]?.state === "r";
          return (
            <div
              key={i}
              className={cn(
                "group relative flex h-12 cursor-pointer items-center gap-3 rounded-lg px-2 transition-colors",
                active ? "bg-amber-500/[0.06]" : "hover:bg-white/[0.03]",
              )}
              onClick={(ev) => { ev.stopPropagation(); d.onOpen?.(); }}
            >
              {/* rail column: two half-lines + a station */}
              <div className="relative h-full w-4 shrink-0">
                {!(i === 0) && (
                  <span className={cn("absolute top-0 left-1/2 h-1/2 w-px -translate-x-1/2",
                    topFilled ? "bg-amber-400/50" : "bg-white/10")} />
                )}
                {!last && (
                  <span className={cn("absolute top-1/2 left-1/2 h-1/2 w-px -translate-x-1/2 overflow-hidden",
                    botFilled ? "bg-amber-400/50" : "bg-white/10")}>
                    {flowing && <span className="sw-rail-flow absolute inset-0" />}
                  </span>
                )}
                <span className={cn(
                  "absolute top-1/2 left-1/2 flex h-4 w-4 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border",
                  done ? "border-emerald-400/70 bg-emerald-400/15 text-emerald-300"
                    : active ? "sw-station-active border-amber-300 bg-amber-400/25 text-amber-100"
                      : fail ? "border-red-400/70 bg-red-500/20 text-red-300"
                        : "border-white/15 bg-slate-800",
                )}>
                  {done ? <Check className="h-2.5 w-2.5" strokeWidth={3} />
                    : active ? <span className="h-1.5 w-1.5 rounded-full bg-amber-200" />
                      : fail ? <X className="h-2.5 w-2.5" strokeWidth={3} />
                        : <span className="h-1 w-1 rounded-full bg-slate-600" />}
                </span>
              </div>

              {/* content */}
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className={cn("font-mono text-[9px]", active ? "text-amber-400/80" : "text-slate-600")}>
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className={cn("truncate text-[12.5px]",
                    active ? "font-semibold text-amber-100" : done ? "text-slate-200" : "text-slate-500")}>
                    {s.label}
                  </span>
                </div>
                {(s.calls > 0 || active) && (
                  <div className="mt-0.5 font-mono text-[9.5px] text-slate-500">
                    {s.calls > 0
                      ? <>{s.calls} 次调用{s.elapsed ? <span className="text-slate-600"> · {s.elapsed}s</span> : null}</>
                      : "调用中…"}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
      <Handle type="target" position={H.l} className={handleCls()} />
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

// ── D2. Asset Group Node (video column clustered by trip/location) ──────────
// 14 individual poster cards made the input column a mile high; videos are
// clustered by capture date (trip gaps) + location into one compact card per
// group, a thumbnail grid inside. Click a thumbnail → that asset's annotation.

export interface AssetGroupItem {
  path: string; fileName: string; contentHash: string;
  annotated: boolean; live?: string;
  onOpen?: () => void;
}
export interface AssetGroupData {
  label: string;          // e.g. "2026 06/15–06/18"
  sub?: string;           // e.g. "📍 阿勒泰"
  items: AssetGroupItem[];
  [key: string]: unknown;
}

export const AssetGroupNode = memo(({ data }: NodeProps) => {
  const d = data as AssetGroupData;
  const anyLive = d.items.some((it) => it.live === "r");
  return (
    <div className={cn(
      "w-[264px] rounded-xl border bg-slate-900/85 shadow-lg",
      anyLive ? "border-sky-400/70 node-breathe" : "border-sky-500/25",
    )}>
      <div className="flex items-baseline gap-1.5 px-3 pt-2 pb-1">
        <Film className="h-3 w-3 shrink-0 self-center text-sky-300" />
        <span className="truncate text-[11.5px] font-semibold text-slate-200">{d.label}</span>
        <span className="ml-auto shrink-0 font-mono text-[9.5px] text-slate-500">{d.items.length} 个</span>
      </div>
      {d.sub && <div className="px-3 pb-1 text-[10px] text-slate-400">{d.sub}</div>}
      <div className="grid grid-cols-3 gap-1 px-2 pb-2">
        {d.items.map((it) => (
          <GroupThumb key={it.path} it={it} />
        ))}
      </div>
      <Handle type="target" position={H.l} className={handleCls()} />
      <Handle type="source" position={H.r} className={handleCls()} />
    </div>
  );
});

function GroupThumb({ it }: { it: AssetGroupItem }) {
  const [failed, setFailed] = useState(false);
  const url = `/api/assets/thumb?hash=${encodeURIComponent(it.contentHash || "")}&path=${encodeURIComponent(it.path)}`;
  return (
    <button
      onClick={(ev) => { ev.stopPropagation(); it.onOpen?.(); }}
      title={`${it.fileName} — 点击查看标注与预览`}
      className={cn(
        "relative aspect-video overflow-hidden rounded-md border bg-slate-950 transition-transform hover:z-10 hover:scale-[1.6]",
        it.live === "r" ? "animate-pulse border-cyan-400/80"
          : it.annotated || it.live === "d" ? "border-white/10 hover:border-sky-400/70"
            : "border-amber-400/40",
      )}
    >
      {!failed
        ? <img src={url} loading="lazy" className="h-full w-full object-cover" onError={() => setFailed(true)} />
        : <Film className="m-auto h-4 w-4 text-slate-700" />}
      {it.live === "r" && <span className="pointer-events-none absolute inset-0 bg-cyan-500/20" />}
    </button>
  );
}

// ── E. Stage Node (batch analysis phases as first-class canvas citizens) ────
// The per-file/per-segment grids (视频片段理解 / 密集片段描述 / …) used to live
// under the canvas; each is now ONE node in an analysis chain between the
// assets and the screenwriter. Click opens the workbench with full traces.

const STAGE_META: Record<string, { title: string; icon: ReactNode; text: string; chip: string }> = {
  video_analysis:       { title: "逐文件分析调度", icon: <FileVideo className="h-3.5 w-3.5" />,    text: "text-slate-300",   chip: "bg-slate-500/15 text-slate-300" },
  video_clips:          { title: "视频片段理解",   icon: <Film className="h-3.5 w-3.5" />,         text: "text-sky-300",     chip: "bg-sky-500/15 text-sky-300" },
  video_dense:          { title: "密集片段描述",   icon: <ScanText className="h-3.5 w-3.5" />,     text: "text-fuchsia-300", chip: "bg-fuchsia-500/15 text-fuchsia-300" },
  video_scenes:         { title: "场景分析",       icon: <Layers className="h-3.5 w-3.5" />,       text: "text-emerald-300", chip: "bg-emerald-500/15 text-emerald-300" },
  audio_analysis_asset: { title: "音频分析",       icon: <AudioWaveform className="h-3.5 w-3.5" />, text: "text-violet-300",  chip: "bg-violet-500/15 text-violet-300" },
  audio_segments:       { title: "音频片段描述",   icon: <AudioWaveform className="h-3.5 w-3.5" />, text: "text-violet-300",  chip: "bg-violet-500/15 text-violet-300" },
};

export interface StageNodeData {
  task: string;
  info: { total: number; states: Record<string, string>; done?: number; fail?: number; avg?: number; eta?: number };
  onOpen?: () => void;
  [key: string]: unknown;
}

const STAGE_BLOCK_CLS: Record<string, string> = {
  d: "bg-emerald-400/80", r: "bg-cyan-400 animate-pulse", f: "bg-red-400", p: "bg-white/10",
};

export const StageNode = memo(({ data }: NodeProps) => {
  const d = data as StageNodeData;
  const meta = STAGE_META[d.task] ?? { title: d.task, icon: <Layers className="h-3.5 w-3.5" />, text: "text-slate-300", chip: "bg-slate-500/15 text-slate-300" };
  const states = d.info?.states ?? {};
  const keys = Object.keys(states).sort((a, b) => Number(a) - Number(b));
  const vals = keys.map((k) => states[k]);
  const total = d.info?.total ?? vals.length;
  const runN = vals.filter((v) => v === "r").length;
  const failN = d.info?.fail ?? vals.filter((v) => v === "f").length;
  const doneN = d.info?.done ?? vals.filter((v) => v === "d").length;
  const pct = total ? Math.round((doneN / total) * 100) : 0;
  const allDone = total > 0 && doneN >= total;
  return (
    <div
      className={cn(
        "w-[212px] cursor-pointer rounded-xl border bg-slate-900/90 px-3 py-2 shadow-lg transition-colors hover:border-white/30",
        runN > 0 ? "border-cyan-400/70 node-breathe"
          : failN > 0 ? "border-red-500/50"
            : allDone ? "border-emerald-500/40" : "border-white/10",
      )}
      onClick={(ev) => { ev.stopPropagation(); d.onOpen?.(); }}
      title="点击在工作台查看每个单元的调用详情"
    >
      <div className="flex items-center gap-1.5">
        <span className={cn("flex h-5 w-5 items-center justify-center rounded", meta.chip)}>{meta.icon}</span>
        <span className={cn("truncate text-[11.5px] font-semibold", meta.text)}>{meta.title}</span>
        {runN > 0 && <Loader2 className="ml-auto h-3 w-3 shrink-0 animate-spin text-cyan-400" />}
        {runN === 0 && allDone && !failN && <Check className="ml-auto h-3 w-3 shrink-0 text-emerald-400" />}
        {runN === 0 && failN > 0 && <TriangleAlert className="ml-auto h-3 w-3 shrink-0 text-red-400" />}
      </div>
      <div className="mt-1 flex items-baseline gap-1.5 font-mono text-[10px] text-slate-400">
        <span>{doneN}/{total || "?"}</span>
        <span className="text-slate-600">({pct}%)</span>
        {runN > 0 && <span className="text-cyan-300">⚡ {runN} 并行</span>}
        {failN > 0 && <span className="text-red-400">✕ {failN}</span>}
      </div>
      {/* unit blocks (mirrors the old grid) — beyond 96 units a slim bar */}
      {total > 0 && total <= 96 ? (
        <div className="mt-1.5 flex flex-wrap gap-[3px]">
          {Array.from({ length: total }, (_, i) => (
            <span key={i} className={cn("h-[7px] w-[7px] rounded-[2px]", STAGE_BLOCK_CLS[states[String(i)] ?? "p"] ?? STAGE_BLOCK_CLS.p)} />
          ))}
        </div>
      ) : total > 96 ? (
        <div className="mt-1.5 h-1.5 overflow-hidden rounded bg-white/10">
          <div className="h-full rounded bg-emerald-400/80" style={{ width: `${pct}%` }} />
        </div>
      ) : null}
      {runN > 0 && (d.info?.avg || d.info?.eta) ? (
        <div className="mt-1 text-[9.5px] text-slate-500">
          {d.info.avg ? `平均 ${d.info.avg}s/个` : ""}{d.info.avg && d.info.eta ? " · " : ""}
          {d.info.eta ? `还需约 ${Math.max(1, Math.round(d.info.eta / 60))} 分钟` : ""}
        </div>
      ) : null}
      <Handle id="in" type="target" position={H.l} className={handleCls()} />
      <Handle id="chain-in" type="target" position={H.t} className={handleCls()} />
      <Handle id="out" type="source" position={H.r} className={handleCls()} />
      <Handle id="chain-out" type="source" position={H.b} className={handleCls()} />
    </div>
  );
});

export const nodeTypes = {
  shotRoot: ShotRootNode,
  shotLane: ShotLaneNode,
  asset: AssetNode,
  clip: ClipNode,
  screenwriter: ScreenwriterNode,
  orchestrator: OrchestratorNode,
  stage: StageNode,
  assetGroup: AssetGroupNode,
};

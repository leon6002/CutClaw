/** In-canvas overlay that previews an AI-selected shot: plays each clip from its
 *  source video, seeked to the clip's start and auto-stopping at its end. */
import { useRef } from "react";
import { RotateCcw, Scissors, X } from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { mediaUrl } from "../../api";
import type { ClipInfo, ShotInfo } from "./WorkflowCanvas";

const basename = (p: string) => (p || "").split(/[\\/]/).pop() || p;

function toSec(t?: string): number {
  if (!t) return 0;
  const parts = String(t).split(":").map((x) => parseFloat(x));
  if (parts.some((x) => isNaN(x))) return 0;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] || 0;
}

function ClipVideo({ clip, idx, many }: { clip: ClipInfo; idx: number; many: boolean }) {
  const ref = useRef<HTMLVideoElement>(null);
  const start = toSec(clip.start);
  const end = toSec(clip.end);
  const seekStart = () => { const v = ref.current; if (v) v.currentTime = start; };
  const stopAtEnd = () => {
    const v = ref.current;
    if (v && end > start && v.currentTime >= end) v.pause();
  };
  const replay = () => { const v = ref.current; if (v) { v.currentTime = start; v.play().catch(() => {}); } };
  return (
    <div>
      <div className="mb-1 flex items-center gap-2 font-mono text-[11px] text-slate-400">
        {many && <span className="text-slate-500">片段 {idx + 1}</span>}
        <span className="text-slate-300">{clip.start}–{clip.end}</span>
        {clip.duration != null && <span className="text-slate-600">{Number(clip.duration).toFixed(1)}s</span>}
        <button
          onClick={replay}
          className="ml-auto flex items-center gap-1 rounded border border-teal-500/30 bg-teal-500/10 px-2 py-0.5 text-teal-300 hover:bg-teal-500/20"
        >
          <RotateCcw className="h-3 w-3" /> 重播片段
        </button>
      </div>
      <video
        ref={ref} src={mediaUrl(clip.video_path)} controls
        onLoadedMetadata={seekStart} onTimeUpdate={stopAtEnd}
        className="max-h-[300px] w-full rounded-lg bg-black"
      />
      <div className="mt-1 truncate text-[10.5px] text-slate-600" title={clip.video_path}>{basename(clip.video_path)}</div>
    </div>
  );
}

/** 选材证据链(§18):这个镜头为什么是它 — 细评/池分/菜单/修复/落点 全程可审计 */
function Evidence({ ev }: { ev: any }) {
  if (!ev) return null;
  const a = ev.anchor;
  const tierCls = (t: string) =>
    t === "S" ? "bg-amber-400/20 text-amber-300" : t === "A" ? "bg-emerald-400/15 text-emerald-300"
      : t === "B" ? "bg-sky-400/15 text-sky-300" : "bg-white/10 text-slate-400";
  return (
    <div className="rounded-xl border border-violet-500/20 bg-violet-500/[0.04] p-3 text-[11.5px] leading-relaxed text-slate-300">
      <div className="mb-1 font-semibold text-violet-300">🧭 选材依据</div>
      {ev.slot && (
        <div className="text-slate-400">
          槽位:{Number(ev.slot.duration ?? 0).toFixed(1)}s · {ev.slot.emotion || "—"}
          {ev.slot.beat && <span className="text-slate-500"> · {ev.slot.beat}</span>}
        </div>
      )}
      {a ? (
        <>
          <div className="mt-1">
            候选时刻 <span className="font-mono text-cyan-300">{a.id}</span>:{a.video}
            <span className="font-mono"> {Number(a.start).toFixed(1)}–{Number(a.end).toFixed(1)}s</span>
            {a.fine_tier && <span className={`ml-2 rounded px-1.5 py-0.5 text-[10.5px] font-bold ${tierCls(a.fine_tier)}`}>{a.fine_tier} 级</span>}
            {a.sound && <span className="ml-1">🎙</span>}
            {a.event === 1 && <span className="ml-1">⚡</span>}
          </div>
          {a.critique && <div className="text-fuchsia-200/80">「{a.critique}」</div>}
          <div className="text-slate-500">
            池分 {(Number(a.score ?? 0) * 10).toFixed(1)} · 稀缺 {a.rarity ?? "—"} ·
            look {a.cluster ?? "—"}{a.cam ? ` · cam ${a.cam}` : ""}
            {a.empty_shot ? " · 空镜(呼吸位)" : ""}
          </div>
        </>
      ) : (
        <div className="mt-1 text-slate-500">无锚点 — 该镜头由剪辑 Agent 自由挑选</div>
      )}
      {ev.repair && (
        <div className="mt-1 rounded-md border border-amber-500/25 bg-amber-500/[0.06] px-2 py-1 text-amber-200/90">
          🔧 换锚:原选 <span className="font-mono">{ev.repair.original ?? "无"}</span>,
          因「{ev.repair.reason}」改为 <span className="font-mono">{ev.repair.result}</span>
        </div>
      )}
      {ev.pick && (
        <div className="mt-1 text-slate-400">
          落点:<span className={ev.pick.method === "anchored" ? "text-emerald-300"
            : ev.pick.method === "fallback" ? "text-amber-300" : "text-sky-300"}>
            {ev.pick.method === "anchored" ? "锚定锁窗" : ev.pick.method === "fallback" ? "确定性兜底" : "Agent 挑选"}
          </span>
          {ev.pick.range?.length === 2 && <span className="font-mono"> {ev.pick.range[0]}–{ev.pick.range[1]}</span>}
          {ev.pick.note && <span className="text-slate-500"> — {ev.pick.note}</span>}
        </div>
      )}
    </div>
  );
}

export default function ClipPlayer({ shot, onClose, evidence }: { shot: ShotInfo; onClose: () => void; evidence?: any }) {
  const clips = shot.clips ?? [];
  const title = basename(clips[0]?.video_path || shot.video_path);
  return (
    <div className="flex h-full w-full flex-col overflow-hidden rounded-2xl border border-teal-500/25 bg-slate-950/95 shadow-[0_8px_40px_rgba(0,0,0,0.6)]">
      <div className="flex items-center gap-2 border-b border-white/[0.07] bg-white/[0.03] px-4 py-2.5">
        <span className="flex h-5 w-5 items-center justify-center rounded-md bg-teal-500/15 text-teal-300">
          <Scissors className="h-3 w-3" />
        </span>
        <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-slate-200" title={title}>
          镜头预览 · {title}
        </span>
        {shot.fallback && (
          <span className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-300/90" title="确定性兜底选取">兜底</span>
        )}
        <button className="text-slate-500 hover:text-slate-300" onClick={onClose}><X className="h-4 w-4" /></button>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-4 p-4">
          {clips.length === 0
            ? <div className="text-sm text-slate-500">该镜头没有片段信息。</div>
            : clips.map((c, i) => <ClipVideo key={i} clip={c} idx={i} many={clips.length > 1} />)}
          <Evidence ev={evidence} />
        </div>
      </ScrollArea>
    </div>
  );
}

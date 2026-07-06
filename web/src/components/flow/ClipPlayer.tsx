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

export default function ClipPlayer({ shot, onClose }: { shot: ShotInfo; onClose: () => void }) {
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
        </div>
      </ScrollArea>
    </div>
  );
}

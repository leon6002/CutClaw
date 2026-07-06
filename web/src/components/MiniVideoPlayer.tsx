/** Compact themed video player for inline previews — replaces the native
 *  browser controls with the app's dark-neon style (cyan accents, custom
 *  seek bar). Click the frame to toggle playback. */
import { useRef, useState } from "react";
import { Maximize2, Pause, Play, Volume2, VolumeX } from "lucide-react";
import { cn } from "@/lib/utils";

const fmt = (s: number) => {
  if (!isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60), ss = Math.floor(s % 60);
  return `${m}:${String(ss).padStart(2, "0")}`;
};

export default function MiniVideoPlayer({ src, className }: { src: string; className?: string }) {
  const ref = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [cur, setCur] = useState(0);
  const [dur, setDur] = useState(0);

  const toggle = () => {
    const v = ref.current; if (!v) return;
    if (v.paused) { v.play().catch(() => {}); } else { v.pause(); }
  };
  const seekFromEvt = (e: React.MouseEvent) => {
    const v = ref.current; if (!v || !dur) return;
    const rect = e.currentTarget.getBoundingClientRect();
    v.currentTime = ((e.clientX - rect.left) / rect.width) * dur;
  };
  const progress = dur > 0 ? Math.min(1, cur / dur) : 0;

  return (
    <div className={cn("overflow-hidden rounded-lg border border-cyan-500/20 bg-black", className)}>
      <div className="relative cursor-pointer" onClick={toggle}>
        <video
          ref={ref} src={src} autoPlay playsInline
          className="max-h-[260px] w-full bg-black"
          onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
          onTimeUpdate={(e) => setCur(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => setDur(e.currentTarget.duration || 0)}
        />
        {!playing && (
          <span className="absolute inset-0 flex items-center justify-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-full border border-white/20 bg-black/60 backdrop-blur-sm">
              <Play className="ml-0.5 h-4.5 w-4.5 text-white" />
            </span>
          </span>
        )}
      </div>
      {/* transport bar */}
      <div className="flex items-center gap-2.5 px-2.5 py-1.5">
        <button
          onClick={toggle}
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-cyan-500 text-slate-950 shadow-[0_0_10px_rgba(34,211,238,0.4)] hover:bg-cyan-400"
        >
          {playing ? <Pause className="h-3 w-3" /> : <Play className="ml-px h-3 w-3" />}
        </button>
        <span className="shrink-0 font-mono text-[10.5px] text-slate-300">
          {fmt(cur)} <span className="text-slate-600">/ {fmt(dur)}</span>
        </span>
        {/* seek bar — click to jump, cyan progress with glow */}
        <div
          className="relative h-4 min-w-0 flex-1 cursor-pointer"
          onClick={(e) => { e.stopPropagation(); seekFromEvt(e); }}
        >
          <div className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-white/10" />
          <div
            className="absolute top-1/2 left-0 h-1 -translate-y-1/2 rounded-full bg-cyan-400 shadow-[0_0_6px_rgba(34,211,238,0.6)]"
            style={{ width: `${progress * 100}%` }}
          />
          <div
            className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-cyan-300 shadow-[0_0_6px_rgba(34,211,238,0.8)]"
            style={{ left: `${progress * 100}%` }}
          />
        </div>
        <button
          className="shrink-0 text-slate-400 hover:text-slate-200"
          onClick={() => { const v = ref.current; if (v) { v.muted = !v.muted; setMuted(v.muted); } }}
          title={muted ? "取消静音" : "静音"}
        >
          {muted ? <VolumeX className="h-3.5 w-3.5" /> : <Volume2 className="h-3.5 w-3.5" />}
        </button>
        <button
          className="shrink-0 text-slate-400 hover:text-slate-200"
          onClick={() => ref.current?.requestFullscreen?.()}
          title="全屏"
        >
          <Maximize2 className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

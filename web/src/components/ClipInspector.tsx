import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";

export interface ClipMapEntry {
  out_start: number;
  out_end: number;
  duration: number;
  video: string;
  src_start: number | null;
  src_end: number | null;
  section_idx: number;
  shot_idx: number;
  content: string;   // Screenwriter intent for this slot
  visuals: string;
  emotion: string;
  visual_beat: string;
  analysis: string;  // what the VLM actually saw at this source range
}

// crude lexical overlap → flags picks that likely don't match their slot
function matchScore(intent: string, seen: string): number {
  const norm = (s: string) =>
    new Set(
      s.toLowerCase().replace(/[^a-z\s]/g, " ").split(/\s+/)
        .filter((w) => w.length > 3),
    );
  const a = norm(intent), b = norm(seen);
  if (a.size === 0 || b.size === 0) return -1;
  let hit = 0;
  a.forEach((w) => { if (b.has(w)) hit++; });
  return hit / a.size;
}

export function useClipMap(shotPoint: string) {
  const [clips, setClips] = useState<ClipMapEntry[] | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    setClips(null); setError("");
    if (!shotPoint) return;
    api<{ clips: ClipMapEntry[]; error?: string }>(
      `/api/render/clip_map?shot_point=${encodeURIComponent(shotPoint)}`,
    ).then((r) => { setClips(r.clips); setError(r.error ?? ""); })
      .catch((e) => { setClips([]); setError(e.message || "请求失败"); });
  }, [shotPoint, tick]);
  return { clips, error, reload: () => setTick((t) => t + 1) };
}

export function activeClipAt(clips: ClipMapEntry[] | null, t: number): number {
  if (!clips) return -1;
  return clips.findIndex((c) => t >= c.out_start && t < c.out_end);
}

/** Live caption strip shown right under the playing video (never covers controls). */
export function ClipCaption({ clip }: { clip: ClipMapEntry | null }) {
  return (
    <div className="mt-1.5 min-h-[46px] w-full rounded-lg border border-white/[0.06] bg-black/30 px-3 py-1.5">
      {clip ? (
        <>
          <div className="text-[10px] font-medium tracking-wide text-cyan-300">
            {clip.video} · 源 {clip.src_start?.toFixed(1)}–{clip.src_end?.toFixed(1)}s
          </div>
          <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-slate-300">
            {clip.analysis || clip.content || "（无描述）"}
          </div>
        </>
      ) : (
        <div className="py-2 text-center text-[11px] text-slate-600">播放后此处实时显示当前片段的来源与 AI 描述</div>
      )}
    </div>
  );
}

/** Full inspector list synced to playhead. */
export function ClipInspector({
  clips, currentTime, error, onRetry, onSeek,
}: {
  clips: ClipMapEntry[] | null; currentTime: number;
  error?: string; onRetry?: () => void;
  onSeek?: (t: number) => void;
}) {
  const activeIdx = useMemo(() => activeClipAt(clips, currentTime), [clips, currentTime]);
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    if (activeIdx >= 0) {
      rowRefs.current[activeIdx]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [activeIdx]);

  if (clips === null) return <div className="py-4 text-xs text-slate-500">加载解析…</div>;
  if (clips.length === 0) {
    return (
      <div className="py-4 text-xs text-slate-500">
        {error ? <span className="text-red-400">加载失败：{error}</span> : "无 clip 映射数据"}
        {onRetry && (
          <button
            className="ml-2 rounded border border-white/10 bg-white/[0.05] px-2 py-0.5 text-[11px] text-slate-300 hover:bg-white/[0.1]"
            onClick={onRetry}
          >
            重试
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="max-h-[440px] space-y-1.5 overflow-y-auto pr-1">
      {clips.map((c, i) => {
        const active = i === activeIdx;
        const score = matchScore(c.content, c.analysis);
        const mismatch = score >= 0 && score < 0.12 && !!c.analysis;
        return (
          <div
            key={i}
            ref={(el) => { rowRefs.current[i] = el; }}
            onClick={() => onSeek?.(c.out_start + 0.05)}
            title="点击跳转到该镜头"
            className={
              "rounded-lg border px-3 py-2 transition-colors " +
              (onSeek ? "cursor-pointer " : "") +
              (active
                ? "border-cyan-400/60 bg-cyan-400/10 shadow-[0_0_12px_rgba(34,211,238,0.15)]"
                : "border-white/[0.06] bg-white/[0.02] hover:border-white/[0.14]")
            }
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-[11px]">
                <span className={active ? "font-bold text-cyan-300" : "text-slate-400"}>
                  #{i + 1}
                </span>
                <span className="tabular-nums text-slate-400">
                  成片 {c.out_start.toFixed(1)}–{c.out_end.toFixed(1)}s
                </span>
                <span className="tabular-nums text-slate-500">
                  ← {c.video} [{c.src_start?.toFixed(1)}–{c.src_end?.toFixed(1)}s]
                </span>
              </div>
              {mismatch && (
                <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-medium text-amber-300">
                  ⚠ 素材可能不符
                </span>
              )}
            </div>
            <div className="mt-1.5 grid grid-cols-1 gap-1 sm:grid-cols-2">
              <div className="rounded bg-black/20 px-2 py-1">
                <div className="text-[9px] uppercase tracking-wider text-fuchsia-400/70">编剧想要</div>
                <div className="text-[11px] leading-snug text-slate-300">{c.content || "—"}</div>
              </div>
              <div className="rounded bg-black/20 px-2 py-1">
                <div className="text-[9px] uppercase tracking-wider text-emerald-400/70">VLM 实际看到</div>
                <div className="text-[11px] leading-snug text-slate-300">
                  {c.analysis || <span className="text-slate-500">（该源时间段无缓存 → 编辑时现调 VLM）</span>}
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

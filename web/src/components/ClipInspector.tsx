import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";

// ── auto-translation (EN → 中文, server-cached, billed once per string) ─────
const zhMem = new Map<string, string>();   // in-tab memo on top of the disk cache
const ZH_KEY = "cutclaw_zh";
const ZH_EVT = "cutclaw-zh-changed";

export const zhEnabled = () => localStorage.getItem(ZH_KEY) !== "0";
export const setZhEnabled = (on: boolean) => {
  localStorage.setItem(ZH_KEY, on ? "1" : "0");
  window.dispatchEvent(new Event(ZH_EVT));
};

/** true for text worth translating: has real English words, not already Chinese */
const translatable = (t?: string): t is string =>
  !!t && /[a-zA-Z]{4}/.test(t) && !/[一-鿿]/.test(t.slice(0, 120));

/** Subscribe to the global 中/EN toggle. */
export function useZhFlag(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(zhEnabled());
  useEffect(() => {
    const h = () => setOn(zhEnabled());
    window.addEventListener(ZH_EVT, h);
    return () => window.removeEventListener(ZH_EVT, h);
  }, []);
  return [on, setZhEnabled];
}

/** Batch-translate the given texts; returns original→中文 map (grows as results land). */
export function useZh(texts: (string | undefined)[], enabled: boolean): Record<string, string> {
  const [map, setMap] = useState<Record<string, string>>({});
  const sig = texts.filter(translatable).join("");
  useEffect(() => {
    if (!enabled) return;
    const wanted = [...new Set(texts.filter(translatable))];
    if (wanted.length === 0) return;
    const fromMem: Record<string, string> = {};
    wanted.forEach((t) => { const z = zhMem.get(t); if (z) fromMem[t] = z; });
    const todo = wanted.filter((t) => !zhMem.has(t));
    if (Object.keys(fromMem).length) setMap((m) => ({ ...m, ...fromMem }));
    if (todo.length === 0) return;
    let dead = false;
    api<{ translations: string[] }>("/api/translate", {
      method: "POST", body: JSON.stringify({ texts: todo }),
    }).then((r) => {
      const add: Record<string, string> = {};
      todo.forEach((t, i) => {
        const z = r.translations[i];
        if (z) { zhMem.set(t, z); add[t] = z; }
      });
      if (!dead && Object.keys(add).length) setMap((m) => ({ ...m, ...add }));
    }).catch(() => {});
    return () => { dead = true; };
  }, [sig, enabled]);
  return map;
}

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
  const [zh] = useZhFlag();
  const zhMap = useZh([clip?.analysis, clip?.content], zh);
  const raw = clip?.analysis || clip?.content || "";
  const text = (zh && zhMap[raw]) || raw;
  return (
    <div className="mt-1.5 min-h-[46px] w-full rounded-lg border border-white/[0.06] bg-black/30 px-3 py-1.5">
      {clip ? (
        <>
          <div className="text-[10px] font-medium tracking-wide text-cyan-300">
            {clip.video} · 源 {clip.src_start?.toFixed(1)}–{clip.src_end?.toFixed(1)}s
          </div>
          <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-slate-300">
            {text || "（无描述）"}
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
  const [zh, setZh] = useZhFlag();
  const zhMap = useZh(
    (clips ?? []).flatMap((c) => [c.content, c.analysis]),
    zh && !!clips?.length,
  );
  const disp = (t?: string) => (zh && t && zhMap[t]) || t;

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
    <div>
      <div className="mb-1.5 flex items-center justify-end gap-1">
        <span className="text-[10px] text-slate-600">AI 描述</span>
        <div className="flex overflow-hidden rounded-md border border-white/10">
          <button
            className={"px-2 py-0.5 text-[10.5px] " + (zh ? "bg-cyan-500/15 text-cyan-300" : "text-slate-500 hover:text-slate-300")}
            onClick={() => setZh(true)} title="自动翻译为中文（翻译一次后永久缓存）"
          >中文</button>
          <button
            className={"px-2 py-0.5 text-[10.5px] " + (!zh ? "bg-cyan-500/15 text-cyan-300" : "text-slate-500 hover:text-slate-300")}
            onClick={() => setZh(false)} title="显示模型原始英文输出"
          >原文</button>
        </div>
      </div>
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
                <div className="text-[11px] leading-snug text-slate-300">{disp(c.content) || "—"}</div>
              </div>
              <div className="rounded bg-black/20 px-2 py-1">
                <div className="text-[9px] uppercase tracking-wider text-emerald-400/70">VLM 实际看到</div>
                <div className="text-[11px] leading-snug text-slate-300">
                  {disp(c.analysis) || <span className="text-slate-500">（该源时间段无缓存 → 编辑时现调 VLM）</span>}
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
    </div>
  );
}

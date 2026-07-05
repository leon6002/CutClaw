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

// crude lexical overlap → flags picks that likely don't match their slot.
// NOTE: the Screenwriter writes imaginative prose while the VLM reports
// literally, so overlap is naturally low — this can only catch EGREGIOUS
// mismatches. Compare against every intent field and stem lightly to keep
// false alarms down.
const stem = (w: string) => w.replace(/(ing|ed|es|s)$/, "");
function matchScore(intent: string, seen: string): number {
  const norm = (s: string) =>
    new Set(
      s.toLowerCase().replace(/[^a-z\s]/g, " ").split(/\s+/)
        .filter((w) => w.length > 3).map(stem),
    );
  const a = norm(intent), b = norm(seen);
  if (a.size < 6 || b.size < 6) return -1;   // too little text to judge
  let hit = 0;
  a.forEach((w) => { if (b.has(w)) hit++; });
  return hit / a.size;
}

/** Split a dense VLM caption like "[312-313.5s] …. [313.5-315.5s] …" into
 * timestamped segments; returns null when the text has no such markers. */
export function parseDenseSegments(text: string): { a: number; b: number; t: string }[] | null {
  const re = /\[(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*s?\]/g;
  const marks: { a: number; b: number; idx: number; len: number }[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    marks.push({ a: parseFloat(m[1]), b: parseFloat(m[2]), idx: m.index, len: m[0].length });
  }
  if (marks.length === 0) return null;
  return marks.map((mk, i) => ({
    a: mk.a, b: mk.b,
    t: text.slice(mk.idx + mk.len, i + 1 < marks.length ? marks[i + 1].idx : undefined).trim(),
  }));
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

/** Live caption strip shown right under the playing video (never covers controls).
 * Dense VLM captions hold several timestamped segments — show ONLY the one
 * covering the current source-time instead of cramming them all together. */
export function ClipCaption({ clip, playhead = -1 }: { clip: ClipMapEntry | null; playhead?: number }) {
  const [zh] = useZhFlag();
  const zhMap = useZh([clip?.analysis, clip?.content], zh);
  const raw = clip?.analysis || clip?.content || "";
  const text = (zh && zhMap[raw]) || raw;

  let shown = text;
  let segTag = "";
  const segs = text ? parseDenseSegments(text) : null;
  if (segs && clip && playhead >= 0 && clip.src_start !== null) {
    const srcT = clip.src_start + Math.max(0, playhead - clip.out_start);
    const seg = segs.find((s) => srcT >= s.a && srcT < s.b) ?? segs[0];
    shown = seg.t;
    segTag = `${seg.a}–${seg.b}s`;
  }
  return (
    // FIXED height — a min-height container grows/shrinks with 1 vs 2 caption
    // lines and nudges the whole layout at every cut
    <div className="mt-1.5 h-[64px] w-full overflow-hidden rounded-lg border border-white/[0.06] bg-black/30 px-3 py-1.5">
      {clip ? (
        <>
          <div className="text-[10px] font-medium tracking-wide text-cyan-300">
            {clip.video} · 源 {clip.src_start?.toFixed(1)}–{clip.src_end?.toFixed(1)}s
            {segTag && <span className="ml-1.5 text-slate-500">当前 [{segTag}]</span>}
          </div>
          <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-slate-300">
            {shown || "（无描述）"}
          </div>
        </>
      ) : (
        <div className="py-2 text-center text-[11px] text-slate-600">播放后此处实时显示当前片段的来源与 AI 描述</div>
      )}
    </div>
  );
}

/** Focus card pinned beside the player: the CURRENT shot's full script,
 * always in the same spot — no hunting inside the scrolling list. */
export function ActiveClipCard({ clip, index, playhead = -1, onReplace }: {
  clip: ClipMapEntry | null; index: number; playhead?: number;
  /** "这个镜头我不满意" — swap it for another highlight-pool moment */
  onReplace?: (clip: ClipMapEntry) => void;
}) {
  const [zh] = useZhFlag();
  const zhMap = useZh([clip?.content, clip?.analysis], zh);
  const disp = (t?: string) => (zh && t && zhMap[t]) || t;
  // split a dense "[a-b s] … [b-c s] …" caption into per-timestamp rows so it
  // reads as a scannable list instead of one wall of run-together text.
  const analysisText = disp(clip?.analysis);
  const segs = analysisText ? parseDenseSegments(analysisText) : null;
  const srcNow =
    clip && playhead >= 0 && clip.src_start !== null
      ? clip.src_start + Math.max(0, playhead - clip.out_start)
      : -1;
  // FIXED height: content changes with every shot — if the card grew/shrank
  // with its text, everything below would jump on each cut.
  return (
    <div className="flex h-[320px] flex-col rounded-xl border border-cyan-400/50 bg-cyan-400/[0.06] px-3.5 py-3 shadow-[0_0_16px_rgba(34,211,238,0.10)]">
      {!clip ? (
        <div className="flex flex-1 items-center justify-center text-xs text-slate-600">
          播放视频 — 当前镜头的剧本会固定显示在这里
        </div>
      ) : (
        <>
          <div className="flex shrink-0 items-center gap-2 text-[11.5px]">
            <span className="font-bold text-cyan-300">▶ 当前镜头 #{index + 1}</span>
            <span className="tabular-nums text-slate-400">成片 {clip.out_start.toFixed(1)}–{clip.out_end.toFixed(1)}s</span>
            <span className="truncate tabular-nums text-slate-500">
              ← {clip.video} [{clip.src_start?.toFixed(1)}–{clip.src_end?.toFixed(1)}s]
            </span>
            {onReplace && (
              <button
                className="ml-auto shrink-0 rounded-md border border-amber-400/40 bg-amber-500/10 px-2 py-0.5 text-[10.5px] text-amber-300 hover:bg-amber-500/20"
                title="不满意这个镜头?告诉 AI 原因,从高光池换一个(该区间进入拒绝名单,以后也不会再选)"
                onClick={(e) => { e.stopPropagation(); onReplace(clip); }}
              >
                换掉
              </button>
            )}
          </div>
          <div className="mt-2 shrink-0 rounded-lg bg-black/25 px-2.5 py-1.5">
            <div className="text-[9px] uppercase tracking-wider text-fuchsia-400/70">编剧想要</div>
            <div className="max-h-[88px] overflow-y-auto text-[11.5px] leading-snug text-slate-200">
              {disp(clip.content) || "—"}
            </div>
          </div>
          <div className="mt-1.5 flex min-h-0 flex-1 flex-col rounded-lg bg-black/25 px-2.5 py-1.5">
            <div className="shrink-0 text-[9px] uppercase tracking-wider text-emerald-400/70">VLM 实际看到</div>
            <div className="mt-1 min-h-0 flex-1 space-y-1 overflow-y-auto pr-0.5 text-[11.5px] leading-snug text-slate-200">
              {segs ? (
                segs.map((s, i) => {
                  const on = srcNow >= s.a && srcNow < s.b;
                  return (
                    <div
                      key={i}
                      className={
                        "flex gap-1.5 rounded border-l-2 py-0.5 pl-1.5 pr-1 transition-colors " +
                        (on
                          ? "border-emerald-400 bg-emerald-400/10"
                          : "border-white/10")
                      }
                    >
                      <span className="shrink-0 tabular-nums text-[10px] leading-[18px] text-emerald-300/80">
                        {s.a}–{s.b}s
                      </span>
                      <span className={on ? "text-slate-100" : "text-slate-300"}>{s.t}</span>
                    </div>
                  );
                })
              ) : (
                analysisText || <span className="text-slate-500">（该源时间段无缓存描述）</span>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/** Full inspector list synced to playhead. */
export function ClipInspector({
  clips, currentTime, error, onRetry, onSeek, maxHeight = "440px", compact = false,
}: {
  clips: ClipMapEntry[] | null; currentTime: number;
  error?: string; onRetry?: () => void;
  onSeek?: (t: number) => void;
  /** CSS max-height of the scrolling list (e.g. "calc(100vh - 260px)") */
  maxHeight?: string;
  /** slim one-line index rows — details live in the ActiveClipCard instead */
  compact?: boolean;
}) {
  const activeIdx = useMemo(() => activeClipAt(clips, currentTime), [clips, currentTime]);
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);
  const listRef = useRef<HTMLDivElement>(null);
  const [zh, setZh] = useZhFlag();
  const zhMap = useZh(
    (clips ?? []).flatMap((c) => [c.content, c.analysis]),
    zh && !!clips?.length,
  );
  const disp = (t?: string) => (zh && t && zhMap[t]) || t;

  useEffect(() => {
    // Scroll ONLY the inspector's own list. scrollIntoView would also scroll
    // every scrollable ancestor — dragging the whole PAGE down on each cut
    // while the video plays.
    const el = rowRefs.current[activeIdx];
    const box = listRef.current;
    if (!el || !box) return;
    const r = el.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    if (r.top < b.top) {
      box.scrollTo({ top: box.scrollTop + (r.top - b.top), behavior: "smooth" });
    } else if (r.bottom > b.bottom) {
      box.scrollTo({ top: box.scrollTop + (r.bottom - b.bottom), behavior: "smooth" });
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
    <div ref={listRef} className="space-y-1.5 overflow-y-auto pr-1" style={{ maxHeight }}>
      {clips.map((c, i) => {
        const active = i === activeIdx;
        // judge against EVERYTHING the Screenwriter specified, not just content
        const intent = [c.content, c.visuals, c.visual_beat, c.emotion].filter(Boolean).join(" ");
        const score = matchScore(intent, c.analysis);
        const mismatch = score >= 0 && score < 0.06 && !!c.analysis;
        if (compact) {
          return (
            <div
              key={i}
              ref={(el) => { rowRefs.current[i] = el; }}
              onClick={() => onSeek?.(c.out_start + 0.05)}
              title="点击跳转到该镜头（详情在上方「当前镜头」卡片）"
              className={
                "flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-[11px] transition-colors " +
                (onSeek ? "cursor-pointer " : "") +
                (active
                  ? "border-cyan-400/60 bg-cyan-400/10 text-cyan-200"
                  : "border-white/[0.05] bg-white/[0.02] text-slate-400 hover:border-white/[0.14]")
              }
            >
              <span className={"w-7 shrink-0 " + (active ? "font-bold text-cyan-300" : "text-slate-500")}>
                #{i + 1}
              </span>
              <span className="shrink-0 tabular-nums">{c.out_start.toFixed(1)}–{c.out_end.toFixed(1)}s</span>
              <span className="min-w-0 flex-1 truncate tabular-nums text-slate-500">
                ← {c.video} [{c.src_start?.toFixed(1)}–{c.src_end?.toFixed(1)}s]
              </span>
              {mismatch && <span className="shrink-0 text-[10px] text-amber-400" title="剧本与画面词汇几乎零重叠 — 建议人工确认">⚠</span>}
            </div>
          );
        }
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
                <span
                  className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-medium text-amber-300"
                  title="启发式提示：剧本描述与 VLM 画面描述几乎没有词汇重叠。编剧文案偏创意、VLM 偏字面，轻度不重叠是正常的——只有这种极端情况才标记，建议点击跳转人工确认。"
                >
                  ⚠ 素材可能不符
                </span>
              )}
            </div>
            <div className="mt-1.5 grid grid-cols-1 gap-1">
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

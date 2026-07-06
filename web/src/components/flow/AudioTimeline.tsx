/** Custom audio player: a real waveform (decoded once, decorative fallback) with
 *  the annotated song sections overlaid directly on the timeline. Click the wave
 *  or a section to seek. Replaces the ugly native <audio controls>. */
import { useEffect, useRef, useState } from "react";
import { Loader2, Pause, Play, Volume2, VolumeX } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "../../api";

export interface Section { name: string; start: number; end: number; instruments?: string[] }

// beat/energy/pitch keypoint lanes — color + Chinese label per madmom method
const KP_META: Record<string, { color: string; label: string }> = {
  downbeat: { color: "#22d3ee", label: "节拍" },
  beat: { color: "#22d3ee", label: "节拍" },
  mel: { color: "#34d399", label: "能量" },
  mel_energy: { color: "#34d399", label: "能量" },
  pitch: { color: "#f59e0b", label: "音高" },
};
const kpMeta = (m: string) => KP_META[m] ?? { color: "#94a3b8", label: m };

/** Normalize the /api/audio/keypoints payload to {method: [[t, intensity]]},
 *  capping each method's point count so the synced lane stays cheap to redraw. */
function normalizeKps(data: Record<string, any[]>): Record<string, [number, number][]> {
  const out: Record<string, [number, number][]> = {};
  for (const [method, kps] of Object.entries(data || {})) {
    const pts = (Array.isArray(kps) ? kps : []).map((k: any): [number, number] => {
      const t = typeof k === "number" ? k : (k.time ?? k.timestamp ?? k.t ?? 0);
      const v = typeof k === "object" ? (k.intensity ?? k.strength ?? k.energy ?? k.confidence ?? 0.5) : 0.5;
      return [Number(t) || 0, Math.min(1, Number(v) || 0.5)];
    }).filter(([t]) => t > 0);
    const step = pts.length > 130 ? Math.ceil(pts.length / 130) : 1;
    out[method] = pts.filter((_, i) => i % step === 0);
  }
  return out;
}

/** Parse "Intro 00:00.0-00:47.5, Verse 1 00:47.5-01:18.5, …" into timed sections.
 *  Returns [] when the text isn't in the "Name start–end" format. */
export function parseSectionTimes(text: string): Section[] {
  const toSec = (t: string) => {
    const p = t.split(":").map((x) => parseFloat(x));
    if (p.some((x) => isNaN(x))) return NaN;
    return p.length === 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p.length === 2 ? p[0] * 60 + p[1] : p[0];
  };
  const out: Section[] = [];
  for (const part of String(text).split(/,\s*/)) {
    const m = /^(.+?)\s+([\d:.]+)\s*[-–~]\s*([\d:.]+)\s*$/.exec(part.trim());
    if (!m) continue;
    const start = toSec(m[2]), end = toSec(m[3]);
    if (isFinite(start) && isFinite(end) && end > start) out.push({ name: m[1].trim(), start, end });
  }
  return out;
}

/** Merge annotation.sections_detail (per-section instruments) into parsed
 *  sections — same source order, so index-matched with a name sanity check. */
export function attachInstruments(secs: Section[], detail: any[] | undefined): Section[] {
  if (!Array.isArray(detail) || detail.length === 0) return secs;
  return secs.map((s, i) => {
    const d = detail[i];
    const ok = d && (!d.name || !s.name || String(d.name).toLowerCase() === s.name.toLowerCase());
    const inst = ok && Array.isArray(d.instruments) ? d.instruments.filter(Boolean) : [];
    return inst.length > 0 ? { ...s, instruments: inst } : s;
  });
}

const N_BARS = 150;

// Map each song section to a color whose hue matches its musical role, so the
// waveform reads at a glance: cool/calm at the edges, warm/hot at the energy
// peaks. First matching pattern wins (order matters — "pre-chorus"/"build"
// must be tested before "chorus"). Unknown names fall back to the violet cycle.
const SECTION_HUES: [RegExp, string][] = [
  [/build|ramp|rise|pre[-\s]?chorus/i, "#f59e0b"],   // 蓄力 → amber (rising tension)
  [/drop|climax/i, "#f43f5e"],                        // 高潮/爆点 → hot rose (impact)
  [/chorus|refrain|hook/i, "#d946ef"],               // 副歌 → bright fuchsia (the hook)
  [/bridge/i, "#14b8a6"],                            // 过渡段 → teal (a departure)
  [/break|interlude|drop\s*out/i, "#6366f1"],        // 间奏/留白 → indigo (sparse)
  [/verse/i, "#8b5cf6"],                             // 主歌 → violet (the base narrative)
  [/intro|opening|start/i, "#22d3ee"],               // 前奏 → cyan (calm opening)
  [/outro|ending|fade|coda|end/i, "#64748b"],        // 尾奏 → slate (winding down)
];
// violet family, for names none of the semantic patterns recognize
const FALLBACK_COLORS = ["#8b5cf6", "#a78bfa", "#7c3aed", "#c084fc", "#6d28d9", "#f0abfc", "#9333ea", "#c4b5fd"];

export function sectionColor(name: string, idx: number): string {
  for (const [re, c] of SECTION_HUES) if (re.test(name)) return c;
  return FALLBACK_COLORS[idx % FALLBACK_COLORS.length];
}

// Chinese label for a section name, preserving any trailing number ("Verse 2"
// → "主歌 2"). Same keyword ordering rule as SECTION_HUES. Unknown → unchanged.
const SECTION_ZH: [RegExp, string][] = [
  [/pre[-\s]?chorus/i, "前副歌"],
  [/build|ramp|rise/i, "蓄力"],
  [/drop|climax/i, "爆点"],
  [/chorus|refrain|hook/i, "副歌"],
  [/bridge/i, "过渡"],
  [/break|interlude|drop\s*out/i, "间奏"],
  [/verse/i, "主歌"],
  [/intro|opening|start/i, "前奏"],
  [/outro|ending|fade|coda|end/i, "尾奏"],
];
function sectionZh(name: string): string {
  for (const [re, zh] of SECTION_ZH) if (re.test(name)) {
    const num = name.match(/\d+/);
    return num ? `${zh} ${num[0]}` : zh;
  }
  return name;
}

// remember the bilingual-label choice across sessions
const LANG_KEY = "cutclaw_audio_seclang";

const fmt = (s: number) => {
  if (!isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60), ss = Math.floor(s % 60);
  return `${m}:${String(ss).padStart(2, "0")}`;
};

function decorativePeaks(seed: string, n: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const c = seed.charCodeAt((i * 13) % Math.max(1, seed.length)) || 60;
    out.push(0.25 + 0.75 * Math.abs(Math.sin(i * 0.28) * 0.6 + (((c * 7 + i * 11) % 60) / 60) * 0.6));
  }
  const peak = Math.max(...out, 0.01);
  return out.map((v) => Math.min(1, v / peak));
}

export default function AudioTimeline({ src, sections, duration, seekRef, keypointsPath, onTime, autoPlay = false }: {
  src: string; sections: Section[]; duration: number;
  /** start playing as soon as the audio can (inline previews) */
  autoPlay?: boolean;
  /** filled with a seek(sec) fn so external UI (e.g. the beats chart) can drive
   *  this player's internal <audio> — it has no exposed DOM ref otherwise. */
  seekRef?: React.MutableRefObject<((s: number) => void) | null>;
  /** raw audio path — when given, a synced beat/energy/pitch lane is drawn on
   *  the SAME timeline as the waveform (same length, same playhead). */
  keypointsPath?: string;
  /** reports playback time so an external chart can draw a synced playhead. */
  onTime?: (cur: number, dur: number) => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [cur, setCur] = useState(0);
  const [dur, setDur] = useState(duration || 0);
  const [peaks, setPeaks] = useState<number[] | null>(null);
  const [decoding, setDecoding] = useState(true);
  const [kps, setKps] = useState<Record<string, [number, number][]> | null>(null);

  // fetch beat keypoints for the synced lane (shares the waveform's timeline)
  useEffect(() => {
    if (!keypointsPath) { setKps(null); return; }
    let cancelled = false;
    api<Record<string, any[]>>(`/api/audio/keypoints?path=${encodeURIComponent(keypointsPath)}`)
      .then((d) => { if (!cancelled) setKps(normalizeKps(d)); })
      .catch(() => { if (!cancelled) setKps(null); });
    return () => { cancelled = true; };
  }, [keypointsPath]);
  const [showZh, setShowZh] = useState(() => localStorage.getItem(LANG_KEY) === "1");
  const toggleZh = () => setShowZh((v) => { localStorage.setItem(LANG_KEY, v ? "0" : "1"); return !v; });

  // decode a real waveform once; fall back to a decorative one on any failure
  useEffect(() => {
    let cancelled = false;
    setDecoding(true); setPeaks(null);
    (async () => {
      try {
        const buf = await (await fetch(src)).arrayBuffer();
        const AC = window.AudioContext || (window as any).webkitAudioContext;
        const ctx = new AC();
        const audio = await ctx.decodeAudioData(buf);
        const data = audio.getChannelData(0);
        const block = Math.max(1, Math.floor(data.length / N_BARS));
        const out: number[] = [];
        for (let i = 0; i < N_BARS; i++) {
          // RMS, not per-block PEAK: modern masters are limited, so peaks sit
          // at the ceiling for every loud passage and the middle of the song
          // rendered as a flat wall — loudness (what the ear tracks) has far
          // more shape.
          let sum = 0;
          for (let j = 0; j < block; j++) { const v = data[i * block + j] || 0; sum += v * v; }
          out.push(Math.sqrt(sum / block));
        }
        // normalize by the 95th percentile (a single hit shouldn't flatten
        // the rest), gentle gamma for visibility of quiet parts
        const sorted = [...out].sort((a, b) => a - b);
        const ref = sorted[Math.floor(sorted.length * 0.95)] || 0.01;
        if (!cancelled) setPeaks(out.map((v) => Math.min(1, (v / ref) ** 0.7)));
        ctx.close();
      } catch { if (!cancelled) setPeaks(decorativePeaks(src, N_BARS)); }
      finally { if (!cancelled) setDecoding(false); }
    })();
    return () => { cancelled = true; };
  }, [src]);

  const bars = peaks ?? decorativePeaks(src, N_BARS);
  const total = dur || duration || 1;
  const progress = Math.min(1, cur / total);

  // resolve each section's semantic color once, then color every bar by the
  // section it lands in (violet fallback where no section covers the time).
  const secColors = sections.map((s, i) => sectionColor(s.name, i));
  const barColorAt = (i: number): string => {
    const t = (i / bars.length) * total;
    const si = sections.findIndex((s) => t >= s.start && t < s.end);
    return si >= 0 ? secColors[si] : "#8b5cf6";
  };

  const toggle = () => {
    const a = audioRef.current; if (!a) return;
    if (a.paused) { a.play().catch(() => {}); } else { a.pause(); }
  };
  const seekTo = (sec: number) => { const a = audioRef.current; if (a) { a.currentTime = Math.max(0, Math.min(total, sec)); } };
  // seek from a click on any full-width timeline element (waveform or beat lane)
  const seekFromEvt = (e: React.MouseEvent) => {
    const rect = e.currentTarget.getBoundingClientRect();
    seekTo(((e.clientX - rect.left) / rect.width) * total);
  };

  // expose seek to external UI (beats chart) — plays from the tapped time
  useEffect(() => {
    if (!seekRef) return;
    seekRef.current = (s: number) => { seekTo(s); audioRef.current?.play().catch(() => {}); };
    return () => { seekRef.current = null; };
  }, [total]);

  // bilingual labels stack on two lines → reserve more room at the bottom;
  // an instruments line (when annotated) adds one more.
  const hasInstruments = sections.some((s) => (s.instruments?.length ?? 0) > 0);
  const labelPx = (showZh ? 30 : 20) + (hasInstruments ? 12 : 0);

  return (
    <div className="rounded-xl border border-violet-500/20 bg-gradient-to-b from-violet-500/[0.06] to-slate-900/40 p-3">
      <audio
        ref={audioRef} src={src} preload="metadata" autoPlay={autoPlay}
        onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
        onTimeUpdate={(e) => { const t = e.currentTarget.currentTime; setCur(t); onTime?.(t, e.currentTarget.duration || total); }}
        onLoadedMetadata={(e) => { const d = e.currentTarget.duration; if (isFinite(d) && d > 0) setDur(d); }}
      />

      {/* waveform + section overlay */}
      <div
        className="relative h-24 w-full cursor-pointer select-none overflow-hidden rounded-lg"
        onClick={seekFromEvt}
      >
        {/* section bands (behind the bars) */}
        {sections.map((s, i) => {
          const left = (s.start / total) * 100;
          const width = Math.max(0, ((s.end - s.start) / total) * 100);
          const color = secColors[i];
          return (
            <div
              key={i}
              className="group absolute top-0 z-0"
              style={{ left: `${left}%`, width: `${width}%`, bottom: labelPx }}
              onClick={(e) => { e.stopPropagation(); seekTo(s.start); }}
              title={`${s.name} · ${fmt(s.start)}–${fmt(s.end)}`}
            >
              <div className="h-full w-full transition-colors" style={{ background: `${color}14`, borderLeft: `1px solid ${color}55` }} />
            </div>
          );
        })}

        {/* waveform bars */}
        <div className="absolute inset-x-0 top-0 z-10 flex items-center gap-[1.5px] px-1" style={{ bottom: labelPx }}>
          {bars.map((h, i) => {
            const played = i / bars.length <= progress;
            const color = barColorAt(i);
            return (
              <span
                key={i}
                className="flex-1 rounded-full transition-opacity"
                style={{
                  height: `${Math.max(6, h * 100)}%`,
                  background: color,
                  // played bars glow at full color; upcoming ones stay dimmed so
                  // progress is still readable even with every bar colored.
                  opacity: played ? 1 : 0.3,
                }}
              />
            );
          })}
        </div>

        {/* playhead */}
        <div className="absolute top-0 z-20 w-px bg-violet-200/90 shadow-[0_0_6px_rgba(196,181,253,0.8)]"
          style={{ left: `${progress * 100}%`, bottom: labelPx }} />

        {/* section labels row (aligned to each band's start).
            Bilingual: Chinese (section color) stacked over English (dimmed). */}
        <div className="absolute inset-x-0 bottom-0 z-10" style={{ height: labelPx }}>
          {sections.map((s, i) => {
            const left = (s.start / total) * 100;
            const width = Math.max(0, ((s.end - s.start) / total) * 100);
            return (
              <div key={i} className="absolute bottom-0 overflow-hidden"
                style={{ left: `${left}%`, width: `${width}%` }}>
                {showZh ? (
                  <span className="block px-1 pb-0.5">
                    <span className="block truncate text-[9.5px] leading-[13px]" style={{ color: secColors[i] }}>
                      {sectionZh(s.name)}
                    </span>
                    <span className="block truncate text-[8.5px] leading-[12px] text-slate-500">
                      {s.name}
                    </span>
                    {(s.instruments?.length ?? 0) > 0 && (
                      <span className="block truncate text-[8px] leading-[11px] text-slate-600" title={s.instruments!.join(", ")}>
                        🎹 {s.instruments!.join(" · ")}
                      </span>
                    )}
                  </span>
                ) : (
                  <span className="block px-1">
                    <span className="block truncate text-[9.5px] leading-5"
                      style={{ color: secColors[i] }}>{s.name}</span>
                    {(s.instruments?.length ?? 0) > 0 && (
                      <span className="block truncate text-[8px] leading-[11px] text-slate-600" title={s.instruments!.join(", ")}>
                        🎹 {s.instruments!.join(" · ")}
                      </span>
                    )}
                  </span>
                )}
              </div>
            );
          })}
        </div>

        {decoding && (
          <div className="absolute right-2 top-1.5 z-30 flex items-center gap-1 text-[9px] text-slate-500">
            <Loader2 className="h-2.5 w-2.5 animate-spin" /> 解析波形…
          </div>
        )}
      </div>

      {/* synced beat/energy/pitch lane — SAME timeline as the waveform (0..total),
          same moving playhead; ticks near the playhead brighten. */}
      {kps && Object.keys(kps).length > 0 && (
        <div className="mt-1.5">
          <div
            className="relative h-8 w-full cursor-pointer overflow-hidden rounded bg-slate-900/50"
            onClick={seekFromEvt}
            title="节奏关键点 — 点击跳转播放"
          >
            {Object.entries(kps).map(([method, pts]) => {
              const color = kpMeta(method).color;
              return pts.map(([t, v], i) => {
                const active = Math.abs(t - cur) < 0.2;
                const passed = t <= cur;
                return (
                  <span
                    key={method + i}
                    className="absolute bottom-0 rounded-sm"
                    style={{
                      left: `${(t / total) * 100}%`,
                      height: `${Math.max(14, v * 100)}%`,
                      width: active ? 3 : 1.5,
                      transform: "translateX(-50%)",
                      background: color,
                      opacity: active ? 1 : passed ? 0.65 : 0.25,
                      boxShadow: active ? `0 0 6px ${color}` : undefined,
                    }}
                  />
                );
              });
            })}
            {/* playhead (same position as the waveform's) */}
            <div className="absolute top-0 bottom-0 z-10 w-px bg-violet-200/90 shadow-[0_0_5px_rgba(196,181,253,0.7)]"
              style={{ left: `${progress * 100}%` }} />
          </div>
          {/* legend */}
          <div className="mt-1 flex items-center gap-3">
            {Array.from(new Set(Object.keys(kps).map((m) => kpMeta(m).label))).map((label) => {
              const color = Object.keys(kps).map(kpMeta).find((x) => x.label === label)!.color;
              return (
                <span key={label} className="flex items-center gap-1 text-[9.5px] text-slate-500">
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />{label}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {/* transport */}
      <div className="mt-2 flex items-center gap-3">
        <button onClick={toggle}
          className="flex h-8 w-8 items-center justify-center rounded-full bg-violet-500 text-white shadow-[0_0_14px_rgba(139,92,246,0.5)] hover:bg-violet-400">
          {playing ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}
        </button>
        <span className="font-mono text-[11px] text-slate-300">{fmt(cur)} <span className="text-slate-600">/ {fmt(total)}</span></span>
        <button
          onClick={toggleZh}
          className={cn(
            "ml-auto rounded-md border px-2 py-0.5 text-[10.5px] transition-colors",
            showZh
              ? "border-violet-400/40 bg-violet-500/15 text-violet-200"
              : "border-white/10 text-slate-500 hover:text-slate-300",
          )}
          title={showZh ? "隐藏中文段落名" : "同时显示中文段落名（前奏 / 主歌 …）"}
        >
          中
        </button>
        <button
          onClick={() => { const a = audioRef.current; if (a) { a.muted = !a.muted; setMuted(a.muted); } }}
          className="text-slate-400 hover:text-slate-200"
          title={muted ? "取消静音" : "静音"}
        >
          {muted ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
        </button>
      </div>
    </div>
  );
}

/** Compact, non-interactive colored section timeline for the annotation tables.
 *  Renders "Intro 00:00-00:47, Verse 1 …" as a proportional bar (segment width =
 *  duration, color = musical role) plus a wrapped color-keyed legend. Falls back
 *  to plain text when the string isn't in the "Name start–end" format. */
export function SectionsBar({ text, zh = false }: { text: string; zh?: boolean }) {
  const secs = parseSectionTimes(text);
  if (secs.length === 0) return <span>{text}</span>;
  const t0 = secs[0].start;
  const span = secs[secs.length - 1].end - t0 || 1;
  return (
    <div>
      <div className="relative h-2.5 w-full overflow-hidden rounded-full bg-white/[0.06]">
        {secs.map((s, i) => (
          <div
            key={i}
            className="absolute inset-y-0"
            style={{
              left: `${((s.start - t0) / span) * 100}%`,
              width: `${((s.end - s.start) / span) * 100}%`,
              background: sectionColor(s.name, i),
            }}
            title={`${s.name} · ${fmt(s.start)}–${fmt(s.end)}`}
          />
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-2.5 gap-y-1">
        {secs.map((s, i) => (
          <span key={i} className="inline-flex items-center gap-1 text-[10px] leading-none"
            style={{ color: sectionColor(s.name, i) }}
            title={`${fmt(s.start)}–${fmt(s.end)}`}>
            <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: sectionColor(s.name, i) }} />
            {zh ? sectionZh(s.name) : s.name}
          </span>
        ))}
      </div>
    </div>
  );
}

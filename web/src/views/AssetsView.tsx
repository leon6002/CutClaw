import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  AudioLines, BookOpen, Bot, Camera, ClipboardList, Code2, Cpu, Database, Drama,
  Eye, FileText, Film, FolderOpen, Images, Layers, Lightbulb, Loader2,
  Music2, Pin, Play, RefreshCw, ScanSearch, Search, SearchCode, Settings2, Sparkles, Tags,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Accordion, AccordionContent, AccordionItem, AccordionTrigger,
} from "@/components/ui/accordion";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { api, mediaUrl, useJob } from "../api";
import { RoleModelSelect } from "../components/ModelConfig";
import JobLog from "../components/JobLog";
import AgentFlow from "../components/AgentFlow";
import { AudioKeypointsChart, QualityCurve } from "../components/Charts";
import AudioTimeline, { parseSectionTimes, SectionsBar } from "../components/flow/AudioTimeline";
import TaskGrids from "../components/TaskGrids";
import AgentWorkbench from "../components/AgentWorkbench";
import LocalGpuPanel from "../components/LocalGpuPanel";
import type { ProjectState } from "../App";

const SELECT_STEPS = [
  { key: "load_index", label: "读取素材索引", icon: <Database className="h-4 w-4" /> },
  { key: "build_prompt", label: "构建选材任务", icon: <FileText className="h-4 w-4" /> },
  { key: "llm_select", label: "Agent 决策", icon: <Bot className="h-4 w-4" /> },
  { key: "parse", label: "解析选择", icon: <SearchCode className="h-4 w-4" /> },
  { key: "slideshow", label: "图片幻灯片", icon: <Images className="h-4 w-4" /> },
  { key: "apply", label: "应用到项目", icon: <Pin className="h-4 w-4" /> },
];

export interface Asset {
  file_path: string;
  file_name?: string;
  absolute_path?: string;
  asset_type: "video" | "image" | "audio";
  content_hash: string;
  duration_sec?: number;
  width?: number;
  height?: number;
  file_size_mb?: number;
  annotated: boolean;
  annotation?: Record<string, any>;
  /** parallel local-VLM track (annotations_local.json) */
  annotated_local?: boolean;
  annotation_local?: Record<string, any>;
  /** journey metadata from the original recording */
  capture_time?: string | null;
  location?: string | null;
}

/** "12-13 15:02" from an ISO capture time */
export function fmtCapture(ct?: string | null): string {
  if (!ct) return "";
  const m = ct.match(/^\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  return m ? `${m[1]}-${m[2]} ${m[3]}:${m[4]}` : "";
}

// ── helpers ────────────────────────────────────────────────────────────────

export function parseTs(raw: any): number | null {
  if (raw === null || raw === undefined) return null;
  let s = String(raw).trim();
  const dash = s.split(/\s*[-–~]\s*/);
  if (dash.length > 1) s = dash[0];
  if (/^[\d.]+$/.test(s)) return parseFloat(s);
  const parts = s.split(":").map((x) => parseFloat(x));
  if (parts.some((x) => isNaN(x))) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return null;
}

function qBadgeCls(q: number): string {
  if (q >= 8) return "border-emerald-500/40 bg-emerald-500/10 text-emerald-400";
  if (q >= 6) return "border-amber-500/40 bg-amber-500/10 text-amber-400";
  if (q >= 4) return "border-orange-500/40 bg-orange-500/10 text-orange-400";
  return "border-red-500/40 bg-red-500/10 text-red-400";
}

const TAG_CLS = "border-white/15 bg-white/[0.06] text-slate-300";
const NEW_CLS = "border-amber-500/40 bg-amber-500/10 text-amber-400";

const FIELD_LABELS: Record<string, string> = {
  summary: "摘要", emotion: "情绪", tags: "标签", visual_tags: "视觉标签",
  scene_types: "场景类型", camera_movement: "运镜", time_of_day: "时间段",
  key_colors: "主色调", suggested_use: "建议用途", has_people: "有人物",
  genre: "曲风", energy_level: "能量", bpm: "BPM", quality_score: "质量分",
  mood: "氛围", instruments: "乐器", vocals: "人声", tempo: "节奏",
  description: "描述", subject: "主体", style: "风格",
};

function fmtVal(v: any): string {
  if (v === null || v === undefined || v === "") return "—";
  if (Array.isArray(v)) return v.map(String).join("、") || "—";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(1);
  if (typeof v === "object") return JSON.stringify(v, null, 1);
  return String(v);
}

function EmptyHint({ children }: { children: React.ReactNode }) {
  return <div className="py-10 text-center text-sm text-slate-500">{children}</div>;
}

// ── color swatches (key_colors) — click a swatch to copy its hex ────────────

function ColorSwatch({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // clipboard API needs a secure context — fall back to a temp textarea
      const ta = document.createElement("textarea");
      ta.value = value; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch { /* ignore */ }
      document.body.removeChild(ta);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button" onClick={copy}
          className="inline-flex items-center gap-1.5 rounded-md border border-white/10 bg-white/[0.04] py-1 pr-2 pl-1 transition-colors hover:border-white/30"
        >
          <span className="h-4 w-4 shrink-0 rounded-sm border border-white/25"
            style={{ backgroundColor: value }} />
          <span className="font-mono text-[11px] text-slate-300">{copied ? "已复制 ✓" : value}</span>
        </button>
      </TooltipTrigger>
      <TooltipContent>点击复制 {value}</TooltipContent>
    </Tooltip>
  );
}

function ColorSwatches({ colors }: { colors: any[] }) {
  const list = colors.filter((c) => typeof c === "string" && c.trim());
  if (list.length === 0) return <span className="text-slate-500">—</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {list.map((c, i) => <ColorSwatch key={i} value={c.trim()} />)}
    </div>
  );
}

function AnnotationTable({ ann }: { ann: Record<string, any> }) {
  const entries = Object.entries(ann).filter(([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && v.length === 0));
  const known = entries.filter(([k]) => FIELD_LABELS[k]);
  const unknown = entries.filter(([k]) => !FIELD_LABELS[k]);
  const rows = [...known, ...unknown];
  if (rows.length === 0) return <EmptyHint>没有标注字段</EmptyHint>;
  return (
    <table className="kv-table">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <td className="kv-key">{FIELD_LABELS[k] ?? k}</td>
            <td className="kv-val">
              {k === "key_colors" && Array.isArray(v)
                ? <ColorSwatches colors={v} />
                : k === "sections_summary" && typeof v === "string"
                  ? <SectionsBar text={v} />
                  : fmtVal(v)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── per-clip detail (the annotation inspector core) ───────────────────────

function SeekBtn({ label, sec, onSeek }: { label: string; sec: number | null; onSeek: (s: number) => void }) {
  if (sec === null) return <span className="text-xs text-slate-500">{label}</span>;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="outline" size="sm"
          className="h-6 gap-1 border-white/10 bg-white/[0.04] px-2 font-mono text-[11px]"
          onClick={() => onSeek(sec)}>
          <Play className="h-3 w-3 text-cyan-400" />{label}
        </Button>
      </TooltipTrigger>
      <TooltipContent>跳转播放器到此时间点</TooltipContent>
    </Tooltip>
  );
}

function DenseSegment({ seg, onSeek }: { seg: any; onSeek: (s: number) => void }) {
  const ts = seg.timestamp ?? "";
  const tsAbs = seg.timestamp_absolute ?? "";
  const sec = parseTs(tsAbs || ts);
  const vq = typeof seg.visual_quality === "object" ? seg.visual_quality?.score : seg.visual_quality;
  const emo = typeof seg.emotion === "object" ? seg.emotion?.mood : seg.emotion;
  return (
    <div className="dense-seg">
      <div className="flex flex-wrap items-center gap-1.5">
        <SeekBtn label={String(tsAbs || ts)} sec={sec} onSeek={onSeek} />
        {vq !== undefined && vq !== null && (
          <Badge variant="outline" className={cn("text-[11px]", qBadgeCls(Number(vq)))}>画质 {vq}</Badge>
        )}
        {emo && (
          <Badge variant="outline" className="border-violet-500/40 bg-violet-500/10 text-[11px] text-violet-300">
            <Drama className="mr-1 h-3 w-3" />{emo}
          </Badge>
        )}
      </div>
      {seg.content_description && <p className="mt-1 text-[13px] text-slate-300">{seg.content_description}</p>}
      {seg.editor_recommendation && (
        <p className="mt-0.5 text-xs text-slate-400">
          <Lightbulb className="mr-1 inline h-3 w-3 -translate-y-px text-amber-400" />{seg.editor_recommendation}
        </p>
      )}
      {Object.entries(seg)
        .filter(([k]) => !["timestamp", "timestamp_absolute", "content_description", "visual_quality", "emotion", "editor_recommendation"].includes(k))
        .map(([k, v]) => (
          <p key={k} className="mt-0.5 text-xs text-slate-500">{k}: {fmtVal(v)}</p>
        ))}
    </div>
  );
}

function ClipPanel({ clip, onSeek }: { clip: any; onSeek: (s: number) => void }) {
  const dur = clip.duration ?? {};
  const start = dur.clip_start_time ?? clip.start_time ?? "?";
  const end = dur.clip_end_time ?? clip.end_time ?? "?";
  const startSec = parseTs(start);
  const action = clip.action_atoms ?? {};
  const narrative = clip.narrative_analysis ?? {};
  const cine = clip.cinematography ?? {};
  const dense: any[] = Array.isArray(clip.dense_segments) ? clip.dense_segments : [];

  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <SeekBtn label={`${start} → ${end}`} sec={startSec} onSeek={onSeek} />
        {cine.shot_scale && <Badge variant="outline" className={cn("text-[11px]", TAG_CLS)}>{cine.shot_scale}</Badge>}
        {cine.camera_movement && (
          <Badge variant="outline" className={cn("text-[11px]", TAG_CLS)}>
            <Camera className="mr-1 h-3 w-3" />{cine.camera_movement}
          </Badge>
        )}
        {narrative.mood && (
          <Badge variant="outline" className="border-violet-500/40 bg-violet-500/10 text-[11px] text-violet-300">
            <Drama className="mr-1 h-3 w-3" />{narrative.mood}
          </Badge>
        )}
      </div>
      {action.event_summary && <p className="mb-1.5 text-[13px] text-slate-300">{action.event_summary}</p>}
      {narrative.narrative_role && (
        <p className="mb-1.5 text-xs text-slate-400">叙事角色：{narrative.narrative_role}</p>
      )}
      {dense.length > 0 && (
        <>
          <div className="text-xs text-slate-400">{dense.length} 个时间片段：</div>
          {dense.map((seg, i) => <DenseSegment key={i} seg={seg} onSeek={onSeek} />)}
        </>
      )}
      <details className="mt-2">
        <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-300">原始 JSON</summary>
        <pre className="rawjson mt-1.5">{JSON.stringify(clip, null, 2)}</pre>
      </details>
    </div>
  );
}

// ── detail sheet ────────────────────────────────────────────────────────────

function DetailView({
  asset, onClose, onReannotate, busy, annMeta, onPrev, onNext, pos,
}: {
  asset: Asset | null; onClose: () => void; onReannotate: (a: Asset) => void;
  busy: boolean; annMeta?: Record<string, any>;
  onPrev?: () => void; onNext?: () => void; pos?: string;
}) {
  const [details, setDetails] = useState<{
    clips: any[]; scenes: any[]; sound_highlights?: any[];
    highlight_pool?: any[]; highlight_pool_version?: number;
    highlight_pool_progress?: { done: number; total: number; note?: string };
  } | null>(null);
  const [poolBuilding, setPoolBuilding] = useState(false);
  const [loading, setLoading] = useState(false);
  // annotation track: cloud (API) vs local VLM — two parallel, persisted results
  const [track, setTrack] = useState<"cloud" | "local">("cloud");
  const [shlBusy, setShlBusy] = useState(false);
  // VAD sensitivity: lower = more sensitive + wider segments
  const [shlThr, setShlThr] = useState(() => Number(localStorage.getItem("cutclaw_shl_thr") || 0.5));
  const videoRef = useRef<HTMLVideoElement>(null);
  // AudioTimeline owns its own <audio>; this lets the beats chart seek it.
  const audioSeekRef = useRef<((s: number) => void) | null>(null);
  // live playback time reported by AudioTimeline → drives the beats chart playhead
  const [audioTime, setAudioTime] = useState(0);

  const buildPool = async (force = false) => {
    if (!asset) return;
    setPoolBuilding(true);
    try {
      const r = await api<{ status: string }>("/api/assets/highlight_pool", {
        method: "POST",
        body: JSON.stringify({ content_hash: asset.content_hash, force }),
      });
      if (r.status === "ready") {
        const d = await api<any>(`/api/assets/${asset.content_hash}/details`);
        setDetails(d);
        setPoolBuilding(false);
      }
      // "building" → the polling effect below picks it up
    } catch {
      setPoolBuilding(false);
    }
  };

  // poll while the background scorer runs (first build measures every segment)
  useEffect(() => {
    if (!poolBuilding || !asset) return;
    const t = window.setInterval(async () => {
      try {
        const d = await api<any>(`/api/assets/${asset.content_hash}/details${track === "local" ? "?variant=local" : ""}`);
        if (d.highlight_pool && (d.highlight_pool_version ?? 1) >= 2) {
          setDetails(d);
          setPoolBuilding(false);
        } else if (d.highlight_pool_progress) {
          // surface live progress without clobbering the rest of the view
          setDetails((prev) => ({ ...(prev ?? d), highlight_pool_progress: d.highlight_pool_progress }));
        }
      } catch { /* keep polling */ }
    }, 2500);
    return () => window.clearInterval(t);
  }, [poolBuilding, asset?.content_hash]);

  const detectHighlights = async (thr = shlThr) => {
    if (!asset) return;
    setShlBusy(true);
    try {
      const r = await api<{ segments: any[] }>("/api/assets/sound_highlights", {
        method: "POST",
        body: JSON.stringify({
          content_hash: asset.content_hash,
          path: asset.absolute_path || asset.file_path,
          threshold: thr,
        }),
      });
      setDetails((d) => ({ ...(d ?? { clips: [], scenes: [] }), sound_highlights: r.segments }));
    } catch { /* surfaced by the empty state */ }
    setShlBusy(false);
  };

  useEffect(() => { setTrack("cloud"); }, [asset?.content_hash]);

  useEffect(() => {
    setDetails(null);
    if (!asset || asset.asset_type === "image") return;
    setLoading(true);
    api<any>(`/api/assets/${asset.content_hash}/details${track === "local" ? "?variant=local" : ""}`)
      .then(setDetails).catch(() => {}).finally(() => setLoading(false));
  }, [asset?.content_hash, track]);

  // full-page view: Esc = back, arrow keys = prev/next asset
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft" && onPrev) onPrev();
      else if (e.key === "ArrowRight" && onNext) onNext();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onPrev, onNext]);

  if (!asset) return null;
  const src = mediaUrl(asset.absolute_path || asset.file_path);
  const seek = (s: number) => {
    // audio uses the custom AudioTimeline player (no shared DOM ref)
    if (asset.asset_type === "audio" && audioSeekRef.current) { audioSeekRef.current(s); return; }
    const v = videoRef.current;
    if (v) { v.currentTime = Math.max(0, s); v.play().catch(() => {}); }
  };
  const clips = details?.clips ?? [];
  const scenes = details?.scenes ?? [];
  const onLocal = track === "local";
  const ann = (onLocal ? asset.annotation_local : asset.annotation) ?? {};
  const hasAnn = onLocal ? !!asset.annotated_local : asset.annotated;
  // timed sections for the waveform player (parsed from sections_summary)
  const audioSections = asset.asset_type === "audio" && typeof ann.sections_summary === "string"
    ? parseSectionTimes(ann.sections_summary) : [];
  const audioDur = typeof ann.duration_sec === "number" && ann.duration_sec > 0
    ? ann.duration_sec
    : (asset.duration_sec || (audioSections.length ? audioSections[audioSections.length - 1].end : 0));

  return (
    <div>
      {/* full-page header: back / title / nav / actions */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" className="h-8 gap-1.5 border-white/10 bg-white/[0.04] text-xs"
          onClick={onClose} title="返回素材库 (Esc)">
          ← 返回素材库
        </Button>
        <span className="max-w-[420px] truncate text-sm font-semibold text-slate-200">
          {asset.file_name || asset.file_path}
        </span>
        {hasAnn
          ? <Badge variant="outline" className={qBadgeCls(Number(ann.quality_score ?? 0))}>Q {fmtVal(ann.quality_score)}</Badge>
          : <Badge variant="outline" className={NEW_CLS}>未标注</Badge>}
        {/* annotation-track switcher — cloud API vs local VLM, both persisted */}
        {asset.asset_type === "video" && (
          <div className="ml-1 flex overflow-hidden rounded-lg border border-white/10">
            <button
              className={cn("px-2.5 py-1 text-[11px] transition-colors",
                !onLocal ? "bg-cyan-500/15 text-cyan-300" : "text-slate-500 hover:text-slate-300")}
              onClick={() => setTrack("cloud")}
            >☁ 云端</button>
            <button
              className={cn("px-2.5 py-1 text-[11px] transition-colors",
                onLocal ? "bg-violet-500/15 text-violet-300" : "text-slate-500 hover:text-slate-300")}
              title={asset.annotated_local ? "查看本地 VLM 标注结果" : "该视频还没有本地 VLM 标注 — 卡片上点「本地」"}
              onClick={() => setTrack("local")}
            >🖥 本地</button>
          </div>
        )}
        {pos && <span className="text-xs text-slate-500">{pos}</span>}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-8 border-white/10 bg-white/[0.04] px-2.5 text-xs"
            disabled={!onPrev} onClick={onPrev} title="上一个 (←)">←</Button>
          <Button variant="outline" size="sm" className="h-8 border-white/10 bg-white/[0.04] px-2.5 text-xs"
            disabled={!onNext} onClick={onNext} title="下一个 (→)">→</Button>
          <Button variant="outline" size="sm"
            className="h-8 gap-1.5 border-white/10 bg-white/[0.04] text-xs"
            disabled={busy} onClick={() => onReannotate(asset)}>
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
            重新标注
          </Button>
        </div>
      </div>

        {/* in-sheet progress: annotation triggered from here must be visible HERE */}
        {busy && (
          <div className="mb-3 flex items-center gap-2.5 rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-300">
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
            <span className="min-w-0 flex-1 truncate">
              正在标注 {annMeta?.filename || "…"}
              {typeof annMeta?.total === "number" && annMeta.total > 0
                ? ` · ${annMeta.current ?? 0}/${annMeta.total}` : ""}
              — 完成后此面板自动刷新
            </span>
          </div>
        )}

      {/* AUDIO: full-width player, with the beat chart aligned directly below and
          the annotation overview under that — everything stacked & full width */}
      {asset.asset_type === "audio" && (
        <div className="mb-4 space-y-3">
          {audioSections.length > 0
            ? <AudioTimeline
                src={src} sections={audioSections} duration={audioDur}
                seekRef={audioSeekRef} keypointsPath={asset.absolute_path || asset.file_path}
                onTime={(t) => setAudioTime(t)}
              />
            : <audio ref={videoRef as any} src={src} controls className="w-full" />}
          {/* full-width detailed beat chart, aligned under the waveform (no
              horizontal padding so its plot area lines up with the wave) */}
          <div className="rounded-xl border border-white/[0.07] bg-white/[0.02] py-2">
            <AudioKeypointsChart
              path={asset.absolute_path || asset.file_path}
              duration={audioDur} onSeek={seek} playhead={audioTime}
            />
          </div>
          {/* annotation overview */}
          <div className="rounded-xl border border-white/[0.07] bg-white/[0.02] p-3.5">
            <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-slate-300">
              <ClipboardList className="h-3.5 w-3.5 text-cyan-400" /> 标注总览
            </div>
            {hasAnn
              ? <AnnotationTable ann={ann} />
              : <EmptyHint>{onLocal ? "本地轨道暂无标注 — 素材卡片上点「本地」按钮" : "尚未标注 — 点右上角「重新标注」"}</EmptyHint>}
          </div>
          <div className="text-xs text-slate-500">
            {asset.capture_time && <span className="text-slate-400">📅 {fmtCapture(asset.capture_time)} · </span>}
            {asset.duration_sec ? `${Math.round(asset.duration_sec)}s · ` : ""}
            {asset.file_size_mb ? `${asset.file_size_mb.toFixed(1)}MB · ` : ""}
            {asset.absolute_path || asset.file_path}
          </div>
        </div>
      )}

      {/* VIDEO / IMAGE top split: player left, annotation overview right */}
      {asset.asset_type !== "audio" && (
      <div className="mb-4 flex flex-wrap items-start gap-4">
        <div className="min-w-[360px] flex-[3] basis-[520px]">
          {asset.asset_type === "video" && <video ref={videoRef} src={src} controls className="max-h-[480px] w-full rounded-xl bg-black" />}
          {asset.asset_type === "image" && <img src={src} className="max-h-[480px] w-full rounded-xl bg-black object-contain" />}
          <div className="mt-1.5 text-xs text-slate-500">
            {asset.capture_time && <span className="text-slate-400">📅 {fmtCapture(asset.capture_time)} · </span>}
            {asset.location && <span className="text-slate-400">📍 {asset.location} · </span>}
            {asset.duration_sec ? `${Math.round(asset.duration_sec)}s · ` : ""}
            {asset.width ? `${asset.width}×${asset.height} · ` : ""}
            {asset.file_size_mb ? `${asset.file_size_mb.toFixed(1)}MB · ` : ""}
            {asset.absolute_path || asset.file_path}
          </div>

          {/* measured voice/laughter segments — click to LISTEN at that spot */}
          {asset.asset_type === "video" && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] font-semibold text-violet-300">🎙 声音高光</span>
              {/* sensitivity: lower threshold = catches quieter voices + wider ranges */}
              <div className="flex overflow-hidden rounded-md border border-white/10">
                {([[0.3, "灵敏"], [0.4, "较灵敏"], [0.5, "标准"], [0.65, "严格"]] as const).map(([v, label]) => (
                  <button
                    key={v}
                    className={"px-1.5 py-0.5 text-[10px] " + (Math.abs(shlThr - v) < 0.01
                      ? "bg-violet-500/20 text-violet-200"
                      : "text-slate-500 hover:text-slate-300")}
                    title={`VAD 阈值 ${v} — 越低越灵敏、段落范围越宽`}
                    onClick={() => {
                      setShlThr(v); localStorage.setItem("cutclaw_shl_thr", String(v));
                      detectHighlights(v);
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <Button
                variant="outline" size="sm"
                className="h-6 gap-1 border-violet-500/30 bg-violet-500/[0.06] px-2 text-[11px] text-violet-300"
                disabled={shlBusy} onClick={() => detectHighlights()}
                title="用当前灵敏度检测原声里的人声/笑声（同灵敏度的结果有缓存）"
              >
                {shlBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                {shlBusy ? "检测中…" : details?.sound_highlights === undefined ? "检测" : "重新检测"}
              </Button>
              {details?.sound_highlights !== undefined && (details.sound_highlights.length === 0 ? (
                <span className="text-[11px] text-slate-600">未检测到人声/笑声（无人机素材通常没有音轨）</span>
              ) : (
                details.sound_highlights.map((h: any, i: number) => (
                  <button
                    key={i}
                    className="rounded-full border border-violet-400/40 bg-violet-500/10 px-2 py-0.5 text-[11px] text-violet-200 hover:bg-violet-500/25"
                    title={`点击跳到 ${h.start}s 试听 · 强度 ${Math.round((h.strength ?? 0) * 100)}%`}
                    onClick={() => seek(h.start)}
                  >
                    {h.start.toFixed(1)}–{h.end.toFixed(1)}s
                  </button>
                ))
              ))}
              <span className="text-[10px] text-slate-600">— 渲染时这些片段会压低 BGM 放出原声</span>
            </div>
          )}
        </div>
        <div className="min-w-[320px] flex-[2] basis-[360px] rounded-xl border border-white/[0.07] bg-white/[0.02] p-3.5">
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-slate-300">
            <ClipboardList className="h-3.5 w-3.5 text-cyan-400" /> 标注总览
          </div>
          {hasAnn
            ? <AnnotationTable ann={ann} />
            : <EmptyHint>{onLocal ? "本地轨道暂无标注 — 素材卡片上点「本地」按钮" : "尚未标注 — 点右上角「重新标注」"}</EmptyHint>}
        </div>
      </div>
      )}

      <Tabs defaultValue={asset.asset_type === "video" ? "clips" : "raw"}>
          <TabsList className="bg-white/[0.05]">
            {asset.asset_type === "video" && (
              <>
                <TabsTrigger value="clips" className="gap-1.5 text-xs">
                  <Film className="h-3.5 w-3.5" />片段分析 ({clips.length})
                </TabsTrigger>
                <TabsTrigger value="scenes" className="gap-1.5 text-xs">
                  <Layers className="h-3.5 w-3.5" />场景 ({scenes.length})
                </TabsTrigger>
                <TabsTrigger value="pool" className="gap-1.5 text-xs">
                  <Sparkles className="h-3.5 w-3.5" />高光评分{details?.highlight_pool ? ` (${details.highlight_pool.length})` : ""}
                </TabsTrigger>
              </>
            )}
            <TabsTrigger value="raw" className="gap-1.5 text-xs">
              <Code2 className="h-3.5 w-3.5" />原始标注
            </TabsTrigger>
          </TabsList>

          {asset.asset_type === "video" && (
            <>
              <TabsContent value="clips" className="pt-3">
                {loading ? <EmptyHint>加载中…</EmptyHint> :
                  clips.length === 0 ? <EmptyHint>没有检测到片段 — VLM 可能超时，试试重新标注</EmptyHint> : (
                    <>
                      <QualityCurve clips={clips} onSeek={seek} />
                      <Accordion type="multiple" defaultValue={clips.map((_, i) => String(i))}>
                        {clips.map((clip, i) => {
                          const d = clip.duration ?? {};
                          return (
                            <AccordionItem key={i} value={String(i)} className="border-white/[0.07]">
                              <AccordionTrigger className="py-2.5 text-[13px] hover:no-underline">
                                <span>
                                  <span className="font-semibold text-slate-200">Clip {i + 1}</span>
                                  <span className="ml-2 font-mono text-xs text-slate-500">
                                    {d.clip_start_time ?? "?"} → {d.clip_end_time ?? "?"}
                                  </span>
                                </span>
                              </AccordionTrigger>
                              <AccordionContent>
                                <ClipPanel clip={clip} onSeek={seek} />
                              </AccordionContent>
                            </AccordionItem>
                          );
                        })}
                      </Accordion>
                    </>
                  )}
              </TabsContent>

              <TabsContent value="scenes" className="pt-3">
                {scenes.length === 0 ? <EmptyHint>没有场景分析</EmptyHint> : (
                  <div>
                    {scenes.map((scene, i) => {
                      const va = scene.video_analysis?.scene_caption ?? {};
                      const sc = va.scene_summary ?? va.visual_analysis ?? {};
                      const summary = typeof sc === "object" ? (sc.summary ?? sc.narrative ?? "") : String(sc);
                      const cls = va.scene_classification ?? {};
                      const tr = scene.time_range ?? {};
                      const sec = parseTs(tr.start_seconds);
                      return (
                        <div key={i} className="mb-2.5 rounded-xl border border-white/[0.07] bg-white/[0.03] p-3">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-[13px] font-semibold text-slate-200">Scene {i + 1}</span>
                            <SeekBtn label={`${tr.start_seconds ?? "?"}s → ${tr.end_seconds ?? "?"}s`} sec={sec} onSeek={seek} />
                            {cls.is_usable !== undefined && (
                              <Badge variant="outline" className={cls.is_usable
                                ? "border-emerald-500/40 bg-emerald-500/10 text-[11px] text-emerald-400"
                                : "border-red-500/40 bg-red-500/10 text-[11px] text-red-400"}>
                                {cls.is_usable ? "可用" : "不可用"}
                              </Badge>
                            )}
                            {cls.importance_score !== undefined && (
                              <Badge variant="outline" className={cn("text-[11px]", qBadgeCls(Number(cls.importance_score)))}>
                                重要度 {cls.importance_score}
                              </Badge>
                            )}
                          </div>
                          {summary && <p className="mt-1.5 text-[13px] text-slate-300">{String(summary)}</p>}
                          <details className="mt-1.5">
                            <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-300">原始 JSON</summary>
                            <pre className="rawjson mt-1.5">{JSON.stringify(scene, null, 2)}</pre>
                          </details>
                        </div>
                      );
                    })}
                  </div>
                )}
              </TabsContent>

              <TabsContent value="pool" className="pt-3">
                <div className="mb-3 rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 text-[11px] leading-relaxed text-slate-400">
                  总分 = <b className="text-cyan-300">40% 内容质量</b>（VLM 评画面构图/光线/叙事价值，附评语）
                  + <b className="text-cyan-300">40% 实测画质</b>（从真实帧测量：清晰度相对<b>本片自身基线</b>的比值 + 光流紊乱度——运动模糊是剧烈晃动的物理指纹，非 AI 猜测）
                  + <b className="text-violet-300">15% 声音高光</b>（原声含真实人声/笑声）
                  + <b className="text-slate-300">5% 有人物</b>。
                  内容 &lt;3/5 或实测画质 &lt;3.5/10 的片段直接淘汰不入池。选材优先制按此排名把镜头锚定在真实瞬间上。
                  <span className="text-slate-500">
                    评分本身<b className="text-emerald-400">不调用任何模型 API</b>（本地/云端都不调）：VLM 分数读取标注时已缓存的结果，画质与人声均为本机计算。
                  </span>
                </div>
                {!details?.highlight_pool ? (
                  <div className="py-6 text-center">
                    <Button
                      variant="outline"
                      className="gap-1.5 border-cyan-500/30 bg-cyan-500/[0.08] text-xs text-cyan-300"
                      disabled={poolBuilding} onClick={() => buildPool()}
                    >
                      {poolBuilding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                      {poolBuilding ? "评分中…" : "构建高光评分"}
                    </Button>
                    {poolBuilding && (
                      <div className="mt-3 text-xs text-slate-400">
                        {details?.highlight_pool_progress ? (
                          <>
                            <span className="text-cyan-300">
                              {details.highlight_pool_progress.done}/{details.highlight_pool_progress.total} 段
                            </span>
                            {details.highlight_pool_progress.note && (
                              <span className="ml-2 text-slate-500">{details.highlight_pool_progress.note}</span>
                            )}
                            <div className="mx-auto mt-1.5 h-1.5 w-56 overflow-hidden rounded-full bg-white/[0.08]">
                              <div className="h-full bg-cyan-400 transition-all"
                                style={{ width: `${Math.min(100, details.highlight_pool_progress.done / Math.max(1, details.highlight_pool_progress.total) * 100)}%` }} />
                            </div>
                          </>
                        ) : (
                          <span className="text-slate-500">启动中…（逐段实测画质，长视频约 1-3 分钟，纯本地计算）</span>
                        )}
                      </div>
                    )}
                  </div>
                ) : (
                  <>
                    {(details.highlight_pool_version ?? 1) < 2 && (
                      <div className="mb-2 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[11px] text-amber-300">
                        旧版评分（缺评分理由明细）
                        <button className="underline hover:text-amber-200" disabled={poolBuilding}
                          onClick={() => buildPool(true)}>
                          {poolBuilding ? "重建中…" : "重建"}
                        </button>
                      </div>
                    )}
                    <div className="max-h-[560px] space-y-1.5 overflow-y-auto pr-1">
                      {details.highlight_pool.map((m: any, i: number) => (
                        <div key={i}
                          className="cursor-pointer rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 hover:border-cyan-500/30"
                          onClick={() => seek(m.start)} title="点击跳转试看"
                        >
                          <div className="flex flex-wrap items-center gap-2">
                            <span className={cn("text-[11px] font-bold", i < 5 ? "text-amber-300" : "text-slate-500")}>#{i + 1}</span>
                            <span className="tabular-nums text-[11px] text-slate-400">{m.start?.toFixed(1)}–{m.end?.toFixed(1)}s ({m.duration?.toFixed(1)}s)</span>
                            <Badge variant="outline" className={cn("text-[11px]", qBadgeCls(Number(m.score ?? 0) * 10))}>
                              总分 {(Number(m.score ?? 0) * 10).toFixed(1)}
                            </Badge>
                            {m.sound && <Badge variant="outline" className="border-violet-500/40 bg-violet-500/10 text-[10px] text-violet-300">🎙 人声 +15%</Badge>}
                            {m.people && <Badge variant="outline" className="border-white/15 bg-white/[0.05] text-[10px] text-slate-300">👤 有人 +5%</Badge>}
                          </div>
                          <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10.5px] text-slate-400">
                            <span className="flex items-center gap-1.5">
                              内容 {Number(m.vlm_q ?? 0).toFixed(0)}/5
                              <span className="inline-block h-1.5 w-14 overflow-hidden rounded-full bg-white/[0.08]">
                                <span className="block h-full bg-cyan-400" style={{ width: `${Math.min(100, Number(m.vlm_q ?? 0) / 5 * 100)}%` }} />
                              </span>
                            </span>
                            <span className="flex items-center gap-1.5">
                              实测画质 {Number(m.stability) >= 0 ? `${Number(m.stability).toFixed(1)}/10` : "未测"}
                              <span className="inline-block h-1.5 w-14 overflow-hidden rounded-full bg-white/[0.08]">
                                <span className="block h-full bg-emerald-400" style={{ width: `${Math.max(0, Math.min(100, Number(m.stability ?? 0) * 10))}%` }} />
                              </span>
                              {m.stability_detail?.rel_sharp !== undefined && m.stability_detail?.rel_sharp !== null && (
                                <span className="text-slate-600">清晰度 {(Number(m.stability_detail.rel_sharp) * 100).toFixed(0)}% 基线 · 紊乱 {Number(m.stability_detail.disorder ?? 0).toFixed(2)}</span>
                              )}
                            </span>
                          </div>
                          <p className="mt-1 line-clamp-2 text-[11.5px] text-slate-300">{m.desc}</p>
                          {m.vlm_notes && (
                            <p className="mt-0.5 line-clamp-1 text-[10.5px] italic text-slate-500">评语：{m.vlm_notes}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </TabsContent>
            </>
          )}

          <TabsContent value="raw" className="pt-3">
            <pre className="rawjson">{JSON.stringify(ann, null, 2)}</pre>
          </TabsContent>
        </Tabs>
    </div>
  );
}

// ── asset card ──────────────────────────────────────────────────────────────

const STAGE_LABELS: Record<string, string> = {
  shot_detection: "镜头检测", captioning: "片段理解", dense_caption: "密集描述",
  scene_merge: "场景合并", scene_analysis: "场景分析",
  // audio annotation stages (madmom pipeline)
  beat_detect: "节奏检测", audio_facts: "节奏事实", sectioning: "段落划分", seg_caption: "分段描述",
};

function AssetCard({ a, onOpen, index, picked, onTogglePick, annotating, queued, queuedLocal, annStage, annStageDetail, onAnnotate, onAnnotateLocal, annBusy, hearted, onToggleHeart }: {
  a: Asset; onOpen: () => void; index: number;
  picked?: boolean; onTogglePick?: () => void;
  /** asset-level ❤️ — 全局口味,红心素材选材权重稍高 */
  hearted?: boolean; onToggleHeart?: () => void;
  /** this exact asset is currently being annotated (hash-keyed job state) */
  annotating?: boolean;
  /** waiting in the current annotation batch */
  queued?: boolean;
  /** queued via the server-side chain (clicked while another batch runs) */
  queuedLocal?: boolean;
  /** current pipeline stage of THIS asset's annotation */
  annStage?: string;
  /** stage detail, e.g. "42%" during shot detection */
  annStageDetail?: string;
  /** start (re-)annotation of this asset */
  onAnnotate?: () => void;
  /** start (re-)annotation with the LOCAL VLM (parallel track, videos only) */
  onAnnotateLocal?: () => void;
  /** any annotation job is running (disables the button) */
  annBusy?: boolean;
}) {
  const ann = a.annotation ?? {};
  const src = mediaUrl(a.absolute_path || a.file_path);
  const [thumbFailed, setThumbFailed] = useState(false);
  const tags: string[] = [
    ...(Array.isArray(ann.tags) ? ann.tags : []),
    ...(Array.isArray(ann.visual_tags) ? ann.visual_tags : []),
  ].slice(0, 4);
  const dur = a.duration_sec ? `${Math.round(a.duration_sec)}s` : "";

  return (
    <motion.div
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, delay: Math.min(index * 0.04, 0.5) }}
    >
      <Card
        className={cn(
          "group relative cursor-pointer gap-0 overflow-hidden rounded-2xl border-white/[0.07] bg-slate-900/50 py-0 transition-all hover:border-cyan-500/30 hover:shadow-[0_0_20px_rgba(34,211,238,0.08)]",
          picked && "border-cyan-400/60 shadow-[0_0_16px_rgba(34,211,238,0.18)]",
        )}
        style={{ backdropFilter: "none", WebkitBackdropFilter: "none" }}
        onClick={onOpen}
        title="点击查看完整标注"
      >
        {/* status stripe: annotation state at a glance across the whole wall */}
        <div className={cn(
          "absolute inset-x-0 top-0 z-10 h-0.5",
          annotating ? "animate-pulse bg-cyan-400"
            : a.annotated ? "bg-emerald-500/60"
              : "bg-amber-500/60",
        )} />
        {onTogglePick && (
          <button
            title={picked ? "取消选择" : a.asset_type === "audio" ? "选为项目音乐" : "加入项目素材"}
            className={cn(
              "absolute top-2.5 left-2 z-10 flex h-6 w-6 items-center justify-center rounded-full border text-xs font-bold transition-all",
              picked
                ? "border-cyan-300 bg-cyan-400 text-slate-950 shadow-[0_0_10px_rgba(34,211,238,0.6)]"
                : "border-white/30 bg-black/50 text-transparent hover:border-cyan-300 hover:text-cyan-300",
            )}
            onClick={(e) => { e.stopPropagation(); onTogglePick(); }}
          >
            ✓
          </button>
        )}

        {/* poster area — no native players in the grid; the detail sheet plays */}
        <div className="relative flex aspect-video items-center justify-center overflow-hidden bg-gradient-to-br from-slate-900 to-slate-950">
          {a.asset_type === "image" && (
            <img src={src} loading="lazy" className="h-full w-full object-cover" />
          )}
          {a.asset_type === "video" && (!thumbFailed ? (
            <img
              src={`/api/assets/thumb?hash=${a.content_hash}&path=${encodeURIComponent(a.absolute_path || a.file_path)}`}
              loading="lazy" className="h-full w-full object-cover"
              onError={() => setThumbFailed(true)}
            />
          ) : (
            <Film className="h-10 w-10 text-slate-700" />
          ))}
          {a.asset_type === "audio" && <Waveform seed={a.content_hash} />}

          {a.asset_type !== "image" && (
            <span className="absolute inset-0 flex items-center justify-center opacity-0 transition-opacity group-hover:opacity-100">
              <span className="flex h-11 w-11 items-center justify-center rounded-full border border-white/20 bg-black/60 backdrop-blur-sm">
                <Play className="ml-0.5 h-4.5 w-4.5 text-white" />
              </span>
            </span>
          )}
          {dur && (
            <span className="absolute right-1.5 bottom-1.5 rounded bg-black/65 px-1.5 py-0.5 font-mono text-[10.5px] text-slate-200">
              {dur}
            </span>
          )}
          {onToggleHeart && (
            <button
              title={hearted ? "取消红心" : "红心:我喜欢这个素材(选材权重稍微提高,全局生效)"}
              className={cn(
                "absolute bottom-1.5 left-1.5 z-10 flex h-6 w-6 items-center justify-center rounded-full border text-[13px] transition-all",
                hearted
                  ? "border-rose-400/70 bg-rose-500/25 shadow-[0_0_10px_rgba(244,63,94,0.45)]"
                  : "border-white/25 bg-black/50 opacity-0 grayscale group-hover:opacity-100 hover:grayscale-0",
              )}
              onClick={(e) => { e.stopPropagation(); onToggleHeart(); }}
            >
              ❤️
            </button>
          )}
          {annotating && (
            <span className="absolute top-2 right-1.5 flex items-center gap-1 rounded-full border border-cyan-400/50 bg-black/70 px-2 py-0.5 text-[10.5px] text-cyan-300">
              <Loader2 className="h-3 w-3 animate-spin" /> 标注中
            </span>
          )}
          {queued && !annotating && (
            <span className="absolute top-2 right-1.5 rounded-full border border-amber-400/40 bg-black/70 px-2 py-0.5 text-[10.5px] text-amber-300">
              排队中
            </span>
          )}
          {/* live stage strip on the running card: 镜头检测 42% → 片段理解 → … */}
          {annotating && (
            <div className="absolute inset-x-0 bottom-0 flex items-center gap-1.5 bg-gradient-to-t from-black/85 to-transparent px-2.5 pt-5 pb-1.5">
              <Loader2 className="h-3 w-3 shrink-0 animate-spin text-cyan-400" />
              <span className="truncate text-[10.5px] text-cyan-200">
                {(STAGE_LABELS[annStage ?? ""] ?? annStage ?? "分析中")}
                {annStageDetail && /^\d+%$/.test(annStageDetail) ? ` ${annStageDetail}` : "…"}
              </span>
            </div>
          )}
        </div>

        <CardContent className="p-3">
          <div className="truncate text-[13px] font-semibold text-slate-200" title={a.absolute_path || a.file_path}>
            {a.file_name || a.file_path}
          </div>
          <div className="mt-0.5 text-[11.5px] text-slate-500">
            {a.width ? `${a.width}×${a.height} · ` : ""}
            {a.file_size_mb ? `${a.file_size_mb.toFixed(1)}MB` : ""}
          </div>
          {(a.capture_time || a.location) && (
            <div className="mt-0.5 truncate text-[11px] text-slate-500">
              {a.capture_time && <span>📅 {fmtCapture(a.capture_time)}</span>}
              {a.location && <span className="ml-1.5">📍 {a.location}</span>}
            </div>
          )}
          <div className="mt-1.5 flex flex-wrap gap-1">
            {a.annotated
              ? <Badge variant="outline" className={cn("text-[11px]", qBadgeCls(Number(ann.quality_score ?? 0)))}>Q {fmtVal(ann.quality_score)}</Badge>
              : <Badge variant="outline" className={cn("text-[11px]", NEW_CLS)}>未标注</Badge>}
            {a.annotated_local && (
              <Badge variant="outline" className="border-violet-500/40 bg-violet-500/10 text-[11px] text-violet-300"
                title="本地 VLM 标注（并行轨道）">
                🖥 Q {fmtVal((a.annotation_local ?? {}).quality_score)}
              </Badge>
            )}
            {tags.map((t, i) => (
              <Badge key={i} variant="outline" className={cn("text-[11px]", TAG_CLS)}>{t}</Badge>
            ))}
          </div>
          {ann.summary && (
            <p className="mt-1.5 line-clamp-2 text-xs text-slate-400">{ann.summary}</p>
          )}
          {onAnnotate && (
            <div className="mt-2.5 flex gap-1.5">
              <Button
                variant="outline" size="sm"
                className={cn(
                  "h-7 flex-1 gap-1.5 text-xs",
                  a.annotated
                    ? "border-white/10 bg-white/[0.03] text-slate-400 hover:text-slate-200"
                    : "border-cyan-500/30 bg-cyan-500/[0.08] text-cyan-300 hover:bg-cyan-500/15",
                )}
                disabled={annBusy}
                onClick={(e) => { e.stopPropagation(); onAnnotate(); }}
              >
                {annotating || queued || queuedLocal
                  ? <Loader2 className="h-3 w-3 animate-spin" />
                  : a.annotated ? <RefreshCw className="h-3 w-3" /> : <Tags className="h-3 w-3" />}
                {annotating ? "标注中…" : (queued || queuedLocal) ? "排队中…" : a.annotated ? "重新标注" : "标注"}
              </Button>
              {onAnnotateLocal && (
                <Button
                  variant="outline" size="sm"
                  className="h-7 gap-1 border-violet-500/30 bg-violet-500/[0.08] px-2 text-xs text-violet-300 hover:bg-violet-500/15"
                  disabled={annBusy}
                  title={a.annotated_local ? "用本地 VLM 重新标注（并行轨道，不覆盖云端结果）" : "用本地 VLM 标注（3090，不消耗 API）"}
                  onClick={(e) => { e.stopPropagation(); onAnnotateLocal(); }}
                >
                  <Cpu className="h-3 w-3" />
                  {a.annotated_local ? "本地重标" : "本地"}
                </Button>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </motion.div>
  );
}

/** Decorative deterministic waveform for audio posters (seeded by hash). */
function Waveform({ seed }: { seed: string }) {
  const bars = Array.from({ length: 44 }, (_, i) => {
    const c = seed.charCodeAt((i * 7) % Math.max(1, seed.length)) || 60;
    return 18 + ((c * 31 + i * 17) % 62);
  });
  return (
    <div className="flex h-full w-full items-center justify-center gap-[2.5px] px-6">
      {bars.map((h, i) => (
        <span key={i} className="w-[3px] rounded-full bg-cyan-500/45" style={{ height: `${h}%` }} />
      ))}
    </div>
  );
}

// ── concurrency setting field (reads/writes config.py via /api/config) ──────

function ConcurrencyField({ k, label, hint }: { k: string; label: string; hint: string }) {
  const [val, setVal] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    api<Record<string, string>>("/api/config")
      .then((c) => setVal(String(c[k] ?? ""))).catch(() => {});
  }, [k]);
  const save = async (v: string) => {
    setVal(v);
    const n = parseInt(v, 10);
    if (!Number.isFinite(n) || n < 1 || n > 64) return;
    try {
      await api("/api/config", { method: "PUT", body: JSON.stringify({ values: { [k]: String(n) } }) });
      setSaved(true); window.setTimeout(() => setSaved(false), 1500);
    } catch { /* ignore */ }
  };
  return (
    <div className="mb-1.5 flex items-center gap-2">
      <span className="w-[110px] shrink-0 text-[11px] text-slate-400">{label}</span>
      <Input
        className="h-7 w-16 border-white/10 bg-black/25 text-center text-xs"
        value={val} onChange={(e) => save(e.target.value)}
      />
      {saved ? <span className="text-[10.5px] text-emerald-400">✓ 已保存</span>
        : <span className="truncate text-[10.5px] text-slate-600">{hint}</span>}
    </div>
  );
}

// ── annotation batch progress panel ─────────────────────────────────────────

const ANN_STAGES: Array<[string, string]> = [
  ["shot_detection", "镜头检测"], ["captioning", "片段理解"],
  ["dense_caption", "密集描述"], ["scene_merge", "场景合并"], ["scene_analysis", "场景分析"],
];

function AnnotationProgress({ meta, jobId, onOpenWorkbench }: {
  meta: Record<string, any>; jobId: string | null;
  onOpenWorkbench: (task: string, idx?: number) => void;
}) {
  const files: Record<string, string> = meta.files ?? {};
  const names: Record<string, string> = meta.names ?? {};
  const order = Object.keys(files);
  const doneN = order.filter((h) => files[h] === "d").length;
  const stages: Record<string, string> = meta.file_stages ?? {};
  const hasStages = Object.keys(stages).length > 0;
  const sbh: Record<string, { stage?: string; detail?: string }> = meta.stage_by_hash ?? {};
  const parallelRows = order.filter((h) => files[h] === "r" && sbh[h]?.stage);

  return (
    <div className="mt-3 rounded-xl border border-white/[0.06] bg-black/20 px-3.5 py-3">
      {/* ① batch dots — every file in this run at a glance */}
      <div className="flex flex-wrap items-center gap-2.5">
        <div className="flex flex-wrap gap-1">
          {order.map((h) => (
            <span
              key={h} title={names[h] || h}
              className={cn(
                "h-2.5 w-2.5 rounded-full transition-colors",
                files[h] === "d" ? "bg-emerald-400"
                  : files[h] === "r" ? "animate-pulse bg-cyan-400 ring-2 ring-cyan-400/40"
                    : files[h] === "f" ? "bg-red-400"
                      : "bg-slate-700",
              )}
            />
          ))}
        </div>
        <span className="text-xs text-slate-400">{doneN}/{order.length} 完成</span>
        <span className="min-w-0 flex-1 truncate text-right text-xs font-medium text-cyan-300">
          {meta.filename || ""}
        </span>
      </div>

      {/* ②a parallel mode: one stage line per running file */}
      {parallelRows.length > 0 && (
        <div className="mt-2.5 space-y-1">
          {parallelRows.map((h) => (
            <div key={h} className="flex items-center gap-2 text-[11px]">
              <Loader2 className="h-3 w-3 shrink-0 animate-spin text-cyan-400" />
              <span className="min-w-0 flex-1 truncate text-slate-300">{names[h] || h}</span>
              <span className="shrink-0 text-cyan-300">
                {STAGE_LABELS[sbh[h]?.stage ?? ""] ?? sbh[h]?.stage}
                {sbh[h]?.detail && /%$/.test(sbh[h]!.detail!) ? ` ${sbh[h]!.detail}` : ""}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* ②b sequential mode: stage stepper for the CURRENT file */}
      {parallelRows.length === 0 && hasStages && (
        <div className="mt-2.5 flex flex-wrap items-center gap-y-1">
          {ANN_STAGES.map(([k, label], i) => {
            const st = stages[k];
            const active = st === "running";
            const detail = active && meta.stage === k ? String(meta.stage_detail || "") : "";
            return (
              <div key={k} className="flex items-center">
                {i > 0 && <span className="mx-1 h-px w-3.5 bg-white/10" />}
                <span className={cn(
                  "flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] whitespace-nowrap",
                  st === "done" ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                    : st === "skip" ? "border-white/10 bg-white/[0.03] text-slate-500"
                      : active ? "border-cyan-400/40 bg-cyan-500/10 text-cyan-300"
                        : "border-white/10 bg-white/[0.02] text-slate-600",
                )}>
                  {active && <Loader2 className="h-3 w-3 animate-spin" />}
                  {st === "done" && <span>✓</span>}
                  {st === "skip" && <span title="缓存命中，已跳过">⏭</span>}
                  {label}
                  {detail && /%$/.test(detail) ? ` ${detail}` : ""}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {/* ③ per-file segment grids (reset on every file server-side) */}
      <TaskGrids tasks={meta.tasks ?? {}} jobId={jobId} onOpenWorkbench={onOpenWorkbench} />
    </div>
  );
}

// ── BGM stitch panel: join favorite tracks WITHOUT running the pipeline ─────
// The result is written into the imports dir as a normal audio file — scan
// and it becomes a selectable BGM (the pipeline treats it as one song).
function BgmStitchPanel({ tracks, hearts, onClose }: {
  tracks: Asset[]; hearts: Set<string>; onClose: () => void;
}) {
  const [order, setOrder] = useState<string[]>([]);
  const [ranges, setRanges] = useState<Record<string, { start?: string; end?: string }>>({});
  const [name, setName] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<{ path: string; meta: any; plan?: any[]; why?: string } | null>(null);
  const [err, setErr] = useState("");
  const [mode, setMode] = useState<"ai" | "manual">("ai");
  const [targetSec, setTargetSec] = useState("180");
  // hearted tracks first — they're the ones the user reaches for
  const sorted = [...tracks].sort(
    (a, b) => Number(hearts.has(b.content_hash)) - Number(hearts.has(a.content_hash)));
  const toggle = (h: string) =>
    setOrder((o) => (o.includes(h) ? o.filter((x) => x !== h) : [...o, h]));

  const generate = async () => {
    setRunning(true); setErr(""); setResult(null);
    try {
      const body = {
        name,
        mode,
        target_sec: Number(targetSec) || 180,
        tracks: order.map((h) => {
          const a = tracks.find((t) => t.content_hash === h)!;
          const r = ranges[h] ?? {};
          return {
            path: a.absolute_path || a.file_path,
            start: mode === "manual" && r.start ? Number(r.start) : undefined,
            end: mode === "manual" && r.end ? Number(r.end) : undefined,
          };
        }),
      };
      setResult(await api<any>("/api/bgm/stitch", { method: "POST", body: JSON.stringify(body) }));
    } catch (e: any) {
      setErr(e.message || String(e));
    }
    setRunning(false);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="max-h-[85vh] w-full max-w-xl overflow-y-auto rounded-2xl border border-violet-500/30 bg-slate-900 p-5 shadow-[0_0_40px_rgba(0,0,0,0.6)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 flex items-center gap-2 text-sm font-bold text-violet-300">
          <Music2 className="h-4 w-4" /> BGM 拼接
          <button className="ml-auto text-slate-500 hover:text-slate-300" onClick={onClose}>✕</button>
        </div>
        <div className="mb-2 flex items-center gap-1.5">
          {([["ai", "🤖 AI 融合"], ["manual", "✋ 手动"]] as const).map(([m, lab]) => (
            <button key={m}
              className={cn("rounded-md border px-2.5 py-1 text-[11px] transition-colors",
                mode === m ? "border-violet-400/60 bg-violet-500/20 text-violet-200"
                  : "border-white/10 bg-black/20 text-slate-500 hover:text-slate-300")}
              onClick={() => setMode(m)}>{lab}</button>
          ))}
          {mode === "ai" && (
            <span className="ml-2 flex items-center gap-1.5 text-[11px] text-slate-400">
              目标总长
              <input className="h-6 w-14 rounded border border-white/10 bg-black/30 px-1.5 text-center font-mono text-[11px] text-slate-200 outline-none"
                value={targetSec} onChange={(e) => setTargetSec(e.target.value)} />
              秒
            </span>
          )}
        </div>
        <p className="mb-3 text-[11px] leading-relaxed text-slate-500">
          {mode === "ai"
            ? "点选参与融合的歌曲(需已标注,有节奏分析)。AI 会按实测段落的能量曲线,为每首挑最合适的一段,编排成 开场→铺垫→高潮→收尾 的弧线;衔接点自动吸附小节线、选能量低谷、响度统一。"
            : "按播放顺序点选歌曲(再点取消)。起止秒数可留空=整首;填了也会自动吸附到该曲的小节线上,段与段之间按 2 小节交叉淡化、响度自动统一。"}
          生成的文件存入素材库,重新扫描后即可选为项目音乐。
        </p>
        <div className="space-y-1.5">
          {sorted.map((a) => {
            const idx = order.indexOf(a.content_hash);
            const sel = idx >= 0;
            return (
              <div key={a.content_hash}
                className={cn("rounded-lg border px-2.5 py-1.5 text-xs transition-colors",
                  sel ? "border-violet-400/50 bg-violet-500/10" : "border-white/[0.07] bg-black/20 hover:border-white/20")}>
                <div className="flex cursor-pointer items-center gap-2" onClick={() => toggle(a.content_hash)}>
                  <span className={cn("flex h-5 w-5 shrink-0 items-center justify-center rounded-full border font-mono text-[10px]",
                    sel ? "border-violet-300 bg-violet-400 text-slate-950" : "border-white/25 text-transparent")}>
                    {sel ? idx + 1 : "·"}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-slate-200">{a.file_name || a.file_path}</span>
                  {hearts.has(a.content_hash) && <span className="text-[11px]">❤️</span>}
                  {a.duration_sec ? <span className="font-mono text-[10px] text-slate-500">{Math.round(a.duration_sec)}s</span> : null}
                </div>
                {sel && mode === "manual" && (
                  <div className="mt-1.5 flex items-center gap-2 pl-7 text-[11px] text-slate-400">
                    取
                    <input className="h-6 w-16 rounded border border-white/10 bg-black/30 px-1.5 font-mono text-[11px] text-slate-200 outline-none"
                      placeholder="起(s)" value={ranges[a.content_hash]?.start ?? ""}
                      onChange={(e) => setRanges((r) => ({ ...r, [a.content_hash]: { ...r[a.content_hash], start: e.target.value } }))} />
                    →
                    <input className="h-6 w-16 rounded border border-white/10 bg-black/30 px-1.5 font-mono text-[11px] text-slate-200 outline-none"
                      placeholder="止(s)" value={ranges[a.content_hash]?.end ?? ""}
                      onChange={(e) => setRanges((r) => ({ ...r, [a.content_hash]: { ...r[a.content_hash], end: e.target.value } }))} />
                    <span className="text-slate-600">留空 = 整首</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <div className="mt-3 flex items-center gap-2">
          <input
            className="h-8 flex-1 rounded-md border border-white/10 bg-black/30 px-2 text-xs text-slate-200 outline-none"
            placeholder="文件名(可选,默认 BGMmix_时间戳)"
            value={name} onChange={(e) => setName(e.target.value)}
          />
          <Button
            className="h-8 gap-1.5 bg-violet-500 text-xs font-semibold text-slate-950 hover:bg-violet-400"
            disabled={order.length < 2 || running} onClick={generate}
          >
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Music2 className="h-3.5 w-3.5" />}
            {mode === "ai" ? "AI 编排并拼接" : "生成拼接 BGM"}
          </Button>
        </div>
        {err && <div className="mt-2 text-[11px] text-red-400">{err}</div>}
        {result && (
          <div className="mt-3 rounded-lg border border-emerald-500/30 bg-emerald-500/[0.06] p-3">
            <div className="mb-1.5 text-[11.5px] font-semibold text-emerald-300">
              ✓ 拼好了 · 总长 {result.meta?.total?.toFixed?.(0)}s · 衔接 {result.meta?.joins?.map((j: number) => `${j}s`).join(" / ")}
            </div>
            {(result.plan?.length ?? 0) > 0 && (
              <div className="mb-2 space-y-0.5 text-[11px] text-slate-300">
                {result.plan!.map((p: any, i: number) => (
                  <div key={i}>
                    <span className="text-violet-300">{i + 1}. [{p.role || "段"}]</span>{" "}
                    {p.track} <span className="font-mono text-slate-500">{p.start?.toFixed?.(0)}–{p.end?.toFixed?.(0)}s</span>
                  </div>
                ))}
                {result.why && <div className="pt-0.5 text-[10.5px] text-slate-500">AI:{result.why}</div>}
              </div>
            )}
            <audio controls className="w-full" src={mediaUrl(result.path)} />
            <div className="mt-1.5 text-[10.5px] text-slate-500">
              已存入素材库({result.path})— 重新扫描后即可选为项目音乐,流水线会把它当一首歌分析。
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── main view ───────────────────────────────────────────────────────────────

const TYPE_TABS = [
  { key: "video", label: "视频", icon: Film },
  { key: "image", label: "图片", icon: Images },
  { key: "audio", label: "音频", icon: Music2 },
];

export default function AssetsView({
  project, setProject,
}: { project: ProjectState; setProject: (fn: (p: ProjectState) => ProjectState) => void }) {
  const [root, setRoot] = useState("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [scanned, setScanned] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState("");
  const [annJobId, setAnnJobId] = useState<string | null>(null);
  const annJob = useJob(annJobId);
  const [selJobId, setSelJobId] = useState<string | null>(null);
  const selJob = useJob(selJobId);
  const selecting = selJob.status === "running";
  const [typeTab, setTypeTab] = useState<string>("video");
  const [annWb, setAnnWb] = useState<{ task: string; idx?: number } | null>(null);
  const [query, setQuery] = useState("");
  const [sortBy, setSortBy] = useState<"score" | "time_desc" | "time_asc">("score");
  const [annFilter, setAnnFilter] = useState<"all" | "cloud" | "local" | "none">("all");
  const [tagFilter, setTagFilter] = useState("");
  const [detail, setDetail] = useState<Asset | null>(null);
  // manual selection: video/image hashes (multi) + audio hash (single)
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [pickedAudio, setPickedAudio] = useState("");
  const [pickApplied, setPickApplied] = useState(false);
  // command-bar popovers
  const [modelsOpen, setModelsOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  // asset-level ❤️ (global taste, by content hash) + BGM stitch panel
  const [hearts, setHearts] = useState<Set<string>>(new Set());
  const [stitchOpen, setStitchOpen] = useState(false);
  useEffect(() => {
    api<{ hearts: string[] }>("/api/assets/hearts")
      .then((r) => setHearts(new Set(r.hearts ?? []))).catch(() => {});
  }, []);
  const toggleHeart = (a: Asset) => {
    const on = !hearts.has(a.content_hash);
    setHearts((s) => { const n = new Set(s); if (on) n.add(a.content_hash); else n.delete(a.content_hash); return n; });
    api("/api/assets/heart", {
      method: "POST",
      body: JSON.stringify({ content_hash: a.content_hash, hearted: on }),
    }).catch(() => {});
  };

  const busy = annJob.status === "running";

  const assetPath = (a: Asset) => a.absolute_path || a.file_path;
  const normPath = (p: string) => p.replace(/\//g, "\\").toLowerCase();

  // pre-check assets already in the project (after scan / project switch)
  useEffect(() => {
    if (assets.length === 0) return;
    const inProj = new Set(project.videos.map(normPath));
    setPicked(new Set(assets.filter((a) => a.asset_type !== "audio" && inProj.has(normPath(assetPath(a)))).map((a) => a.content_hash)));
    const audio = assets.find((a) => a.asset_type === "audio" && project.audio && normPath(assetPath(a)) === normPath(project.audio));
    setPickedAudio(audio?.content_hash ?? "");
    setPickApplied(false);
  }, [assets, project.id]);

  const togglePick = (a: Asset) => {
    setPickApplied(false);
    if (a.asset_type === "audio") {
      setPickedAudio((h) => (h === a.content_hash ? "" : a.content_hash));
    } else {
      setPicked((s) => {
        const n = new Set(s);
        if (n.has(a.content_hash)) n.delete(a.content_hash); else n.add(a.content_hash);
        return n;
      });
    }
  };

  const pickedVideos = assets.filter((a) => picked.has(a.content_hash));
  const pickedAudioAsset = assets.find((a) => a.content_hash === pickedAudio);

  const applyPick = () => {
    setProject((p) => ({
      ...p,
      videos: pickedVideos.map(assetPath),
      audio: pickedAudioAsset ? assetPath(pickedAudioAsset) : p.audio,
      selectionRationale: "手动选材",
    }));
    setPickApplied(true);
  };

  // reattach to jobs still running server-side after a page refresh
  const [interrupted, setInterrupted] = useState<{ unfinished: number } | null>(null);
  // cards queued while another batch runs (server chains them automatically)
  const [localQueued, setLocalQueued] = useState<Set<string>>(new Set());
  // asset source: local folder vs the Immich library (full-page grid mode)
  const [source, setSource] = useState<"local" | "immich">("local");
  const [imStatus, setImStatus] = useState<{ version?: string; videos?: number } | null>(null);
  const [imItems, setImItems] = useState<any[]>([]);
  const [imQuery, setImQuery] = useState("");
  const [imLoading, setImLoading] = useState(false);
  const [imPicked, setImPicked] = useState<Set<string>>(new Set());
  const [imPage, setImPage] = useState(1);
  const [imImporting, setImImporting] = useState(false);
  const [imMsg, setImMsg] = useState("");

  const imSearch = async (q: string, page = 1, append = false) => {
    setImLoading(true);
    try {
      const r = await api<{ items: any[] }>("/api/immich/search", {
        method: "POST", body: JSON.stringify({ query: q, size: 36, page }),
      });
      setImItems((prev) => (append ? [...prev, ...r.items] : r.items));
      setImPage(page);
    } catch (e: any) { setImMsg(`搜索失败：${e.message}`); }
    setImLoading(false);
  };

  const openImmich = () => {
    setSource("immich");
    if (!imStatus) {
      api<any>("/api/immich/status")
        .then((st) => { setImStatus(st); imSearch(""); })
        .catch((e) => setImMsg(e.message || "无法连接 Immich — 检查 IMMICH_URL / IMMICH_API_KEY"));
    }
  };

  const imImport = async () => {
    if (imPicked.size === 0) return;
    setImImporting(true); setImMsg("");
    try {
      const r = await api<{ imported: string[]; skipped: string[]; errors: string[] }>(
        "/api/immich/import", { method: "POST", body: JSON.stringify({ ids: [...imPicked] }) });
      setImMsg(`✓ 导入 ${r.imported.length} 个代理` +
        (r.skipped.length ? ` · 复用已有 ${r.skipped.length}` : "") +
        (r.errors.length ? ` · 失败 ${r.errors.length}` : ""));
      setImPicked(new Set());
      scan();
    } catch (e: any) { setImMsg(`导入失败：${e.message}`); }
    setImImporting(false);
  };
  useEffect(() => {
    api<any>("/api/jobs/current/annotate")
      .then((r) => {
        if (r.job) { setAnnJobId(r.job.id); return; }
        // no running job — was the last batch killed by a backend restart?
        api<any>("/api/jobs/latest/annotate").then((l) => {
          if (l.job && l.job.status === "error" && (l.job.unfinished ?? 0) > 0) {
            setInterrupted({ unfinished: l.job.unfinished });
          }
        }).catch(() => {});
      }).catch(() => {});
    api<any>("/api/jobs/current/select")
      .then((r) => { if (r.job) setSelJobId(r.job.id); }).catch(() => {});
  }, []);

  // Restore the last scan on mount so a page refresh (or backend restart)
  // doesn't force a manual re-scan. Cheap: the server serves it from memory,
  // or re-scans against the on-disk metadata cache (unchanged files skip
  // hashing + ffprobe).
  useEffect(() => {
    setScanning(true);
    api<{ root: string; assets: Asset[]; scanned?: boolean }>("/api/assets/scan")
      .then((r) => {
        if (r.assets?.length) {
          setAssets(r.assets);
          setScanned(true);
          if (r.root) setRoot((cur) => cur || r.root);
        }
      })
      .catch(() => {})
      .finally(() => setScanning(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const scan = async () => {
    setError(""); setScanning(true);
    try {
      const r = await api<{ root: string; assets: Asset[] }>("/api/assets/scan", {
        method: "POST", body: JSON.stringify({ root }),
      });
      setAssets(r.assets); setScanned(true);
      if (!root) setRoot(r.root);
      setDetail((d) => d ? r.assets.find((a) => a.content_hash === d.content_hash) ?? d : null);
    } catch (e: any) { setError(e.message); }
    setScanning(false);
  };

  const annotate = async (hashes: string[] = [], force = false, provider: "cloud" | "local" = "cloud") => {
    setError("");
    try {
      const r = await api<{ job_id: string | null; queued?: boolean; message?: string }>("/api/assets/annotate", {
        method: "POST", body: JSON.stringify({ content_hashes: hashes, force, provider }),
      });
      if (r.job_id) setAnnJobId(r.job_id);
      else if (r.queued) setLocalQueued((s0) => new Set([...s0, ...hashes]));
      else setError(r.message || "没有需要标注的素材。");
    } catch (e: any) { setError(e.message); }
  };

  const autoSelect = async () => {
    setError("");
    try {
      const r = await api<{ job_id: string }>("/api/assets/auto-select", {
        method: "POST",
        body: JSON.stringify({
          instruction: project.instruction,
          project_id: project.id,
          target_length: project.targetLength,
        }),
      });
      setSelJobId(r.job_id);
    } catch (e: any) { setError(e.message); }
  };

  useEffect(() => {
    if (selJob.status === "done" && selJobId) {
      const m = selJob.meta;
      setProject((p) => ({
        ...p,
        videos: (m.videos as string[])?.length ? m.videos : p.videos,
        audio: (m.audio as string) || p.audio,
        selectionRationale: m.selection?.rationale ?? p.selectionRationale,
      }));
    }
  }, [selJob.status]);

  useEffect(() => {
    if ((annJob.status === "done" || annJob.status === "error") && annJobId) {
      setAnnJobId(null);
      setLocalQueued(new Set());
      scan();
      // a queued request may have been chained into a fresh batch — attach
      const t = window.setTimeout(() => {
        api<any>("/api/jobs/current/annotate")
          .then((r) => { if (r.job) setAnnJobId(r.job.id); }).catch(() => {});
      }, 1500);
      return () => window.clearTimeout(t);
    }
  }, [annJob.status]);

  const byType = (t: string) => assets.filter((a) => a.asset_type === t);
  const newCount = assets.filter((a) => !a.annotated).length;
  const q = query.trim().toLowerCase();

  const assetTags = (a: Asset): string[] => [
    ...(Array.isArray(a.annotation?.tags) ? a.annotation!.tags : []),
    ...(Array.isArray(a.annotation?.visual_tags) ? a.annotation!.visual_tags : []),
    ...(Array.isArray(a.annotation_local?.tags) ? a.annotation_local!.tags : []),
  ];
  const assetScore = (a: Asset) =>
    Number(a.annotation?.quality_score ?? a.annotation_local?.quality_score ?? -1);

  // tag dropdown options: most frequent tags within the current type
  const tagOptions = useMemo(() => {
    const freq = new Map<string, number>();
    byType(typeTab).forEach((a) => assetTags(a).forEach((t) => {
      const s = String(t).trim();
      if (s) freq.set(s, (freq.get(s) ?? 0) + 1);
    }));
    return [...freq.entries()].sort((x, y) => y[1] - x[1]).slice(0, 30).map(([t]) => t);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assets, typeTab]);

  const shown = byType(typeTab)
    .filter((a) => !q ||
      (a.file_name || a.file_path).toLowerCase().includes(q) ||
      JSON.stringify(a.annotation ?? {}).toLowerCase().includes(q))
    .filter((a) => annFilter === "all" ? true
      : annFilter === "cloud" ? a.annotated
        : annFilter === "local" ? !!a.annotated_local
          : (!a.annotated && !a.annotated_local))
    .filter((a) => !tagFilter || assetTags(a).includes(tagFilter))
    .sort((a, b) => {
      if (sortBy === "time_desc") return (b.capture_time ?? "").localeCompare(a.capture_time ?? "");
      if (sortBy === "time_asc") {
        // assets without capture time sink to the end
        const ta = a.capture_time ?? "9999", tb = b.capture_time ?? "9999";
        return ta.localeCompare(tb);
      }
      return assetScore(b) - assetScore(a);
    });

  const selSel = selJob.meta.selection;
  const glass = "rounded-2xl border-white/[0.07] bg-slate-900/50";

  // ── full-page detail view (Option A: replaces the grid, no overlay) ──
  if (detail) {
    const di = shown.findIndex((x) => x.content_hash === detail.content_hash);
    return (
      <DetailView
        asset={detail} busy={busy} annMeta={annJob.meta}
        onClose={() => setDetail(null)}
        onReannotate={(a) => annotate([a.content_hash], a.annotated)}
        onPrev={di > 0 ? () => setDetail(shown[di - 1]) : undefined}
        onNext={di >= 0 && di < shown.length - 1 ? () => setDetail(shown[di + 1]) : undefined}
        pos={di >= 0 ? `${di + 1} / ${shown.length}` : undefined}
      />
    );
  }

  return (
    <div>
      {stitchOpen && (
        <BgmStitchPanel
          tracks={assets.filter((a) => a.asset_type === "audio")}
          hearts={hearts}
          onClose={() => setStitchOpen(false)}
        />
      )}
      {/* ── ① command bar: scan → annotate. Low-frequency stuff lives in popovers ── */}
      <Card className={cn(glass, (busy || selecting) && "border-beam")}>
        <CardContent className="px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <FolderOpen className="h-4 w-4 shrink-0 text-cyan-400" />
            <Input
              className="h-9 w-[290px] border-white/10 bg-black/25 text-xs"
              placeholder="素材文件夹（默认 resource/imports/）"
              value={root} onChange={(e) => setRoot(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && scan()}
            />
            <Button variant="outline" className="h-9 gap-1.5 border-white/10 bg-white/[0.04]"
              onClick={scan} disabled={scanning}>
              {scanning ? <Loader2 className="h-4 w-4 animate-spin" /> : <ScanSearch className="h-4 w-4" />}
              扫描
            </Button>
            {/* primary CTA follows the workflow state */}
            {scanned && newCount > 0 && (
              <Button
                className="h-9 gap-1.5 bg-cyan-500 font-semibold text-slate-950 shadow-[0_0_16px_rgba(34,211,238,0.3)] hover:bg-cyan-400"
                onClick={() => annotate()} disabled={busy}>
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Tags className="h-4 w-4" />}
                标注新素材 ({newCount})
              </Button>
            )}
            {scanned && newCount === 0 && assets.length > 0 && !busy && (
              <span className="text-xs text-emerald-400">✓ 全部已标注</span>
            )}

            <div className="relative ml-auto flex items-center gap-2">
              <Button variant="outline" className="h-9 gap-1.5 border-white/10 bg-white/[0.04] text-xs"
                onClick={() => { setModelsOpen((o) => !o); setMoreOpen(false); }}>
                <Settings2 className="h-3.5 w-3.5" /> 模型
              </Button>
              <Button variant="outline" className="h-9 border-white/10 bg-white/[0.04] px-2.5 text-xs"
                onClick={() => { setMoreOpen((o) => !o); setModelsOpen(false); }}>
                ⋯
              </Button>
              {modelsOpen && (
                <div className="absolute top-10 right-0 z-50 w-[320px] rounded-xl border border-white/10 bg-slate-950/95 p-3 shadow-[0_12px_40px_rgba(0,0,0,0.6)] backdrop-blur-xl">
                  <div className="mb-2 text-xs font-semibold text-slate-300">本页使用的模型</div>
                  <div className="flex flex-col gap-2">
                    <RoleModelSelect role="vision" disabled={busy} className="justify-between" />
                    <RoleModelSelect role="audio" disabled={busy} className="justify-between" />
                    <RoleModelSelect role="agent" disabled={selecting} className="justify-between" />
                  </div>
                  <div className="mt-2 text-[11px] text-slate-600">视觉/音频用于标注 · Agent 用于智能选材</div>
                  <div className="mt-3 border-t border-white/[0.07] pt-2.5">
                    <div className="mb-1.5 text-xs font-semibold text-slate-300">并发设置</div>
                    <ConcurrencyField k="ANNOTATE_VIDEO_WORKERS" label="并行标注文件数" hint="进程级并行，2-4；上限看 API 限流" />
                    <ConcurrencyField k="CAPTION_BATCH_SIZE" label="VLM 并发调用数" hint="片段/密集/场景分析的并发上限" />
                  </div>
                </div>
              )}
              {moreOpen && (
                <div className="absolute top-10 right-0 z-50 w-[240px] rounded-xl border border-white/10 bg-slate-950/95 p-1.5 shadow-[0_12px_40px_rgba(0,0,0,0.6)] backdrop-blur-xl">
                  <button
                    className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs text-slate-300 hover:bg-white/[0.06] disabled:opacity-40"
                    disabled={!scanned || assets.length === 0 || busy}
                    onClick={() => {
                      setMoreOpen(false);
                      const hs = assets.map((a) => a.content_hash).filter(Boolean);
                      if (hs.length && window.confirm(`重新标注全部 ${hs.length} 个素材？将重跑视觉/音频分析（消耗 API）。`))
                        annotate(hs, true);
                    }}>
                    <RefreshCw className="h-3.5 w-3.5 text-slate-500" />
                    强制重新标注全部…
                  </button>
                  <button
                    className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs text-slate-300 hover:bg-white/[0.06]"
                    onClick={async () => {
                      setMoreOpen(false); setError("");
                      try {
                        const r = await api<any>("/api/immich/writeback", { method: "POST", body: JSON.stringify({}) });
                        setError(`Immich 回写完成：同步 ${r.synced} 个` + (r.errors?.length ? ` · 失败 ${r.errors.length}` : ""));
                      } catch (e: any) { setError(`Immich 回写失败：${e.message}`); }
                    }}>
                    🖼 同步标注到 Immich 描述
                  </button>
                </div>
              )}
            </div>
          </div>

          {error && (
            <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-400">{error}</div>
          )}

          {/* a previous batch was killed (e.g. backend restart) — say so, loudly */}
          {interrupted && !busy && (
            <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">
              <span>
                ⚠ 上次标注批次被中断（服务重启），{interrupted.unfinished} 个文件未完成 —
                点「扫描」后再点「标注新素材」继续，已完成的部分会自动跳过。
              </span>
              <button className="ml-auto text-xs text-slate-400 hover:text-slate-200"
                onClick={() => setInterrupted(null)}>知道了</button>
            </div>
          )}

          {/* annotation batch panel: file dots + stage stepper + per-file grids */}
          {busy && (
            <>
              <AnnotationProgress
                meta={annJob.meta} jobId={annJobId}
                onOpenWorkbench={(task, idx) => setAnnWb({ task, idx })}
              />
              {annWb && annJobId && (annJob.meta.tasks ?? {})[annWb.task] && (
                <AgentWorkbench
                  name={annWb.task} t={annJob.meta.tasks[annWb.task]} jobId={annJobId}
                  initialIdx={annWb.idx} onClose={() => setAnnWb(null)}
                />
              )}
            </>
          )}
          {annJob.status === "error" && (
            <div className="mt-3">
              <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-400">标注失败 — 查看日志</div>
              <JobLog lines={annJob.lines.slice(-40)} height={180} />
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── selection flow (runs from the grid toolbar's 智能选材 button) ── */}
      {selJobId && (
        <div className="mt-3">
          <AgentFlow steps={SELECT_STEPS} stages={selJob.meta.stages ?? {}} />
          {selJob.status === "error" && (
            <>
              <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-400">选材失败 — 查看日志</div>
              <JobLog lines={selJob.lines.slice(-30)} height={140} />
            </>
          )}
        </div>
      )}
      {selJob.status === "done" && selSel && (
            <motion.div
              initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
              className="mt-2 rounded-xl border border-emerald-500/25 bg-emerald-500/[0.07] px-4 py-3"
            >
              <div className="flex flex-wrap items-center gap-1.5 text-sm">
                <span className="font-semibold text-emerald-400">已选素材：</span>
                {(selSel.selected_videos ?? []).map((v: string, i: number) => (
                  <Badge key={`v${i}`} variant="outline" className="border-sky-500/40 bg-sky-500/10 text-[11px] text-sky-300">
                    <Film className="mr-1 h-3 w-3" />{v.split(/[\\/]/).pop()}
                  </Badge>
                ))}
                {(selSel.selected_images ?? []).map((v: string, i: number) => (
                  <Badge key={`i${i}`} variant="outline" className="border-cyan-500/40 bg-cyan-500/10 text-[11px] text-cyan-300">
                    <Images className="mr-1 h-3 w-3" />{v.split(/[\\/]/).pop()}
                  </Badge>
                ))}
                {(selSel.selected_audio ?? []).slice(0, 1).map((v: string, i: number) => (
                  <Badge key={`a${i}`} variant="outline" className="border-violet-500/40 bg-violet-500/10 text-[11px] text-violet-300">
                    <Music2 className="mr-1 h-3 w-3" />{v.split(/[\\/]/).pop()}
                  </Badge>
                ))}
              </div>
              {selSel.rationale && (
                <p className="mt-1.5 text-xs text-slate-300">
                  <Lightbulb className="mr-1 inline h-3 w-3 -translate-y-px text-amber-400" />{selSel.rationale}
                </p>
              )}
              {selSel.narrative_idea && (
                <p className="mt-1 text-xs text-slate-400">
                  <BookOpen className="mr-1 inline h-3 w-3 -translate-y-px" />{selSel.narrative_idea}
                </p>
              )}
              <p className="mt-1 text-xs text-slate-500">已写入项目 — 切换到「项目编辑」运行流水线。</p>
            </motion.div>
          )}
          {!selJobId && project.selectionRationale && (
            <div className="mt-3 rounded-xl border border-sky-500/25 bg-sky-500/[0.06] px-4 py-3">
              <div className="text-sm font-semibold text-sky-300">
                本项目已有选材（{project.videos.length} 视频{project.audio ? " · 1 音乐" : ""}）
              </div>
              <p className="mt-1 text-xs text-slate-400">
                <Lightbulb className="mr-1 inline h-3 w-3 -translate-y-px text-amber-400" />{project.selectionRationale}
              </p>
            </div>
          )}

      <LocalGpuPanel />

      {/* ── source tabs: local folder vs Immich library (full-page, no drawer) ── */}
      <div className="mt-4 flex gap-1 rounded-lg border border-white/10 bg-white/[0.03] p-1" style={{ width: "fit-content" }}>
        <button
          onClick={() => setSource("local")}
          className={cn(
            "flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-xs font-medium transition-colors",
            source === "local" ? "bg-cyan-500/15 text-cyan-300" : "text-slate-400 hover:text-slate-200",
          )}>
          本地素材{assets.length ? ` (${assets.length})` : ""}
        </button>
        <button
          onClick={openImmich}
          className={cn(
            "flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-xs font-medium transition-colors",
            source === "immich" ? "bg-violet-500/15 text-violet-300" : "text-slate-400 hover:text-slate-200",
          )}>
          🖼 Immich 库{imStatus?.videos ? ` (${imStatus.videos.toLocaleString()})` : ""}
        </button>
      </div>

      {source === "immich" ? (
        <>
          <div className="my-4 flex flex-wrap items-center gap-3">
            <div className="relative flex-1 basis-[360px]">
              <Search className="absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
              <Input
                className="h-9 w-full border-white/10 bg-black/25 pl-8 text-xs"
                placeholder="CLIP 语义搜索（英文效果最佳，如 two women running in flower field）— 回车搜索，留空显示最新"
                value={imQuery}
                onChange={(e) => setImQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && imSearch(imQuery)}
              />
            </div>
            <Button variant="outline" className="h-9 border-white/10 bg-white/[0.04] text-xs"
              onClick={() => imSearch(imQuery)} disabled={imLoading}>
              {imLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : "搜索"}
            </Button>
            {imMsg && <span className="text-xs text-emerald-400">{imMsg}</span>}
            <div className="ml-auto flex items-center gap-2">
              {imPicked.size > 0 && (
                <Button variant="outline" size="sm" className="h-8 border-white/10 bg-white/[0.04] text-xs"
                  onClick={() => setImPicked(new Set())}>清空 ({imPicked.size})</Button>
              )}
              <Button size="sm"
                className="h-8 gap-1.5 bg-violet-500 text-xs font-semibold text-white hover:bg-violet-400"
                disabled={imPicked.size === 0 || imImporting}
                onClick={imImport}>
                {imImporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                导入代理到本地 ({imPicked.size})
              </Button>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5">
            {imItems.map((it: any) => (
              <button
                key={it.id}
                disabled={it.imported}
                title={it.imported ? "该素材已导入本地（按内容识别，不会重复）" : undefined}
                className={cn(
                  "group relative overflow-hidden rounded-lg border text-left transition-all",
                  it.imported
                    ? "cursor-default border-emerald-500/40"
                    : imPicked.has(it.id)
                      ? "border-violet-400/70 shadow-[0_0_12px_rgba(167,139,250,0.3)]"
                      : "border-white/[0.08] hover:border-white/25",
                )}
                onClick={() => {
                  if (it.imported) return;
                  setImPicked((s0) => {
                    const n = new Set(s0);
                    if (n.has(it.id)) n.delete(it.id); else n.add(it.id);
                    return n;
                  });
                }}
              >
                <img src={it.thumb} loading="lazy"
                  className={cn("aspect-video w-full object-cover", it.imported && "opacity-40")} />
                {it.imported ? (
                  <span className="absolute top-1.5 left-1.5 rounded-full border border-emerald-400/50 bg-emerald-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-300">已导入</span>
                ) : imPicked.has(it.id) && (
                  <span className="absolute top-1.5 left-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-violet-400 text-xs font-bold text-slate-950">✓</span>
                )}
                <div className="truncate bg-black/55 px-1.5 py-0.5 text-[10.5px] text-slate-300">{it.name}</div>
              </button>
            ))}
          </div>
          {imItems.length === 0 && !imLoading && (
            <EmptyHint>{imMsg || "没有结果"}</EmptyHint>
          )}
          {imItems.length >= 36 && (
            <div className="mt-3 text-center">
              <Button variant="outline" size="sm" className="h-8 border-white/10 bg-white/[0.04] text-xs"
                disabled={imLoading} onClick={() => imSearch(imQuery, imPage + 1, true)}>
                {imLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "加载更多"}
              </Button>
            </div>
          )}
          <p className="mt-3 text-[11px] text-slate-600">
            导入的是 Immich 转码代理（约 3-10MB/个），标注/剪辑全程用代理，4K 原片仅渲染时按需读取。
          </p>
        </>
      ) : scanned ? (
        <>
          <div className="my-4 flex flex-wrap items-center gap-3">
            <div className="flex gap-1 rounded-lg border border-white/10 bg-white/[0.03] p-1">
              {TYPE_TABS.map((t) => (
                <button
                  key={t.key}
                  onClick={() => setTypeTab(t.key)}
                  className={cn(
                    "flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition-colors",
                    typeTab === t.key
                      ? "bg-cyan-500/15 text-cyan-300 shadow-[0_0_10px_rgba(34,211,238,0.1)]"
                      : "text-slate-400 hover:text-slate-200",
                  )}
                >
                  <t.icon className="h-3.5 w-3.5" />
                  {t.label} ({byType(t.key).length})
                </button>
              ))}
            </div>
            <div className="relative">
              <Search className="absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
              <Input
                className="h-8 w-[240px] border-white/10 bg-black/25 pl-8 text-xs"
                placeholder="搜索文件名 / 标注内容…"
                value={query} onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <span className="text-xs text-slate-500">
              共 {assets.length} 个素材 · {assets.length - newCount} 已标注 · {newCount} 新
            </span>
            {/* sort & filter — journey time / score / tags / annotation tracks */}
            <select
              className="h-7 rounded-md border border-white/10 bg-black/25 px-1.5 text-[11px] text-slate-300 outline-none"
              value={sortBy} onChange={(e) => setSortBy(e.target.value as any)}
            >
              <option value="score">按评分 高→低</option>
              <option value="time_desc">按拍摄时间 新→旧</option>
              <option value="time_asc">按拍摄时间 旧→新</option>
            </select>
            <select
              className="h-7 rounded-md border border-white/10 bg-black/25 px-1.5 text-[11px] text-slate-300 outline-none"
              value={annFilter} onChange={(e) => setAnnFilter(e.target.value as any)}
            >
              <option value="all">全部标注状态</option>
              <option value="cloud">☁ 已云端标注</option>
              <option value="local">🖥 已本地标注</option>
              <option value="none">未标注</option>
            </select>
            <select
              className="h-7 max-w-[130px] rounded-md border border-white/10 bg-black/25 px-1.5 text-[11px] text-slate-300 outline-none"
              value={tagFilter} onChange={(e) => setTagFilter(e.target.value)}
            >
              <option value="">全部 tag</option>
              {tagOptions.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            {(annFilter !== "all" || tagFilter) && (
              <span className="text-[11px] text-cyan-300">筛出 {shown.length} 个</span>
            )}
            <div className="ml-auto flex items-center gap-2">
              <span className="text-[11px] text-slate-600">勾选卡片=手动选材</span>
              <Button
                variant="outline"
                className="h-8 gap-1.5 border-violet-500/40 bg-violet-500/10 text-xs text-violet-300 hover:bg-violet-500/20"
                onClick={() => setStitchOpen(true)}
                disabled={assets.filter((a) => a.asset_type === "audio").length < 2}
                title="把几首喜欢的音乐无缝拼成一条 BGM(不用跑流水线)"
              >
                <Music2 className="h-3.5 w-3.5" /> 拼接 BGM
              </Button>
              <Button
                className="h-8 gap-1.5 bg-cyan-500 text-xs font-semibold text-slate-950 shadow-[0_0_14px_rgba(34,211,238,0.3)] hover:bg-cyan-400"
                onClick={autoSelect} disabled={selecting || assets.every((a) => !a.annotated)}>
                {selecting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                智能选材
              </Button>
            </div>
          </div>

          {shown.length === 0 ? (
            <EmptyHint>该类型下没有素材</EmptyHint>
          ) : (
            <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-4">
              {shown.map((a, i) => (
                <AssetCard
                  key={a.content_hash} a={a} index={i} onOpen={() => setDetail(a)}
                  picked={a.asset_type === "audio" ? a.content_hash === pickedAudio : picked.has(a.content_hash)}
                  onTogglePick={a.asset_type === "image" ? undefined : () => togglePick(a)}
                  hearted={hearts.has(a.content_hash)}
                  onToggleHeart={() => toggleHeart(a)}
                  annotating={busy && (annJob.meta.files ?? {})[a.content_hash] === "r"}
                  queued={busy && (annJob.meta.files ?? {})[a.content_hash] === "p"}
                  annStage={(annJob.meta.stage_by_hash ?? {})[a.content_hash]?.stage || annJob.meta.stage}
                  annStageDetail={(annJob.meta.stage_by_hash ?? {})[a.content_hash]?.detail || annJob.meta.stage_detail}
                  annBusy={
                    // only the card's OWN activity blocks its button — other
                    // cards stay clickable and join the queue mid-batch
                    (busy && (annJob.meta.files ?? {})[a.content_hash] !== undefined)
                    || localQueued.has(a.content_hash)
                  }
                  queuedLocal={localQueued.has(a.content_hash)}
                  onAnnotate={() => {
                    if (a.annotated && !window.confirm(`重新标注「${a.file_name || a.file_path}」？将重跑视觉分析（消耗 API）。`)) return;
                    annotate([a.content_hash], a.annotated);
                  }}
                  onAnnotateLocal={a.asset_type === "video" ? () => {
                    if (a.annotated_local && !window.confirm(`用本地 VLM 重新标注「${a.file_name || a.file_path}」？只更新本地轨道，不影响云端结果。`)) return;
                    annotate([a.content_hash], !!a.annotated_local, "local");
                  } : undefined}
                />
              ))}
            </div>
          )}

          {/* manual selection apply bar */}
          {(pickedVideos.length > 0 || pickedAudioAsset) && (
            <div className="sticky bottom-3 z-20 mt-4 flex flex-wrap items-center gap-2 rounded-xl border border-cyan-500/30 bg-slate-900/95 px-4 py-2.5 shadow-[0_0_24px_rgba(0,0,0,0.5)]">
              <span className="text-sm font-semibold text-cyan-300">手动选材：</span>
              {pickedVideos.map((a) => (
                <Badge key={a.content_hash} variant="outline" className="border-sky-500/40 bg-sky-500/10 text-[11px] text-sky-300">
                  <Film className="mr-1 h-3 w-3" />{a.file_name || a.file_path}
                </Badge>
              ))}
              {pickedAudioAsset && (
                <Badge variant="outline" className="border-violet-500/40 bg-violet-500/10 text-[11px] text-violet-300">
                  <Music2 className="mr-1 h-3 w-3" />{pickedAudioAsset.file_name || pickedAudioAsset.file_path}
                </Badge>
              )}
              <div className="ml-auto flex items-center gap-2">
                {pickApplied ? (
                  <span className="text-xs text-emerald-400">✓ 已写入项目 — 切到「项目编辑」运行流水线</span>
                ) : (
                  <span className="text-[11px] text-slate-500">
                    {pickedVideos.length} 视频{pickedAudioAsset ? " · 1 音乐" : " · 未选音乐"}
                  </span>
                )}
                <Button variant="outline" size="sm" className="h-7 border-white/10 bg-white/[0.04] text-xs"
                  onClick={() => { setPicked(new Set()); setPickedAudio(""); setPickApplied(false); }}>
                  清空
                </Button>
                <Button size="sm"
                  className="h-7 gap-1 bg-cyan-500 text-xs font-semibold text-slate-950 hover:bg-cyan-400"
                  disabled={pickedVideos.length === 0}
                  onClick={applyPick}>
                  <Pin className="h-3 w-3" /> 应用到项目
                </Button>
              </div>
            </div>
          )}
        </>
      ) : (
        <EmptyHint>点击「扫描」发现素材文件</EmptyHint>
      )}

    </div>
  );
}

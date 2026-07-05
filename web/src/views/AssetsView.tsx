import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  AudioLines, BookOpen, Bot, Camera, ClipboardList, Code2, Database, Drama,
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
import TaskGrids from "../components/TaskGrids";
import AgentWorkbench from "../components/AgentWorkbench";
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
            <td className="kv-val">{fmtVal(v)}</td>
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

function DetailSheet({
  asset, onClose, onReannotate, busy, annMeta,
}: {
  asset: Asset | null; onClose: () => void; onReannotate: (a: Asset) => void;
  busy: boolean; annMeta?: Record<string, any>;
}) {
  const [details, setDetails] = useState<{ clips: any[]; scenes: any[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    setDetails(null);
    if (!asset || asset.asset_type === "image") return;
    setLoading(true);
    api<any>(`/api/assets/${asset.content_hash}/details`)
      .then(setDetails).catch(() => {}).finally(() => setLoading(false));
  }, [asset?.content_hash]);

  if (!asset) return null;
  const src = mediaUrl(asset.absolute_path || asset.file_path);
  const seek = (s: number) => {
    const v = videoRef.current;
    if (v) { v.currentTime = Math.max(0, s); v.play().catch(() => {}); }
  };
  const clips = details?.clips ?? [];
  const scenes = details?.scenes ?? [];
  const ann = asset.annotation ?? {};

  return (
    <Sheet open onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="w-full overflow-y-auto border-white/10 bg-slate-950/95 p-5 backdrop-blur-xl sm:max-w-[920px]">
        <SheetHeader className="p-0 pb-3">
          <SheetTitle className="flex flex-wrap items-center gap-2 pr-8 text-sm">
            <span className="truncate">{asset.file_name || asset.file_path}</span>
            {asset.annotated
              ? <Badge variant="outline" className={qBadgeCls(Number(ann.quality_score ?? 0))}>Q {fmtVal(ann.quality_score)}</Badge>
              : <Badge variant="outline" className={NEW_CLS}>未标注</Badge>}
            <Button variant="outline" size="sm"
              className="ml-auto h-7 gap-1.5 border-white/10 bg-white/[0.04] text-xs"
              disabled={busy} onClick={() => onReannotate(asset)}>
              {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
              重新标注
            </Button>
          </SheetTitle>
        </SheetHeader>

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

        <div className="drawer-player">
          {asset.asset_type === "video" && <video ref={videoRef} src={src} controls className="max-h-[340px] w-full rounded-lg bg-black" />}
          {asset.asset_type === "image" && <img src={src} className="max-h-[340px] w-full rounded-lg bg-black object-contain" />}
          {asset.asset_type === "audio" && <audio ref={videoRef as any} src={src} controls className="w-full" />}
          <div className="mt-1 text-xs text-slate-500">
            {asset.duration_sec ? `${Math.round(asset.duration_sec)}s · ` : ""}
            {asset.width ? `${asset.width}×${asset.height} · ` : ""}
            {asset.file_size_mb ? `${asset.file_size_mb.toFixed(1)}MB · ` : ""}
            {asset.absolute_path || asset.file_path}
          </div>
        </div>

        <Tabs defaultValue={asset.asset_type === "video" ? "clips" : "overview"}>
          <TabsList className="bg-white/[0.05]">
            <TabsTrigger value="overview" className="gap-1.5 text-xs">
              <ClipboardList className="h-3.5 w-3.5" />标注总览
            </TabsTrigger>
            {asset.asset_type === "video" && (
              <>
                <TabsTrigger value="clips" className="gap-1.5 text-xs">
                  <Film className="h-3.5 w-3.5" />片段分析 ({clips.length})
                </TabsTrigger>
                <TabsTrigger value="scenes" className="gap-1.5 text-xs">
                  <Layers className="h-3.5 w-3.5" />场景 ({scenes.length})
                </TabsTrigger>
              </>
            )}
            {asset.asset_type === "audio" && (
              <TabsTrigger value="beats" className="gap-1.5 text-xs">
                <Music2 className="h-3.5 w-3.5" />节奏关键点
              </TabsTrigger>
            )}
            <TabsTrigger value="raw" className="gap-1.5 text-xs">
              <Code2 className="h-3.5 w-3.5" />原始标注
            </TabsTrigger>
          </TabsList>

          <TabsContent value="overview" className="pt-3">
            {asset.annotated ? <AnnotationTable ann={ann} /> : <EmptyHint>尚未标注 — 点击右上角「重新标注」</EmptyHint>}
          </TabsContent>

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
            </>
          )}

          {asset.asset_type === "audio" && (
            <TabsContent value="beats" className="pt-3">
              <AudioKeypointsChart
                path={asset.absolute_path || asset.file_path}
                duration={asset.duration_sec} onSeek={seek}
              />
            </TabsContent>
          )}

          <TabsContent value="raw" className="pt-3">
            <pre className="rawjson">{JSON.stringify(ann, null, 2)}</pre>
          </TabsContent>
        </Tabs>
      </SheetContent>
    </Sheet>
  );
}

// ── asset card ──────────────────────────────────────────────────────────────

const STAGE_LABELS: Record<string, string> = {
  shot_detection: "镜头检测", captioning: "片段理解", dense_caption: "密集描述",
  scene_merge: "场景合并", scene_analysis: "场景分析",
};

function AssetCard({ a, onOpen, index, picked, onTogglePick, annotating, queued, annStage, onAnnotate, annBusy }: {
  a: Asset; onOpen: () => void; index: number;
  picked?: boolean; onTogglePick?: () => void;
  /** this exact asset is currently being annotated (hash-keyed job state) */
  annotating?: boolean;
  /** waiting in the current annotation batch */
  queued?: boolean;
  /** current pipeline stage of THIS asset's annotation */
  annStage?: string;
  /** start (re-)annotation of this asset */
  onAnnotate?: () => void;
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
          {/* live stage strip on the running card: 镜头检测 → 片段理解 → … */}
          {annotating && (
            <div className="absolute inset-x-0 bottom-0 flex items-center gap-1.5 bg-gradient-to-t from-black/85 to-transparent px-2.5 pt-5 pb-1.5">
              <Loader2 className="h-3 w-3 shrink-0 animate-spin text-cyan-400" />
              <span className="truncate text-[10.5px] text-cyan-200">
                {STAGE_LABELS[annStage ?? ""] ?? annStage ?? "分析中"}…
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
          <div className="mt-1.5 flex flex-wrap gap-1">
            {a.annotated
              ? <Badge variant="outline" className={cn("text-[11px]", qBadgeCls(Number(ann.quality_score ?? 0)))}>Q {fmtVal(ann.quality_score)}</Badge>
              : <Badge variant="outline" className={cn("text-[11px]", NEW_CLS)}>未标注</Badge>}
            {tags.map((t, i) => (
              <Badge key={i} variant="outline" className={cn("text-[11px]", TAG_CLS)}>{t}</Badge>
            ))}
          </div>
          {ann.summary && (
            <p className="mt-1.5 line-clamp-2 text-xs text-slate-400">{ann.summary}</p>
          )}
          {onAnnotate && (
            <Button
              variant="outline" size="sm"
              className={cn(
                "mt-2.5 h-7 w-full gap-1.5 text-xs",
                a.annotated
                  ? "border-white/10 bg-white/[0.03] text-slate-400 hover:text-slate-200"
                  : "border-cyan-500/30 bg-cyan-500/[0.08] text-cyan-300 hover:bg-cyan-500/15",
              )}
              disabled={annBusy}
              onClick={(e) => { e.stopPropagation(); onAnnotate(); }}
            >
              {annotating || (annBusy && queued)
                ? <Loader2 className="h-3 w-3 animate-spin" />
                : a.annotated ? <RefreshCw className="h-3 w-3" /> : <Tags className="h-3 w-3" />}
              {annotating ? "标注中…" : queued ? "排队中…" : a.annotated ? "重新标注" : "标注"}
            </Button>
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
  const [detail, setDetail] = useState<Asset | null>(null);
  // manual selection: video/image hashes (multi) + audio hash (single)
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [pickedAudio, setPickedAudio] = useState("");
  const [pickApplied, setPickApplied] = useState(false);
  // command-bar popovers + collapsible annotation detail
  const [modelsOpen, setModelsOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [showAnnDetail, setShowAnnDetail] = useState(false);

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
  useEffect(() => {
    api<any>("/api/jobs/current/annotate")
      .then((r) => { if (r.job) setAnnJobId(r.job.id); }).catch(() => {});
    api<any>("/api/jobs/current/select")
      .then((r) => { if (r.job) setSelJobId(r.job.id); }).catch(() => {});
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

  const annotate = async (hashes: string[] = [], force = false) => {
    setError("");
    try {
      const r = await api<{ job_id: string | null; message?: string }>("/api/assets/annotate", {
        method: "POST", body: JSON.stringify({ content_hashes: hashes, force }),
      });
      if (r.job_id) setAnnJobId(r.job_id);
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
    if (annJob.status === "done" && annJobId) {
      setAnnJobId(null);
      scan();
    }
  }, [annJob.status]);

  const byType = (t: string) => assets.filter((a) => a.asset_type === t);
  const newCount = assets.filter((a) => !a.annotated).length;
  const q = query.trim().toLowerCase();
  const shown = byType(typeTab)
    .filter((a) => !q ||
      (a.file_name || a.file_path).toLowerCase().includes(q) ||
      JSON.stringify(a.annotation ?? {}).toLowerCase().includes(q))
    .sort((a, b) => (b.annotation?.quality_score ?? -1) - (a.annotation?.quality_score ?? -1));

  const selSel = selJob.meta.selection;
  const glass = "rounded-2xl border-white/[0.07] bg-slate-900/50";

  return (
    <div>
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
                </div>
              )}
            </div>
          </div>

          {error && (
            <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-400">{error}</div>
          )}

          {/* slim progress strip — details on demand; the JobDock mirrors this globally */}
          {busy && (
            <div className="mt-3">
              <div className="flex items-center gap-3">
                <Progress
                  value={Math.round(((annJob.meta.current ?? 0) / Math.max(annJob.meta.total ?? 1, 1)) * 100)}
                  className="h-2 flex-1 bg-white/[0.06]"
                />
                <span className="shrink-0 text-xs text-slate-400">
                  {annJob.meta.current ?? 0}/{annJob.meta.total ?? "?"} · {annJob.meta.filename || "…"}
                </span>
                <button
                  className="shrink-0 text-xs text-cyan-400 hover:text-cyan-300"
                  onClick={() => setShowAnnDetail((v) => !v)}>
                  {showAnnDetail ? "收起详情" : "分段详情"}
                </button>
              </div>
              {showAnnDetail && (
                <>
                  <TaskGrids
                    tasks={annJob.meta.tasks ?? {}} jobId={annJobId}
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
            </div>
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

      {scanned ? (
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
            <div className="ml-auto flex items-center gap-2">
              <span className="text-[11px] text-slate-600">勾选卡片=手动选材</span>
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
                  annotating={busy && (annJob.meta.files ?? {})[a.content_hash] === "r"}
                  queued={busy && (annJob.meta.files ?? {})[a.content_hash] === "p"}
                  annStage={annJob.meta.stage}
                  annBusy={busy}
                  onAnnotate={() => {
                    if (a.annotated && !window.confirm(`重新标注「${a.file_name || a.file_path}」？将重跑视觉分析（消耗 API）。`)) return;
                    annotate([a.content_hash], a.annotated);
                  }}
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

      <DetailSheet
        asset={detail} busy={busy} annMeta={annJob.meta}
        onClose={() => setDetail(null)}
        onReannotate={(a) => annotate([a.content_hash], a.annotated)}
      />
    </div>
  );
}

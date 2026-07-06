/** In-canvas overlay showing one source asset's annotation (opened by clicking
 *  an AssetNode). Mirrors the AssetsView detail, trimmed for the pipeline canvas. */
import { useState } from "react";
import { Film, Music2, X } from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { mediaUrl } from "../../api";
import type { AssetInfo } from "./WorkflowCanvas";
import AudioTimeline, { attachInstruments, parseSectionTimes, SectionsBar } from "./AudioTimeline";

const FIELD_LABELS: Record<string, string> = {
  summary: "摘要", emotion: "情绪", tags: "标签", visual_tags: "视觉标签",
  scene_types: "场景类型", camera_movement: "运镜", time_of_day: "时间段",
  key_colors: "主色调", suggested_use: "建议用途", has_people: "有人物",
  people_description: "人物描述", genre: "曲风", energy_level: "能量",
  bpm: "BPM", quality_score: "质量分", mood: "氛围", instruments: "乐器",
  vocals: "人声", tempo: "节奏", composition: "构图",
  sections_summary: "段落结构", duration_sec: "时长", duration_summary_sec: "时长",
  structure_notes: "结构叙述",
};

function fmtVal(v: any): string {
  if (v === null || v === undefined || v === "") return "—";
  if (Array.isArray(v)) return v.map(String).join("、") || "—";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(1);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function fmtDuration(sec: number): string {
  const mm = Math.floor(sec / 60);
  const ss = Math.floor(sec % 60);
  return `${mm}:${String(ss).padStart(2, "0")} (${sec.toFixed(1)}s)`;
}

/** Structured value renderer: sections timeline + duration get special layout. */
function ValueCell({ k, v }: { k: string; v: any }) {
  // colored proportional section bar (falls back to plain text for prose)
  if (k === "sections_summary" && typeof v === "string" && v.trim()) {
    return <SectionsBar text={v} />;
  }
  if ((k === "duration_sec" || k === "duration_summary_sec") && typeof v === "number" && v > 0) {
    return <span>{fmtDuration(v)}</span>;
  }
  return <span>{fmtVal(v)}</span>;
}

function ColorSwatch({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try { await navigator.clipboard.writeText(value); }
    catch {
      const ta = document.createElement("textarea");
      ta.value = value; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch { /* ignore */ }
      document.body.removeChild(ta);
    }
    setCopied(true); window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <button onClick={copy} title={`点击复制 ${value}`}
      className="inline-flex items-center gap-1.5 rounded-md border border-white/10 bg-white/[0.04] py-1 pr-2 pl-1 transition-colors hover:border-white/30">
      <span className="h-4 w-4 shrink-0 rounded-sm border border-white/25" style={{ backgroundColor: value }} />
      <span className="font-mono text-[11px] text-slate-300">{copied ? "已复制 ✓" : value}</span>
    </button>
  );
}

export default function AssetPanel({ asset, onClose }: { asset: AssetInfo; onClose: () => void }) {
  const isAudio = asset.asset_type === "audio";
  const ann = asset.annotation ?? {};
  const src = mediaUrl(asset.path);
  // timed sections for the audio timeline (parsed from sections_summary),
  // with per-section instruments when the annotation carries them
  const sectionTimes = isAudio && typeof ann.sections_summary === "string"
    ? attachInstruments(parseSectionTimes(ann.sections_summary), ann.sections_detail) : [];
  const audioDur = typeof ann.duration_sec === "number" && ann.duration_sec > 0
    ? ann.duration_sec : (sectionTimes.length ? sectionTimes[sectionTimes.length - 1].end : 0);
  // Show only populated fields — same rule as the library's AnnotationTable, so
  // both views agree. (Previously kept empty known fields as "—", which spammed
  // audio clips with irrelevant video-schema blanks like 曲风/情绪.)
  // When the sections timeline is shown, drop the redundant text row.
  const entries = Object.entries(ann).filter(([k, v]) => {
    if (k === "key_colors") return false;   // rendered as swatches above
    if (k === "sections_summary" && sectionTimes.length > 0) return false;
    if (k === "sections_detail") return false;   // rendered on the timeline labels
    const empty = v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0);
    return !empty;
  });
  const known = entries.filter(([k]) => FIELD_LABELS[k]);
  const unknown = entries.filter(([k]) => !FIELD_LABELS[k]);
  const rows = [...known, ...unknown];
  const colors: string[] = Array.isArray(ann.key_colors) ? ann.key_colors.filter((c: any) => typeof c === "string" && c.trim()) : [];

  return (
    <div className="flex h-full w-full flex-col overflow-hidden rounded-2xl border border-sky-500/25 bg-slate-950/95 shadow-[0_8px_40px_rgba(0,0,0,0.6)]">
      <div className="flex items-center gap-2 border-b border-white/[0.07] bg-white/[0.03] px-4 py-2.5">
        <span className={cn("flex h-5 w-5 items-center justify-center rounded-md",
          isAudio ? "bg-violet-500/15 text-violet-300" : "bg-sky-500/15 text-sky-300")}>
          {isAudio ? <Music2 className="h-3 w-3" /> : <Film className="h-3 w-3" />}
        </span>
        <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-slate-200" title={asset.path}>
          {asset.file_name}
        </span>
        <span className="shrink-0 text-[10px] text-slate-500">{isAudio ? "音乐分析" : "视频理解"}</span>
        <button className="text-slate-500 hover:text-slate-300" onClick={onClose}><X className="h-4 w-4" /></button>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-3 p-4">
          {/* media preview: custom waveform player (with section overlay) for
              audio, native player for video */}
          {isAudio
            ? (sectionTimes.length > 0
                ? <AudioTimeline src={src} sections={sectionTimes} duration={audioDur} keypointsPath={asset.path} />
                : <audio src={src} controls className="w-full" />)
            : <video src={src} controls className="max-h-[220px] w-full rounded-lg bg-black" />}

          {!asset.annotated ? (
            <div className="rounded-lg border border-amber-500/25 bg-amber-500/[0.06] px-3 py-2.5 text-xs text-amber-300/90">
              该素材还没有标注 — 在「素材库」标注后这里会显示摘要、标签、主色调等。
            </div>
          ) : (
            <>
              {colors.length > 0 && (
                <div>
                  <div className="mb-1 text-[11px] font-semibold text-slate-400">主色调（点击复制）</div>
                  <div className="flex flex-wrap gap-1.5">
                    {colors.map((c, i) => <ColorSwatch key={i} value={c.trim()} />)}
                  </div>
                </div>
              )}
              <table className="w-full">
                <tbody>
                  {rows.map(([k, v]) => (
                    <tr key={k} className="border-b border-white/[0.05] last:border-0">
                      <td className="w-24 py-1.5 pr-3 align-top text-[11px] text-slate-500">{FIELD_LABELS[k] ?? k}</td>
                      <td className="py-1.5 text-[12px] text-slate-200"><ValueCell k={k} v={v} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}

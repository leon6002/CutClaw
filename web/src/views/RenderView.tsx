import { useEffect, useRef, useState } from "react";
import { Download, GanttChartSquare, Loader2, Play, RotateCw, Video } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { api, mediaUrl, useJob } from "../api";
import JobLog from "../components/JobLog";
import { ShotTimeline } from "../components/Charts";
import { ClipCaption, ClipInspector, activeClipAt, useClipMap } from "../components/ClipInspector";
import type { PipelineStatus, ProjectState } from "../App";

interface RecentProject {
  shot_point: string;
  project: string;
  instruction_id: string;
  mtime: number;
}
interface Output { ratio: string; path: string; size_mb: number; mtime: number }

const RATIOS = ["9:16", "16:9", "1:1"];
// max preview width per ratio (height capped at 480 so player + chart fit side by side)
const PREVIEW_W: Record<string, number> = { "9:16": 280, "16:9": 620, "1:1": 460 };

export default function RenderView({
  project, setProject, pipelineStatus, onOutputsCount,
}: {
  project: ProjectState;
  setProject: (fn: (p: ProjectState) => ProjectState) => void;
  pipelineStatus: PipelineStatus;
  onOutputsCount?: (n: number) => void;
}) {
  const [recent, setRecent] = useState<RecentProject[]>([]);
  const [outputs, setOutputs] = useState<Output[]>([]);
  const [hasEnding, setHasEnding] = useState(false);
  const [addEnding, setAddEnding] = useState(false);
  const [transitionMode, setTransitionMode] = useState<"none" | "uniform" | "ai">("none");
  const [sourceQuality, setSourceQuality] = useState<"proxy" | "original">("proxy");
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [renderRatio, setRenderRatio] = useState("");
  const [spExists, setSpExists] = useState<boolean | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [activeRatio, setActiveRatio] = useState("");
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const job = useJob(jobId);

  const seekTo = (t: number) => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = t;
    setPlayhead(t);
    if (v.paused) v.play().catch(() => {});
  };

  const shotPoint = project.shotPoint;
  const { clips: clipMap, error: clipMapError, reload: reloadClipMap } = useClipMap(shotPoint);
  const activeIdx = activeClipAt(clipMap, playhead);
  const activeClip = activeIdx >= 0 && clipMap ? clipMap[activeIdx] : null;
  const activeOutput = outputs.find((o) => o.ratio === activeRatio) ?? outputs[0];

  const refreshRecent = () => {
    api<RecentProject[]>("/api/project/recent").then(setRecent).catch(() => {});
  };

  const refreshOutputs = (sp: string) => {
    if (!sp) { setOutputs([]); setSpExists(null); onOutputsCount?.(0); return; }
    api<any>(`/api/render/outputs?shot_point=${encodeURIComponent(sp)}`).then((r) => {
      setOutputs(r.outputs);
      setHasEnding(r.has_ending_video);
      setSpExists(!!r.shot_point_exists);
      onOutputsCount?.(r.outputs.length);
    }).catch(() => {});
  };

  // Self-heal: legacy projects may reference a shot_point path derived with an
  // old (wrong) scheme. If the file doesn't exist, re-point to the on-disk
  // result with the SAME filename (identical instruction id).
  useEffect(() => {
    if (spExists !== false || !shotPoint || recent.length === 0) return;
    const base = shotPoint.split(/[\\/]/).pop();
    const match = recent.find((r) => r.shot_point.split(/[\\/]/).pop() === base);
    if (match && match.shot_point !== shotPoint) {
      setProject((p) => ({ ...p, shotPoint: match.shot_point }));
    }
  }, [spExists, recent, shotPoint]);

  // reattach to a render still running server-side after a page refresh
  useEffect(() => {
    api<any>("/api/jobs/current/render")
      .then((r) => { if (r.job) setJobId(r.job.id); }).catch(() => {});
  }, []);

  useEffect(() => { refreshRecent(); }, [pipelineStatus]);
  useEffect(() => { refreshOutputs(shotPoint); }, [shotPoint]);
  useEffect(() => {
    if (job.status === "done") { refreshOutputs(shotPoint); setRenderRatio(""); }
  }, [job.status]);

  const rendering = job.status === "running";

  const render = async (ratio: string) => {
    setError(""); setRenderRatio(ratio);
    try {
      const r = await api<any>("/api/render", {
        method: "POST",
        body: JSON.stringify({
          shot_point: shotPoint,
          video_path: project.effectiveVideo || project.videos[0] || "",
          audio_path: project.audio,
          ratio, add_ending: addEnding, has_dialogue: project.hasDialogue,
          transition: transitionMode === "uniform" ? 0.4 : 0,
          transition_mode: transitionMode === "ai" ? "ai" : "",
          source_quality: sourceQuality,
        }),
      });
      setJobId(r.job_id);
    } catch (e: any) { setError(e.message); setRenderRatio(""); }
  };

  const glass = "rounded-2xl border-white/[0.07] bg-slate-900/50";

  if (pipelineStatus === "running") {
    return (
      <div className="rounded-lg border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-300">
        流水线运行中 — 完成后可在此渲染。
      </div>
    );
  }

  return (
    <div>
      <Card className={cn(glass, rendering && "border-beam")}>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Video className="h-4 w-4 text-cyan-400" /> 渲染导出
          </CardTitle>
        </CardHeader>
        <CardContent>
          {recent.length === 0 && !shotPoint ? (
            <div className="py-8 text-center text-sm text-slate-500">
              本项目还没有可渲染的结果 — 先在「项目编辑」中运行流水线
            </div>
          ) : (
            <>
              <div className="mb-4">
                <div className="mb-1.5 text-xs text-slate-400">
                  剪辑结果（本项目运行流水线后自动填入，也可手动挂载历史结果）
                </div>
                <Select
                  value={shotPoint || undefined}
                  onValueChange={(v) => setProject((p) => ({ ...p, shotPoint: v }))}
                >
                  <SelectTrigger className="w-full border-white/10 bg-black/25 text-xs">
                    <SelectValue placeholder="— 选择一个剪辑结果 —" />
                  </SelectTrigger>
                  <SelectContent>
                    {!recent.some((r) => r.shot_point === shotPoint) && shotPoint && (
                      <SelectItem value={shotPoint} className="text-xs">{shotPoint}</SelectItem>
                    )}
                    {recent.map((r) => (
                      <SelectItem key={r.shot_point} value={r.shot_point} className="text-xs">
                        {r.project} / {r.instruction_id} · {new Date(r.mtime * 1000).toLocaleString()}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {hasEnding && (
                <label className="mb-2 flex cursor-pointer items-center gap-2.5 text-[13px] text-slate-300">
                  <Checkbox checked={addEnding} onCheckedChange={(v) => setAddEnding(v === true)} />
                  追加片尾视频
                </label>
              )}
              <div className="mb-4 flex flex-wrap items-center gap-2.5 text-[13px] text-slate-300">
                <span>画面转场</span>
                <div className="flex gap-1 rounded-lg border border-white/10 bg-white/[0.03] p-0.5">
                  {([
                    ["none", "无（硬切）"],
                    ["uniform", "统一叠化"],
                    ["ai", "AI 智能转场"],
                  ] as const).map(([v, label]) => (
                    <button
                      key={v}
                      className={cn(
                        "rounded-md px-2.5 py-1 text-xs transition-colors",
                        transitionMode === v
                          ? "bg-cyan-500/15 font-semibold text-cyan-300"
                          : "text-slate-400 hover:text-slate-200",
                      )}
                      onClick={() => setTransitionMode(v)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <span className="text-[11px] text-slate-500">
                  {transitionMode === "ai"
                    ? "AI 按每个切点的画面内容，从 12 种转场里挑（快切为主，仅在情绪转折处加转场）"
                    : transitionMode === "uniform"
                      ? "所有切点统一 0.4s 交叉溶解"
                      : "快节奏卡点推荐（硬切更带感）"}
                </span>
              </div>

              <div className="mb-4 flex flex-wrap items-center gap-2.5 text-[13px] text-slate-300">
                <span>渲染源</span>
                <div className="flex gap-1 rounded-lg border border-white/10 bg-white/[0.03] p-0.5">
                  {([
                    ["proxy", "1080p 代理（快）"],
                    ["original", "4K 原片"],
                  ] as const).map(([v, label]) => (
                    <button
                      key={v}
                      className={cn(
                        "rounded-md px-2.5 py-1 text-xs transition-colors",
                        sourceQuality === v
                          ? "bg-cyan-500/15 font-semibold text-cyan-300"
                          : "text-slate-400 hover:text-slate-200",
                      )}
                      onClick={() => setSourceQuality(v)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <span className="text-[11px] text-slate-500">
                  {sourceQuality === "original"
                    ? "Immich 素材将拉取/直读 4K 原片渲染（首次较慢，之后缓存）"
                    : "发抖音等平台 1080p 足够 — 本地 4K 素材不受此选项影响"}
                </span>
              </div>

              <div className="flex flex-wrap gap-2">
                {RATIOS.map((r) => (
                  <Button
                    key={r}
                    className="gap-1.5 bg-cyan-500 font-semibold text-slate-950 shadow-[0_0_14px_rgba(34,211,238,0.3)] hover:bg-cyan-400"
                    disabled={rendering || !shotPoint}
                    onClick={() => render(r)}
                  >
                    {rendering && renderRatio === r
                      ? <Loader2 className="h-4 w-4 animate-spin" />
                      : <Play className="h-4 w-4" />}
                    渲染 {r}
                  </Button>
                ))}
                <Button variant="outline" className="gap-1.5 border-white/10 bg-white/[0.04]"
                  onClick={() => { refreshRecent(); refreshOutputs(shotPoint); }}>
                  <RotateCw className="h-3.5 w-3.5" /> 刷新
                </Button>
              </div>
            </>
          )}

          {error && (
            <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-400">{error}</div>
          )}
          {job.status === "error" && (
            <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-400">渲染失败 — 查看日志</div>
          )}
          {job.status === "done" && (
            <div className="mt-3 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2.5 text-sm text-emerald-400">渲染完成！</div>
          )}
          {(rendering || job.status === "error") && <JobLog lines={job.lines} height={240} />}
        </CardContent>
      </Card>

      {shotPoint && (
        <Card className={cn(glass, "mt-4")} style={{ backdropFilter: "none", WebkitBackdropFilter: "none" }}>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <GanttChartSquare className="h-4 w-4 text-cyan-400" /> 成片预览与解析
              <span className="text-[11px] font-normal text-slate-500">
                播放视频 — 时间轴游标与镜头解析实时联动 · 对照「编剧想要」vs「VLM 实际看到」
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {/* top: player (centered) */}
            <div className="flex flex-col items-center">
              {outputs.length > 1 && (
                <div className="mb-2 flex gap-1.5 self-start">
                  {outputs.map((o) => (
                    <button
                      key={o.ratio}
                      className={cn(
                        "rounded-md border px-2.5 py-1 text-xs transition-colors",
                        o.ratio === activeOutput?.ratio
                          ? "border-cyan-400/60 bg-cyan-400/15 font-semibold text-cyan-300"
                          : "border-white/10 bg-white/[0.04] text-slate-400 hover:bg-white/[0.08]",
                      )}
                      onClick={() => { setActiveRatio(o.ratio); setPlayhead(0); }}
                    >
                      {o.ratio}
                    </button>
                  ))}
                </div>
              )}
              {activeOutput ? (
                <>
                  <video
                    ref={videoRef}
                    key={activeOutput.ratio}
                    src={mediaUrl(activeOutput.path) + `&t=${activeOutput.mtime}`} controls playsInline
                    className="rounded-lg bg-black"
                    style={{ maxHeight: 520, maxWidth: PREVIEW_W[activeOutput.ratio] ?? 720 }}
                    onTimeUpdate={(e) => setPlayhead((e.target as HTMLVideoElement).currentTime)}
                  />
                  <div className="w-full" style={{ maxWidth: 720 }}>
                    <ClipCaption clip={activeClip} />
                  </div>
                  <div className="mt-2 flex items-center gap-2 text-xs text-slate-400">
                    {activeOutput.ratio} · {activeOutput.size_mb}MB · {new Date(activeOutput.mtime * 1000).toLocaleString()}
                    <Button asChild variant="outline" size="sm" className="h-6 gap-1 border-white/10 bg-white/[0.04] px-2 text-[11px]">
                      <a href={mediaUrl(activeOutput.path)} download>
                        <Download className="h-3 w-3" /> 下载
                      </a>
                    </Button>
                  </div>
                </>
              ) : (
                <div className="flex h-[180px] w-full items-center justify-center rounded-lg border border-dashed border-white/10 text-xs text-slate-500">
                  还没有渲染结果 — 点击上方「渲染」
                </div>
              )}
            </div>

            {/* below: timeline chart (progress bar) + per-shot inspector, synced to playhead */}
            <div className="mt-4">
              <ShotTimeline shotPoint={shotPoint} playhead={playhead} />
            </div>
            <div className="mt-3">
              <ClipInspector clips={clipMap} currentTime={playhead} error={clipMapError} onRetry={reloadClipMap} onSeek={seekTo} />
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

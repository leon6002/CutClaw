import { useEffect, useState } from "react";
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
import type { PipelineStatus, ProjectState } from "../App";

interface RecentProject {
  shot_point: string;
  project: string;
  instruction_id: string;
  mtime: number;
}
interface Output { ratio: string; path: string; size_mb: number; mtime: number }

const RATIOS = ["9:16", "16:9", "1:1"];
const WIDTH: Record<string, number> = { "9:16": 240, "16:9": 480, "1:1": 320 };

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
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [renderRatio, setRenderRatio] = useState("");
  const job = useJob(jobId);

  const shotPoint = project.shotPoint;

  const refreshRecent = () => {
    api<RecentProject[]>("/api/project/recent").then(setRecent).catch(() => {});
  };

  const refreshOutputs = (sp: string) => {
    if (!sp) { setOutputs([]); onOutputsCount?.(0); return; }
    api<any>(`/api/render/outputs?shot_point=${encodeURIComponent(sp)}`).then((r) => {
      setOutputs(r.outputs);
      setHasEnding(r.has_ending_video);
      onOutputsCount?.(r.outputs.length);
    }).catch(() => {});
  };

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
                <label className="mb-4 flex cursor-pointer items-center gap-2.5 text-[13px] text-slate-300">
                  <Checkbox checked={addEnding} onCheckedChange={(v) => setAddEnding(v === true)} />
                  追加片尾视频
                </label>
              )}

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
        <Card className={cn(glass, "mt-4")}>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <GanttChartSquare className="h-4 w-4 text-cyan-400" /> 成片时间轴
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ShotTimeline shotPoint={shotPoint} />
          </CardContent>
        </Card>
      )}

      {outputs.length > 0 && (
        <>
          <div className="mt-5 mb-3 text-xs font-semibold tracking-wider text-slate-400 uppercase">预览</div>
          <div className="flex flex-wrap items-start gap-4">
            {outputs.map((o) => (
              <Card key={o.ratio} className={glass}>
                <CardContent className="p-3">
                  <div className="mb-2 text-xs text-slate-400">
                    {o.ratio} · {o.size_mb}MB · {new Date(o.mtime * 1000).toLocaleString()}
                  </div>
                  <video
                    src={mediaUrl(o.path) + `&t=${o.mtime}`} controls
                    className="rounded-lg bg-black" style={{ width: WIDTH[o.ratio] ?? 320, maxHeight: 430 }}
                  />
                  <div className="mt-2.5">
                    <Button asChild variant="outline" size="sm" className="h-7 gap-1.5 border-white/10 bg-white/[0.04] text-xs">
                      <a href={mediaUrl(o.path)} download>
                        <Download className="h-3.5 w-3.5" /> 下载 output_{o.ratio.replace(":", "x")}.mp4
                      </a>
                    </Button>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

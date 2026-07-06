import { useEffect, useState } from "react";
import {
  AudioLines, LayoutGrid, Lightbulb, Loader2, Mic, PenLine, Play,
  Scissors, Settings2, Square, TerminalSquare, Video,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { api, fmtDuration, mediaUrl, type JobState } from "../api";
import { RoleModelSelect } from "../components/ModelConfig";
import JobLog from "../components/JobLog";
import AgentFlow from "../components/AgentFlow";
import TaskGrids from "../components/TaskGrids";
import AgentWorkbench from "../components/AgentWorkbench";
import WorkflowCanvas, { type AssetInfo, type ShotInfo } from "../components/flow/WorkflowCanvas";
import AssetPanel from "../components/flow/AssetPanel";
import ClipPlayer from "../components/flow/ClipPlayer";
import type { PipelineStatus, ProjectState } from "../App";

const basename = (p: string) => p.split(/[\\/]/).pop() || p;

const PIPELINE_STEPS = [
  { key: "shot_detection", label: "镜头检测", icon: <LayoutGrid className="h-4 w-4" /> },
  { key: "asr", label: "语音识别", icon: <Mic className="h-4 w-4" /> },
  { key: "video_captioning", label: "视频理解", icon: <Video className="h-4 w-4" /> },
  { key: "audio_analysis", label: "音频分析", icon: <AudioLines className="h-4 w-4" /> },
  { key: "screenwriter", label: "AI 编剧", icon: <PenLine className="h-4 w-4" /> },
  { key: "editor", label: "AI 剪辑", icon: <Scissors className="h-4 w-4" /> },
];

const FieldLabel = ({ children }: { children: React.ReactNode }) => (
  <div className="mb-1.5 text-xs text-slate-400">{children}</div>
);

const NONE = "__none__";

export default function EditorView({
  project, setProject, pipelineStatus, pipelineJobId, setPipelineJobId, pipelineJob,
}: {
  project: ProjectState;
  setProject: (fn: (p: ProjectState) => ProjectState) => void;
  pipelineStatus: PipelineStatus;
  pipelineJobId: string | null;
  setPipelineJobId: (id: string | null) => void;
  pipelineJob: JobState;
}) {
  const [videoFiles, setVideoFiles] = useState<string[]>([]);
  const [audioFiles, setAudioFiles] = useState<string[]>([]);
  const [srtFiles, setSrtFiles] = useState<string[]>([]);
  const [customVideo, setCustomVideo] = useState("");
  const [error, setError] = useState("");
  const [wb, setWb] = useState<{ task: string; idx?: number } | null>(null);
  const [assetView, setAssetView] = useState<AssetInfo | null>(null);
  const [assets, setAssets] = useState<AssetInfo[]>([]);
  const [clipView, setClipView] = useState<ShotInfo | null>(null);
  const [shots, setShots] = useState<ShotInfo[]>([]);
  const [monitorView, setMonitorView] = useState<"canvas" | "grid">("canvas");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [suggesting, setSuggesting] = useState(false);
  const [sugError, setSugError] = useState("");
  const [paramSug, setParamSug] = useState<{ target_length: number; shot_length: number; rationale: string } | null>(null);
  const [paramSugLoading, setParamSugLoading] = useState(false);
  const [paramSugError, setParamSugError] = useState("");
  // multi-music fusion pre-step status (runs right before the pipeline)
  const [fusion, setFusion] = useState<{ state: "running" | "done" | "error"; detail: string } | null>(null);
  // inline media preview — one shared player, no detail page needed
  const [preview, setPreview] = useState<{ kind: "video" | "audio"; path: string } | null>(null);
  const previewToggle = (kind: "video" | "audio", path: string) =>
    setPreview((cur) => (cur?.path === path ? null : { kind, path }));

  const p = project;
  const job = pipelineJob;
  const set = (patch: Partial<ProjectState>) => setProject((old) => ({ ...old, ...patch }));

  useEffect(() => {
    api<string[]>("/api/files?kind=video").then(setVideoFiles).catch(() => {});
    api<string[]>("/api/files?kind=audio").then(setAudioFiles).catch(() => {});
    api<string[]>("/api/files?kind=srt").then(setSrtFiles).catch(() => {});
  }, []);

  // Reattach THIS project's latest pipeline run (page refresh / project switch).
  useEffect(() => {
    api<any>(`/api/pipeline/current?project_id=${encodeURIComponent(p.id)}`).then((r) => {
      const j = r.job;
      if (!j) return;
      setPipelineJobId(j.id);
      if (j.status === "running") {
        setProject((old) => ({
          ...old,
          shotPoint: j.meta.shot_point || old.shotPoint,
          effectiveVideo: j.meta.effective_video || old.effectiveVideo,
        }));
      }
    }).catch(() => {});
  }, [p.id]);

  // Resolve the project's selected assets to their cached annotations so the
  // pipeline canvas can show which videos/audio are in play + open each's标注.
  useEffect(() => {
    const paths = [...p.videos, p.audio].filter(Boolean);
    if (paths.length === 0) { setAssets([]); return; }
    api<{ assets: AssetInfo[] }>("/api/assets/by_paths", {
      method: "POST", body: JSON.stringify({ paths }),
    }).then((r) => setAssets(r.assets ?? [])).catch(() => setAssets([]));
  }, [p.videos, p.audio]);

  // Poll the final selected shots (shot_point.json) so the canvas can show each
  // shot's chosen source + time slice and preview it. Grows during the run.
  useEffect(() => {
    if (!pipelineJobId) { setShots([]); return; }
    let stop = false;
    const fetchShots = () => api<{ shots: ShotInfo[] }>(`/api/pipeline/shots?project_id=${encodeURIComponent(p.id)}`)
      .then((r) => { if (!stop) setShots(r.shots ?? []); }).catch(() => {});
    fetchShots();
    if (pipelineStatus !== "running") return;
    const t = window.setInterval(fetchShots, 2500);
    return () => { stop = true; window.clearInterval(t); };
  }, [pipelineJobId, pipelineStatus, p.id, job.status]);

  const running = pipelineStatus === "running";
  const allVideoOptions = Array.from(new Set([...p.videos, ...videoFiles]));
  const allAudioOptions = Array.from(new Set([p.audio, ...audioFiles].filter(Boolean)));

  const toggleVideo = (v: string) => {
    set({ videos: p.videos.includes(v) ? p.videos.filter((x) => x !== v) : [...p.videos, v] });
  };

  const start = async () => {
    setError("");
    let audioPath = p.audio;
    // WORKFLOW PRE-STEP: multiple songs were selected → fuse them into ONE
    // BGM HERE, where the target length is actually known (fusing at asset-
    // apply time once produced a 51s track for a 220s film). The fused mp3
    // becomes the project audio; re-running with the same set+target reuses
    // the previous fusion (audio already points at a BGMmix file).
    const needFuse = (p.audios?.length ?? 0) > 1
      && !/BGMmix/i.test(p.audio.split(/[\\/]/).pop() ?? "");
    if (needFuse) {
      setFusion({ state: "running", detail: `AI 正在把 ${p.audios!.length} 首音乐按目标 ${Math.round(p.targetLength * 1.25 + 20)}s 融合…` });
      try {
        const r = await api<any>("/api/bgm/stitch", {
          method: "POST",
          body: JSON.stringify({
            mode: "ai",
            target_sec: Math.round(p.targetLength * 1.25 + 20),
            tracks: p.audios!.map((a) => ({ path: a })),
            video_paths: p.videos,
          }),
        });
        audioPath = r.path;
        set({ audio: r.path });
        const planTxt = (r.plan ?? [])
          .map((x: any, i: number) => `${i + 1}.[${x.role || "段"}] ${x.track} ${Math.round(x.start)}–${Math.round(x.end)}s`)
          .join("  ");
        setFusion({ state: "done", detail: `已融合为 ${Math.round(r.meta?.total ?? 0)}s:${planTxt}${r.why ? " — " + r.why : ""}` });
      } catch (e: any) {
        setFusion({ state: "error", detail: `融合失败:${e.message} — 已改用第一首音乐` });
        audioPath = p.audios![0];
        set({ audio: audioPath });
      }
    }
    try {
      const r = await api<any>("/api/pipeline/start", {
        method: "POST",
        body: JSON.stringify({
          video_paths: p.videos, audio_path: audioPath, instruction: p.instruction,
          has_dialogue: p.hasDialogue, main_character: p.mainCharacter, srt_path: p.srt,
          target_length: p.targetLength, shot_length: p.shotLength,
          project_id: p.id,
        }),
      });
      set({ shotPoint: r.shot_point, effectiveVideo: r.effective_video });
      setPipelineJobId(r.job_id);
    } catch (e: any) { setError(e.message); }
  };

  const stop = async () => {
    try { await api("/api/pipeline/stop", { method: "POST" }); } catch { /* ignore */ }
  };

  // Re-generate one shot: drop its pick server-side, then resume the pipeline
  // (cached analysis/plan → only this shot is redone).
  const retryShot = async (sectionIdx: number, shotIdx: number) => {
    if (running) { setError("流水线正在运行，请先停止再重试单个镜头。"); return; }
    if (!window.confirm(`重新生成镜头 S${sectionIdx + 1}·Shot${shotIdx + 1}？将重跑流水线，仅补选这个镜头。`)) return;
    setError("");
    try {
      const r = await api<any>("/api/pipeline/retry_shot", {
        method: "POST",
        body: JSON.stringify({ project_id: p.id, section_idx: sectionIdx, shot_idx: shotIdx }),
      });
      if (r.shot_point) set({ shotPoint: r.shot_point });
      setPipelineJobId(r.job_id);
    } catch (e: any) { setError(e.message); }
  };

  const fetchParamSug = async () => {
    setParamSugError(""); setParamSugLoading(true);
    try {
      const r = await api<{ target_length: number; shot_length: number; rationale: string }>(
        "/api/params/suggestions",
        { method: "POST", body: JSON.stringify({ project_id: p.id }) },
      );
      setParamSug(r);
    } catch (e: any) { setParamSugError(e.message); }
    setParamSugLoading(false);
  };

  const fetchSuggestions = async () => {
    setSugError(""); setSuggesting(true);
    try {
      const r = await api<{ suggestions: string[] }>("/api/instruction/suggestions", {
        method: "POST", body: JSON.stringify({ project_id: p.id }),
      });
      setSuggestions(r.suggestions);
    } catch (e: any) { setSugError(e.message); }
    setSuggesting(false);
  };

  const stageTimes: Record<string, number> = job.meta.stage_times ?? {};
  const stagesView: Record<string, any> = {};
  for (const s of PIPELINE_STEPS) {
    const status = (job.meta.stages ?? {})[s.key] ?? "pending";
    stagesView[s.key] = { status, detail: stageTimes[s.key] ? fmtDuration(stageTimes[s.key]) : "" };
  }

  const glass = "rounded-2xl border-white/[0.07] bg-slate-900/50";

  return (
    <div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
        <Card className={glass}>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Scissors className="h-4 w-4 text-cyan-400" /> 项目设置
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="mb-4">
              <FieldLabel>视频素材（可多选，按顺序拼接）</FieldLabel>
              <ScrollArea className="h-[180px] rounded-lg border border-white/10 bg-black/25 px-3 py-1.5">
                {allVideoOptions.length === 0 && (
                  <div className="py-3 text-xs text-slate-500">resource/video 下没有视频 — 可在下方粘贴路径添加</div>
                )}
                {allVideoOptions.map((v) => (
                  <label key={v} className="group flex cursor-pointer items-center gap-2.5 py-1.5">
                    <Checkbox
                      checked={p.videos.includes(v)} disabled={running}
                      onCheckedChange={() => toggleVideo(v)}
                    />
                    <span className="min-w-0 flex-1 truncate text-[13px] text-slate-300" title={v}>{basename(v)}</span>
                    <button
                      className={cn("shrink-0 rounded px-1.5 text-[11px]",
                        preview?.path === v ? "text-cyan-300" : "text-slate-600 opacity-0 group-hover:opacity-100 hover:text-cyan-300")}
                      title="预览播放"
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); previewToggle("video", v); }}
                    >{preview?.path === v ? "⏹" : "▶"}</button>
                  </label>
                ))}
              </ScrollArea>
              {preview?.kind === "video" && (
                <div className="mt-2 rounded-lg border border-cyan-500/25 bg-black/40 p-2">
                  <div className="mb-1 flex items-center text-[11px] text-slate-400">
                    <span className="min-w-0 truncate">▶ {basename(preview.path)}</span>
                    <button className="ml-auto shrink-0 text-slate-500 hover:text-slate-300" onClick={() => setPreview(null)}>✕</button>
                  </div>
                  <video src={mediaUrl(preview.path)} controls autoPlay className="max-h-[240px] w-full rounded bg-black" />
                </div>
              )}
              <div className="mt-2 flex gap-2">
                <Input
                  className="h-8 flex-1 text-xs" placeholder="粘贴视频路径回车添加…"
                  value={customVideo} disabled={running}
                  onChange={(e) => setCustomVideo(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && customVideo.trim()) {
                      set({ videos: [...p.videos, customVideo.trim()] });
                      setCustomVideo("");
                    }
                  }}
                />
                <Button variant="outline" size="sm" className="h-8"
                  disabled={running || !customVideo.trim()}
                  onClick={() => { set({ videos: [...p.videos, customVideo.trim()] }); setCustomVideo(""); }}>
                  添加
                </Button>
              </div>
            </div>

            <div className="mb-4">
              <FieldLabel>音乐{(p.audios?.length ?? 0) > 1 ? `(${p.audios!.length} 首 · 运行时 AI 融合)` : ""}</FieldLabel>
              {(p.audios?.length ?? 0) > 1 ? (
                <div className="rounded-lg border border-violet-500/25 bg-black/25 p-2">
                  <div className="space-y-1">
                    {p.audios!.map((a, i) => (
                      <div key={a} className="group flex items-center gap-2 text-xs text-slate-300">
                        <span className="flex h-4.5 w-4.5 shrink-0 items-center justify-center rounded-full border border-violet-400/50 bg-violet-500/15 font-mono text-[10px] text-violet-300">{i + 1}</span>
                        <span className="min-w-0 flex-1 truncate">{basename(a)}</span>
                        <button
                          className={cn("shrink-0 rounded px-1 text-[11px]",
                            preview?.path === a ? "text-violet-300" : "text-slate-600 opacity-0 group-hover:opacity-100 hover:text-violet-300")}
                          title="试听"
                          onClick={() => previewToggle("audio", a)}
                        >{preview?.path === a ? "⏹" : "▶"}</button>
                        <button
                          className="shrink-0 text-slate-600 hover:text-red-400"
                          title="从融合列表移除"
                          disabled={running}
                          onClick={() => {
                            const next = p.audios!.filter((x) => x !== a);
                            set({ audios: next, audio: next[0] || "" });
                          }}
                        >✕</button>
                      </div>
                    ))}
                  </div>
                  {!/BGMmix/i.test(p.audio.split(/[\\/]/).pop() ?? "") && (
                    <div className="mt-1.5 border-t border-white/[0.06] pt-1.5 text-[10.5px] text-violet-300/70">
                      运行时会按目标时长 AI 融合成一条 BGM
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <Select value={p.audio || undefined} onValueChange={(v) => set({ audio: v === NONE ? "" : v, audios: v === NONE ? [] : [v] })} disabled={running}>
                    <SelectTrigger className="w-full border-white/10 bg-black/25">
                      <SelectValue placeholder="选择音频…" />
                    </SelectTrigger>
                    <SelectContent>
                      {allAudioOptions.map((a) => (
                        <SelectItem key={a} value={a}>{basename(a)}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {p.audio && (
                    <button
                      className={cn("shrink-0 rounded border border-white/10 px-2 py-1.5 text-xs",
                        preview?.path === p.audio ? "text-violet-300" : "text-slate-500 hover:text-violet-300")}
                      title="试听" onClick={() => previewToggle("audio", p.audio)}
                    >{preview?.path === p.audio ? "⏹" : "▶"}</button>
                  )}
                </div>
              )}
              {/* 合成 BGM — a project ARTIFACT, shown apart from the raw tracks */}
              {(p.audios?.length ?? 0) > 1 && /BGMmix/i.test(p.audio.split(/[\\/]/).pop() ?? "") && (
                <div className="mt-2 rounded-lg border border-emerald-500/25 bg-emerald-500/[0.05] p-2">
                  <div className="flex items-center gap-2 text-xs text-emerald-300">
                    <span className="shrink-0">🤖 合成 BGM</span>
                    <span className="min-w-0 flex-1 truncate text-emerald-400/90" title={p.audio}>{basename(p.audio)}</span>
                    <button
                      className={cn("shrink-0 rounded px-1 text-[11px]",
                        preview?.path === p.audio ? "text-emerald-300" : "text-slate-500 hover:text-emerald-300")}
                      title="试听合成结果"
                      onClick={() => previewToggle("audio", p.audio)}
                    >{preview?.path === p.audio ? "⏹" : "▶"}</button>
                    <button
                      className="shrink-0 rounded border border-violet-400/40 bg-violet-500/10 px-1.5 py-0.5 text-[10.5px] text-violet-300 hover:bg-violet-500/20"
                      disabled={running}
                      title="丢弃这次融合结果,下次运行时按当前列表和目标时长重新融合"
                      onClick={() => set({ audio: p.audios![0] || "" })}
                    >重新融合</button>
                  </div>
                </div>
              )}
              {preview?.kind === "audio" && (
                <div className="mt-2 flex items-center gap-2 rounded-lg border border-violet-500/25 bg-black/40 p-2">
                  <span className="min-w-0 shrink truncate text-[11px] text-slate-400">🎵 {basename(preview.path)}</span>
                  <audio src={mediaUrl(preview.path)} controls autoPlay className="h-8 min-w-0 flex-1" />
                  <button className="shrink-0 text-slate-500 hover:text-slate-300" onClick={() => setPreview(null)}>✕</button>
                </div>
              )}
            </div>

            <div>
              <FieldLabel>剪辑指令</FieldLabel>
              <Textarea
                rows={3} value={p.instruction} disabled={running}
                placeholder="描述你想要的剪辑效果…"
                className="border-white/10 bg-black/25"
                onChange={(e) => set({ instruction: e.target.value })}
              />
              <div className="mt-2">
                <Button variant="outline" size="sm" className="h-7 gap-1.5 border-white/10 bg-white/[0.04] text-xs"
                  disabled={running || suggesting} onClick={fetchSuggestions}>
                  {suggesting
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <Lightbulb className="h-3.5 w-3.5 text-amber-400" />}
                  AI 生成指令建议
                </Button>
                {sugError && <span className="ml-2 text-xs text-red-400">{sugError}</span>}
                {suggestions.map((s, i) => (
                  <div
                    key={i}
                    className={cn("suggestion-chip", p.instruction === s && "picked")}
                    onClick={() => !running && set({ instruction: s })}
                    title="点击填充为剪辑指令"
                  >
                    <Lightbulb className="mr-1.5 inline h-3.5 w-3.5 -translate-y-px text-amber-400" />{s}
                  </div>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>

        <Card className={glass}>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Settings2 className="h-4 w-4 text-cyan-400" /> 参数
            </CardTitle>
          </CardHeader>
          <CardContent>
            <label className="mb-4 flex cursor-pointer items-center gap-2.5 text-[13px] text-slate-300">
              <Checkbox
                checked={p.hasDialogue} disabled={running}
                onCheckedChange={(v) => set({ hasDialogue: v === true })}
              />
              包含对白（启用语音识别 / 角色识别）
            </label>

            {p.hasDialogue && (
              <>
                <div className="mb-4">
                  <FieldLabel>主角名字</FieldLabel>
                  <Input value={p.mainCharacter} disabled={running} className="border-white/10 bg-black/25"
                    onChange={(e) => set({ mainCharacter: e.target.value })} />
                </div>
                <div className="mb-4">
                  <FieldLabel>SRT 字幕（可选）</FieldLabel>
                  <Select value={p.srt || NONE} onValueChange={(v) => set({ srt: v === NONE ? "" : v })} disabled={running}>
                    <SelectTrigger className="w-full border-white/10 bg-black/25">
                      <SelectValue placeholder="无" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NONE}>无</SelectItem>
                      {srtFiles.map((s) => <SelectItem key={s} value={s}>{basename(s)}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
              </>
            )}

            <div className="mb-4 rounded-xl border border-white/[0.07] bg-white/[0.02] p-2.5">
              <div className="mb-2 text-xs text-slate-400">本次运行使用的模型（来自 API 池）</div>
              <div className="space-y-1.5">
                <RoleModelSelect role="vision" disabled={running} className="justify-between" />
                <RoleModelSelect role="audio" disabled={running} className="justify-between" />
                <RoleModelSelect role="agent" disabled={running} className="justify-between" />
              </div>
            </div>

            <div className="mb-2 flex items-center gap-3">
              <span className="w-32 text-xs text-slate-400">目标时长（秒）</span>
              <Input
                type="number" min={10} max={300} step={5} value={p.targetLength} disabled={running}
                className="h-8 w-24 border-white/10 bg-black/25"
                onChange={(e) => set({ targetLength: parseFloat(e.target.value) || 30 })}
              />
            </div>
            <div className="mb-2 flex items-center gap-2 text-[11px] text-slate-500">
              <span className="w-32 shrink-0" />
              单镜头时长由 AI 按音乐节奏自动决定（高潮快切、舒缓长留），无需设置。
            </div>

            <div className="mb-5">
              <Button variant="outline" size="sm"
                className="h-7 gap-1.5 border-white/10 bg-white/[0.04] text-xs"
                disabled={running || paramSugLoading || (p.videos.length === 0 && !p.audio)}
                onClick={fetchParamSug}>
                {paramSugLoading
                  ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  : <Lightbulb className="h-3.5 w-3.5 text-amber-400" />}
                AI 建议时长参数
              </Button>
              {paramSugError && <span className="ml-2 text-xs text-red-400">{paramSugError}</span>}
              {paramSug && (
                <div
                  className={cn(
                    "suggestion-chip",
                    p.targetLength === paramSug.target_length && "picked",
                  )}
                  onClick={() => !running && set({ targetLength: paramSug.target_length })}
                  title="点击应用建议参数"
                >
                  <Lightbulb className="mr-1.5 inline h-3.5 w-3.5 -translate-y-px text-amber-400" />
                  目标 <span className="font-semibold text-cyan-300">{paramSug.target_length}s</span>
                  {paramSug.rationale && <span className="text-slate-400"> — {paramSug.rationale}</span>}
                </div>
              )}
            </div>

            {(p.audios?.length ?? 0) > 1 && !/BGMmix/i.test(p.audio.split(/[\\/]/).pop() ?? "") && (
              <div className="mb-2 rounded-lg border border-violet-500/25 bg-violet-500/[0.06] px-3 py-2 text-[11.5px] text-violet-300/90">
                🎵 已选 {p.audios!.length} 首音乐 — 运行时会先按目标时长(约 {Math.round(p.targetLength * 1.25 + 20)}s)AI 融合成一条 BGM,再启动流水线。
              </div>
            )}
            {fusion && (
              <div className={cn(
                "mb-2 rounded-lg border px-3 py-2 text-[11.5px] leading-relaxed",
                fusion.state === "running" ? "border-violet-400/40 bg-violet-500/10 text-violet-200"
                  : fusion.state === "done" ? "border-emerald-500/30 bg-emerald-500/[0.07] text-emerald-300"
                    : "border-amber-500/30 bg-amber-500/[0.07] text-amber-300",
              )}>
                {fusion.state === "running" && <Loader2 className="mr-1.5 inline h-3.5 w-3.5 animate-spin" />}
                {fusion.state === "done" ? "✓ " : ""}步骤 0 · BGM 融合:{fusion.detail}
              </div>
            )}
            <div className="flex gap-2">
              <Button
                className="flex-1 gap-1.5 bg-cyan-500 font-semibold text-slate-950 shadow-[0_0_18px_rgba(34,211,238,0.35)] hover:bg-cyan-400"
                disabled={running || fusion?.state === "running" || p.videos.length === 0 || !p.audio || !p.instruction.trim()}
                onClick={start}
              >
                {fusion?.state === "running" ? <Loader2 className="h-4 w-4 animate-spin" />
                  : running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                {fusion?.state === "running" ? "AI 融合音乐中…" : running ? "运行中…" : "运行流水线"}
              </Button>
              <Button variant="destructive" className="gap-1.5 bg-red-500/15 text-red-400 hover:bg-red-500/25"
                disabled={!running} onClick={stop}>
                <Square className="h-3.5 w-3.5" /> 停止
              </Button>
            </div>
            {(p.videos.length === 0 || !p.audio || !p.instruction.trim()) && (
              <div className="mt-2 text-xs text-slate-500">
                {p.videos.length === 0 ? "请先选择视频素材（或在素材库中「智能选材」）。"
                  : !p.audio ? "请选择一首音乐。"
                  : "请填写剪辑指令。"}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {error && (
        <div className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-400">
          {error}
        </div>
      )}

      {(pipelineJobId || job.lines.length > 0) && (
        <Card className={cn("mt-4 rounded-2xl border-white/[0.07] bg-slate-900/50", running && "border-beam")}>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <TerminalSquare className="h-4 w-4 text-cyan-400" /> 流水线状态
            </CardTitle>
          </CardHeader>
          <CardContent>
            <AgentFlow steps={PIPELINE_STEPS} stages={stagesView} />

            {/* monitor view toggle: node canvas (agents) / dense grid */}
            <div className="mb-2 flex items-center gap-1 text-xs">
              <div className="flex gap-1 rounded-lg border border-white/10 bg-white/[0.03] p-0.5">
                {(["canvas", "grid"] as const).map((v) => (
                  <button
                    key={v}
                    onClick={() => setMonitorView(v)}
                    className={cn(
                      "rounded-md px-2.5 py-1 font-medium transition-colors",
                      monitorView === v ? "bg-cyan-500/15 text-cyan-300" : "text-slate-400 hover:text-slate-200",
                    )}
                  >
                    {v === "canvas" ? "节点画布" : "密度网格"}
                  </button>
                ))}
              </div>
              {monitorView === "canvas" && (
                <span className="text-[11px] text-slate-500">
                  点击步骤节点展开参数/思考 · 点击编剧节点看提示词 · 拖拽平移，滚轮缩放
                </span>
              )}
            </div>

            {monitorView === "canvas" && pipelineJobId ? (
              <>
                <WorkflowCanvas
                  jobId={pipelineJobId}
                  tasks={job.meta.tasks ?? {}}
                  jobRunning={running}
                  assets={assets}
                  onOpenAsset={(a) => { setAssetView(a); setWb(null); setClipView(null); }}
                  shots={shots}
                  onOpenClip={(s) => { setClipView(s); setAssetView(null); setWb(null); }}
                  onOpenScreenwriter={() => { setWb({ task: "screenwriter_llm" }); setAssetView(null); setClipView(null); }}
                  onRetryShot={retryShot}
                  // overlay renders INSIDE the canvas (also visible in fullscreen);
                  // priority: clip preview → asset annotation → agent workbench.
                  overlay={clipView ? (
                    <ClipPlayer shot={clipView} onClose={() => setClipView(null)} />
                  ) : assetView ? (
                    <AssetPanel asset={assetView} onClose={() => setAssetView(null)} />
                  ) : wb && (job.meta.tasks ?? {})[wb.task] ? (
                    <AgentWorkbench
                      key={`${wb.task}-${wb.idx ?? "auto"}`} embedded
                      name={wb.task} t={job.meta.tasks[wb.task]} jobId={pipelineJobId}
                      initialIdx={wb.idx} onClose={() => setWb(null)}
                    />
                  ) : undefined}
                />
                {/* batch tasks (audio/clip captioning) stay as grids under the canvas */}
                <TaskGrids
                  tasks={Object.fromEntries(Object.entries(job.meta.tasks ?? {})
                    .filter(([k]) => !["editor_shots", "editor_rounds", "screenwriter_llm"].includes(k))) as Record<string, import("../components/trace").TaskInfo>}
                  jobId={pipelineJobId} jobRunning={running}
                  onOpenWorkbench={(task, idx) => setWb({ task, idx })}
                />
              </>
            ) : (
              <>
                <TaskGrids
                  tasks={Object.fromEntries(Object.entries(job.meta.tasks ?? {})
                    .filter(([k]) => k !== "editor_rounds")) as Record<string, import("../components/trace").TaskInfo>}
                  jobId={pipelineJobId} jobRunning={running}
                  onRetryFailed={(task) => { if (task === "editor_shots") start(); }}
                  onOpenWorkbench={(task, idx) => setWb({ task, idx })}
                />
                {wb && pipelineJobId && (job.meta.tasks ?? {})[wb.task] && (
                  <AgentWorkbench
                    name={wb.task} t={job.meta.tasks[wb.task]} jobId={pipelineJobId}
                    initialIdx={wb.idx} onClose={() => setWb(null)}
                  />
                )}
              </>
            )}
            {job.status === "done" && (
              <div className="mb-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2.5 text-sm text-emerald-400">
                流水线完成！切换到「渲染导出」生成视频。
              </div>
            )}
            {job.status === "error" && (
              <div className="mb-2 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-400">
                流水线失败 — 展开左下角「流水线」工作台查看日志。缓存已保存，修复后重新运行会跳过已完成步骤。
              </div>
            )}
            <div className="mt-1 text-xs text-slate-500">
              完整日志在左下角「流水线」工作台中查看（点击展开，可拖拽右下角调节大小）。
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

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
import { ShotTimeline, type BeatMark } from "../components/Charts";
import { ActiveClipCard, ClipCaption, ClipInspector, activeClipAt, useClipMap } from "../components/ClipInspector";
import type { PipelineStatus, ProjectState } from "../App";

interface RecentProject {
  shot_point: string;
  project: string;
  instruction_id: string;
  mtime: number;
}
interface Output {
  ratio: string; path: string; size_mb: number; mtime: number;
  render_meta?: {
    transition_mode: string;
    transitions: (string | null)[];
    audio: { path: string; start: number; duration: number; total?: number; name?: string };
    clips?: number;
  };
}

const RATIOS = ["9:16", "16:9", "1:1"];
// max preview width per ratio (script panel sits beside the player now)
const PREVIEW_W: Record<string, number> = { "9:16": 330, "16:9": 820, "1:1": 540 };

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
  // 被新渲染顶替的旧版成片(服务端自动归档,不覆盖)
  const [history, setHistory] = useState<{ ratio: string; version: string; path: string; size_mb: number; mtime: number }[]>([]);
  const [hasEnding, setHasEnding] = useState(false);
  const [addEnding, setAddEnding] = useState(false);
  const [transitionMode, setTransitionMode] = useState<"none" | "uniform" | "ai">("uniform");
  const [sourceQuality, setSourceQuality] = useState<"proxy" | "original">("proxy");
  const [colorGrade, setColorGrade] = useState<"" | "teal_orange" | "film" | "warm">("");
  const [letterbox, setLetterbox] = useState(false);
  const [fades, setFades] = useState(true);
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

  const replaceShot = async (c: { section_idx: number; shot_idx: number }) => {
    const reason = window.prompt(
      "为什么不满意?(可留空)\n· 写「晃/歪/晕」→ 替代镜头要求更稳\n· 写「重复/一样」→ 避开相似画面\n· 写「不再使用」或以 ! 开头 → 永久拉黑该片段(所有项目不再选它)\n其余原因会作为负分计入该片段的综合评分", "") ?? null;
    if (reason === null) return;   // 取消
    try {
      const r = await api<{ ok: boolean; new_clip: any; moment: any }>("/api/shots/replace", {
        method: "POST",
        body: JSON.stringify({
          shot_point: shotPoint, section_idx: c.section_idx,
          shot_idx: c.shot_idx, reason,
        }),
      });
      reloadClipMap();
      window.alert(`已换为高光池 ${r.moment.id}(${(r.moment.score * 10).toFixed(1)} 分):\n${r.moment.desc}\n\n重新渲染后生效;被换掉的区间已进入拒绝名单。`);
    } catch (e: any) {
      window.alert(`换镜头失败:${e.message}`);
    }
  };

  const likeShot = async (c: { section_idx: number; shot_idx: number }) => {
    const reason = window.prompt(
      "为什么满意这个镜头?(可留空,AI 会自行分析画面特征)\nAI 会把你的理由提炼成剪辑偏好并永久记住,以后自动优先选择同类镜头。", "") ?? null;
    if (reason === null) return;   // 取消
    try {
      const r = await api<{ ok: boolean; analysis: any }>("/api/shots/like", {
        method: "POST",
        body: JSON.stringify({
          shot_point: shotPoint, section_idx: c.section_idx,
          shot_idx: c.shot_idx, reason,
        }),
      });
      const a = r.analysis;
      window.alert(a
        ? `已记住这条偏好 ✓\n\nAI 的理解:${a.summary || ""}\n${(a.principles || []).map((p: string) => "· " + p).join("\n")}\n适用范围:${a.applies_to || "所有镜头"}\n\n该片段已获得全局加分,提炼出的原则会进入以后每次编排。`
        : "已记录点赞 ✓(AI 理由分析暂时不可用,原始理由已保存,该片段已获得全局加分)");
    } catch (e: any) {
      window.alert(`点赞失败:${e.message}`);
    }
  };

  const shotPoint = project.shotPoint;
  const { clips: clipMap, error: clipMapError, reload: reloadClipMap } = useClipMap(shotPoint);

  // measured music keypoints inside the render window → beat guides on the timeline
  const [beats, setBeats] = useState<BeatMark[]>([]);
  const renderAudio = outputs.find((o) => o.ratio === activeRatio)?.render_meta?.audio
    ?? outputs[0]?.render_meta?.audio;
  useEffect(() => {
    if (!renderAudio?.path) { setBeats([]); return; }
    api<{ beats: BeatMark[] }>(
      `/api/render/beats?path=${encodeURIComponent(renderAudio.path)}`
      + `&start=${renderAudio.start ?? 0}&duration=${renderAudio.duration ?? 0}`)
      .then((r) => setBeats(r.beats ?? []))
      .catch(() => setBeats([]));
  }, [renderAudio?.path, renderAudio?.start, renderAudio?.duration]);
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
      setHistory(r.history ?? []);
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

  // ── 片头/片尾字幕(自动从素材的拍摄时间+地点填充,可改可清空)──────────
  const [titleText, setTitleText] = useState("");
  const [endText, setEndText] = useState("");
  const [cardsFilled, setCardsFilled] = useState(false);
  useEffect(() => {
    if (cardsFilled || project.videos.length === 0) return;
    api<{ assets: any[] }>("/api/assets/by_paths", {
      method: "POST", body: JSON.stringify({ paths: project.videos }),
    }).then((r) => {
      const as = r.assets ?? [];
      const times = as.map((a) => a.capture_time).filter(Boolean).sort();
      const locs = as.map((a) => (a.location || "").split("·")[0].trim()).filter(Boolean);
      const loc = locs.sort((a, b) =>
        locs.filter((x) => x === b).length - locs.filter((x) => x === a).length)[0] ?? "";
      if (times.length) {
        const d0 = new Date(times[0]); const d1 = new Date(times[times.length - 1]);
        const ym = `${d0.getFullYear()}.${String(d0.getMonth() + 1).padStart(2, "0")}`;
        const range = d0.toDateString() === d1.toDateString()
          ? `${ym}.${String(d0.getDate()).padStart(2, "0")}`
          : `${ym}.${String(d0.getDate()).padStart(2, "0")} – ${String(d1.getMonth() + 1).padStart(2, "0")}.${String(d1.getDate()).padStart(2, "0")}`;
        setTitleText(loc ? `${loc} · ${ym}` : ym);
        setEndText(`${loc ? loc + "\\n" : ""}${range}`);
      } else if (loc) {
        setTitleText(loc);
      }
      setCardsFilled(true);
    }).catch(() => setCardsFilled(true));
  }, [project.videos, cardsFilled]);

  // ── AI 旁白(narration sidecar,渲染时自动混入)────────────────────────
  const [nar, setNar] = useState<any>(null);
  const [narBusy, setNarBusy] = useState("");
  const [narDirty, setNarDirty] = useState(false);
  useEffect(() => {
    setNar(null); setNarDirty(false);
    if (!shotPoint) return;
    api<any>(`/api/narration?shot_point=${encodeURIComponent(shotPoint)}`)
      .then(setNar).catch(() => {});
  }, [shotPoint]);

  const narGenerate = async () => {
    setNarBusy("AI 写稿 + 配音中…约 30-60 秒"); setError("");
    try {
      const r = await api<any>("/api/narration/generate", {
        method: "POST",
        body: JSON.stringify({ shot_point: shotPoint, voice: nar?.voice ?? "yunxi",
          instruction: project.instruction ?? "" }),
      });
      setNar({ exists: true, voices: nar?.voices ?? ["yunxi", "xiaoxiao", "yunjian"], ...r });
      setNarDirty(false);
    } catch (e: any) { setError(e.message); }
    setNarBusy("");
  };
  const narSave = async (patch?: { enabled?: boolean; voice?: string }) => {
    const payload = {
      shot_point: shotPoint,
      voice: patch?.voice ?? nar.voice,
      enabled: patch?.enabled ?? nar.enabled,
      lines: nar.lines,
    };
    setNarBusy(patch?.voice ? "换声线重配音中…" : narDirty ? "重配改动的句子…" : "保存中…");
    try {
      const r = await api<any>("/api/narration/save", { method: "POST", body: JSON.stringify(payload) });
      setNar((old: any) => ({ ...old, ...r })); setNarDirty(false);
    } catch (e: any) { setError(e.message); }
    setNarBusy("");
  };

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
          color_grade: colorGrade,
          letterbox: letterbox && ratio === "16:9",
          fades,
          title_text: titleText,
          end_text: endText,
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

              <div className="mb-4 flex flex-wrap items-center gap-2.5 text-[13px] text-slate-300">
                <span>画面风格</span>
                <div className="flex gap-1 rounded-lg border border-white/10 bg-white/[0.03] p-0.5">
                  {([
                    ["", "原色"],
                    ["teal_orange", "青橙电影感"],
                    ["film", "胶片柔和"],
                    ["warm", "暖阳回忆"],
                  ] as const).map(([v, label]) => (
                    <button
                      key={v}
                      className={cn(
                        "rounded-md px-2.5 py-1 text-xs transition-colors",
                        colorGrade === v
                          ? "bg-cyan-500/15 font-semibold text-cyan-300"
                          : "text-slate-400 hover:text-slate-200",
                      )}
                      onClick={() => setColorGrade(v)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-400">
                  <Checkbox checked={letterbox} onCheckedChange={(v) => setLetterbox(v === true)} />
                  2.35:1 电影黑边<span className="text-[10px] text-slate-600">(仅 16:9)</span>
                </label>
                <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-400">
                  <Checkbox checked={fades} onCheckedChange={(v) => setFades(v === true)} />
                  淡入淡出收尾
                </label>
              </div>

              <div className="mb-4 flex flex-wrap items-center gap-2.5 text-[13px] text-slate-300">
                <span>片头字幕</span>
                <input
                  className="w-56 rounded-md border border-white/10 bg-black/25 px-2 py-1 text-xs text-slate-200 focus:border-cyan-400/50 focus:outline-none"
                  placeholder="留空 = 不加(如:长白山 · 2025.12)"
                  value={titleText} onChange={(e) => setTitleText(e.target.value)} />
                <span>片尾字幕</span>
                <input
                  className="w-56 rounded-md border border-white/10 bg-black/25 px-2 py-1 text-xs text-slate-200 focus:border-cyan-400/50 focus:outline-none"
                  placeholder="留空 = 不加(\n 换行)"
                  value={endText} onChange={(e) => setEndText(e.target.value)} />
                <span className="text-[11px] text-slate-500">
                  自动按素材的拍摄时间/地点填好,可改可清空;叠在首尾镜头上,不动时间轴
                </span>
              </div>

              <div className="mb-4 rounded-xl border border-violet-500/20 bg-violet-500/[0.04] px-3 py-2.5 text-[13px] text-slate-300">
                <div className="flex flex-wrap items-center gap-2.5">
                  <span className="font-medium text-violet-300">🎙 AI 旁白</span>
                  {nar?.exists ? (
                    <>
                      <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-400">
                        <Checkbox checked={!!nar.enabled}
                          onCheckedChange={(v) => { setNar((o: any) => ({ ...o, enabled: v === true })); narSave({ enabled: v === true }); }} />
                        渲染时混入
                      </label>
                      <div className="flex gap-1 rounded-lg border border-white/10 bg-white/[0.03] p-0.5">
                        {([["yunxi", "云希·男"], ["xiaoxiao", "晓晓·女"], ["yunjian", "云健·厚"]] as const).map(([v, label]) => (
                          <button key={v}
                            className={cn("rounded-md px-2 py-0.5 text-xs transition-colors",
                              nar.voice === v ? "bg-violet-500/20 font-semibold text-violet-300" : "text-slate-400 hover:text-slate-200")}
                            onClick={() => { if (nar.voice !== v) { setNar((o: any) => ({ ...o, voice: v })); narSave({ voice: v }); } }}>
                            {label}
                          </button>
                        ))}
                      </div>
                      <button className="text-xs text-violet-400 hover:underline" disabled={!!narBusy} onClick={narGenerate}>
                        ↻ 整篇重写
                      </button>
                      {narDirty && (
                        <button className="rounded-md bg-violet-500/20 px-2 py-0.5 text-xs font-semibold text-violet-200 hover:bg-violet-500/30"
                          disabled={!!narBusy} onClick={() => narSave()}>
                          保存并重配改动句
                        </button>
                      )}
                    </>
                  ) : (
                    <>
                      <Button size="sm" className="h-6 gap-1 bg-violet-500/80 px-2.5 text-xs text-white hover:bg-violet-500"
                        disabled={!shotPoint || !!narBusy} onClick={narGenerate}>
                        {narBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : null} 生成旁白
                      </Button>
                      <span className="text-[11px] text-slate-500">
                        AI 按成片内容写 4-6 句第一人称旁白并配音,BGM 自动闪避 — 让片子有"谁在回忆"
                      </span>
                    </>
                  )}
                  {narBusy && <span className="text-[11px] text-violet-300">{narBusy}</span>}
                </div>
                {nar?.exists && (
                  <div className="mt-2 space-y-1">
                    {(nar.lines ?? []).map((l: any, i: number) => (
                      <div key={i} className="flex items-center gap-2">
                        <span className="w-14 shrink-0 text-right text-[11px] text-slate-500">@{Math.round(l.at_sec)}s</span>
                        <input
                          className="flex-1 rounded-md border border-white/10 bg-black/25 px-2 py-1 text-xs text-slate-200 focus:border-violet-400/50 focus:outline-none"
                          value={l.text}
                          onChange={(e) => {
                            const v = e.target.value;
                            setNar((o: any) => ({ ...o, lines: o.lines.map((x: any, j: number) => j === i ? { ...x, text: v } : x) }));
                            setNarDirty(true);
                          }} />
                        <span className="w-10 shrink-0 text-[11px] text-slate-600">{l.dur ? `${l.dur}s` : ""}</span>
                      </div>
                    ))}
                  </div>
                )}
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
            {/* one-screen layout: player + timeline LEFT, script RIGHT */}
            <div className="flex flex-wrap items-start gap-4">
              <div className="min-w-[420px] flex-[3] basis-[620px]">
                {outputs.length > 1 && (
                  <div className="mb-2 flex gap-1.5">
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
                  <div className="flex flex-col items-center">
                    <video
                      ref={videoRef}
                      key={activeOutput.ratio}
                      src={mediaUrl(activeOutput.path) + `&t=${activeOutput.mtime}`} controls playsInline
                      className="w-full rounded-lg bg-black"
                      style={{ maxHeight: 560, maxWidth: PREVIEW_W[activeOutput.ratio] ?? 820 }}
                      onTimeUpdate={(e) => setPlayhead((e.target as HTMLVideoElement).currentTime)}
                    />
                    <div className="w-full" style={{ maxWidth: PREVIEW_W[activeOutput.ratio] ?? 820 }}>
                      <ClipCaption clip={activeClip} playhead={playhead} />
                    </div>
                    <div className="mt-2 flex items-center gap-2 text-xs text-slate-400">
                      {activeOutput.ratio} · {activeOutput.size_mb}MB · {new Date(activeOutput.mtime * 1000).toLocaleString()}
                      <Button asChild variant="outline" size="sm" className="h-6 gap-1 border-white/10 bg-white/[0.04] px-2 text-[11px]">
                        <a href={mediaUrl(activeOutput.path)} download>
                          <Download className="h-3 w-3" /> 下载
                        </a>
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="flex h-[180px] w-full items-center justify-center rounded-lg border border-dashed border-white/10 text-xs text-slate-500">
                    还没有渲染结果 — 点击上方「渲染」
                  </div>
                )}

                {history.length > 0 && (
                  <details className="mt-2 rounded-lg border border-white/[0.07] bg-white/[0.02] px-3 py-1.5 text-xs">
                    <summary className="cursor-pointer text-slate-400">
                      🕘 历史版本({history.length})— 重渲前的旧成片自动留底,不会被覆盖
                    </summary>
                    <div className="mt-1.5 space-y-1">
                      {history.map((h) => (
                        <div key={h.path} className="flex items-center gap-2 text-slate-400">
                          <span className="text-slate-300">{h.ratio}</span>
                          <span>{new Date(h.mtime * 1000).toLocaleString()}</span>
                          <span>{h.size_mb}MB</span>
                          <a className="text-cyan-400 hover:underline" href={mediaUrl(h.path)} target="_blank" rel="noreferrer">▶ 播放</a>
                          <a className="text-cyan-400 hover:underline" href={mediaUrl(h.path)} download>下载</a>
                        </div>
                      ))}
                    </div>
                  </details>
                )}

                <div className="mt-4">
                  <ShotTimeline
                    shotPoint={shotPoint} playhead={playhead}
                    transitions={activeOutput?.render_meta?.transitions ?? null}
                    audio={activeOutput?.render_meta?.audio ?? null}
                    onSeek={seekTo}
                    beats={beats}
                  />
                </div>
              </div>

              {/* right: pinned focus card (current shot) + compact full list.
                  List height is capped so both columns end together — no
                  page-long scroll, no blank space under the player. */}
              <div className="min-w-[360px] flex-[2] basis-[400px]">
                <ActiveClipCard clip={activeClip} index={activeIdx} playhead={playhead} onReplace={replaceShot} onLike={likeShot} />
                <div className="mt-3">
                  <ClipInspector
                    clips={clipMap} currentTime={playhead} error={clipMapError}
                    onRetry={reloadClipMap} onSeek={seekTo}
                    maxHeight="360px" compact
                  />
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

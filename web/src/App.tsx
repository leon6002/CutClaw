import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  AudioLines, Clapperboard, FolderOpen, Images, PenLine, Plus, Scissors,
  Settings2, SlidersHorizontal, Sparkles, Video,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { TooltipProvider } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { api, useJob, type JobState } from "./api";
import { ModelConfigProvider } from "./components/ModelConfig";
import PipelineSettings from "./components/PipelineSettings";
import AgentFlow from "./components/AgentFlow";
import JobDock from "./components/JobDock";
import AssetsView from "./views/AssetsView";
import EditorView from "./views/EditorView";
import RenderView from "./views/RenderView";
import ImmichAdminView from "./views/ImmichAdminView";
import SettingsModal from "./views/SettingsModal";

export interface ProjectState {
  id: string;
  name: string;
  videos: string[];
  audio: string;
  /** multi-music selection — fused into ONE track as a pipeline PRE-STEP
   *  (when the target length is actually known); audio then points at it */
  audios?: string[];
  instruction: string;
  hasDialogue: boolean;
  mainCharacter: string;
  srt: string;
  targetLength: number;
  shotLength: number;
  selectionRationale: string;
  shotPoint: string;
  effectiveVideo: string;
}

export type PipelineStatus = "idle" | "running" | "done" | "error";

const BADGE_CLS: Record<PipelineStatus, string> = {
  idle: "border-white/15 bg-white/5 text-slate-400",
  running: "border-cyan-500/40 bg-cyan-500/10 text-cyan-300 shadow-[0_0_10px_rgba(34,211,238,0.2)]",
  done: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
  error: "border-red-500/40 bg-red-500/10 text-red-400",
};
const BADGE_TXT: Record<PipelineStatus, string> = {
  idle: "空闲", running: "运行中…", done: "已完成", error: "失败",
};

const WORKFLOW_STEPS = [
  { key: "select", label: "挑选素材", icon: <Sparkles className="h-4 w-4" /> },
  { key: "analyze", label: "素材分析", icon: <AudioLines className="h-4 w-4" /> },
  { key: "screenwriter", label: "AI 编剧", icon: <PenLine className="h-4 w-4" /> },
  { key: "editor", label: "AI 剪辑", icon: <Scissors className="h-4 w-4" /> },
  { key: "render", label: "渲染导出", icon: <Video className="h-4 w-4" /> },
];
const WORKFLOW_TAB: Record<string, string> = {
  select: "assets", analyze: "editor", screenwriter: "editor", editor: "editor", render: "render",
};
const NEXT_HINT: Record<string, string> = {
  select: "去素材库选材或手动挑选",
  analyze: "在项目编辑中运行流水线",
  screenwriter: "在项目编辑中运行流水线",
  editor: "等待编剧完成",
  render: "选择比例渲染成片",
};

const NAV_TABS = [
  { key: "assets", label: "素材库", icon: FolderOpen },
  { key: "editor", label: "项目编辑", icon: Scissors },
  { key: "render", label: "渲染导出", icon: Video },
  { key: "immich", label: "Immich 管理", icon: Images },
];

function toState(p: any): ProjectState {
  return {
    id: p.id, name: p.name ?? "",
    videos: p.videos ?? [], audio: p.audio ?? "",
    audios: p.audios ?? (p.audio ? [p.audio] : []),
    instruction: p.instruction ?? "",
    hasDialogue: !!p.has_dialogue, mainCharacter: p.main_character ?? "", srt: p.srt ?? "",
    targetLength: p.target_length ?? 30, shotLength: p.shot_length ?? 4,
    selectionRationale: p.selection_rationale ?? "",
    shotPoint: p.shot_point ?? "", effectiveVideo: p.effective_video ?? "",
  };
}

function toPatch(s: ProjectState): Record<string, any> {
  return {
    name: s.name, videos: s.videos, audio: s.audio, audios: s.audios ?? [],
    instruction: s.instruction,
    has_dialogue: s.hasDialogue, main_character: s.mainCharacter, srt: s.srt,
    target_length: s.targetLength, shot_length: s.shotLength,
    selection_rationale: s.selectionRationale,
    shot_point: s.shotPoint, effective_video: s.effectiveVideo,
  };
}

function deriveWorkflow(
  project: ProjectState, pipelineJob: JobState, pipelineStatus: PipelineStatus, outputsCount: number,
): Record<string, { status: string; detail?: string }> {
  const stages: Record<string, string> = pipelineJob.meta.stages ?? {};
  const w: Record<string, { status: string; detail?: string }> = {};

  const hasMaterial = project.videos.length > 0 && !!project.audio;
  w.select = hasMaterial
    ? { status: "done", detail: `${project.videos.length} 视频 · 1 音乐` }
    : { status: "pending" };

  const anaKeys = ["shot_detection", "asr", "video_captioning", "audio_analysis"];
  const anaVals = anaKeys.map((k) => stages[k] ?? "pending");
  const sw = stages["screenwriter"] ?? "pending";
  const ed = stages["editor"] ?? "pending";
  const pipelineActive = pipelineStatus === "running";
  const hasResult = !!project.shotPoint && pipelineStatus !== "running" && pipelineStatus !== "error";

  if (anaVals.includes("error")) w.analyze = { status: "error" };
  else if (anaVals.includes("running")) w.analyze = { status: "running" };
  else if (anaVals.every((s) => s === "done") || sw !== "pending" || ed !== "pending") w.analyze = { status: "done" };
  else if (pipelineActive) w.analyze = { status: "running", detail: "准备中…" };
  else if (hasResult && pipelineStatus === "idle") w.analyze = { status: "done", detail: "已有缓存结果" };
  else w.analyze = { status: "pending" };

  w.screenwriter = { status: sw };
  w.editor = { status: ed };
  if (pipelineStatus === "idle" && hasResult) {
    w.screenwriter = { status: "done" };
    w.editor = { status: "done" };
  }
  if (pipelineStatus === "done") {
    w.analyze = w.analyze.status === "error" ? w.analyze : { status: "done" };
    if (w.screenwriter.status !== "error") w.screenwriter = { status: "done" };
    if (w.editor.status !== "error") w.editor = { status: "done" };
  }

  w.render = outputsCount > 0
    ? { status: "done", detail: `${outputsCount} 个成片` }
    : { status: "pending" };

  const order = ["select", "analyze", "screenwriter", "editor", "render"];
  for (const k of order) {
    const st = w[k].status;
    if (st === "running" || st === "error") break;
    if (st === "pending") {
      w[k] = { status: "next", detail: NEXT_HINT[k] };
      break;
    }
  }
  return w;
}

export default function App() {
  const [tab, setTab] = useState("assets");
  const [showSettings, setShowSettings] = useState(false);
  const [showParams, setShowParams] = useState(false);
  const [projects, setProjects] = useState<any[]>([]);
  const [project, setProjectState] = useState<ProjectState | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [newModal, setNewModal] = useState(false);
  const [newName, setNewName] = useState("");
  const [outputsCount, setOutputsCount] = useState(0);
  const skipSave = useRef(true);
  const saveTimer = useRef<number>(0);

  const [pipelineJobId, setPipelineJobId] = useState<string | null>(null);
  const pipelineJob = useJob(pipelineJobId);
  const [pipelineStatus, setPipelineStatus] = useState<PipelineStatus>("idle");

  useEffect(() => {
    if (!pipelineJobId) return;
    setPipelineStatus(pipelineJob.status === "idle" ? "running" : pipelineJob.status);
  }, [pipelineJob.status, pipelineJobId]);

  // Pipeline view is per-project: clear it when switching projects
  // (EditorView reattaches the new project's own latest run right after).
  const lastProjectId = useRef<string | null>(null);
  useEffect(() => {
    if (!project) return;
    if (lastProjectId.current !== null && lastProjectId.current !== project.id) {
      setPipelineJobId(null);
      setPipelineStatus("idle");
    }
    lastProjectId.current = project.id;
  }, [project?.id]);

  const loadProject = (p: any) => {
    skipSave.current = true;
    setProjectState(toState(p));
    localStorage.setItem("cutclaw_project", p.id);
  };

  useEffect(() => {
    (async () => {
      try {
        let list = await api<any[]>("/api/projects");
        if (list.length === 0) {
          const created = await api<any>("/api/projects", {
            method: "POST", body: JSON.stringify({ name: "默认项目", from_config: true }),
          });
          list = [created];
        }
        setProjects(list);
        const savedId = localStorage.getItem("cutclaw_project");
        loadProject(list.find((x) => x.id === savedId) ?? list[0]);
      } catch { /* server not ready */ }
      setLoaded(true);
    })();
  }, []);

  useEffect(() => {
    if (!project) return;
    if (skipSave.current) { skipSave.current = false; return; }
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      api<any>(`/api/projects/${project.id}`, {
        method: "PUT", body: JSON.stringify({ patch: toPatch(project) }),
      }).then((p) => {
        setProjects((ps) => ps.map((x) => (x.id === p.id ? p : x)));
      }).catch(() => {});
    }, 600);
  }, [project]);

  const setProject = (fn: (p: ProjectState) => ProjectState) =>
    setProjectState((p) => (p ? fn(p) : p));

  const switchProject = (id: string) => {
    const p = projects.find((x) => x.id === id);
    if (p) loadProject(p);
  };

  const createProject = async () => {
    try {
      const p = await api<any>("/api/projects", {
        method: "POST", body: JSON.stringify({ name: newName }),
      });
      setProjects((ps) => [p, ...ps]);
      loadProject(p);
      setNewModal(false);
      setNewName("");
      setTab("assets");
    } catch { /* ignore */ }
  };

  const workflow = project ? deriveWorkflow(project, pipelineJob, pipelineStatus, outputsCount) : {};

  return (
    <TooltipProvider delayDuration={200}>
      <ModelConfigProvider>
      <div className="flex min-h-screen flex-col">
        {/* ── glass header ── */}
        <header className="sticky top-0 z-40 border-b border-white/[0.07] bg-slate-950/60 backdrop-blur-xl">
          <div className="mx-auto flex h-14 max-w-[1320px] items-center gap-3 px-5">
            <div className="flex items-center gap-2 whitespace-nowrap text-[17px] font-bold">
              <Clapperboard className="h-5 w-5 text-cyan-400 drop-shadow-[0_0_6px_rgba(34,211,238,0.7)]" />
              <span>Cut<span className="text-cyan-400">Claw</span></span>
            </div>

            <Select value={project?.id} onValueChange={switchProject}>
              <SelectTrigger className="h-8 w-[190px] border-white/10 bg-white/[0.04] text-xs">
                <SelectValue placeholder="选择项目" />
              </SelectTrigger>
              <SelectContent>
                {projects.map((p) => (
                  <SelectItem key={p.id} value={p.id} className="text-xs">
                    {p.name}{p.last_run_status === "done" ? " ✓" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button variant="outline" size="sm" className="h-8 gap-1 border-white/10 bg-white/[0.04] text-xs"
              onClick={() => setNewModal(true)}>
              <Plus className="h-3.5 w-3.5" /> 新建
            </Button>

            {/* nav with neon active glow */}
            <nav className="ml-2 flex flex-1 items-center gap-1">
              {NAV_TABS.map((t) => (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  className={cn(
                    "relative flex items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-[13px] font-medium transition-colors",
                    tab === t.key ? "text-cyan-300" : "text-slate-400 hover:text-slate-200",
                  )}
                >
                  {tab === t.key && (
                    <motion.span
                      layoutId="nav-glow"
                      className="absolute inset-0 rounded-lg border border-cyan-500/30 bg-cyan-500/10 shadow-[0_0_14px_rgba(34,211,238,0.15)]"
                      transition={{ type: "spring", stiffness: 400, damping: 32 }}
                    />
                  )}
                  <t.icon className="relative h-3.5 w-3.5" />
                  <span className="relative">{t.label}</span>
                </button>
              ))}
            </nav>

            <Badge variant="outline" className={cn("px-2.5", BADGE_CLS[pipelineStatus])}>
              {pipelineStatus === "running" && (
                <span className="mr-1.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-400" />
              )}
              {BADGE_TXT[pipelineStatus]}
            </Badge>
            <Button variant="outline" size="sm" className="h-8 gap-1 border-white/10 bg-white/[0.04] text-xs"
              onClick={() => setShowSettings(true)}>
              <Settings2 className="h-3.5 w-3.5" /> 模型设置
            </Button>
            <Button variant="outline" size="sm" className="h-8 gap-1 border-white/10 bg-white/[0.04] text-xs"
              onClick={() => setShowParams(true)}>
              <SlidersHorizontal className="h-3.5 w-3.5" /> 参数
            </Button>
          </div>
        </header>

        {/* ── content ── */}
        <main className="mx-auto w-full max-w-[1320px] flex-1 px-5 py-5">
          {!loaded || !project ? (
            <div className="py-20 text-center text-sm text-slate-500">正在连接 CutClaw 服务…</div>
          ) : (
            <>
              <Card className={cn(
                "mb-5 rounded-2xl border-white/[0.07] bg-slate-900/50 py-2",
                pipelineStatus === "running" && "border-beam",
              )}>
                <CardContent className="px-4 py-1">
                  <AgentFlow
                    steps={WORKFLOW_STEPS} stages={workflow}
                    onStepClick={(k) => setTab(WORKFLOW_TAB[k] ?? tab)}
                  />
                </CardContent>
              </Card>

              <div style={{ display: tab === "assets" ? "block" : "none" }}>
                <AssetsView project={project} setProject={setProject} />
              </div>
              <div style={{ display: tab === "editor" ? "block" : "none" }}>
                <EditorView
                  project={project} setProject={setProject}
                  pipelineStatus={pipelineStatus}
                  pipelineJobId={pipelineJobId} setPipelineJobId={setPipelineJobId}
                  pipelineJob={pipelineJob}
                />
              </div>
              <div style={{ display: tab === "render" ? "block" : "none" }}>
                <RenderView
                  project={project} setProject={setProject}
                  pipelineStatus={pipelineStatus} onOutputsCount={setOutputsCount}
                />
              </div>
              <div style={{ display: tab === "immich" ? "block" : "none" }}>
                <ImmichAdminView />
              </div>
            </>
          )}
        </main>

        {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
        <PipelineSettings open={showParams} onOpenChange={setShowParams} />

      {/* global job dock — bottom-left, above all overlays. Shows every running
          job (annotate/select/pipeline/render) with an expandable, resizable log.
          Replaced the old bottom-right FloatingTerminal, which duplicated it. */}
      <JobDock />

        <Dialog open={newModal} onOpenChange={(o) => !o && setNewModal(false)}>
          <DialogContent className="border-white/10 bg-slate-900/90 backdrop-blur-xl sm:max-w-md">
            <DialogHeader>
              <DialogTitle>新建项目</DialogTitle>
            </DialogHeader>
            <Input
              placeholder="项目名称（如：新疆草原混剪）" value={newName} autoFocus
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createProject()}
            />
            <DialogFooter>
              <Button variant="outline" onClick={() => setNewModal(false)}>取消</Button>
              <Button onClick={createProject}>创建</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
      </ModelConfigProvider>
    </TooltipProvider>
  );
}

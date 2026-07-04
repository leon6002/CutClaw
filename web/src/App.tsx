import { useEffect, useRef, useState } from "react";
import { Button, ConfigProvider, Input, Layout, Menu, Modal, Select, Tag, theme as antdTheme } from "antd";
import {
  FolderOpenOutlined, PlusOutlined, ProjectOutlined, ScissorOutlined,
  SettingOutlined, VideoCameraOutlined,
} from "@ant-design/icons";
import zhCN from "antd/locale/zh_CN";
import { api, useJob, type JobState } from "./api";
import AgentFlow from "./components/AgentFlow";
import AssetsView from "./views/AssetsView";
import EditorView from "./views/EditorView";
import RenderView from "./views/RenderView";
import SettingsModal from "./views/SettingsModal";

export interface ProjectState {
  id: string;
  name: string;
  videos: string[];
  audio: string;
  instruction: string;
  hasDialogue: boolean;
  mainCharacter: string;
  srt: string;
  targetLength: number;
  shotLength: number;
  selectionRationale: string;
  shotPoint: string;      // path of shot_point json for rendering
  effectiveVideo: string; // primary/merged video used by the pipeline
}

export type PipelineStatus = "idle" | "running" | "done" | "error";

const BADGE: Record<PipelineStatus, [string, string]> = {
  idle: ["default", "空闲"],
  running: ["gold", "运行中…"],
  done: ["green", "已完成"],
  error: ["red", "失败"],
};

const WORKFLOW_STEPS = [
  { key: "select", label: "挑选素材", icon: "✨" },
  { key: "analyze", label: "素材分析", icon: "🎧" },
  { key: "screenwriter", label: "AI 编剧", icon: "✍️" },
  { key: "editor", label: "AI 剪辑", icon: "✂️" },
  { key: "render", label: "渲染导出", icon: "🎬" },
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

function toState(p: any): ProjectState {
  return {
    id: p.id, name: p.name ?? "",
    videos: p.videos ?? [], audio: p.audio ?? "", instruction: p.instruction ?? "",
    hasDialogue: !!p.has_dialogue, mainCharacter: p.main_character ?? "", srt: p.srt ?? "",
    targetLength: p.target_length ?? 30, shotLength: p.shot_length ?? 4,
    selectionRationale: p.selection_rationale ?? "",
    shotPoint: p.shot_point ?? "", effectiveVideo: p.effective_video ?? "",
  };
}

function toPatch(s: ProjectState): Record<string, any> {
  return {
    name: s.name, videos: s.videos, audio: s.audio, instruction: s.instruction,
    has_dialogue: s.hasDialogue, main_character: s.mainCharacter, srt: s.srt,
    target_length: s.targetLength, shot_length: s.shotLength,
    selection_rationale: s.selectionRationale,
    shot_point: s.shotPoint, effective_video: s.effectiveVideo,
  };
}

/** Derive the global workflow node states from project + pipeline state. */
function deriveWorkflow(
  project: ProjectState, pipelineJob: JobState, pipelineStatus: PipelineStatus, outputsCount: number,
): Record<string, { status: string; detail?: string }> {
  const stages: Record<string, string> = pipelineJob.meta.stages ?? {};
  const w: Record<string, { status: string; detail?: string }> = {};

  // 1. select
  const hasMaterial = project.videos.length > 0 && !!project.audio;
  w.select = hasMaterial
    ? { status: "done", detail: `${project.videos.length} 视频 · 1 音乐` }
    : { status: "pending" };

  // 2-4. pipeline stages
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

  // 5. render
  w.render = outputsCount > 0
    ? { status: "done", detail: `${outputsCount} 个成片` }
    : { status: "pending" };

  // highlight the next actionable step
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
  const [projects, setProjects] = useState<any[]>([]);
  const [project, setProjectState] = useState<ProjectState | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [newModal, setNewModal] = useState(false);
  const [newName, setNewName] = useState("");
  const [outputsCount, setOutputsCount] = useState(0);
  const skipSave = useRef(true);
  const saveTimer = useRef<number>(0);

  // pipeline job state lives here so the global workflow bar can see it
  const [pipelineJobId, setPipelineJobId] = useState<string | null>(null);
  const pipelineJob = useJob(pipelineJobId);
  const [pipelineStatus, setPipelineStatus] = useState<PipelineStatus>("idle");

  useEffect(() => {
    if (!pipelineJobId) return;
    setPipelineStatus(pipelineJob.status === "idle" ? "running" : pipelineJob.status);
  }, [pipelineJob.status, pipelineJobId]);

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

  // debounced autosave of the active project
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

  const [badgeColor, badgeText] = BADGE[pipelineStatus];
  const workflow = project ? deriveWorkflow(project, pipelineJob, pipelineStatus, outputsCount) : {};

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: antdTheme.darkAlgorithm,
        token: {
          colorPrimary: "#ff6b4a",
          colorBgBase: "#0d0f13",
          colorBgContainer: "#171a21",
          colorBgElevated: "#1d212a",
          borderRadius: 8,
          fontSize: 14,
        },
        components: {
          Layout: { headerBg: "#12151b", bodyBg: "#0d0f13", headerHeight: 56 },
          Menu: { darkItemBg: "transparent" },
        },
      }}
    >
      <Layout style={{ minHeight: "100vh" }}>
        <Layout.Header style={{ display: "flex", alignItems: "center", gap: 14, paddingInline: 20, borderBottom: "1px solid #262b36" }}>
          <div style={{ fontWeight: 700, fontSize: 17, whiteSpace: "nowrap" }}>
            🎬 Cut<span style={{ color: "#ff6b4a" }}>Claw</span>
          </div>

          <Select
            size="small" style={{ minWidth: 180 }}
            suffixIcon={<ProjectOutlined />}
            value={project?.id} onChange={switchProject}
            options={projects.map((p) => ({
              value: p.id,
              label: p.name + (p.last_run_status === "done" ? " ✓" : ""),
            }))}
          />
          <Button size="small" icon={<PlusOutlined />} onClick={() => setNewModal(true)}>新建</Button>

          <Menu
            mode="horizontal" theme="dark" selectedKeys={[tab]}
            onClick={(e) => setTab(e.key)}
            style={{ flex: 1, minWidth: 300, background: "transparent", borderBottom: "none" }}
            items={[
              { key: "assets", icon: <FolderOpenOutlined />, label: "素材库" },
              { key: "editor", icon: <ScissorOutlined />, label: "项目编辑" },
              { key: "render", icon: <VideoCameraOutlined />, label: "渲染导出" },
            ]}
          />
          <Tag color={badgeColor}>{badgeText}</Tag>
          <Button size="small" icon={<SettingOutlined />} onClick={() => setShowSettings(true)}>模型设置</Button>
        </Layout.Header>

        <Layout.Content style={{ padding: 24 }}>
          <div style={{ maxWidth: 1280, margin: "0 auto" }}>
            {!loaded || !project ? (
              <span style={{ color: "#888" }}>正在连接 CutClaw 服务…</span>
            ) : (
              <>
                {/* global workflow: where you are + what's next (click to jump) */}
                <div className="workflow-bar">
                  <AgentFlow
                    steps={WORKFLOW_STEPS} stages={workflow}
                    onStepClick={(k) => setTab(WORKFLOW_TAB[k] ?? tab)}
                  />
                </div>

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
              </>
            )}
          </div>
        </Layout.Content>
      </Layout>

      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}

      <Modal
        open={newModal} title="新建项目" okText="创建" cancelText="取消"
        onOk={createProject} onCancel={() => setNewModal(false)}
      >
        <Input
          placeholder="项目名称（如：新疆草原混剪）" value={newName} autoFocus
          onChange={(e) => setNewName(e.target.value)} onPressEnter={createProject}
        />
      </Modal>
    </ConfigProvider>
  );
}

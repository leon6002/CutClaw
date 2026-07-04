import { useEffect, useState } from "react";
import { ConfigProvider, Layout, Menu, Tag, Button, theme as antdTheme } from "antd";
import { FolderOpenOutlined, ScissorOutlined, SettingOutlined, VideoCameraOutlined } from "@ant-design/icons";
import zhCN from "antd/locale/zh_CN";
import { api } from "./api";
import AssetsView from "./views/AssetsView";
import EditorView from "./views/EditorView";
import RenderView from "./views/RenderView";
import SettingsModal from "./views/SettingsModal";

export interface ProjectState {
  videos: string[];
  audio: string;
  instruction: string;
  hasDialogue: boolean;
  mainCharacter: string;
  srt: string;
  targetLength: number;
  shotLength: number;
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

export default function App() {
  const [tab, setTab] = useState("assets");
  const [showSettings, setShowSettings] = useState(false);
  const [pipelineStatus, setPipelineStatus] = useState<PipelineStatus>("idle");
  const [project, setProject] = useState<ProjectState>({
    videos: [], audio: "", instruction: "", hasDialogue: false, mainCharacter: "",
    srt: "", targetLength: 30, shotLength: 4, shotPoint: "", effectiveVideo: "",
  });
  const [configLoaded, setConfigLoaded] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const c = await api<Record<string, string>>("/api/config");
        const maxD = parseFloat(c.AUDIO_SEGMENT_MAX_DURATION_SEC || "35") || 35;
        const minSeg = parseFloat(c.AUDIO_MIN_SEGMENT_DURATION || "3") || 3;
        const maxSeg = parseFloat(c.AUDIO_MAX_SEGMENT_DURATION || "5") || 5;
        setProject((p) => ({
          ...p,
          videos: (c.VIDEO_PATH || "").split("||").filter(Boolean),
          audio: c.AUDIO_PATH || "",
          instruction: c.INSTRUCTION || "",
          mainCharacter: c.MAIN_CHARACTER_NAME || "",
          srt: c.SRT_PATH || "",
          targetLength: Math.max(10, maxD - 5),
          shotLength: Math.max(0.2, Math.round(((minSeg + maxSeg) / 2) * 10) / 10),
        }));
      } catch { /* server not ready */ }
      setConfigLoaded(true);
    })();
  }, []);

  const [badgeColor, badgeText] = BADGE[pipelineStatus];

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
        <Layout.Header style={{ display: "flex", alignItems: "center", gap: 16, paddingInline: 20, borderBottom: "1px solid #262b36" }}>
          <div style={{ fontWeight: 700, fontSize: 17, whiteSpace: "nowrap" }}>
            🎬 Cut<span style={{ color: "#ff6b4a" }}>Claw</span>
          </div>
          <Menu
            mode="horizontal" theme="dark" selectedKeys={[tab]}
            onClick={(e) => setTab(e.key)}
            style={{ flex: 1, minWidth: 360, background: "transparent", borderBottom: "none" }}
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
            {!configLoaded ? (
              <span style={{ color: "#888" }}>正在连接 CutClaw 服务…</span>
            ) : (
              <>
                <div style={{ display: tab === "assets" ? "block" : "none" }}>
                  <AssetsView project={project} setProject={setProject} />
                </div>
                <div style={{ display: tab === "editor" ? "block" : "none" }}>
                  <EditorView
                    project={project} setProject={setProject}
                    pipelineStatus={pipelineStatus} setPipelineStatus={setPipelineStatus}
                  />
                </div>
                <div style={{ display: tab === "render" ? "block" : "none" }}>
                  <RenderView project={project} setProject={setProject} pipelineStatus={pipelineStatus} />
                </div>
              </>
            )}
          </div>
        </Layout.Content>
      </Layout>

      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </ConfigProvider>
  );
}

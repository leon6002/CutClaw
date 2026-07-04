import { useEffect, useState } from "react";
import { Alert, Button, Card, Checkbox, Empty, Select, Space, Typography } from "antd";
import { DownloadOutlined, PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { api, mediaUrl, useJob } from "../api";
import JobLog from "../components/JobLog";
import type { PipelineStatus, ProjectState } from "../App";

const { Text } = Typography;

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
  project, setProject, pipelineStatus,
}: {
  project: ProjectState;
  setProject: (fn: (p: ProjectState) => ProjectState) => void;
  pipelineStatus: PipelineStatus;
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
    api<RecentProject[]>("/api/project/recent").then((r) => {
      setRecent(r);
      if (r.length > 0) {
        setProject((p) => (p.shotPoint ? p : { ...p, shotPoint: r[0].shot_point }));
      }
    }).catch(() => {});
  };

  const refreshOutputs = (sp: string) => {
    if (!sp) { setOutputs([]); return; }
    api<any>(`/api/render/outputs?shot_point=${encodeURIComponent(sp)}`).then((r) => {
      setOutputs(r.outputs);
      setHasEnding(r.has_ending_video);
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

  if (pipelineStatus === "running") {
    return <Alert type="info" showIcon message="⏳ 流水线运行中 — 完成后可在此渲染。" />;
  }

  return (
    <div>
      <Card size="small" title="🎬 渲染导出">
        {recent.length === 0 ? (
          <Empty description="还没有可渲染的结果 — 先在「✂️ 项目编辑」中运行流水线" />
        ) : (
          <>
            <div className="mb-3">
              <Text type="secondary" className="mb-1 block text-xs">选择剪辑结果（shot_point）</Text>
              <Select
                style={{ width: "100%" }} value={shotPoint || undefined}
                onChange={(v) => setProject((p) => ({ ...p, shotPoint: v as string }))}
                options={[
                  ...(!recent.some((r) => r.shot_point === shotPoint) && shotPoint
                    ? [{ value: shotPoint, label: shotPoint }] : []),
                  ...recent.map((r) => ({
                    value: r.shot_point,
                    label: `${r.project} / ${r.instruction_id} · ${new Date(r.mtime * 1000).toLocaleString()}`,
                  })),
                ]}
              />
            </div>

            {hasEnding && (
              <Checkbox className="mb-3" checked={addEnding} onChange={(e) => setAddEnding(e.target.checked)}>
                🎬 追加片尾视频
              </Checkbox>
            )}

            <Space wrap>
              {RATIOS.map((r) => (
                <Button key={r} type="primary" icon={<PlayCircleOutlined />} disabled={rendering || !shotPoint}
                  loading={rendering && renderRatio === r} onClick={() => render(r)}>
                  渲染 {r}
                </Button>
              ))}
              <Button icon={<ReloadOutlined />} onClick={() => { refreshRecent(); refreshOutputs(shotPoint); }}>刷新</Button>
            </Space>
          </>
        )}

        {error && <Alert type="error" showIcon message={error} className="mt-3" />}
        {job.status === "error" && <Alert type="error" showIcon message="❌ 渲染失败 — 查看日志" className="mt-3" />}
        {job.status === "done" && <Alert type="success" showIcon message="✅ 渲染完成！" className="mt-3" />}
        {(rendering || job.status === "error") && <JobLog lines={job.lines} height={240} />}
      </Card>

      {outputs.length > 0 && (
        <>
          <div className="mt-5 mb-3 text-xs font-semibold tracking-wider text-neutral-400 uppercase">预览</div>
          <div className="flex flex-wrap items-start gap-4">
            {outputs.map((o) => (
              <Card size="small" key={o.ratio}>
                <Text type="secondary" className="mb-2 block text-xs">
                  {o.ratio} · {o.size_mb}MB · {new Date(o.mtime * 1000).toLocaleString()}
                </Text>
                <video
                  src={mediaUrl(o.path) + `&t=${o.mtime}`} controls
                  className="rounded-md bg-black" style={{ width: WIDTH[o.ratio] ?? 320, maxHeight: 430 }}
                />
                <div className="mt-2">
                  <Button size="small" icon={<DownloadOutlined />} href={mediaUrl(o.path)} download>
                    下载 output_{o.ratio.replace(":", "x")}.mp4
                  </Button>
                </div>
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

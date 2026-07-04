import { useEffect, useState } from "react";
import {
  Alert, Button, Card, Checkbox, Input, InputNumber, Select, Space, Typography,
} from "antd";
import { CaretRightOutlined, StopOutlined } from "@ant-design/icons";
import { api, useJob } from "../api";
import JobLog from "../components/JobLog";
import StagePipeline from "../components/StagePipeline";
import type { PipelineStatus, ProjectState } from "../App";

const { Text } = Typography;

const basename = (p: string) => p.split(/[\\/]/).pop() || p;

export default function EditorView({
  project, setProject, pipelineStatus, setPipelineStatus,
}: {
  project: ProjectState;
  setProject: (fn: (p: ProjectState) => ProjectState) => void;
  pipelineStatus: PipelineStatus;
  setPipelineStatus: (s: PipelineStatus) => void;
}) {
  const [videoFiles, setVideoFiles] = useState<string[]>([]);
  const [audioFiles, setAudioFiles] = useState<string[]>([]);
  const [srtFiles, setSrtFiles] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(jobId);

  const p = project;
  const set = (patch: Partial<ProjectState>) => setProject((old) => ({ ...old, ...patch }));

  useEffect(() => {
    api<string[]>("/api/files?kind=video").then(setVideoFiles).catch(() => {});
    api<string[]>("/api/files?kind=audio").then(setAudioFiles).catch(() => {});
    api<string[]>("/api/files?kind=srt").then(setSrtFiles).catch(() => {});
    // reattach to a pipeline that is already running (e.g. page refresh)
    api<any>("/api/pipeline/current").then((r) => {
      if (r.job && r.job.status === "running") {
        setJobId(r.job.id);
        setProject((old) => ({
          ...old,
          shotPoint: r.job.meta.shot_point || old.shotPoint,
          effectiveVideo: r.job.meta.effective_video || old.effectiveVideo,
        }));
      }
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!jobId) return;
    setPipelineStatus(job.status === "idle" ? "running" : job.status);
  }, [job.status, jobId]);

  const running = job.status === "running";
  const videoOptions = Array.from(new Set([...p.videos, ...videoFiles]))
    .map((v) => ({ value: v, label: basename(v) }));
  const audioOptions = Array.from(new Set([p.audio, ...audioFiles].filter(Boolean)))
    .map((a) => ({ value: a, label: basename(a) }));

  const start = async () => {
    setError("");
    try {
      const r = await api<any>("/api/pipeline/start", {
        method: "POST",
        body: JSON.stringify({
          video_paths: p.videos, audio_path: p.audio, instruction: p.instruction,
          has_dialogue: p.hasDialogue, main_character: p.mainCharacter, srt_path: p.srt,
          target_length: p.targetLength, shot_length: p.shotLength,
        }),
      });
      set({ shotPoint: r.shot_point, effectiveVideo: r.effective_video });
      setJobId(r.job_id);
      setPipelineStatus("running");
    } catch (e: any) { setError(e.message); }
  };

  const stop = async () => {
    try { await api("/api/pipeline/stop", { method: "POST" }); } catch { /* ignore */ }
  };

  return (
    <div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
        <Card size="small" title="✂️ 项目设置">
          <div className="mb-3">
            <Text type="secondary" className="mb-1 block text-xs">🎬 视频素材（可多选，按顺序拼接；可直接粘贴路径回车添加）</Text>
            <Select
              mode="tags" style={{ width: "100%" }} disabled={running}
              placeholder="选择或粘贴视频路径…"
              value={p.videos} options={videoOptions}
              onChange={(vals) => set({ videos: vals as string[] })}
            />
          </div>

          <div className="mb-3">
            <Text type="secondary" className="mb-1 block text-xs">🎵 音乐</Text>
            <Select
              showSearch allowClear style={{ width: "100%" }} disabled={running}
              placeholder="选择音频…" value={p.audio || undefined} options={audioOptions}
              onChange={(v) => set({ audio: (v as string) || "" })}
            />
          </div>

          <div>
            <Text type="secondary" className="mb-1 block text-xs">📝 剪辑指令</Text>
            <Input.TextArea
              rows={3} value={p.instruction} disabled={running}
              placeholder="描述你想要的剪辑效果…"
              onChange={(e) => set({ instruction: e.target.value })}
            />
          </div>
        </Card>

        <Card size="small" title="⚙️ 参数">
          <Checkbox
            checked={p.hasDialogue} disabled={running}
            onChange={(e) => set({ hasDialogue: e.target.checked })}
            className="mb-3"
          >
            🎙️ 包含对白（启用语音识别 / 角色识别）
          </Checkbox>

          {p.hasDialogue && (
            <>
              <div className="mb-3">
                <Text type="secondary" className="mb-1 block text-xs">主角名字</Text>
                <Input value={p.mainCharacter} disabled={running}
                  onChange={(e) => set({ mainCharacter: e.target.value })} />
              </div>
              <div className="mb-3">
                <Text type="secondary" className="mb-1 block text-xs">SRT 字幕（可选）</Text>
                <Select
                  allowClear style={{ width: "100%" }} disabled={running}
                  value={p.srt || undefined}
                  options={srtFiles.map((s) => ({ value: s, label: basename(s) }))}
                  onChange={(v) => set({ srt: (v as string) || "" })}
                />
              </div>
            </>
          )}

          <div className="mb-3 flex items-center gap-3">
            <Text type="secondary" className="w-32 text-xs">目标时长（秒）</Text>
            <InputNumber min={10} max={300} step={5} value={p.targetLength} disabled={running}
              onChange={(v) => set({ targetLength: Number(v) || 30 })} />
          </div>
          <div className="mb-4 flex items-center gap-3">
            <Text type="secondary" className="w-32 text-xs">单镜头长度（秒）</Text>
            <InputNumber min={0.2} max={30} step={0.1} value={p.shotLength} disabled={running}
              onChange={(v) => set({ shotLength: Number(v) || 4 })} />
          </div>

          <Space.Compact block>
            <Button type="primary" block icon={<CaretRightOutlined />} onClick={start}
              disabled={running || p.videos.length === 0 || !p.audio} loading={running}>
              {running ? "运行中…" : "运行流水线"}
            </Button>
            <Button danger icon={<StopOutlined />} onClick={stop} disabled={!running}>停止</Button>
          </Space.Compact>
          {p.videos.length === 0 && (
            <Text type="secondary" className="mt-2 block text-xs">
              请先选择视频素材（或在素材库中「智能选材」）。
            </Text>
          )}
        </Card>
      </div>

      {error && <Alert type="error" showIcon message={error} className="mt-4" />}

      {(jobId || job.lines.length > 0) && (
        <Card size="small" title="🖥️ 流水线状态" className="mt-4">
          <StagePipeline stages={job.meta.stages ?? {}} times={job.meta.stage_times ?? {}} />
          {job.status === "done" && (
            <Alert type="success" showIcon className="mb-2"
              message="✅ 流水线完成！切换到「🎬 渲染导出」生成视频。" />
          )}
          {job.status === "error" && (
            <Alert type="error" showIcon className="mb-2"
              message="❌ 流水线失败 — 查看下方日志。缓存已保存，修复后重新运行会跳过已完成步骤。" />
          )}
          <JobLog lines={job.lines} />
        </Card>
      )}
    </div>
  );
}

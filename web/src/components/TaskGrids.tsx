import { useMemo } from "react";
import { Typography } from "antd";

const { Text } = Typography;

const TASK_LABEL: Record<string, string> = {
  audio_segments: "🎵 音频片段描述",
  editor_shots: "✂️ 镜头选择 Agent",
  video_clips: "🎬 视频片段理解",
  video_scenes: "🔍 场景分析",
};

interface TaskInfo {
  total: number;
  states: Record<string, string>; // idx → "r" | "d" | "f"
  labels?: Record<string, string>;
  done?: number;
  fail?: number;
  avg?: number;
  eta?: number;
}

function SegGrid({ name, t }: { name: string; t: TaskInfo }) {
  const total = t.total ?? 0;
  const states = t.states ?? {};

  const cells = useMemo(
    () => Array.from({ length: total }, (_, i) => states[String(i)] ?? "p"),
    [total, states],
  );
  const running = cells.filter((s) => s === "r").length;
  const done = t.done ?? cells.filter((s) => s === "d").length;
  const fail = t.fail ?? 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  if (total === 0) return null;

  return (
    <div className="seg-task">
      <div className="seg-head">
        <Text strong style={{ fontSize: 13 }}>{TASK_LABEL[name] ?? name}</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {done}/{total} 完成（{pct}%）
          {running > 0 && <span className="seg-running-txt"> · ⚡ {running} 并行中</span>}
          {fail > 0 && <span style={{ color: "#f87171" }}> · {fail} 失败</span>}
          {t.avg ? ` · 平均 ${t.avg}s/个` : ""}
          {t.eta && running > 0 ? ` · 预计还需 ${(t.eta / 60).toFixed(1)} 分钟` : ""}
        </Text>
      </div>
      <div className="seg-grid">
        {cells.map((s, i) => {
          const lab = t.labels?.[String(i)];
          const st = s === "d" ? "已完成" : s === "r" ? "处理中" : s === "f" ? "失败" : "等待中";
          return <div key={i} className={`seg seg-${s}`} title={`#${i + 1}${lab ? ` ${lab}` : ""} · ${st}`} />;
        })}
      </div>
    </div>
  );
}

/**
 * Fine-grained execution monitor: one cell per work unit (audio segment /
 * shot). Grey = pending, pulsing amber = in flight (parallel workers),
 * green = done, red = failed. Driven by job.meta.tasks.
 */
export default function TaskGrids({ tasks }: { tasks: Record<string, TaskInfo> }) {
  const entries = Object.entries(tasks ?? {});
  if (entries.length === 0) return null;
  return (
    <div className="my-2">
      {entries.map(([name, t]) => <SegGrid key={name} name={name} t={t} />)}
    </div>
  );
}

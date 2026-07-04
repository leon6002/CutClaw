import { fmtDuration } from "../api";

const LABELS: Record<string, string> = {
  shot_detection: "🎞️ 镜头检测",
  asr: "🎙️ 语音识别",
  video_captioning: "🎬 视频理解",
  audio_analysis: "🎵 音频分析",
  screenwriter: "✍️ 编剧",
  editor: "✂️ 剪辑",
};
const ICONS: Record<string, string> = { pending: "⏳", running: "🔄", done: "✅", error: "❌" };

const STYLE: Record<string, string> = {
  pending: "border-neutral-800 text-neutral-500",
  running: "border-amber-500/70 text-amber-400 animate-pulse",
  done: "border-green-700/70 text-green-400",
  error: "border-red-600/70 text-red-400",
};

export default function StagePipeline({
  stages, times,
}: { stages: Record<string, string>; times: Record<string, number> }) {
  return (
    <div className="my-3 flex flex-wrap gap-2">
      {Object.keys(LABELS).map((k) => {
        const st = stages?.[k] ?? "pending";
        return (
          <div key={k}
            className={`min-w-28 flex-1 rounded-lg border bg-neutral-900/60 px-2 py-2 text-center text-xs ${STYLE[st] ?? STYLE.pending}`}>
            <span>{ICONS[st] ?? "⏳"}</span>
            <span className="mt-1 block font-semibold">{LABELS[k]}</span>
            {times?.[k] ? <span>{fmtDuration(times[k])}</span> : null}
          </div>
        );
      })}
    </div>
  );
}

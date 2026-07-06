/**
 * 流水线参数设置 — curated tuning knobs with plain-Chinese explanations.
 * Values read/write src/config.py via /api/config (whitelisted keys only);
 * jobs reload config before running, so changes apply to the NEXT run.
 */
import { useEffect, useState } from "react";
import { SlidersHorizontal } from "lucide-react";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api } from "../api";

type Item = {
  k: string; label: string; hint: string;
  type: "int" | "float" | "bool";
  min?: number; max?: number;
};

const GROUPS: { title: string; items: Item[] }[] = [
  {
    title: "标注 / 分析",
    items: [
      { k: "ANNOTATE_VIDEO_WORKERS", label: "并行标注文件数", type: "int", min: 1, max: 8,
        hint: "进程级并行，2-4 常用；上限看内存与 API 限流" },
      { k: "CAPTION_BATCH_SIZE", label: "VLM 并发请求数", type: "int", min: 1, max: 128,
        hint: "片段/密集/场景分析的并发上限（本地 VLM 运行时自动压到 4）" },
      { k: "VIDEO_CAPTION_MAX_FRAMES", label: "每镜头最大帧数", type: "int", min: 0, max: 120,
        hint: "送 VLM 的帧数上限（运动感知选帧）；0 = 不限，更准更贵" },
      { k: "SOUND_HIGHLIGHT_THRESHOLD", label: "声音高光灵敏度", type: "float", min: 0.1, max: 0.9,
        hint: "自动标注用的 VAD 阈值：越低越灵敏、段落更宽。0.3 灵敏 / 0.5 标准 / 0.65 严格" },
      { k: "AUDIO_BATCH_SIZE", label: "音频描述并发数", type: "int", min: 1, max: 20,
        hint: "音乐分段 LLM 描述的并发请求数（标注与流水线共用）；越大越快，受 API 限流约束" },
    ],
  },
  {
    title: "剪辑 / 选材",
    items: [
      { k: "AGENT_MAX_ITERATIONS", label: "每镜头调用预算", type: "int", min: 2, max: 8,
        hint: "agent 每镜头的模型调用次数（超出后有 1 次强制提交宽限）" },
      { k: "PARALLEL_SHOT_MAX_WORKERS", label: "并行选镜数", type: "int", min: 1, max: 8,
        hint: "不同源视频的镜头并行选取；同源镜头始终串行防重叠" },
      { k: "PARALLEL_SHOT_MAX_RERUNS", label: "冲突重试轮数", type: "int", min: 0, max: 3,
        hint: "冲突镜头的重选轮数，每轮是完整 agent 调用（较贵）" },
      { k: "SHOT_MIN_GAP_SEC", label: "同源镜头最小间距 (秒)", type: "float", min: 0, max: 10,
        hint: "同一源视频里两个选取之间的最小距离，防止画面看着重复" },
      { k: "ALLOW_DURATION_TOLERANCE", label: "时长容差 (秒)", type: "float", min: 0.2, max: 3,
        hint: "选取时长与剧本目标的允许偏差，超出会被审查退回" },
      { k: "MIN_ACCEPTABLE_SHOT_DURATION", label: "镜头时长下限 (秒)", type: "float", min: 1, max: 4,
        hint: "任何镜头不得短于此值（宽限提交时的底线）" },
    ],
  },
  {
    title: "画质门槛（实测值）",
    items: [
      { k: "STABILITY_CHECK_ENABLED", label: "启用画质门槛", type: "bool",
        hint: "从真实帧测运动模糊/剧烈晃动，低分区间的选取会被拒绝" },
      { k: "STABILITY_MIN_SCORE", label: "画质最低分 (0-10)", type: "float", min: 0, max: 10,
        hint: "3.5 = 只拦严重糊片（无人机修正镜头≈1 分）；5 = 更严格" },
    ],
  },
  {
    title: "节奏（音乐切分）",
    items: [
      { k: "AUDIO_MIN_SEGMENT_DURATION", label: "镜头槽最短 (秒)", type: "float", min: 0.5, max: 10,
        hint: "音乐切分出的镜头槽下限；实际配速还会按实测小节与曲目强度调整" },
      { k: "AUDIO_MAX_SEGMENT_DURATION", label: "镜头槽最长 (秒)", type: "float", min: 2, max: 20,
        hint: "长镜头的时长上限（平静段的呼吸镜头）" },
    ],
  },
];

function Field({ item, value, onSave }: {
  item: Item; value: string; onSave: (k: string, v: string) => Promise<boolean>;
}) {
  const [val, setVal] = useState(value);
  const [saved, setSaved] = useState(false);
  useEffect(() => setVal(value), [value]);

  const flash = () => { setSaved(true); window.setTimeout(() => setSaved(false), 1400); };

  if (item.type === "bool") {
    const on = val === "True" || val === "true" || val === "1";
    return (
      <div className="flex items-start gap-2 py-1.5">
        <button
          className={"mt-0.5 h-5 w-9 shrink-0 rounded-full border transition-colors " +
            (on ? "border-cyan-400/60 bg-cyan-500/40" : "border-white/15 bg-white/[0.06]")}
          onClick={async () => {
            const nv = on ? "False" : "True";
            setVal(nv);
            if (await onSave(item.k, nv)) flash();
          }}
        >
          <span className={"block h-3.5 w-3.5 rounded-full bg-white transition-transform " +
            (on ? "translate-x-[18px]" : "translate-x-[3px]")} />
        </button>
        <div className="min-w-0">
          <div className="text-xs text-slate-300">{item.label}
            {saved && <span className="ml-2 text-[10px] text-emerald-400">✓ 已保存</span>}
          </div>
          <div className="text-[10.5px] leading-snug text-slate-600">{item.hint}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-2 py-1.5">
      <Input
        className="h-7 w-[72px] shrink-0 border-white/10 bg-black/25 text-center text-xs"
        value={val}
        onChange={async (e) => {
          const v = e.target.value;
          setVal(v);
          const n = item.type === "int" ? parseInt(v, 10) : parseFloat(v);
          if (!Number.isFinite(n)) return;
          if (item.min !== undefined && n < item.min) return;
          if (item.max !== undefined && n > item.max) return;
          if (item.type === "int" && String(n) !== v.trim()) return;
          if (await onSave(item.k, String(n))) flash();
        }}
      />
      <div className="min-w-0">
        <div className="text-xs text-slate-300">{item.label}
          <span className="ml-1.5 text-[10px] text-slate-600">
            {item.min !== undefined ? `${item.min}–${item.max}` : ""}
          </span>
          {saved && <span className="ml-2 text-[10px] text-emerald-400">✓ 已保存</span>}
        </div>
        <div className="text-[10.5px] leading-snug text-slate-600">{item.hint}</div>
      </div>
    </div>
  );
}

export default function PipelineSettings({ open, onOpenChange }: {
  open: boolean; onOpenChange: (o: boolean) => void;
}) {
  const [cfg, setCfg] = useState<Record<string, string>>({});

  useEffect(() => {
    if (open) {
      api<Record<string, string>>("/api/config").then(setCfg).catch(() => {});
    }
  }, [open]);

  const save = async (k: string, v: string) => {
    try {
      await api("/api/config", { method: "PUT", body: JSON.stringify({ values: { [k]: v } }) });
      setCfg((c) => ({ ...c, [k]: v }));
      return true;
    } catch {
      return false;
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto border-white/10 bg-slate-950 sm:max-w-[680px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-sm">
            <SlidersHorizontal className="h-4 w-4 text-cyan-400" /> 流水线参数
          </DialogTitle>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-x-8 gap-y-3 sm:grid-cols-2">
          {GROUPS.map((g) => (
            <div key={g.title}>
              <div className="mb-1 border-b border-white/[0.07] pb-1 text-[11px] font-semibold tracking-wide text-cyan-300/80">
                {g.title}
              </div>
              {g.items.map((it) => (
                <Field key={it.k} item={it} value={String(cfg[it.k] ?? "")} onSave={save} />
              ))}
            </div>
          ))}
        </div>
        <div className="mt-1 rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 text-[10.5px] leading-relaxed text-slate-500">
          改动即时写入配置，<b className="text-slate-400">下一次运行</b>（标注 / 流水线 / 渲染）自动生效，无需重启。
          声音高光灵敏度是<b className="text-slate-400">自动标注</b>的默认值——素材详情页的灵敏度选择器只影响手动检测。
        </div>
      </DialogContent>
    </Dialog>
  );
}

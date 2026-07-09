import { useEffect, useState } from "react";
import { ChevronDown, Coins } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "../api";

type Row = {
  stage?: string; model?: string; calls: number; prompt_tokens: number;
  completion_tokens: number; cost_usd: number; images: number; media_bytes: number;
};
type Usage = {
  available: boolean; running?: boolean; cny_rate?: number;
  totals?: Row & { ok: number; fail: number };
  by_stage?: Row[]; by_model?: Row[];
};

const fmtTok = (n: number) =>
  n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n ?? 0);
const fmtMB = (b: number) => (b / 1048576).toFixed(1);

const STAGE_LABEL: Record<string, string> = {
  shot_detection: "镜头检测", captioning: "片段理解", dense_caption: "密集描述",
  scene_analysis: "场景分析", scene_merge: "场景合并", audio_analysis: "音频分析",
  screenwriter: "AI 编剧", editor: "AI 剪辑", bgm_fusion: "BGM 融合",
  "(未标注)": "其他",
};

/** 本次流水线的实时 API 账单:总成本 + 阶段/模型分解(轮询 llm_calls 日志聚合). */
export default function ApiCostPanel({ jobId, running }: { jobId: string; running: boolean }) {
  const [u, setU] = useState<Usage | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let stop = false;
    const tick = () =>
      api<Usage>(`/api/pipeline/usage?job_id=${encodeURIComponent(jobId)}`)
        .then((r) => { if (!stop) setU(r); })
        .catch(() => {});
    tick();
    if (!running) return () => { stop = true; };
    const t = window.setInterval(tick, 3000);
    return () => { stop = true; window.clearInterval(t); };
  }, [jobId, running]);

  if (!u?.available || !u.totals) return null;
  const t = u.totals;
  const rate = u.cny_rate ?? 7.2;
  const maxCost = Math.max(...(u.by_stage ?? []).map((s) => s.cost_usd), 1e-9);

  return (
    <div className="mb-3 rounded-xl border border-amber-500/20 bg-amber-500/[0.04]">
      <button
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs"
        onClick={() => setOpen((o) => !o)}
      >
        <Coins className="h-3.5 w-3.5 shrink-0 text-amber-400" />
        <span className="font-semibold text-amber-300">
          本次 API ≈ ¥{(t.cost_usd * rate).toFixed(2)}
          <span className="ml-1 font-normal text-amber-400/70">(${t.cost_usd.toFixed(4)})</span>
        </span>
        <span className="text-slate-400">
          {t.calls} 次调用{t.fail ? <span className="text-red-400">(失败 {t.fail})</span> : null}
          · 输入 {fmtTok(t.prompt_tokens)} / 输出 {fmtTok(t.completion_tokens)} tok
          {t.images > 0 && <> · 上传 {t.images} 帧 / {fmtMB(t.media_bytes)} MB</>}
        </span>
        {running && <span className="ml-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />}
        <ChevronDown className={cn("ml-auto h-3.5 w-3.5 shrink-0 text-slate-500 transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div className="border-t border-white/[0.06] px-3 pb-2.5 pt-2 text-[11px]">
          <table className="w-full">
            <thead>
              <tr className="text-slate-500">
                <th className="pb-1 text-left font-normal">阶段</th>
                <th className="pb-1 text-right font-normal">调用</th>
                <th className="pb-1 text-right font-normal">输入 tok</th>
                <th className="pb-1 text-right font-normal">输出 tok</th>
                <th className="pb-1 text-right font-normal">媒体</th>
                <th className="pb-1 text-right font-normal">成本</th>
                <th className="w-1/4 pb-1" />
              </tr>
            </thead>
            <tbody>
              {(u.by_stage ?? []).map((s) => (
                <tr key={s.stage} className="text-slate-300">
                  <td className="py-0.5">{STAGE_LABEL[s.stage ?? ""] ?? s.stage}</td>
                  <td className="py-0.5 text-right">{s.calls}</td>
                  <td className="py-0.5 text-right">{fmtTok(s.prompt_tokens)}</td>
                  <td className="py-0.5 text-right">{fmtTok(s.completion_tokens)}</td>
                  <td className="py-0.5 text-right text-slate-500">
                    {s.images > 0 ? `${s.images}帧/${fmtMB(s.media_bytes)}MB` : "—"}
                  </td>
                  <td className="py-0.5 text-right font-medium text-amber-300">
                    ¥{(s.cost_usd * rate).toFixed(2)}
                  </td>
                  <td className="py-0.5 pl-2">
                    <div className="h-1.5 rounded bg-amber-400/70"
                      style={{ width: `${Math.max(2, (s.cost_usd / maxCost) * 100)}%` }} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-1.5 text-slate-500">
            {(u.by_model ?? []).map((m) => (
              <span key={m.model} className="mr-3">
                {m.model}:{m.calls} 次 · ¥{(m.cost_usd * rate).toFixed(2)}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

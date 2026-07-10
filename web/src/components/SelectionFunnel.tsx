import { useState } from "react";
import { ChevronDown, Filter } from "lucide-react";
import { cn } from "@/lib/utils";

/** 选材漏斗(§18):从高光池到成片镜头,每一层淘汰都可见。
 *  数据来自 /api/pipeline/selection_trace(零 API,全读缓存)。 */

const REASON_LABEL: Record<string, string> = {
  cluster_quota: "同款画面配额满",
  source_quota: "同源素材配额满",
  interval_overlap: "与已选区间重叠",
};

const tierCls = (t?: string) =>
  t === "S" ? "bg-amber-400/20 text-amber-300" : t === "A" ? "bg-emerald-400/15 text-emerald-300"
    : t === "B" ? "bg-sky-400/15 text-sky-300" : "bg-white/10 text-slate-400";

function MomentRow({ m, used, extra }: { m: any; used?: boolean; extra?: string }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-md px-2 py-1",
      used ? "bg-emerald-500/[0.06]" : "bg-white/[0.02]")}>
      <span className="w-9 font-mono text-[10.5px] text-cyan-300">{m.id}</span>
      {m.fine_tier && <span className={cn("rounded px-1 text-[10px] font-bold", tierCls(m.fine_tier))}>{m.fine_tier}</span>}
      <span className="font-mono text-[10.5px] text-slate-500">
        {m.video?.slice(0, 22)} {Number(m.start ?? 0).toFixed(0)}–{Number(m.end ?? 0).toFixed(0)}s
      </span>
      {m.sound && <span className="text-[10px]">🎙</span>}
      {m.event === 1 && <span className="text-[10px]">⚡</span>}
      {m.empty_shot && <span className="text-[10px] text-slate-500">空镜</span>}
      {used && <span className="text-[10px] text-emerald-400">✓ 已用</span>}
      {extra && <span className="text-[10px] text-amber-300">{extra}</span>}
      <span className="w-full truncate text-[10.5px] text-slate-400" title={m.critique || m.desc}>
        {m.critique ? `「${m.critique}」` : m.desc}
      </span>
    </div>
  );
}

export default function SelectionFunnel({ trace }: { trace: any }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"menu" | "rejects" | "repairs">("menu");
  if (!trace?.available) return null;
  const f = trace.funnel ?? {};
  const usedSet = new Set(trace.menu_used ?? []);
  const rejStats: [string, number][] = Object.entries(f.reject_stats ?? {}) as any;

  return (
    <div className="mb-3 rounded-xl border border-emerald-500/20 bg-emerald-500/[0.04]">
      <button className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left text-xs"
        onClick={() => setOpen((o) => !o)}>
        <Filter className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
        <span className="font-semibold text-emerald-300">选材漏斗</span>
        <span className="text-slate-400">
          高光池 <b className="text-slate-200">{f.pool_total}</b> 时刻
          <span className="mx-1 text-slate-600">→</span>
          {f.looks} 种画面
          <span className="mx-1 text-slate-600">→</span>
          菜单 <b className="text-slate-200">{f.menu}</b> 条
          <span className="mx-1 text-slate-600">→</span>
          锚定 <b className="text-emerald-300">{f.anchored}/{f.shots}</b> 镜头
          {f.repairs > 0 && <span className="text-amber-300">(修复 {f.repairs})</span>}
        </span>
        {rejStats.length > 0 && (
          <span className="text-[10.5px] text-slate-500">
            淘汰:{rejStats.map(([k, v]) => `${REASON_LABEL[k] ?? k} ${v}`).join(" · ")}
          </span>
        )}
        <ChevronDown className={cn("ml-auto h-3.5 w-3.5 shrink-0 text-slate-500 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="border-t border-white/[0.06] px-3 pb-2.5 pt-2">
          <div className="mb-2 flex gap-1 rounded-lg border border-white/10 bg-white/[0.03] p-0.5 text-[11px]">
            {([["menu", `菜单 (${(trace.menu ?? []).length})`],
               ["rejects", `被拒 (${(trace.rejects ?? []).length})`],
               ["repairs", `修复 (${(trace.repairs ?? []).length})`]] as const).map(([v, label]) => (
              <button key={v}
                className={cn("rounded-md px-2.5 py-1 transition-colors",
                  tab === v ? "bg-emerald-500/15 font-semibold text-emerald-300" : "text-slate-400 hover:text-slate-200")}
                onClick={() => setTab(v)}>
                {label}
              </button>
            ))}
            <span className="ml-auto self-center text-[10px] text-slate-600">
              ✓绿底 = 编剧最终采用 · 配额 look≤{f.caps?.look ?? "?"} / 源≤{f.caps?.source ?? "?"}
            </span>
          </div>
          <div className="max-h-72 space-y-1 overflow-y-auto pr-1">
            {tab === "menu" && (trace.menu ?? []).map((m: any) => (
              <MomentRow key={m.id} m={m} used={usedSet.has(m.id)} />
            ))}
            {tab === "rejects" && (trace.rejects ?? []).map((m: any, i: number) => (
              <MomentRow key={`${m.id}-${i}`} m={m} extra={REASON_LABEL[m.reason] ?? m.reason} />
            ))}
            {tab === "repairs" && ((trace.repairs ?? []).length === 0
              ? <div className="py-2 text-center text-[11px] text-slate-500">没有发生换锚修复 — 编剧的选择全部直接通过品控</div>
              : (trace.repairs ?? []).map((r: any, i: number) => (
                <div key={i} className="rounded-md bg-amber-500/[0.06] px-2 py-1 text-[11px] text-amber-200/90">
                  镜头 #{r.shot}:原选 <span className="font-mono">{r.original ?? "无"}</span>,
                  因「{r.reason}」改为 <span className="font-mono">{r.result === "agent" ? "Agent 自由挑选" : r.result}</span>
                </div>
              )))}
          </div>
        </div>
      )}
    </div>
  );
}

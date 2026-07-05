/**
 * Local VLM / GPU panel — manual, observable control over the local model.
 * Slim bar (always visible on the assets page): live GPU util / VRAM / model
 * residency. Expanded: load / unload / test buttons with REAL timings, so the
 * user can verify the 3090 is actually doing the work.
 */
import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Cpu, FlaskConical, Loader2, Power, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { api } from "../api";

interface Gpu { ok: boolean; name?: string; util?: number; mem_used_mb?: number; mem_total_mb?: number; temp?: number; error?: string }
interface OllamaModel { name: string; size_gb: number; loaded: boolean; vram_gb?: number | null; until?: string }

export default function LocalGpuPanel() {
  const [open, setOpen] = useState(false);
  const [gpu, setGpu] = useState<Gpu | null>(null);
  const [models, setModels] = useState<OllamaModel[] | null>(null);
  const [ollamaErr, setOllamaErr] = useState("");
  const [busyBtn, setBusyBtn] = useState("");
  const [msg, setMsg] = useState("");
  const [testOut, setTestOut] = useState<{ seconds: number; reply: string } | null>(null);
  const [stats, setStats] = useState<{ count: number; avg_s?: number; last_s?: number } | null>(null);
  const timer = useRef(0);

  const refreshGpu = () => api<Gpu>("/api/local/gpu").then(setGpu).catch(() => {});
  const refreshOllama = () =>
    api<any>("/api/local/ollama").then((r) => {
      if (r.ok) { setModels(r.models); setOllamaErr(""); }
      else { setModels([]); setOllamaErr(r.error || "Ollama 不可用"); }
    }).catch((e) => setOllamaErr(e.message));
  const refreshStats = () =>
    api<any>("/api/local/vlm-stats?model=qwen2.5vl").then(setStats).catch(() => {});

  useEffect(() => {
    refreshGpu(); refreshOllama(); refreshStats();
    timer.current = window.setInterval(() => {
      refreshGpu();
      if (open) { refreshOllama(); refreshStats(); }
    }, 2500);
    return () => window.clearInterval(timer.current);
  }, [open]);

  const act = async (btn: string, path: string, model: string) => {
    setBusyBtn(btn); setMsg(""); setTestOut(null);
    try {
      const r = await api<any>(path, { method: "POST", body: JSON.stringify({ model, keep_alive: "2h" }) });
      if (r.ok) {
        if (btn.startsWith("test")) setTestOut({ seconds: r.seconds, reply: r.reply });
        else setMsg(`✓ ${r.message}${r.seconds ? `（${r.seconds}s）` : ""}`);
      } else setMsg(`✗ ${r.error}`);
    } catch (e: any) { setMsg(`✗ ${e.message}`); }
    setBusyBtn("");
    refreshOllama(); refreshGpu();
  };

  const vram = gpu?.ok ? `${((gpu.mem_used_mb ?? 0) / 1024).toFixed(1)}/${((gpu.mem_total_mb ?? 0) / 1024).toFixed(0)}G` : "—";
  const loadedModel = models?.find((m) => m.loaded);

  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-white/[0.07] bg-slate-900/40">
      {/* slim status bar */}
      <button
        className="flex w-full items-center gap-3 px-3.5 py-2 text-left hover:bg-white/[0.03]"
        onClick={() => setOpen((o) => !o)}
      >
        <Cpu className="h-3.5 w-3.5 shrink-0 text-violet-400" />
        <span className="text-xs font-semibold text-slate-300">本地模型 / GPU</span>
        {gpu?.ok ? (
          <span className="flex items-center gap-3 text-[11px] text-slate-400">
            <span className="flex items-center gap-1.5">
              GPU
              <span className="inline-block h-1.5 w-16 overflow-hidden rounded-full bg-white/[0.08]">
                <span
                  className={cn("block h-full transition-all", (gpu.util ?? 0) > 60 ? "bg-emerald-400" : "bg-cyan-400")}
                  style={{ width: `${gpu.util ?? 0}%` }}
                />
              </span>
              <span className="font-mono">{gpu.util}%</span>
            </span>
            <span className="font-mono">VRAM {vram}</span>
            <span className="font-mono">{gpu.temp}°C</span>
          </span>
        ) : (
          <span className="text-[11px] text-slate-600">GPU 状态不可用</span>
        )}
        {loadedModel
          ? <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10.5px] text-emerald-400">
              {loadedModel.name} 已驻显存 {loadedModel.vram_gb ? `(${loadedModel.vram_gb}G)` : ""}
            </span>
          : <span className="rounded-full border border-white/10 bg-white/[0.03] px-2 py-0.5 text-[10.5px] text-slate-500">
              模型未加载
            </span>}
        <span className="ml-auto text-slate-500">
          {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </span>
      </button>

      {open && (
        <div className="border-t border-white/[0.06] px-3.5 py-3">
          {ollamaErr && (
            <div className="mb-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
              {ollamaErr}
            </div>
          )}
          {(models ?? []).filter((m) => m.name.includes("vl") || m.name.includes("VL")).map((m) => (
            <div key={m.name} className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-slate-200">{m.name}</span>
              <span className="text-[11px] text-slate-500">{m.size_gb}GB</span>
              {m.loaded
                ? <span className="text-[11px] text-emerald-400">✓ 显存驻留 {m.vram_gb}G</span>
                : <span className="text-[11px] text-slate-500">未加载</span>}
              <div className="ml-auto flex gap-1.5">
                <Button variant="outline" size="sm" className="h-7 gap-1 border-white/10 bg-white/[0.04] text-[11px]"
                  disabled={!!busyBtn} onClick={() => act(`load-${m.name}`, "/api/local/ollama/load", m.name)}>
                  {busyBtn === `load-${m.name}` ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}
                  加载到显存 (驻留2h)
                </Button>
                <Button variant="outline" size="sm" className="h-7 gap-1 border-white/10 bg-white/[0.04] text-[11px]"
                  disabled={!!busyBtn || !m.loaded} onClick={() => act(`unload-${m.name}`, "/api/local/ollama/unload", m.name)}>
                  {busyBtn === `unload-${m.name}` ? <Loader2 className="h-3 w-3 animate-spin" /> : <Power className="h-3 w-3" />}
                  卸载
                </Button>
                <Button variant="outline" size="sm" className="h-7 gap-1 border-cyan-500/25 bg-cyan-500/[0.06] text-[11px] text-cyan-300"
                  disabled={!!busyBtn} onClick={() => act(`test-${m.name}`, "/api/local/ollama/test", m.name)}>
                  {busyBtn === `test-${m.name}` ? <Loader2 className="h-3 w-3 animate-spin" /> : <FlaskConical className="h-3 w-3" />}
                  测试一帧
                </Button>
              </div>
            </div>
          ))}
          {models !== null && (models ?? []).filter((m) => m.name.toLowerCase().includes("vl")).length === 0 && !ollamaErr && (
            <div className="text-xs text-slate-500">没有发现视觉模型 — 终端运行: ollama pull qwen2.5vl</div>
          )}

          {msg && <div className="mt-1 text-xs text-slate-300">{msg}</div>}
          {testOut && (
            <div className="mt-2 rounded-lg border border-cyan-500/20 bg-cyan-500/[0.05] px-3 py-2">
              <div className="text-xs font-semibold text-cyan-300">实测耗时 {testOut.seconds}s（单帧真实调用）</div>
              <div className="mt-1 text-[11.5px] text-slate-300">{testOut.reply}</div>
            </div>
          )}

          {stats && stats.count > 0 && (
            <div className="mt-2 text-[11px] text-slate-500">
              最近 {stats.count} 次本地 VLM 调用：平均 {stats.avg_s}s · 最近一次 {stats.last_s}s
            </div>
          )}
          <div className="mt-2 text-[10.5px] text-slate-600">
            标注运行时盯着上面的 GPU 利用率条 — 有波动 = 显卡在干活；模型未驻留时首次调用需 ~45s 加载。
          </div>
        </div>
      )}
    </div>
  );
}

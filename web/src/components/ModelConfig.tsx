/**
 * Single source of truth for AI model assignment.
 * Models/endpoints/keys are ONLY editable in the API pool; every AI call-site
 * gets a RoleModelSelect that assigns a pool entry to a role (vision/audio/agent).
 */
import {
  createContext, useCallback, useContext, useEffect, useState, type ReactNode,
} from "react";
import { AudioLines, Bot, Eye, TriangleAlert, type LucideIcon } from "lucide-react";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { api } from "../api";

export interface PoolEntry {
  name?: string;
  model?: string;
  endpoint?: string;
  api_base?: string;
  api_key?: string;
  multimodal?: boolean;
}

export const ROLES = {
  vision: {
    label: "视觉", mmOnly: true, icon: Eye as LucideIcon, iconCls: "text-sky-400",
    refKey: "VISION_POOL_REF",
    keys: ["VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY"],
    hint: "视频/图片理解",
  },
  audio: {
    label: "音频", mmOnly: false, icon: AudioLines as LucideIcon, iconCls: "text-violet-400",
    refKey: "AUDIO_POOL_REF",
    keys: ["AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY"],
    hint: "音乐分析",
  },
  agent: {
    label: "Agent", mmOnly: false, icon: Bot as LucideIcon, iconCls: "text-cyan-400",
    refKey: "AGENT_POOL_REF",
    keys: ["AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY"],
    hint: "编剧/剪辑/选材/建议",
  },
} as const;

export type RoleKey = keyof typeof ROLES;

interface Ctx {
  cfg: Record<string, string>;
  pool: PoolEntry[];
  setRole: (role: RoleKey, entryName: string) => Promise<void>;
  reload: () => Promise<void>;
}

const ModelConfigContext = createContext<Ctx>({
  cfg: {}, pool: [], setRole: async () => {}, reload: async () => {},
});

export function ModelConfigProvider({ children }: { children: ReactNode }) {
  const [cfg, setCfg] = useState<Record<string, string>>({});
  const [pool, setPool] = useState<PoolEntry[]>([]);

  const reload = useCallback(async () => {
    try { setCfg(await api<Record<string, string>>("/api/config")); } catch { /* ignore */ }
    try { setPool(await api<PoolEntry[]>("/api/api-pool")); } catch { /* ignore */ }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  const setRole = useCallback(async (role: RoleKey, entryName: string) => {
    const e = pool.find((p) => (p.name ?? p.model) === entryName);
    if (!e) return;
    const [mk, ek, kk] = ROLES[role].keys;
    const values: Record<string, string> = {
      [ROLES[role].refKey]: entryName,   // authoritative reference
      [mk]: e.model ?? "",               // materialized cache for the pipeline
      [ek]: e.endpoint ?? e.api_base ?? "",
      [kk]: e.api_key ?? "",
    };
    setCfg((c) => ({ ...c, ...values }));
    try {
      await api("/api/config", { method: "PUT", body: JSON.stringify({ values }) });
    } catch { /* ignore */ }
  }, [pool]);

  return (
    <ModelConfigContext.Provider value={{ cfg, pool, setRole, reload }}>
      {children}
    </ModelConfigContext.Provider>
  );
}

export function useModelConfig() {
  return useContext(ModelConfigContext);
}

/** Pool-entry selector bound to a role. The only way to switch models outside the pool. */
export function RoleModelSelect({
  role, showLabel = true, className, disabled,
}: { role: RoleKey; showLabel?: boolean; className?: string; disabled?: boolean }) {
  const { cfg, pool, setRole } = useModelConfig();
  const def = ROLES[role];
  const [mk, ek, kk] = def.keys;
  const candidates = pool.filter((p) => !def.mmOnly || p.multimodal);
  // authoritative: the stored reference; fallback: legacy model+endpoint match
  const current =
    candidates.find((p) => (p.name ?? p.model) === (cfg[def.refKey] ?? "")) ??
    candidates.find(
      (p) => (p.model ?? "") === (cfg[mk] ?? "")
        && ((p.endpoint ?? p.api_base ?? "") === (cfg[ek] ?? "")),
    );
  // key drift: config cache diverged (e.g. hand-edited) — auto-healed on pool save
  const keyDrift = !!current && (current.api_key ?? "") !== (cfg[kk] ?? "");
  const Icon = def.icon;

  return (
    <div className={cn("flex items-center gap-1.5", className)}>
      {showLabel && (
        <span className="flex items-center gap-1 whitespace-nowrap text-xs text-slate-400" title={def.hint}>
          <Icon className={cn("h-3.5 w-3.5", def.iconCls)} />
          {def.label}
        </span>
      )}
      <Select
        value={current ? (current.name ?? current.model) : undefined}
        onValueChange={(v) => setRole(role, v)}
        disabled={disabled}
      >
        <SelectTrigger className="h-7 min-w-[150px] border-white/10 bg-black/25 text-xs">
          <SelectValue placeholder={cfg[mk] ? `未入池：${cfg[mk]}` : "选择模型"} />
        </SelectTrigger>
        <SelectContent>
          {candidates.length === 0 && (
            <div className="px-3 py-2 text-xs text-slate-500">
              API 池为空{def.mmOnly ? "（或没有多模态条目）" : ""} — 先到「模型设置 → API 池管理」添加
            </div>
          )}
          {candidates.map((p) => (
            <SelectItem key={p.name ?? p.model} value={(p.name ?? p.model)!} className="text-xs">
              {p.name ?? p.model}{!(p.api_key ?? "").trim() ? "（无 Key）" : ""}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {current && !(current.api_key ?? "").trim() && (
        <span
          className="flex items-center gap-1 text-[11px] text-amber-400"
          title="该池条目没有配置 API Key，云端网关会返回 401（本地无鉴权服务可忽略）"
        >
          <TriangleAlert className="h-3.5 w-3.5" />无 Key
        </span>
      )}
      {keyDrift && (current.api_key ?? "").trim() !== "" && (
        <button
          className="flex cursor-pointer items-center gap-1 text-[11px] text-amber-400 hover:text-amber-300"
          title="配置中缓存的 Key 与池条目不一致（池被改过），点击用池里的最新值重新指派"
          onClick={() => setRole(role, (current.name ?? current.model)!)}
        >
          <TriangleAlert className="h-3.5 w-3.5" />Key 不同步，点击修复
        </button>
      )}
    </div>
  );
}

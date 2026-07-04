import { useEffect, useState, type ReactNode } from "react";
import {
  CircleCheck, CircleX, FlaskConical, Loader2, Plus, Save, Settings2, Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import { api } from "../api";
import {
  ROLES, RoleModelSelect, useModelConfig, type PoolEntry, type RoleKey,
} from "../components/ModelConfig";

interface TestResult { loading?: boolean; ok?: boolean; msg?: string }

const FieldLabel = ({ children }: { children: ReactNode }) => (
  <div className="mb-1 text-xs text-slate-400">{children}</div>
);
const inputCls = "border-white/10 bg-black/25";

export default function SettingsModal({ onClose }: { onClose: () => void }) {
  const ctx = useModelConfig();
  const [pool, setPool] = useState<PoolEntry[]>([]);
  const [poolSaving, setPoolSaving] = useState(false);
  const [poolDirty, setPoolDirty] = useState(false);
  const [error, setError] = useState("");
  const [tests, setTests] = useState<Record<number, TestResult>>({});

  // local editable copy of the pool, seeded from context
  useEffect(() => {
    if (!poolDirty) setPool(ctx.pool);
  }, [ctx.pool]);

  const patchEntry = (i: number, patch: Partial<PoolEntry>) => {
    setPool((ps) => ps.map((p, j) => (j === i ? { ...p, ...patch } : p)));
    setPoolDirty(true);
  };

  const savePool = async () => {
    setError(""); setPoolSaving(true);
    try {
      await api("/api/api-pool", { method: "PUT", body: JSON.stringify({ pool }) });
      setPoolDirty(false);
      await ctx.reload();
    } catch (e: any) { setError(e.message); }
    setPoolSaving(false);
  };

  const testEntry = async (i: number) => {
    const e = pool[i];
    setTests((t) => ({ ...t, [i]: { loading: true } }));
    try {
      const r = await api<any>("/api/api-pool/test", {
        method: "POST",
        body: JSON.stringify({
          model: e.model ?? "",
          endpoint: e.endpoint ?? e.api_base ?? "",
          api_key: e.api_key ?? "",
        }),
      });
      setTests((t) => ({
        ...t,
        [i]: r.ok
          ? { ok: true, msg: `${r.latency_s}s · ${r.reply || "(空回复)"}${r.tokens ? ` · ${r.tokens} tokens` : ""}` }
          : { ok: false, msg: `${r.latency_s}s · ${r.error}` },
      }));
    } catch (e2: any) {
      setTests((t) => ({ ...t, [i]: { ok: false, msg: e2.message } }));
    }
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[86vh] overflow-y-auto border-white/10 bg-slate-900/90 backdrop-blur-xl sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Settings2 className="h-4 w-4 text-cyan-400" /> 模型设置
          </DialogTitle>
        </DialogHeader>

        <Tabs defaultValue="roles">
          <TabsList className="bg-white/[0.05]">
            <TabsTrigger value="roles" className="text-xs">角色指派</TabsTrigger>
            <TabsTrigger value="pool" className="text-xs">API 池管理 ({pool.length})</TabsTrigger>
          </TabsList>

          {/* ── role → pool-entry assignment (selection only, no free input) ── */}
          <TabsContent value="roles" className="pt-3">
            <div className="mb-3 text-xs text-slate-500">
              模型、Endpoint 和 API Key 只能在「API 池管理」中维护；这里只做角色指派，选择即时生效。
            </div>
            {(Object.keys(ROLES) as RoleKey[]).map((role) => {
              const def = ROLES[role];
              const [mk, ek] = def.keys;
              return (
                <div key={role} className="mb-3 rounded-xl border border-white/[0.08] bg-white/[0.03] p-3">
                  <div className="flex flex-wrap items-center gap-3">
                    <RoleModelSelect role={role} className="min-w-[240px]" />
                    <span className="text-xs text-slate-500">{def.hint}</span>
                  </div>
                  <div className="mt-2 font-mono text-[11px] text-slate-500">
                    {ctx.cfg[mk] || "(未设置)"}{ctx.cfg[ek] ? ` @ ${ctx.cfg[ek]}` : ""}
                  </div>
                </div>
              );
            })}
          </TabsContent>

          {/* ── API pool manager: the ONLY place to edit credentials ── */}
          <TabsContent value="pool" className="pt-3">
            {pool.length === 0 && (
              <div className="py-6 text-center text-sm text-slate-500">API 池为空 — 添加一个条目</div>
            )}
            {pool.map((e, i) => {
              const t = tests[i];
              return (
                <div key={i} className="mb-3 rounded-xl border border-white/[0.08] bg-white/[0.03] p-3">
                  <div className="mb-2 grid grid-cols-2 gap-2">
                    <div>
                      <FieldLabel>名称</FieldLabel>
                      <Input className={cn("h-8 text-xs", inputCls)} value={e.name ?? ""}
                        onChange={(ev) => patchEntry(i, { name: ev.target.value })} />
                    </div>
                    <div>
                      <FieldLabel>Model（litellm 格式）</FieldLabel>
                      <Input className={cn("h-8 text-xs", inputCls)} value={e.model ?? ""}
                        onChange={(ev) => patchEntry(i, { model: ev.target.value })} />
                    </div>
                    <div>
                      <FieldLabel>Endpoint</FieldLabel>
                      <Input className={cn("h-8 text-xs", inputCls)} value={e.endpoint ?? e.api_base ?? ""}
                        onChange={(ev) => patchEntry(i, { endpoint: ev.target.value, api_base: undefined })} />
                    </div>
                    <div>
                      <FieldLabel>API Key</FieldLabel>
                      <Input type="password" className={cn("h-8 text-xs", inputCls)} value={e.api_key ?? ""}
                        onChange={(ev) => patchEntry(i, { api_key: ev.target.value })} />
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-300">
                      <Checkbox checked={!!e.multimodal}
                        onCheckedChange={(v) => patchEntry(i, { multimodal: v === true })} />
                      多模态（可做视觉分析）
                    </label>
                    <div className="ml-auto flex items-center gap-2">
                      <Button variant="outline" size="sm"
                        className="h-7 gap-1.5 border-cyan-500/25 bg-cyan-500/[0.06] text-xs text-cyan-300 hover:bg-cyan-500/15"
                        disabled={t?.loading} onClick={() => testEntry(i)}>
                        {t?.loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <FlaskConical className="h-3 w-3" />}
                        测试
                      </Button>
                      <Button variant="outline" size="sm"
                        className="h-7 gap-1 border-red-500/25 bg-red-500/[0.06] text-xs text-red-400 hover:bg-red-500/15"
                        onClick={() => { setPool((ps) => ps.filter((_, j) => j !== i)); setPoolDirty(true); }}>
                        <Trash2 className="h-3 w-3" /> 删除
                      </Button>
                    </div>
                  </div>
                  {t && !t.loading && (
                    <div className={cn(
                      "mt-2 flex items-start gap-1.5 rounded-lg px-2.5 py-1.5 text-xs",
                      t.ok ? "bg-emerald-500/10 text-emerald-400" : "bg-red-500/10 text-red-400",
                    )}>
                      {t.ok ? <CircleCheck className="mt-px h-3.5 w-3.5 shrink-0" /> : <CircleX className="mt-px h-3.5 w-3.5 shrink-0" />}
                      <span className="break-all">{t.msg}</span>
                    </div>
                  )}
                </div>
              );
            })}

            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" className="h-8 gap-1.5 border-white/10 bg-white/[0.04] text-xs"
                onClick={() => { setPool((ps) => [...ps, { name: "", model: "", endpoint: "", api_key: "", multimodal: false }]); setPoolDirty(true); }}>
                <Plus className="h-3.5 w-3.5" /> 新增条目
              </Button>
              <Button size="sm" className="ml-auto h-8 gap-1.5 text-xs" onClick={savePool}
                disabled={poolSaving || !poolDirty}>
                {poolSaving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                保存 API 池{poolDirty ? "（有未保存修改）" : ""}
              </Button>
            </div>
            {poolDirty && (
              <div className="mt-2 text-xs text-amber-400/90">修改仅在点击「保存 API 池」后写入 src/api_pool.json。</div>
            )}
          </TabsContent>
        </Tabs>

        {error && (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">{error}</div>
        )}
      </DialogContent>
    </Dialog>
  );
}

import { useEffect, useState, type ReactNode } from "react";
import { AudioLines, Bot, Eye, Loader2, Settings2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { api } from "../api";

interface PoolEntry {
  name?: string;
  model?: string;
  endpoint?: string;
  api_base?: string;
  api_key?: string;
  multimodal?: boolean;
}

const GROUPS: { title: ReactNode; mmOnly: boolean; keys: string[] }[] = [
  {
    title: <span className="flex items-center gap-1.5"><Eye className="h-3.5 w-3.5 text-sky-400" />Vision（视频/图片理解）</span>,
    mmOnly: true,
    keys: ["VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY"],
  },
  {
    title: <span className="flex items-center gap-1.5"><AudioLines className="h-3.5 w-3.5 text-violet-400" />Audio（音乐分析）</span>,
    mmOnly: false,
    keys: ["AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY"],
  },
  {
    title: <span className="flex items-center gap-1.5"><Bot className="h-3.5 w-3.5 text-cyan-400" />Agent（编剧/剪辑/选材）</span>,
    mmOnly: false,
    keys: ["AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY"],
  },
];

const FieldLabel = ({ children }: { children: ReactNode }) => (
  <div className="mb-1 text-xs text-slate-400">{children}</div>
);

export default function SettingsModal({ onClose }: { onClose: () => void }) {
  const [cfg, setCfg] = useState<Record<string, string>>({});
  const [pool, setPool] = useState<PoolEntry[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Record<string, string>>("/api/config").then(setCfg).catch(() => {});
    api<PoolEntry[]>("/api/api-pool").then(setPool).catch(() => {});
  }, []);

  const applyPreset = (groupIdx: number, name: string) => {
    const e = pool.find((p) => (p.name ?? p.model) === name);
    if (!e) return;
    const [mk, ek, kk] = GROUPS[groupIdx].keys;
    setCfg((c) => ({
      ...c,
      [mk]: e.model ?? "",
      [ek]: e.endpoint ?? e.api_base ?? "",
      [kk]: e.api_key ?? "",
    }));
  };

  const save = async () => {
    setError(""); setSaving(true);
    try {
      const values: Record<string, string> = {};
      GROUPS.forEach((g) => g.keys.forEach((k) => { values[k] = cfg[k] ?? ""; }));
      await api("/api/config", { method: "PUT", body: JSON.stringify({ values }) });
      onClose();
    } catch (e: any) { setError(e.message); }
    setSaving(false);
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[84vh] overflow-y-auto border-white/10 bg-slate-900/90 backdrop-blur-xl sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Settings2 className="h-4 w-4 text-cyan-400" /> 模型设置
          </DialogTitle>
        </DialogHeader>

        {GROUPS.map((g, gi) => {
          const candidates = pool.filter((p) => !g.mmOnly || p.multimodal);
          const [mk, ek, kk] = g.keys;
          return (
            <div key={gi} className="mb-4">
              <div className="mb-2 text-xs font-semibold tracking-wider text-slate-400 uppercase">{g.title}</div>
              {candidates.length > 0 && (
                <div className="mb-2">
                  <FieldLabel>预设（来自 api_pool.json）</FieldLabel>
                  <Select onValueChange={(v) => v && applyPreset(gi, v)}>
                    <SelectTrigger className="w-full border-white/10 bg-black/25 text-xs">
                      <SelectValue placeholder="— 选择预设 —" />
                    </SelectTrigger>
                    <SelectContent>
                      {candidates.map((p) => (
                        <SelectItem key={p.name ?? p.model} value={(p.name ?? p.model)!} className="text-xs">
                          {p.name ?? p.model}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
              <div className="mb-2">
                <FieldLabel>Model</FieldLabel>
                <Input className="border-white/10 bg-black/25" value={cfg[mk] ?? ""}
                  onChange={(e) => setCfg({ ...cfg, [mk]: e.target.value })} />
              </div>
              <div className="mb-2">
                <FieldLabel>Endpoint</FieldLabel>
                <Input className="border-white/10 bg-black/25" value={cfg[ek] ?? ""}
                  onChange={(e) => setCfg({ ...cfg, [ek]: e.target.value })} />
              </div>
              <div>
                <FieldLabel>API Key</FieldLabel>
                <Input type="password" className="border-white/10 bg-black/25" value={cfg[kk] ?? ""}
                  onChange={(e) => setCfg({ ...cfg, [kk]: e.target.value })} />
              </div>
            </div>
          );
        })}

        {error && (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">{error}</div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button onClick={save} disabled={saving}>
            {saving && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

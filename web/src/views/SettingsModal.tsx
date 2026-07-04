import { useEffect, useState } from "react";
import { Alert, Button, Input, Modal, Select, Typography } from "antd";
import { api } from "../api";

const { Text } = Typography;

interface PoolEntry {
  name?: string;
  model?: string;
  endpoint?: string;
  api_base?: string;
  api_key?: string;
  multimodal?: boolean;
}

const GROUPS = [
  {
    title: "🖼️ Vision（视频/图片理解）", mmOnly: true,
    keys: ["VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY"],
  },
  {
    title: "🎵 Audio（音乐分析）", mmOnly: false,
    keys: ["AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY"],
  },
  {
    title: "🧠 Agent（编剧/剪辑/选材）", mmOnly: false,
    keys: ["AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY"],
  },
];

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
    <Modal
      open title="⚙️ 模型设置" onCancel={onClose} width={640}
      footer={[
        <Button key="c" onClick={onClose}>取消</Button>,
        <Button key="s" type="primary" loading={saving} onClick={save}>保存</Button>,
      ]}
    >
      {GROUPS.map((g, gi) => {
        const candidates = pool.filter((p) => !g.mmOnly || p.multimodal);
        const [mk, ek, kk] = g.keys;
        return (
          <div key={g.title} className="mb-5">
            <div className="mb-2 text-xs font-semibold tracking-wider text-neutral-400 uppercase">{g.title}</div>
            {candidates.length > 0 && (
              <div className="mb-2">
                <Text type="secondary" className="mb-1 block text-xs">预设（来自 api_pool.json）</Text>
                <Select
                  style={{ width: "100%" }} placeholder="— 选择预设 —" allowClear
                  options={candidates.map((p) => ({ value: p.name ?? p.model, label: p.name ?? p.model }))}
                  onChange={(v) => v && applyPreset(gi, v as string)}
                />
              </div>
            )}
            <div className="mb-2">
              <Text type="secondary" className="mb-1 block text-xs">Model</Text>
              <Input value={cfg[mk] ?? ""} onChange={(e) => setCfg({ ...cfg, [mk]: e.target.value })} />
            </div>
            <div className="mb-2">
              <Text type="secondary" className="mb-1 block text-xs">Endpoint</Text>
              <Input value={cfg[ek] ?? ""} onChange={(e) => setCfg({ ...cfg, [ek]: e.target.value })} />
            </div>
            <div>
              <Text type="secondary" className="mb-1 block text-xs">API Key</Text>
              <Input.Password value={cfg[kk] ?? ""} onChange={(e) => setCfg({ ...cfg, [kk]: e.target.value })} />
            </div>
          </div>
        );
      })}
      {error && <Alert type="error" showIcon message={error} />}
    </Modal>
  );
}

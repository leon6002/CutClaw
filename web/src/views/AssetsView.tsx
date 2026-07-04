import { useEffect, useRef, useState } from "react";
import {
  Alert, Button, Card, Col, Collapse, Drawer, Empty, Input, Progress,
  Row, Segmented, Space, Tabs, Tag, Tooltip, Typography,
} from "antd";
import {
  CaretRightFilled, EyeOutlined, ScanOutlined, SyncOutlined,
  TagsOutlined, ThunderboltOutlined,
} from "@ant-design/icons";
import { api, mediaUrl, useJob } from "../api";
import JobLog from "../components/JobLog";
import type { ProjectState } from "../App";

const { Text, Paragraph } = Typography;

export interface Asset {
  file_path: string;
  file_name?: string;
  absolute_path?: string;
  asset_type: "video" | "image" | "audio";
  content_hash: string;
  duration_sec?: number;
  width?: number;
  height?: number;
  file_size_mb?: number;
  annotated: boolean;
  annotation?: Record<string, any>;
}

// ── helpers ────────────────────────────────────────────────────────────────

/** "00:01:23.5" | "01:23" | "83.5" | "00:01:23 - 00:01:30" → seconds of the first ts */
export function parseTs(raw: any): number | null {
  if (raw === null || raw === undefined) return null;
  let s = String(raw).trim();
  const dash = s.split(/\s*[-–~]\s*/);
  if (dash.length > 1) s = dash[0];
  if (/^[\d.]+$/.test(s)) return parseFloat(s);
  const parts = s.split(":").map((x) => parseFloat(x));
  if (parts.some((x) => isNaN(x))) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return null;
}

function qColor(q: number): string {
  if (q >= 8) return "green";
  if (q >= 6) return "gold";
  if (q >= 4) return "orange";
  return "red";
}

const FIELD_LABELS: Record<string, string> = {
  summary: "摘要", emotion: "情绪", tags: "标签", visual_tags: "视觉标签",
  scene_types: "场景类型", camera_movement: "运镜", time_of_day: "时间段",
  key_colors: "主色调", suggested_use: "建议用途", has_people: "有人物",
  genre: "曲风", energy_level: "能量", bpm: "BPM", quality_score: "质量分",
  mood: "氛围", instruments: "乐器", vocals: "人声", tempo: "节奏",
  description: "描述", subject: "主体", style: "风格",
};

function fmtVal(v: any): string {
  if (v === null || v === undefined || v === "") return "—";
  if (Array.isArray(v)) return v.map(String).join("、") || "—";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(1);
  if (typeof v === "object") return JSON.stringify(v, null, 1);
  return String(v);
}

/** Render EVERY annotation field — known ones with Chinese labels first, unknown ones after. */
function AnnotationTable({ ann }: { ann: Record<string, any> }) {
  const entries = Object.entries(ann).filter(([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && v.length === 0));
  const known = entries.filter(([k]) => FIELD_LABELS[k]);
  const unknown = entries.filter(([k]) => !FIELD_LABELS[k]);
  const rows = [...known, ...unknown];
  if (rows.length === 0) return <Empty description="没有标注字段" />;
  return (
    <table className="kv-table">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <td className="kv-key">{FIELD_LABELS[k] ?? k}</td>
            <td className="kv-val">{fmtVal(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── per-clip detail (the annotation inspector core) ───────────────────────

function SeekBtn({ label, sec, onSeek }: { label: string; sec: number | null; onSeek: (s: number) => void }) {
  if (sec === null) return <Text type="secondary">{label}</Text>;
  return (
    <Tooltip title="跳转播放器到此时间点">
      <Button
        size="small" icon={<CaretRightFilled />}
        style={{ fontFamily: "monospace", fontSize: 12 }}
        onClick={() => onSeek(sec)}
      >
        {label}
      </Button>
    </Tooltip>
  );
}

function DenseSegment({ seg, onSeek }: { seg: any; onSeek: (s: number) => void }) {
  const ts = seg.timestamp ?? "";
  const tsAbs = seg.timestamp_absolute ?? "";
  const sec = parseTs(tsAbs || ts);
  const vq = typeof seg.visual_quality === "object" ? seg.visual_quality?.score : seg.visual_quality;
  const emo = typeof seg.emotion === "object" ? seg.emotion?.mood : seg.emotion;
  return (
    <div className="dense-seg">
      <Space size={6} wrap>
        <SeekBtn label={String(tsAbs || ts)} sec={sec} onSeek={onSeek} />
        {vq !== undefined && vq !== null && <Tag color={qColor(Number(vq))}>画质 {vq}</Tag>}
        {emo && <Tag color="purple">🎭 {emo}</Tag>}
      </Space>
      {seg.content_description && <Paragraph style={{ margin: "4px 0 0" }}>{seg.content_description}</Paragraph>}
      {seg.editor_recommendation && (
        <Paragraph type="secondary" style={{ margin: "2px 0 0", fontSize: 12 }}>💡 {seg.editor_recommendation}</Paragraph>
      )}
      {/* any extra fields the VLM produced */}
      {Object.entries(seg)
        .filter(([k]) => !["timestamp", "timestamp_absolute", "content_description", "visual_quality", "emotion", "editor_recommendation"].includes(k))
        .map(([k, v]) => (
          <Paragraph key={k} type="secondary" style={{ margin: "2px 0 0", fontSize: 12 }}>
            {k}: {fmtVal(v)}
          </Paragraph>
        ))}
    </div>
  );
}

function ClipPanel({ clip, idx, onSeek }: { clip: any; idx: number; onSeek: (s: number) => void }) {
  const dur = clip.duration ?? {};
  const start = dur.clip_start_time ?? clip.start_time ?? "?";
  const end = dur.clip_end_time ?? clip.end_time ?? "?";
  const startSec = parseTs(start);
  const action = clip.action_atoms ?? {};
  const narrative = clip.narrative_analysis ?? {};
  const cine = clip.cinematography ?? {};
  const dense: any[] = Array.isArray(clip.dense_segments) ? clip.dense_segments : [];

  return (
    <div>
      <Space size={6} wrap style={{ marginBottom: 6 }}>
        <SeekBtn label={`${start} → ${end}`} sec={startSec} onSeek={onSeek} />
        {cine.shot_scale && <Tag>{cine.shot_scale}</Tag>}
        {cine.camera_movement && <Tag>📷 {cine.camera_movement}</Tag>}
        {narrative.mood && <Tag color="purple">🎭 {narrative.mood}</Tag>}
      </Space>
      {action.event_summary && <Paragraph style={{ margin: "0 0 6px" }}>{action.event_summary}</Paragraph>}
      {narrative.narrative_role && (
        <Paragraph type="secondary" style={{ margin: "0 0 6px", fontSize: 12 }}>叙事角色：{narrative.narrative_role}</Paragraph>
      )}
      {dense.length > 0 && (
        <>
          <Text type="secondary" style={{ fontSize: 12 }}>⏱️ {dense.length} 个时间片段：</Text>
          {dense.map((seg, i) => <DenseSegment key={i} seg={seg} onSeek={onSeek} />)}
        </>
      )}
      <Collapse
        ghost size="small" style={{ marginTop: 6 }}
        items={[{ key: "raw", label: <Text type="secondary" style={{ fontSize: 12 }}>原始 JSON</Text>, children: <pre className="rawjson">{JSON.stringify(clip, null, 2)}</pre> }]}
      />
    </div>
  );
}

// ── detail drawer ───────────────────────────────────────────────────────────

function DetailDrawer({
  asset, onClose, onReannotate, busy,
}: { asset: Asset | null; onClose: () => void; onReannotate: (a: Asset) => void; busy: boolean }) {
  const [details, setDetails] = useState<{ clips: any[]; scenes: any[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    setDetails(null);
    if (!asset || asset.asset_type === "image") return;
    setLoading(true);
    api<any>(`/api/assets/${asset.content_hash}/details`)
      .then(setDetails).catch(() => {}).finally(() => setLoading(false));
  }, [asset?.content_hash]);

  if (!asset) return null;
  const src = mediaUrl(asset.absolute_path || asset.file_path);
  const seek = (s: number) => {
    const v = videoRef.current;
    if (v) { v.currentTime = Math.max(0, s); v.play().catch(() => {}); }
  };
  const clips = details?.clips ?? [];
  const scenes = details?.scenes ?? [];
  const ann = asset.annotation ?? {};

  return (
    <Drawer
      open onClose={onClose} width={Math.min(920, window.innerWidth - 40)}
      title={<Space><span>{asset.file_name || asset.file_path}</span>
        {asset.annotated
          ? <Tag color={qColor(Number(ann.quality_score ?? 0))}>Q {fmtVal(ann.quality_score)}</Tag>
          : <Tag color="gold">未标注</Tag>}
      </Space>}
      extra={<Button size="small" icon={<SyncOutlined />} loading={busy} onClick={() => onReannotate(asset)}>重新标注</Button>}
    >
      {/* sticky player so you can verify annotations against footage */}
      <div className="drawer-player">
        {asset.asset_type === "video" && <video ref={videoRef} src={src} controls style={{ width: "100%", maxHeight: 340, background: "#000", borderRadius: 8 }} />}
        {asset.asset_type === "image" && <img src={src} style={{ width: "100%", maxHeight: 340, objectFit: "contain", background: "#000", borderRadius: 8 }} />}
        {asset.asset_type === "audio" && <audio src={src} controls style={{ width: "100%" }} />}
        <Text type="secondary" style={{ fontSize: 12 }}>
          {asset.duration_sec ? `${Math.round(asset.duration_sec)}s · ` : ""}
          {asset.width ? `${asset.width}×${asset.height} · ` : ""}
          {asset.file_size_mb ? `${asset.file_size_mb.toFixed(1)}MB · ` : ""}
          {asset.absolute_path || asset.file_path}
        </Text>
      </div>

      <Tabs
        defaultActiveKey={asset.asset_type === "video" ? "clips" : "overview"}
        items={[
          {
            key: "overview", label: `📋 标注总览`,
            children: asset.annotated ? <AnnotationTable ann={ann} /> : <Empty description="尚未标注 — 点击右上角「重新标注」" />,
          },
          ...(asset.asset_type === "video" ? [
            {
              key: "clips", label: `🎬 片段分析 (${clips.length})`,
              children: loading ? <Text type="secondary">加载中…</Text> :
                clips.length === 0 ? <Empty description="没有检测到片段 — VLM 可能超时，试试重新标注" /> :
                <Collapse
                  defaultActiveKey={clips.map((_, i) => String(i))}
                  items={clips.map((clip, i) => {
                    const d = clip.duration ?? {};
                    return {
                      key: String(i),
                      label: <Text strong>Clip {i + 1}　<Text type="secondary" style={{ fontFamily: "monospace", fontSize: 12 }}>{d.clip_start_time ?? "?"} → {d.clip_end_time ?? "?"}</Text></Text>,
                      children: <ClipPanel clip={clip} idx={i} onSeek={seek} />,
                    };
                  })}
                />,
            },
            {
              key: "scenes", label: `🎞️ 场景 (${scenes.length})`,
              children: scenes.length === 0 ? <Empty description="没有场景分析" /> :
                <div>
                  {scenes.map((scene, i) => {
                    const va = scene.video_analysis?.scene_caption ?? {};
                    const sc = va.scene_summary ?? va.visual_analysis ?? {};
                    const summary = typeof sc === "object" ? (sc.summary ?? sc.narrative ?? "") : String(sc);
                    const cls = va.scene_classification ?? {};
                    const tr = scene.time_range ?? {};
                    const sec = parseTs(tr.start_seconds);
                    return (
                      <Card size="small" key={i} style={{ marginBottom: 10 }}>
                        <Space size={6} wrap>
                          <Text strong>Scene {i + 1}</Text>
                          <SeekBtn label={`${tr.start_seconds ?? "?"}s → ${tr.end_seconds ?? "?"}s`} sec={sec} onSeek={seek} />
                          {cls.is_usable !== undefined && <Tag color={cls.is_usable ? "green" : "red"}>{cls.is_usable ? "可用" : "不可用"}</Tag>}
                          {cls.importance_score !== undefined && <Tag color={qColor(Number(cls.importance_score))}>重要度 {cls.importance_score}</Tag>}
                        </Space>
                        {summary && <Paragraph style={{ margin: "6px 0 0" }}>{String(summary)}</Paragraph>}
                        <Collapse ghost size="small" style={{ marginTop: 4 }}
                          items={[{ key: "raw", label: <Text type="secondary" style={{ fontSize: 12 }}>原始 JSON</Text>, children: <pre className="rawjson">{JSON.stringify(scene, null, 2)}</pre> }]} />
                      </Card>
                    );
                  })}
                </div>,
            },
          ] : []),
          {
            key: "raw", label: "🧾 原始标注",
            children: <pre className="rawjson">{JSON.stringify(ann, null, 2)}</pre>,
          },
        ]}
      />
    </Drawer>
  );
}

// ── asset card ──────────────────────────────────────────────────────────────

function AssetCard({ a, onOpen }: { a: Asset; onOpen: () => void }) {
  const ann = a.annotation ?? {};
  const src = mediaUrl(a.absolute_path || a.file_path);
  const tags: string[] = [
    ...(Array.isArray(ann.tags) ? ann.tags : []),
    ...(Array.isArray(ann.visual_tags) ? ann.visual_tags : []),
  ].slice(0, 4);

  return (
    <Card
      hoverable size="small" onClick={onOpen}
      cover={
        <div className="card-media" onClick={(e) => e.stopPropagation()}>
          {a.asset_type === "video" && <video src={src} controls preload="metadata" />}
          {a.asset_type === "image" && <img src={src} loading="lazy" />}
          {a.asset_type === "audio" && <div className="audio-wrap"><span style={{ fontSize: 34 }}>🎵</span><audio src={src} controls preload="none" /></div>}
        </div>
      }
    >
      <Card.Meta
        title={
          <Tooltip title={a.absolute_path || a.file_path}>
            <span className="text-[13px]">{a.file_name || a.file_path}</span>
          </Tooltip>
        }
        description={
          <div>
            <Text type="secondary" className="text-[11.5px]">
              {a.duration_sec ? `${Math.round(a.duration_sec)}s · ` : ""}
              {a.width ? `${a.width}×${a.height} · ` : ""}
              {a.file_size_mb ? `${a.file_size_mb.toFixed(1)}MB` : ""}
            </Text>
            <div className="mt-1.5 flex flex-wrap gap-y-1">
              {a.annotated
                ? <Tag color={qColor(Number(ann.quality_score ?? 0))}>Q {fmtVal(ann.quality_score)}</Tag>
                : <Tag color="gold">未标注</Tag>}
              {tags.map((t, i) => <Tag key={i}>{t}</Tag>)}
            </div>
            {ann.summary && (
              <Paragraph type="secondary" ellipsis={{ rows: 2 }} className="!mt-1.5 !mb-0 text-xs">
                {ann.summary}
              </Paragraph>
            )}
          </div>
        }
      />
      <Button block ghost type="primary" size="small" icon={<EyeOutlined />}
        className="!mt-3" onClick={onOpen}>
        查看完整标注
      </Button>
    </Card>
  );
}

// ── main view ───────────────────────────────────────────────────────────────

export default function AssetsView({
  project, setProject,
}: { project: ProjectState; setProject: (fn: (p: ProjectState) => ProjectState) => void }) {
  const [root, setRoot] = useState("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [scanned, setScanned] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState<any>(null);
  const [annJobId, setAnnJobId] = useState<string | null>(null);
  const annJob = useJob(annJobId);
  const [typeTab, setTypeTab] = useState<string>("video");
  const [query, setQuery] = useState("");
  const [detail, setDetail] = useState<Asset | null>(null);

  const busy = annJob.status === "running";

  const scan = async () => {
    setError(""); setScanning(true);
    try {
      const r = await api<{ root: string; assets: Asset[] }>("/api/assets/scan", {
        method: "POST", body: JSON.stringify({ root }),
      });
      setAssets(r.assets); setScanned(true);
      if (!root) setRoot(r.root);
      // keep detail drawer in sync after re-annotation
      setDetail((d) => d ? r.assets.find((a) => a.content_hash === d.content_hash) ?? d : null);
    } catch (e: any) { setError(e.message); }
    setScanning(false);
  };

  const annotate = async (hashes: string[] = [], force = false) => {
    setError("");
    try {
      const r = await api<{ job_id: string | null; message?: string }>("/api/assets/annotate", {
        method: "POST", body: JSON.stringify({ content_hashes: hashes, force }),
      });
      if (r.job_id) setAnnJobId(r.job_id);
      else setError(r.message || "没有需要标注的素材。");
    } catch (e: any) { setError(e.message); }
  };

  const autoSelect = async () => {
    setError(""); setSelecting(true);
    try {
      const r = await api<any>("/api/assets/auto-select", {
        method: "POST", body: JSON.stringify({ instruction: project.instruction }),
      });
      setSelection(r.selection);
      setProject((p) => ({ ...p, videos: r.videos, audio: r.audio || p.audio }));
    } catch (e: any) { setError(e.message); }
    setSelecting(false);
  };

  useEffect(() => {
    if (annJob.status === "done" && annJobId) {
      setAnnJobId(null);
      scan();
    }
  }, [annJob.status]);

  const byType = (t: string) => assets.filter((a) => a.asset_type === t);
  const newCount = assets.filter((a) => !a.annotated).length;
  const q = query.trim().toLowerCase();
  const shown = byType(typeTab)
    .filter((a) => !q ||
      (a.file_name || a.file_path).toLowerCase().includes(q) ||
      JSON.stringify(a.annotation ?? {}).toLowerCase().includes(q))
    .sort((a, b) => (b.annotation?.quality_score ?? -1) - (a.annotation?.quality_score ?? -1));

  return (
    <div>
      <Card size="small">
        <Space wrap style={{ width: "100%" }}>
          <Input
            style={{ width: 340 }} placeholder="素材文件夹（默认 resource/imports/）"
            value={root} onChange={(e) => setRoot(e.target.value)} onPressEnter={scan}
          />
          <Button icon={<ScanOutlined />} onClick={scan} loading={scanning}>扫描</Button>
          <Button icon={<TagsOutlined />} onClick={() => annotate()} disabled={!scanned || newCount === 0} loading={busy}>
            全部标注{newCount > 0 ? ` (${newCount})` : ""}
          </Button>
          <Button type="primary" icon={<ThunderboltOutlined />} onClick={autoSelect} loading={selecting}
            disabled={assets.every((a) => !a.annotated)}>
            智能选材
          </Button>
        </Space>

        {error && <Alert type="error" showIcon message={error} style={{ marginTop: 10 }} />}

        {busy && (
          <div style={{ marginTop: 12 }}>
            <Progress
              percent={Math.round(((annJob.meta.current ?? 0) / Math.max(annJob.meta.total ?? 1, 1)) * 100)}
              status="active"
              format={() => `${annJob.meta.current ?? 0}/${annJob.meta.total ?? "?"}`}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>📹 {annJob.meta.filename || "…"}</Text>
            <JobLog lines={annJob.lines.slice(-80)} height={180} />
          </div>
        )}
        {annJob.status === "error" && (
          <div style={{ marginTop: 10 }}>
            <Alert type="error" showIcon message="标注失败 — 查看日志" />
            <JobLog lines={annJob.lines.slice(-40)} height={180} />
          </div>
        )}

        {selection && (
          <Alert
            type="success" showIcon style={{ marginTop: 10 }}
            message={
              <>
                <b>已选素材：</b>{" "}
                {(selection.selected_videos ?? []).map((v: string) => `📹${v.split(/[\\/]/).pop()}`).join("  ")}{" "}
                {(selection.selected_images ?? []).map((v: string) => `🖼️${v.split(/[\\/]/).pop()}`).join("  ")}{" "}
                {(selection.selected_audio ?? []).slice(0, 1).map((v: string) => `🎵${v.split(/[\\/]/).pop()}`).join("")}
              </>
            }
            description={
              <>
                {selection.rationale && <div>💡 {selection.rationale}</div>}
                <Text type="secondary">已写入项目 — 切换到「✂️ 项目编辑」运行流水线。</Text>
              </>
            }
          />
        )}
      </Card>

      {scanned ? (
        <>
          <Space style={{ margin: "16px 0 12px" }} wrap>
            <Segmented
              value={typeTab}
              onChange={(v) => setTypeTab(String(v))}
              options={[
                { label: `📹 视频 (${byType("video").length})`, value: "video" },
                { label: `🖼️ 图片 (${byType("image").length})`, value: "image" },
                { label: `🎵 音频 (${byType("audio").length})`, value: "audio" },
              ]}
            />
            <Input.Search allowClear placeholder="搜索文件名 / 标注内容…" style={{ width: 260 }}
              onSearch={setQuery} onChange={(e) => !e.target.value && setQuery("")} />
            <Text type="secondary">
              共 {assets.length} 个素材 · {assets.length - newCount} 已标注 · {newCount} 新
            </Text>
          </Space>

          {shown.length === 0 ? (
            <Empty description="该类型下没有素材" />
          ) : (
            <Row gutter={[14, 14]}>
              {shown.map((a) => (
                <Col key={a.content_hash} xs={24} sm={12} md={8} lg={6}>
                  <AssetCard a={a} onOpen={() => setDetail(a)} />
                </Col>
              ))}
            </Row>
          )}
        </>
      ) : (
        <Empty style={{ marginTop: 40 }} description="点击「扫描」发现素材文件" />
      )}

      <DetailDrawer
        asset={detail} busy={busy}
        onClose={() => setDetail(null)}
        onReannotate={(a) => annotate([a.content_hash], a.annotated)}
      />
    </div>
  );
}

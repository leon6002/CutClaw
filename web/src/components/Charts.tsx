/** ECharts-based animated visualizations (canvas), dark-themed to match the app. */
import { useEffect, useMemo, useState } from "react";
import ReactECharts from "echarts-for-react";
import { Empty, Typography } from "antd";
import { api } from "../api";

const { Text } = Typography;

// ── shared theme bits ────────────────────────────────────────────────────────
const AXIS = { color: "#9aa3b2", fontSize: 11 };
const SPLIT = { lineStyle: { color: "#232733" } };
const TOOLTIP = {
  backgroundColor: "#1d212a", borderColor: "#384050",
  textStyle: { color: "#e6e9ef", fontSize: 12 },
};
const PALETTE = ["#ff6b4a", "#60a5fa", "#4ade80", "#fbbf24", "#c084fc", "#22d3ee", "#f87171", "#a3e635"];

export function Chart({ option, height = 220, onEvents }: {
  option: any; height?: number; onEvents?: Record<string, (p: any) => void>;
}) {
  return (
    <ReactECharts
      option={{ backgroundColor: "transparent", color: PALETTE, ...option }}
      style={{ height, width: "100%" }}
      notMerge lazyUpdate
      opts={{ renderer: "canvas" }}
      onEvents={onEvents}
    />
  );
}

// local copy (avoid circular import with AssetsView)
function ts2sec(raw: any): number | null {
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

const basename = (p: string) => p.split(/[\\/]/).pop() || p;

// ── 素材库统计仪表盘 ─────────────────────────────────────────────────────────

export function AssetStatsCharts({ assets }: { assets: any[] }) {
  const typeCounts = useMemo(() => {
    const m: Record<string, number> = {};
    assets.forEach((a) => { m[a.asset_type] = (m[a.asset_type] ?? 0) + 1; });
    const label: Record<string, string> = { video: "视频", image: "图片", audio: "音频" };
    return Object.entries(m).map(([k, v]) => ({ name: label[k] ?? k, value: v }));
  }, [assets]);

  const qualityBuckets = useMemo(() => {
    const buckets = new Array(10).fill(0);
    assets.forEach((a) => {
      const q = a.annotation?.quality_score;
      if (typeof q === "number") buckets[Math.min(9, Math.max(0, Math.floor(q)))]++;
    });
    return buckets;
  }, [assets]);

  const scatterData = useMemo(() =>
    assets
      .filter((a) => a.asset_type === "video" && a.annotation?.quality_score !== undefined)
      .map((a) => ({
        value: [a.duration_sec ?? 0, a.annotation.quality_score, Math.max(6, Math.sqrt(a.file_size_mb ?? 1) * 4)],
        name: a.file_name ?? a.file_path,
      })), [assets]);

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
      <div>
        <Text type="secondary" className="text-xs">素材构成</Text>
        <Chart height={200} option={{
          tooltip: { ...TOOLTIP, trigger: "item" },
          series: [{
            type: "pie", radius: ["45%", "72%"], center: ["50%", "52%"],
            itemStyle: { borderColor: "#171a21", borderWidth: 3, borderRadius: 6 },
            label: { color: "#9aa3b2", fontSize: 11, formatter: "{b}\n{c}" },
            data: typeCounts,
            animationType: "scale", animationEasing: "elasticOut",
          }],
        }} />
      </div>
      <div>
        <Text type="secondary" className="text-xs">质量分布（已标注）</Text>
        <Chart height={200} option={{
          tooltip: { ...TOOLTIP },
          grid: { left: 34, right: 10, top: 16, bottom: 24 },
          xAxis: { type: "category", data: qualityBuckets.map((_, i) => `${i}`), axisLabel: AXIS, axisLine: { lineStyle: { color: "#384050" } } },
          yAxis: { type: "value", axisLabel: AXIS, splitLine: SPLIT, minInterval: 1 },
          series: [{
            type: "bar", data: qualityBuckets, barWidth: "62%",
            itemStyle: {
              borderRadius: [4, 4, 0, 0],
              color: (p: any) => p.dataIndex >= 8 ? "#4ade80" : p.dataIndex >= 6 ? "#fbbf24" : p.dataIndex >= 4 ? "#ff6b4a" : "#f87171",
            },
            animationDelay: (i: number) => i * 40,
          }],
        }} />
      </div>
      <div>
        <Text type="secondary" className="text-xs">时长 × 质量（气泡 = 文件大小）</Text>
        <Chart height={200} option={{
          tooltip: { ...TOOLTIP, formatter: (p: any) => `${p.name}<br/>时长 ${p.value[0].toFixed(0)}s · 质量 ${p.value[1]}` },
          grid: { left: 34, right: 14, top: 16, bottom: 24 },
          xAxis: { type: "value", name: "s", axisLabel: AXIS, splitLine: SPLIT },
          yAxis: { type: "value", min: 0, max: 10, axisLabel: AXIS, splitLine: SPLIT },
          series: [{
            type: "scatter", data: scatterData,
            symbolSize: (v: number[]) => v[2],
            itemStyle: { color: "#ff6b4a", opacity: 0.75, shadowBlur: 8, shadowColor: "#ff6b4a55" },
          }],
        }} />
      </div>
    </div>
  );
}

// ── 视频画质曲线（标注详情抽屉）────────────────────────────────────────────

export function QualityCurve({ clips, onSeek }: { clips: any[]; onSeek?: (s: number) => void }) {
  const points = useMemo(() => {
    const pts: { t: number; q: number; desc: string }[] = [];
    for (const clip of clips) {
      for (const seg of (Array.isArray(clip.dense_segments) ? clip.dense_segments : [])) {
        const t = ts2sec(seg.timestamp_absolute || seg.timestamp);
        const vq = typeof seg.visual_quality === "object" ? seg.visual_quality?.score : seg.visual_quality;
        if (t !== null && vq !== undefined && vq !== null) {
          pts.push({ t, q: Number(vq), desc: seg.content_description ?? "" });
        }
      }
    }
    return pts.sort((a, b) => a.t - b.t);
  }, [clips]);

  if (points.length < 2) return null;

  return (
    <div className="mb-3">
      <Text type="secondary" className="text-xs">画质随时间变化（点击数据点跳转播放）</Text>
      <Chart
        height={180}
        onEvents={onSeek ? { click: (p: any) => onSeek(p.value?.[0] ?? 0) } : undefined}
        option={{
          tooltip: {
            ...TOOLTIP, trigger: "axis",
            formatter: (ps: any[]) => {
              const p = ps[0];
              return `${p.value[0].toFixed(1)}s · 画质 ${p.value[1]}<br/><span style="font-size:11px;color:#9aa3b2">${(p.data.desc ?? "").slice(0, 80)}</span>`;
            },
          },
          grid: { left: 30, right: 14, top: 14, bottom: 24 },
          xAxis: { type: "value", name: "s", axisLabel: AXIS, splitLine: { show: false } },
          yAxis: { type: "value", min: 0, max: 10, axisLabel: AXIS, splitLine: SPLIT },
          series: [{
            type: "line", smooth: true, symbolSize: 7,
            data: points.map((p) => ({ value: [p.t, p.q], desc: p.desc })),
            lineStyle: { color: "#ff6b4a", width: 2 },
            itemStyle: { color: "#ff6b4a" },
            areaStyle: {
              color: {
                type: "linear", x: 0, y: 0, x2: 0, y2: 1,
                colorStops: [{ offset: 0, color: "#ff6b4a44" }, { offset: 1, color: "#ff6b4a00" }],
              },
            },
            markLine: {
              silent: true, symbol: "none",
              lineStyle: { color: "#4ade8088", type: "dashed" },
              label: { color: "#4ade80", fontSize: 10, formatter: "均值 {c}" },
              data: [{ type: "average" }],
            },
          }],
        }}
      />
    </div>
  );
}

// ── 音频节奏关键点（音频详情抽屉）────────────────────────────────────────────

const METHOD_LABEL: Record<string, string> = { downbeat: "🥁 节拍", pitch: "🎼 音高", mel: "⚡ 能量", mel_energy: "⚡ 能量" };

export function AudioKeypointsChart({ path, duration, onSeek }: {
  path: string; duration?: number; onSeek?: (s: number) => void;
}) {
  const [data, setData] = useState<Record<string, any[]> | null>(null);

  useEffect(() => {
    setData(null);
    api<Record<string, any[]>>(`/api/audio/keypoints?path=${encodeURIComponent(path)}`)
      .then(setData).catch(() => setData({}));
  }, [path]);

  const series = useMemo(() => {
    if (!data) return [];
    return Object.entries(data).map(([method, kps], i) => ({
      name: METHOD_LABEL[method] ?? method,
      type: "scatter" as const,
      symbolSize: (v: number[]) => 4 + (v[1] ?? 0.5) * 10,
      itemStyle: { color: PALETTE[i % PALETTE.length], opacity: 0.8 },
      data: (Array.isArray(kps) ? kps : []).map((k: any) => {
        const t = typeof k === "number" ? k : (k.time ?? k.timestamp ?? k.t ?? ts2sec(k.time_str) ?? 0);
        const inten = typeof k === "object" ? (k.intensity ?? k.strength ?? k.energy ?? k.confidence ?? 0.5) : 0.5;
        return [Number(t), Math.min(1, Number(inten))];
      }),
    }));
  }, [data]);

  if (data === null) return <Text type="secondary">加载节奏关键点…</Text>;
  if (series.length === 0 || series.every((s) => s.data.length === 0)) {
    return <Empty description="还没有节奏分析数据 — 标注或运行流水线后生成" />;
  }

  return (
    <div>
      <Text type="secondary" className="text-xs">Madmom 节奏关键点（横轴时间，纵轴/大小 = 强度，点击跳转播放）</Text>
      <Chart
        height={240}
        onEvents={onSeek ? { click: (p: any) => onSeek(p.value?.[0] ?? 0) } : undefined}
        option={{
          tooltip: { ...TOOLTIP, formatter: (p: any) => `${p.seriesName}<br/>${p.value[0].toFixed(2)}s · 强度 ${p.value[1].toFixed(2)}` },
          legend: { textStyle: { color: "#9aa3b2", fontSize: 11 }, top: 0 },
          grid: { left: 34, right: 14, top: 30, bottom: 24 },
          xAxis: { type: "value", name: "s", max: duration, axisLabel: AXIS, splitLine: SPLIT },
          yAxis: { type: "value", min: 0, max: 1, axisLabel: AXIS, splitLine: SPLIT },
          series,
        }}
      />
    </div>
  );
}

// ── 成片时间轴（渲染页）──────────────────────────────────────────────────────

interface TimelineClip { start: number; end: number; src: string; desc: string }

function extractClips(data: any): TimelineClip[] {
  let arr: any[] =
    Array.isArray(data) ? data
    : data?.shots ?? data?.clips ?? data?.shot_points ?? data?.results ?? data?.segments ?? [];
  if (!Array.isArray(arr) && typeof arr === "object") arr = Object.values(arr ?? {});
  if (!Array.isArray(arr)) return [];
  const num = (v: any): number => (typeof v === "number" ? v : ts2sec(v) ?? NaN);
  return arr
    .map((c: any) => ({
      start: num(c.local_start ?? c.start_time ?? c.clip_start_time ?? c.start ?? c.Start_Time),
      end: num(c.local_end ?? c.end_time ?? c.clip_end_time ?? c.end ?? c.End_Time),
      src: String(c.video_path ?? c.source_video_path ?? c.video ?? c.source ?? ""),
      desc: String(c.description ?? c.caption ?? c.shot_description ?? c.content ?? c.summary ?? c.name ?? ""),
    }))
    .filter((c) => isFinite(c.start) && isFinite(c.end) && c.end > c.start);
}

export function ShotTimeline({ shotPoint }: { shotPoint: string }) {
  const [clips, setClips] = useState<TimelineClip[] | null>(null);

  useEffect(() => {
    setClips(null);
    if (!shotPoint) return;
    api<any>(`/api/json?path=${encodeURIComponent(shotPoint)}`)
      .then((d) => setClips(extractClips(d)))
      .catch(() => setClips([]));
  }, [shotPoint]);

  const { items, sources, total } = useMemo(() => {
    const cs = clips ?? [];
    const srcs = Array.from(new Set(cs.map((c) => basename(c.src) || "(未知来源)")));
    let cursor = 0;
    const its = cs.map((c, i) => {
      const dur = c.end - c.start;
      const item = {
        value: [cursor, cursor + dur, srcs.indexOf(basename(c.src) || "(未知来源)"), dur],
        clip: c, idx: i,
      };
      cursor += dur;
      return item;
    });
    return { items: its, sources: srcs, total: cursor };
  }, [clips]);

  if (!shotPoint) return null;
  if (clips === null) return <Text type="secondary">加载成片时间轴…</Text>;
  if (clips.length === 0) return <Empty description="无法解析 shot_point 数据" />;

  return (
    <div>
      <Text type="secondary" className="text-xs">
        成片时间轴 — {clips.length} 个镜头 · 总时长 {total.toFixed(1)}s · 颜色 = 来源视频
      </Text>
      <Chart
        height={Math.max(140, sources.length * 44 + 70)}
        option={{
          tooltip: {
            ...TOOLTIP,
            formatter: (p: any) => {
              const c: TimelineClip = p.data.clip;
              return `<b>镜头 ${p.data.idx + 1}</b> · ${p.value[3].toFixed(1)}s<br/>`
                + `${basename(c.src)} [${c.start.toFixed(1)}s → ${c.end.toFixed(1)}s]`
                + (c.desc ? `<br/><span style="font-size:11px;color:#9aa3b2">${c.desc.slice(0, 100)}</span>` : "");
            },
          },
          grid: { left: 110, right: 16, top: 10, bottom: 26 },
          xAxis: { type: "value", name: "s", max: Math.ceil(total), axisLabel: AXIS, splitLine: SPLIT },
          yAxis: {
            type: "category", data: sources.map(basename),
            axisLabel: { ...AXIS, width: 96, overflow: "truncate" as const },
            axisLine: { lineStyle: { color: "#384050" } },
          },
          series: [{
            type: "custom",
            renderItem: (params: any, apiE: any) => {
              const start = apiE.coord([apiE.value(0), apiE.value(2)]);
              const end = apiE.coord([apiE.value(1), apiE.value(2)]);
              const h = 22;
              return {
                type: "rect",
                shape: { x: start[0], y: start[1] - h / 2, width: Math.max(2, end[0] - start[0] - 1.5), height: h, r: 4 },
                style: {
                  fill: PALETTE[(apiE.value(2) as number) % PALETTE.length],
                  opacity: 0.9,
                },
                emphasis: { style: { opacity: 1, shadowBlur: 10, shadowColor: "#000" } },
              };
            },
            encode: { x: [0, 1], y: 2 },
            data: items,
            animationDelay: (i: number) => i * 30,
          }],
        }}
      />
    </div>
  );
}

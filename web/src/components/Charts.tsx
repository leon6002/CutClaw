/** ECharts-based visualizations (canvas), themed for the dark neon UI. */
import { useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { api } from "@/api";

const AXIS = { color: "#94a3b8", fontSize: 11 };
const SPLIT = { lineStyle: { color: "rgba(255,255,255,0.07)" } };
const TOOLTIP = {
  backgroundColor: "rgba(15,23,42,0.95)", borderColor: "rgba(255,255,255,0.15)",
  textStyle: { color: "#e2e8f0", fontSize: 12 },
};
const PALETTE = ["#22d3ee", "#34d399", "#fbbf24", "#c084fc", "#60a5fa", "#f87171", "#a3e635", "#fb923c"];

function Hint({ children }: { children: React.ReactNode }) {
  return <div className="py-6 text-center text-xs text-slate-500">{children}</div>;
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1 text-xs text-slate-400">{children}</div>;
}

export function Chart({ option, height = 220, onEvents }: {
  option: any; height?: number; onEvents?: Record<string, (p: any) => void>;
}) {
  const ref = useRef<ReactECharts>(null);

  // echarts-for-react only listens to WINDOW resize. When the chart mounts
  // before its flex container settles (e.g. while a sibling <video> is still
  // loading), the canvas is initialized at the tiny provisional width and
  // stays collapsed. Observe the actual container and resize the instance.
  useEffect(() => {
    const el = (ref.current as any)?.ele as HTMLElement | undefined;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      try { ref.current?.getEchartsInstance()?.resize(); } catch { /* disposed */ }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <ReactECharts
      ref={ref}
      option={{ backgroundColor: "transparent", color: PALETTE, ...option }}
      style={{ height, width: "100%" }}
      notMerge lazyUpdate
      opts={{ renderer: "canvas" }}
      onEvents={onEvents}
    />
  );
}

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
      <Label>画质随时间变化（点击数据点跳转播放）</Label>
      <Chart
        height={180}
        onEvents={onSeek ? { click: (p: any) => onSeek(p.value?.[0] ?? 0) } : undefined}
        option={{
          tooltip: {
            ...TOOLTIP, trigger: "axis",
            formatter: (ps: any[]) => {
              const p = ps[0];
              return `${p.value[0].toFixed(1)}s · 画质 ${p.value[1]}<br/><span style="font-size:11px;color:#94a3b8">${(p.data.desc ?? "").slice(0, 80)}</span>`;
            },
          },
          grid: { left: 30, right: 14, top: 14, bottom: 24 },
          xAxis: { type: "value", name: "s", axisLabel: AXIS, splitLine: { show: false } },
          yAxis: { type: "value", min: 0, max: 10, axisLabel: AXIS, splitLine: SPLIT },
          series: [{
            type: "line", smooth: true, symbolSize: 7,
            data: points.map((p) => ({ value: [p.t, p.q], desc: p.desc })),
            lineStyle: { color: "#22d3ee", width: 2, shadowBlur: 8, shadowColor: "rgba(34,211,238,0.4)" },
            itemStyle: { color: "#22d3ee" },
            areaStyle: {
              color: {
                type: "linear", x: 0, y: 0, x2: 0, y2: 1,
                colorStops: [{ offset: 0, color: "rgba(34,211,238,0.28)" }, { offset: 1, color: "rgba(34,211,238,0)" }],
              },
            },
            markLine: {
              silent: true, symbol: "none",
              lineStyle: { color: "rgba(52,211,153,0.55)", type: "dashed" },
              label: { color: "#34d399", fontSize: 10, formatter: "均值 {c}" },
              data: [{ type: "average" }],
            },
          }],
        }}
      />
    </div>
  );
}

// ── 音频节奏关键点（音频详情抽屉）────────────────────────────────────────────

const METHOD_LABEL: Record<string, string> = { downbeat: "节拍", pitch: "音高", mel: "能量", mel_energy: "能量" };

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
      itemStyle: { color: PALETTE[i % PALETTE.length], opacity: 0.85, shadowBlur: 6, shadowColor: "rgba(34,211,238,0.25)" },
      data: (Array.isArray(kps) ? kps : []).map((k: any) => {
        const t = typeof k === "number" ? k : (k.time ?? k.timestamp ?? k.t ?? ts2sec(k.time_str) ?? 0);
        const inten = typeof k === "object" ? (k.intensity ?? k.strength ?? k.energy ?? k.confidence ?? 0.5) : 0.5;
        return [Number(t), Math.min(1, Number(inten))];
      }),
    }));
  }, [data]);

  if (data === null) return <Hint>加载节奏关键点…</Hint>;
  if (series.length === 0 || series.every((s) => s.data.length === 0)) {
    return <Hint>还没有节奏分析数据 — 标注或运行流水线后生成</Hint>;
  }

  return (
    <div>
      <Label>Madmom 节奏关键点（横轴时间，纵轴/大小 = 强度，点击跳转播放）</Label>
      <Chart
        height={240}
        onEvents={onSeek ? { click: (p: any) => onSeek(p.value?.[0] ?? 0) } : undefined}
        option={{
          tooltip: { ...TOOLTIP, formatter: (p: any) => `${p.seriesName}<br/>${p.value[0].toFixed(2)}s · 强度 ${p.value[1].toFixed(2)}` },
          legend: { textStyle: { color: "#94a3b8", fontSize: 11 }, top: 0 },
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
  // shot_point entries nest the real clips under .clips[] (multi-source format)
  arr = arr.flatMap((e: any) =>
    Array.isArray(e?.clips)
      ? e.clips.map((c: any) => ({
          ...c,
          video_path: c?.video_path ?? e?.video_path,
          description: c?.description ?? e?.description,
        }))
      : [e]);
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

export function ShotTimeline({ shotPoint, playhead = -1 }: { shotPoint: string; playhead?: number }) {
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

  // clip index the playhead is currently inside (output-timeline coordinates)
  const activeIdx = playhead >= 0
    ? items.findIndex((it) => playhead >= (it.value[0] as number) && playhead < (it.value[1] as number))
    : -1;

  if (!shotPoint) return null;
  if (clips === null) return <Hint>加载成片时间轴…</Hint>;
  if (clips.length === 0) return <Hint>无法解析 shot_point 数据</Hint>;

  return (
    <div>
      <Label>
        成片时间轴 — {clips.length} 个镜头 · 总时长 {total.toFixed(1)}s · 颜色 = 来源视频
      </Label>
      <Chart
        height={Math.max(140, sources.length * 44 + 70)}
        option={{
          tooltip: {
            ...TOOLTIP,
            formatter: (p: any) => {
              const c: TimelineClip = p.data.clip;
              return `<b>镜头 ${p.data.idx + 1}</b> · ${p.value[3].toFixed(1)}s<br/>`
                + `${basename(c.src)} [${c.start.toFixed(1)}s → ${c.end.toFixed(1)}s]`
                + (c.desc ? `<br/><span style="font-size:11px;color:#94a3b8">${c.desc.slice(0, 100)}</span>` : "");
            },
          },
          grid: { left: 110, right: 16, top: 18, bottom: 26 },
          xAxis: { type: "value", name: "s", max: Math.ceil(total), axisLabel: AXIS, splitLine: SPLIT },
          yAxis: {
            type: "category", data: sources.map(basename),
            axisLabel: { ...AXIS, width: 96, overflow: "truncate" as const },
            axisLine: { lineStyle: { color: "rgba(255,255,255,0.15)" } },
          },
          series: [{
            type: "custom",
            renderItem: (params: any, apiE: any) => {
              const start = apiE.coord([apiE.value(0), apiE.value(2)]);
              const end = apiE.coord([apiE.value(1), apiE.value(2)]);
              const h = 22;
              const isActive = params.dataIndex === activeIdx;
              return {
                type: "rect",
                shape: { x: start[0], y: start[1] - h / 2, width: Math.max(2, end[0] - start[0] - 1.5), height: h, r: 4 },
                style: {
                  fill: PALETTE[(apiE.value(2) as number) % PALETTE.length],
                  opacity: isActive ? 1 : (activeIdx >= 0 ? 0.45 : 0.9),
                  ...(isActive ? { shadowBlur: 12, shadowColor: "#22d3ee", stroke: "#22d3ee", lineWidth: 1.5 } : {}),
                },
                emphasis: { style: { opacity: 1, shadowBlur: 10, shadowColor: "#22d3ee" } },
              };
            },
            encode: { x: [0, 1], y: 2 },
            data: items,
            animation: false,
            // moving playhead cursor while the preview video plays
            markLine: playhead >= 0 && playhead <= total + 0.5 ? {
              silent: true, symbol: "none", animation: false,
              lineStyle: { color: "#22d3ee", width: 1.5 },
              label: {
                show: true, position: "end", rotate: 0, distance: 4,
                formatter: () => `${playhead.toFixed(1)}s`,
                color: "#22d3ee", fontSize: 10, fontWeight: "bold" as const,
              },
              data: [{ xAxis: Math.min(playhead, total) }],
            } : undefined,
          }],
        }}
      />
    </div>
  );
}

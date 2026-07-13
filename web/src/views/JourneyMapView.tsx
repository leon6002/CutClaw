/** 旅程地图(§22)— 独立工具页:选相簿 → 选日期 → 交互地图看轨迹、点照片。
 *  Leaflet + 高德栅格瓦片(CSS 滤镜暗色化);照片 GPS 转 GCJ-02 才能对上瓦片。
 *  标记密度自适应:每天等距挑 ≤40 张出缩略图钉,其余画成轨迹色小点。 */
import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { ExternalLink, Loader2, MapIcon, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "../api";
import { wgs2gcj } from "../lib/geo";
import { useImmichStore, type ImItem } from "../store";

const DAY_COLORS = ["#38bdf8", "#f472b6", "#34d399", "#fbbf24", "#a78bfa", "#fb7185", "#4ade80", "#60a5fa"];
const WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const dayLabel = (k: string) => {
  const d = new Date(k);
  return isNaN(d.getTime()) ? k : `${d.getMonth() + 1}.${d.getDate()} ${WEEK[d.getDay()]}`;
};

export default function JourneyMapView() {
  const { albums, itemsByAlbum, loadAlbums, loadItems } = useImmichStore();
  const [albumId, setAlbumId] = useState<string>("");
  const [selDays, setSelDays] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<ImItem | null>(null);
  const [darkMap, setDarkMap] = useState(false);   // 默认原色(压暗被用户否决)
  const [baseLayer, setBaseLayer] = useState<"vector" | "sat">("vector");
  const tilesRef = useRef<L.TileLayer[]>([]);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [autoZoom, setAutoZoom] = useState(true);   // 播放时随速度丝滑缩放
  const [playPhoto, setPlayPhoto] = useState<ImItem | null>(null);
  const speedRef = useRef(1);
  speedRef.current = speed;
  const autoZoomRef = useRef(true);
  autoZoomRef.current = autoZoom;
  const playingRef = useRef(false);
  playingRef.current = playing;
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);
  const boundsRef = useRef<L.LatLngBounds | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => { loadAlbums(); }, []);
  const items = albumId ? (itemsByAlbum[albumId]?.items ?? []) : [];
  const loading = !!albumId && !itemsByAlbum[albumId];

  // 有 GPS 的资产按天分组
  const days = useMemo(() => {
    const g = new Map<string, ImItem[]>();
    for (const m of items) {
      if (m.lat == null || m.lon == null) continue;
      const k = String(m.taken_at || "").slice(0, 10);
      if (!k) continue;
      if (!g.has(k)) g.set(k, []);
      g.get(k)!.push(m);
    }
    return [...g.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [items]);
  const noGps = items.length - days.reduce((a, [, v]) => a + v.length, 0);

  const pickAlbum = (id: string) => {
    setAlbumId(id); setSelDays(new Set()); setDetail(null); setPlaying(false);
    if (id) loadItems(id);
  };

  // 播放序列:选中天的全部资产按拍摄时间合并排序
  const playSeq = useMemo(() => {
    const chosen = days.filter(([k]) => selDays.has(k)).flatMap(([, v]) => v);
    return chosen
      .slice().sort((a, b) => String(a.taken_at).localeCompare(String(b.taken_at)))
      .map((m) => ({ m, ll: wgs2gcj(m.lat!, m.lon!) as [number, number] }));
  }, [days, selDays]);

  // 行程分段(播放/真实路线共用):停留簇(相邻 <300m)与移动腿,
  // 腿带均速 kmh(>180 判飞机)与放映采样。
  type Seg = { kind: "stop" | "leg"; i0: number; i1: number; show: number[]; kmh: number };
  const segs = useMemo<Seg[]>(() => {
    const n = playSeq.length;
    if (n < 2) return [];
    const midCos = Math.cos((playSeq[0].ll[0] * Math.PI) / 180);
    const dKm = (a: number, b: number) => {
      const [p1, p2] = [playSeq[a].ll, playSeq[b].ll];
      return Math.hypot(p1[0] - p2[0], (p1[1] - p2[1]) * midCos) * 111;
    };
    const epoch = (i: number) => Date.parse(String(playSeq[i].m.taken_at).replace(" ", "T")) || 0;
    const out: Seg[] = [];
    let i = 0;
    while (i < n - 1) {
      if (dKm(i, i + 1) < 0.3) {
        let j = i + 1;
        while (j < n - 1 && dKm(j, j + 1) < 0.3) j++;
        const span = j - i;
        const cnt = Math.min(8, span + 1);
        const show = [...new Set(Array.from({ length: cnt },
          (_, k) => i + Math.round(span * k / Math.max(1, cnt - 1))))];
        out.push({ kind: "stop", i0: i, i1: j, show, kmh: 0 });
        i = j;
      } else {
        let j = i, km = 0;
        while (j < n - 1 && dKm(j, j + 1) >= 0.3) { km += dKm(j, j + 1); j++; }
        const hrs = Math.max(1 / 3600, (epoch(j) - epoch(i)) / 3.6e6);
        out.push({ kind: "leg", i0: i, i1: j, show: [], kmh: km / hrs });
        i = j;
      }
    }
    return out;
  }, [playSeq]);
  const legKey = (s: Seg) => `${playSeq[s.i0]?.m.id}_${playSeq[s.i1]?.m.id}`;

  // 真实路线:自驾腿吸附道路(高德驾车规划,每腿 1 次调用,服务端永久缓存;
  // >180km/h 判为航段画虚线不调 API)。串行请求,礼貌对待配额与 QPS。
  const [roadMode, setRoadMode] = useState(false);
  const roadsRef = useRef<Map<string, [number, number][] | "flight" | "pending" | "fail">>(new Map());
  const [roadsTick, setRoadsTick] = useState(0);
  useEffect(() => {
    if (!roadMode || !segs.length) return;
    let stopped = false;
    (async () => {
      for (const s of segs) {
        if (stopped || s.kind !== "leg") continue;
        const key = legKey(s);
        const cur = roadsRef.current.get(key);
        if (cur && cur !== "fail") continue;
        if (s.kmh > 180) {
          roadsRef.current.set(key, "flight");
          setRoadsTick((t) => t + 1);
          continue;
        }
        roadsRef.current.set(key, "pending");
        const span = s.i1 - s.i0;
        const nv = Math.min(6, Math.max(0, span - 1));
        const vias = [...new Set(Array.from({ length: nv },
          (_, k) => s.i0 + 1 + Math.round((span - 2) * k / Math.max(1, nv - 1))))];
        try {
          const r = await api<any>("/api/map/drive_route", {
            method: "POST",
            body: JSON.stringify({
              origin: [playSeq[s.i0].m.lat, playSeq[s.i0].m.lon],
              destination: [playSeq[s.i1].m.lat, playSeq[s.i1].m.lon],
              waypoints: vias.map((v) => [playSeq[v].m.lat, playSeq[v].m.lon]),
            }),
          });
          roadsRef.current.set(key,
            Array.isArray(r.polyline) && r.polyline.length >= 2 ? r.polyline : "fail");
        } catch { roadsRef.current.set(key, "fail"); }
        if (!stopped) setRoadsTick((t) => t + 1);
      }
    })();
    return () => { stopped = true; };
  }, [roadMode, segs]);

  // 轨迹播放引擎(§22):停留点驻留放映照片;移动腿 🚗 开过去(开了真实
  // 路线且已缓存时沿道路形状走,航段 ✈️);interval 驱动(rAF 失焦冻结)。
  useEffect(() => {
    const map = mapRef.current;
    if (!playing || !map || !segs.length) return;
    const midCos = Math.cos((playSeq[0].ll[0] * Math.PI) / 180);
    const lenOf = (pts: [number, number][]) => {
      const cum = [0];
      for (let k = 1; k < pts.length; k++)
        cum.push(cum[k - 1] + Math.hypot(pts[k][0] - pts[k - 1][0],
                                         (pts[k][1] - pts[k - 1][1]) * midCos));
      return cum;
    };
    type Plan = { s: Seg; dur: number; t0: number; pts: [number, number][]; cum: number[];
                  flight: boolean; km: number };
    const plans: Plan[] = segs.map((s) => {
      let pts = playSeq.slice(s.i0, s.i1 + 1).map((x) => x.ll);
      if (s.kind === "stop") {
        return { s, dur: Math.min(8, Math.max(1.4, 1.0 * s.show.length)),
                 t0: 0, pts, cum: [0], flight: false, km: 0 };
      }
      const rd = roadMode ? roadsRef.current.get(legKey(s)) : undefined;
      if (Array.isArray(rd) && rd.length >= 2) pts = rd as [number, number][];
      const cum = lenOf(pts);
      const km = cum[cum.length - 1] * 111;
      return { s, dur: Math.min(9, Math.max(2.2, km * 0.35)), t0: 0, pts, cum,
               flight: s.kmh > 180, km };
    });
    let total = 0;
    for (const pl of plans) { pl.t0 = total; total += pl.dur; }
    const STOP_ZOOM = 13.5;

    // 绿色尾迹:淡辉光宽线 + 亮主线,走过的地方被点亮
    const trailGlow = L.polyline([], { color: "#22c55e", weight: 7, opacity: 0.25 }).addTo(map);
    const trail = L.polyline([], { color: "#4ade80", weight: 3, opacity: 0.95 }).addTo(map);
    const head = L.marker(playSeq[0].ll, {
      icon: L.divIcon({
        className: "",
        html: '<div class="jm-head-wrap"><div class="jm-head-dot"></div><div class="jm-head-car">🚗</div></div>',
        iconSize: [34, 34], iconAnchor: [17, 17],
      }),
      zIndexOffset: 2000, interactive: false,
    }).addTo(map);

    let p = 0, last = performance.now(), lastIdx = -1, doneAt = 0, si = 0;
    let donePath: [number, number][] = [];
    // 镜头纪律:不逐帧改缩放(瓦片会加载不完→黑屏);平移用 Leaflet 动画
    // 短步(280ms);缩放按"到本段终点的剩余距离"连续收紧 —— 越接近节点
    // 越放大(以前到站才瞬间跳,且巡航缩太远看不见路,用户反馈)。
    // 差值 ≥0.6 级才触发一次 flyTo(0.9s),链式衔接即为渐进推近。
    let lastPan = 0, zoomHoldUntil = 0;
    const step = () => {
      const now = performance.now();
      p = Math.min(1, p + ((now - last) / 1000) * speedRef.current / total);
      last = now;
      const tt = p * total;
      while (si < plans.length - 1 && tt >= plans[si].t0 + plans[si].dur) {
        donePath = donePath.concat(plans[si].pts);    // 段完成 → 并入尾迹
        si++;
      }
      const pl = plans[si];
      const s = pl.s;
      const f = Math.min(1, (tt - pl.t0) / pl.dur);
      let ll: [number, number];
      let idx: number;
      if (s.kind === "stop") {
        ll = pl.pts[pl.pts.length - 1];
        idx = s.show[Math.min(s.show.length - 1, Math.floor(f * s.show.length))];
        const tl = [...donePath, ...pl.pts];
        trail.setLatLngs(tl);
        trailGlow.setLatLngs(tl);
      } else {
        const d = f * pl.cum[pl.cum.length - 1];
        let j = 0;
        while (j < pl.cum.length - 2 && pl.cum[j + 1] < d) j++;
        const g = Math.min(1, (d - pl.cum[j]) / Math.max(pl.cum[j + 1] - pl.cum[j], 1e-12));
        const [a, b] = [pl.pts[j], pl.pts[j + 1]];
        ll = [a[0] + (b[0] - a[0]) * g, a[1] + (b[1] - a[1]) * g];
        idx = Math.min(s.i1, s.i0 + Math.round((s.i1 - s.i0) * f));
        const tl = [...donePath, ...pl.pts.slice(0, j + 1), ll];
        trail.setLatLngs(tl);
        trailGlow.setLatLngs(tl);
        const el = head.getElement()?.querySelector(".jm-head-car") as HTMLElement | null;
        if (el) {
          el.textContent = pl.flight ? "✈️" : "🚗";
          el.style.transform = b[1] < a[1] ? "scaleX(-1)" : "";
        }
      }
      head.getElement()?.querySelector(".jm-head-wrap")?.classList.toggle("is-leg", s.kind === "leg");
      head.setLatLng(ll);
      if (idx !== lastIdx) { setPlayPhoto(playSeq[idx].m); lastIdx = idx; }
      if (autoZoomRef.current && now > zoomHoldUntil) {
        let zt: number;
        if (s.kind === "stop") {
          zt = STOP_ZOOM;
        } else {
          const remainKm = Math.max(0, (1 - f) * pl.km);   // 弧长匀速下时间比≈距离比
          const floor_ = pl.flight ? 5 : 8;                // 自驾下限 8:公路仍清晰可见
          zt = Math.max(floor_, Math.min(STOP_ZOOM, 15.2 - 1.05 * Math.log2(remainKm + 1.5)));
        }
        if (Math.abs(zt - map.getZoom()) >= 0.6) {
          map.flyTo(ll, zt, { duration: 0.9, easeLinearity: 0.4 });
          zoomHoldUntil = now + 1000;   // flyTo 期间不 pan,别打断它
        }
      }
      if (now > zoomHoldUntil && now - lastPan > 280) {
        map.panTo(ll, { animate: true, duration: 0.3, easeLinearity: 0.5, noMoveStart: true } as any);
        lastPan = now;
      }
      if (p >= 1) {
        if (!doneAt) doneAt = now;
        if (now - doneAt > 1800) setPlaying(false);
      }
    };
    const timer = window.setInterval(step, 33);
    return () => { window.clearInterval(timer); trail.remove(); trailGlow.remove();
                   head.remove(); setPlayPhoto(null); };
  }, [playing, playSeq, segs, roadMode]);
  // 首次载入相簿后默认选最后一天
  useEffect(() => {
    if (days.length && selDays.size === 0) setSelDays(new Set([days[days.length - 1][0]]));
  }, [days.length]);

  // 初始化地图(一次)
  useEffect(() => {
    if (!boxRef.current || mapRef.current) return;
    const map = L.map(boxRef.current, {
      center: [35, 105], zoom: 4, zoomControl: false, attributionControl: false,
      preferCanvas: true, zoomSnap: 0,   // 允许小数级缩放(播放时丝滑推拉镜头)
    });
    L.control.zoom({ position: "bottomright" }).addTo(map);
    mapRef.current = map;
    layerRef.current = L.layerGroup().addTo(map);
    // tab 用 display:none 保活,初始化时容器是 0 尺寸 → 变可见时必须
    // invalidateSize 并重新套框,否则瓦片错位/空白
    const ro = new ResizeObserver(() => {
      map.invalidateSize();
      if (boundsRef.current) map.fitBounds(boundsRef.current.pad(0.15));
    });
    ro.observe(boxRef.current);
    return () => { ro.disconnect(); map.remove(); mapRef.current = null; layerRef.current = null; };
  }, []);

  // 底图图层:标准矢量 / 卫星影像(+透明路网标注)。均为高德公开瓦片,
  // 不走 API key、零配额;浏览器 HTTP 缓存生效。坐标系同为 GCJ-02。
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    tilesRef.current.forEach((t) => t.remove());
    const mk = (url: string, cls: string) =>
      L.tileLayer(url, { subdomains: ["1", "2", "3", "4"], maxZoom: 18, className: cls });
    tilesRef.current = baseLayer === "sat"
      ? [mk("https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}", "amap-sat"),
         mk("https://webst0{s}.is.autonavi.com/appmaptile?style=8&x={x}&y={y}&z={z}", "amap-anno")]
      : [mk("https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}", "amap-dark")];
    tilesRef.current.forEach((t) => t.addTo(map));
  }, [baseLayer]);

  // 选择变化 → 重画轨迹与照片钉
  useEffect(() => {
    const map = mapRef.current, layer = layerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();
    const chosen = days.filter(([k]) => selDays.has(k));
    if (!chosen.length) return;
    const allPts: L.LatLngExpression[] = [];
    chosen.forEach(([k, list], di) => {
      const color = DAY_COLORS[days.findIndex(([x]) => x === k) % DAY_COLORS.length];
      const sorted = [...list].sort((a, b) => String(a.taken_at).localeCompare(String(b.taken_at)));
      const pts = sorted.map((m) => {
        const [gla, gln] = wgs2gcj(m.lat!, m.lon!);
        return [gla, gln] as [number, number];
      });
      pts.forEach((p) => allPts.push(p));
      // 真实路线模式下不画整天直连线(转场直线弦和道路线重叠,乱);
      // 播放中一律降为幽灵线 —— 走过的路由绿色尾迹点亮,而不是开局全画
      if (!roadMode && pts.length >= 2) {
        L.polyline(pts, { color, weight: 3, opacity: playing ? 0.08 : 0.5 }).addTo(layer);
        L.polyline(pts, { color, weight: 1.5, opacity: playing ? 0.15 : 0.95 }).addTo(layer);
      }
      // 缩略图钉:每天等距 ≤40 张,其余小点
      const step = Math.max(1, Math.ceil(sorted.length / 40));
      sorted.forEach((m, i) => {
        const p = pts[i];
        if (i % step === 0) {
          const icon = L.divIcon({
            className: "",
            html: `<div class="jm-pin" style="border-color:${color}">
                     <img src="${m.thumb}" loading="lazy"/>
                     ${m.type === "VIDEO" ? '<span class="jm-play">▶</span>' : ""}
                   </div>`,
            iconSize: [44, 44], iconAnchor: [22, 22],
          });
          L.marker(p, { icon }).on("click", () => setDetail(m)).addTo(layer);
        } else {
          L.circleMarker(p, { radius: 3.5, color, weight: 1, fillColor: color, fillOpacity: 0.85 })
            .on("click", () => setDetail(m)).addTo(layer);
        }
      });
      void di;
    });
    // 真实路线覆盖层:停留簇画当天色短线,自驾腿画道路形状(白线+描边),
    // 航段虚线,还没拉回来的腿画细虚线占位
    if (roadMode) {
      const colorOf = (i0: number) => {
        const k = String(playSeq[i0]?.m.taken_at || "").slice(0, 10);
        return DAY_COLORS[Math.max(0, days.findIndex(([x]) => x === k)) % DAY_COLORS.length];
      };
      const gh = playing;   // 播放中:幽灵化,让绿色尾迹当主角
      for (const s of segs) {
        if (s.kind === "stop") {
          const pts = playSeq.slice(s.i0, s.i1 + 1).map((x) => x.ll);
          if (pts.length >= 2)
            L.polyline(pts, { color: colorOf(s.i0), weight: 2.5, opacity: gh ? 0.15 : 0.85 }).addTo(layer);
          continue;
        }
        const rd = roadsRef.current.get(legKey(s));
        if (rd === "flight") {
          L.polyline([playSeq[s.i0].ll, playSeq[s.i1].ll],
            { color: "#e2e8f0", weight: 2.5, opacity: gh ? 0.15 : 0.85, dashArray: "6 9" }).addTo(layer);
        } else if (Array.isArray(rd)) {
          if (!gh) L.polyline(rd, { color: "#0f172a", weight: 6, opacity: 0.4 }).addTo(layer);
          L.polyline(rd, { color: "#ffffff", weight: gh ? 2 : 3.5, opacity: gh ? 0.14 : 0.95 }).addTo(layer);
        } else {
          L.polyline([playSeq[s.i0].ll, playSeq[s.i1].ll],
            { color: "#64748b", weight: 1.5, opacity: gh ? 0.1 : 0.5, dashArray: "2 6" }).addTo(layer);
        }
      }
    }
    if (allPts.length) {
      boundsRef.current = L.latLngBounds(allPts as any);
      // 播放中禁止 fitBounds 抢镜头 —— 路线异步加载完成触发的重画会把
      // 视野猛拉回全局,和播放跟车互相打架("晃得厉害"的主因)
      if (!playingRef.current) map.fitBounds(boundsRef.current.pad(0.15));
    }
  }, [days, selDays, roadMode, roadsTick, playing]);

  return (
    <div className="flex h-[calc(100vh-140px)] min-h-[480px] flex-col gap-3">
      {/* 控制条 */}
      <div className="flex flex-wrap items-center gap-2">
        <MapIcon className="h-4 w-4 text-cyan-400" />
        <select
          className="h-9 rounded-lg border border-white/10 bg-slate-900 px-2.5 text-[12.5px] text-slate-200 focus:border-cyan-500/40 focus:outline-none"
          value={albumId} onChange={(e) => pickAlbum(e.target.value)}>
          <option value="">选择相簿…</option>
          {albums.map((a) => <option key={a.id} value={a.id}>{a.name}（{a.count}）</option>)}
        </select>
        {loading && <Loader2 className="h-4 w-4 animate-spin text-slate-500" />}
        {albumId && !loading && (
          <>
            <div className="flex max-w-[60vw] flex-wrap gap-1">
              {days.map(([k, list], i) => {
                const on = selDays.has(k);
                const color = DAY_COLORS[i % DAY_COLORS.length];
                return (
                  <button key={k}
                    className={cn("flex h-7 items-center gap-1.5 rounded-full px-2.5 text-[11.5px] transition-colors",
                      on ? "bg-white/[0.1] font-medium text-white" : "bg-white/[0.04] text-slate-400 hover:bg-white/[0.08]")}
                    onClick={() => setSelDays((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; })}>
                    <span className="h-2 w-2 rounded-full" style={{ background: color, opacity: on ? 1 : 0.4 }} />
                    {dayLabel(k)} <span className="text-slate-500">{list.length}</span>
                  </button>
                );
              })}
            </div>
            <button className="text-[11px] text-slate-500 hover:text-cyan-300"
              onClick={() => setSelDays(new Set(days.map(([k]) => k)))}>全选</button>
            <button className="text-[11px] text-slate-500 hover:text-cyan-300"
              onClick={() => setSelDays(new Set())}>清空</button>
            {noGps > 0 && <span className="text-[10.5px] text-slate-600">{noGps} 项无 GPS 未显示</span>}
            {selDays.size > 0 && playSeq.length >= 2 && (
              <>
                <button
                  className={cn("flex h-7 items-center gap-1 rounded-full px-3 text-[11.5px] font-medium transition-colors",
                    playing ? "bg-rose-500/20 text-rose-300 hover:bg-rose-500/30"
                      : "bg-cyan-500/20 text-cyan-200 hover:bg-cyan-500/30")}
                  onClick={() => setPlaying((v) => !v)}>
                  {playing ? "■ 停止" : "▶ 播放轨迹"}
                </button>
                <button
                  className="h-7 rounded-full bg-white/[0.06] px-2.5 text-[11px] tabular-nums text-slate-300 transition-colors hover:bg-white/10"
                  title="播放倍速" onClick={() => setSpeed((s) => (s === 1 ? 2 : s === 2 ? 4 : 1))}>
                  {speed}×
                </button>
                <button
                  className={cn("h-7 rounded-full px-2.5 text-[11px] transition-colors",
                    autoZoom ? "bg-white/[0.1] text-slate-200" : "bg-white/[0.04] text-slate-500 hover:text-slate-300")}
                  title="播放时随移动速度平滑推拉镜头:停留点推近、长途拉远"
                  onClick={() => setAutoZoom((v) => !v)}>
                  🔍 自动缩放
                </button>
              </>
            )}
          </>
        )}
        <div className="ml-auto flex items-center gap-1">
          <button
            className={cn("h-7 rounded-full px-2.5 text-[11px] transition-colors",
              roadMode ? "bg-emerald-500/20 text-emerald-300" : "bg-white/[0.04] text-slate-500 hover:text-slate-300")}
            title="把自驾转场吸附到真实道路(高德驾车规划,每段路线 1 次调用、永久缓存,再看零成本;均速 >180km/h 判为航段画虚线,不调用)"
            onClick={() => setRoadMode((v) => !v)}>
            🛣 真实路线
          </button>
          <button
            className={cn("h-7 rounded-full px-2.5 text-[11px] transition-colors",
              baseLayer === "sat" ? "bg-white/[0.1] text-slate-200" : "bg-white/[0.04] text-slate-500 hover:text-slate-300")}
            title="卫星影像 + 路网标注(高德公开瓦片,零配额)"
            onClick={() => setBaseLayer((v) => (v === "sat" ? "vector" : "sat"))}>
            {baseLayer === "sat" ? "🛰 卫星" : "🗺 标准"}
          </button>
          <button
            className={cn("h-7 rounded-full px-2.5 text-[11px] transition-colors",
              darkMap ? "bg-white/[0.1] text-slate-200" : "bg-white/[0.04] text-slate-500 hover:text-slate-300",
              baseLayer === "sat" && "pointer-events-none opacity-30")}
            title="暗色氛围模式(仅标准图层)"
            onClick={() => setDarkMap((v) => !v)}>
            {darkMap ? "🌙 暗色" : "☀️ 原色"}
          </button>
        </div>
      </div>

      {/* 地图 */}
      <div className="relative min-h-0 flex-1">
        <div ref={boxRef} className={cn("absolute inset-0 overflow-hidden rounded-2xl ring-1 ring-white/10",
          darkMap && "jm-dark")} />
        {/* 播放时:走到哪儿就"放映"哪儿的照片(大图 + 淡入,点击开灯箱) */}
        {playing && playPhoto && (
          <button
            className="absolute bottom-4 left-4 z-[1100] w-[min(38vw,400px)] overflow-hidden rounded-xl bg-slate-950/90 text-left shadow-[0_10px_36px_rgba(0,0,0,0.6)] ring-1 ring-white/15 backdrop-blur-sm"
            onClick={() => setDetail(playPhoto)}>
            <img key={playPhoto.id} src={`/api/immich/thumb/${playPhoto.id}`}
              className="jm-fade max-h-[42vh] w-full bg-black/40 object-contain" />
            <div className="flex items-center gap-2.5 px-3 py-2 text-[11.5px] text-slate-300">
              <span className="font-medium text-white">{String(playPhoto.taken_at).replace("T", " ").slice(5, 16)}</span>
              {playPhoto.type === "VIDEO" && <span className="text-[10px] text-slate-400">▶ 视频</span>}
              {playPhoto.city && <span className="text-slate-400">📍 {playPhoto.city}</span>}
            </div>
          </button>
        )}
      </div>

      {/* 灯箱 */}
      {detail && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm"
          onClick={() => setDetail(null)}>
          <div className="max-w-[86vw]" onClick={(e) => e.stopPropagation()}>
            <img src={`/api/immich/thumb/${detail.id}`} className="max-h-[76vh] rounded-xl object-contain shadow-2xl" />
            <div className="mt-2.5 flex items-center gap-3 text-[12.5px] text-slate-300">
              <span className="font-medium text-white">{detail.name}</span>
              <span className="text-slate-500">{String(detail.taken_at).replace("T", " ").slice(0, 16)}</span>
              {detail.city && <span className="text-slate-500">📍 {detail.city}</span>}
              <a className="ml-auto flex items-center gap-1 text-cyan-400 hover:underline" target="_blank" rel="noreferrer"
                href={`http://127.0.0.1:2284/photos/${detail.id}`}>
                在 Immich 打开 <ExternalLink className="h-3 w-3" />
              </a>
              <button className="text-slate-400 hover:text-white" onClick={() => setDetail(null)}><X className="h-4 w-4" /></button>
            </div>
          </div>
        </div>
      )}

      {/* 瓦片暗色化 + 照片钉样式 */}
      <style>{`
        .jm-dark .amap-dark { filter: grayscale(35%) brightness(0.62) contrast(1.05) saturate(0.7); }
        .jm-pin { width: 44px; height: 44px; border-radius: 10px; overflow: hidden;
                  border: 2px solid; box-shadow: 0 2px 10px rgba(0,0,0,.55); background:#0f172a;
                  transition: transform .12s; position: relative; }
        .jm-pin:hover { transform: scale(1.6); z-index: 1000; }
        .jm-pin img { width: 100%; height: 100%; object-fit: cover; display:block; }
        .jm-play { position:absolute; right:2px; bottom:1px; font-size:9px; color:#fff;
                   text-shadow:0 1px 3px rgba(0,0,0,.9); }
        .leaflet-container { background:#0b0f1a; }
        .jm-head-wrap { width:34px; height:34px; display:flex; align-items:center; justify-content:center; }
        .jm-head-dot { width:16px; height:16px; border-radius:50%; background:#fff;
                       box-shadow:0 0 0 4px rgba(255,255,255,.25), 0 0 18px 6px rgba(74,222,128,.85);
                       animation: jm-pulse 1.2s ease-in-out infinite; }
        .jm-head-car { display:none; font-size:26px; line-height:1;
                       filter: drop-shadow(0 2px 5px rgba(0,0,0,.6)); }
        .jm-head-wrap.is-leg .jm-head-dot { display:none; }
        .jm-head-wrap.is-leg .jm-head-car { display:block; }
        @keyframes jm-pulse { 50% { box-shadow:0 0 0 7px rgba(255,255,255,.15), 0 0 22px 8px rgba(56,189,248,.9); } }
        .jm-fade { animation: jm-fadein .32s ease; }
        @keyframes jm-fadein { from { opacity:0; transform:scale(.975); } }
      `}</style>
    </div>
  );
}

/** 旅程地图(§22)— 独立工具页:选相簿 → 选日期 → 交互地图看轨迹、点照片。
 *  Leaflet + 高德栅格瓦片(CSS 滤镜暗色化);照片 GPS 转 GCJ-02 才能对上瓦片。
 *  标记密度自适应:每天等距挑 ≤40 张出缩略图钉,其余画成轨迹色小点。 */
import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { ExternalLink, Loader2, MapIcon, X } from "lucide-react";
import { cn } from "@/lib/utils";
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
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [playPhoto, setPlayPhoto] = useState<ImItem | null>(null);
  const speedRef = useRef(1);
  speedRef.current = speed;
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

  // 轨迹播放引擎:白色进度线 + 发光行进头,地图跟随,经过的照片浮出左下角
  useEffect(() => {
    const map = mapRef.current;
    if (!playing || !map || playSeq.length < 2) return;
    const trail = L.polyline([], { color: "#ffffff", weight: 3, opacity: 0.95 }).addTo(map);
    const head = L.marker(playSeq[0].ll, {
      icon: L.divIcon({ className: "", html: '<div class="jm-head"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }),
      zIndexOffset: 2000, interactive: false,
    }).addTo(map);
    const total = Math.min(90, Math.max(15, playSeq.length * 0.18));   // 整段秒数
    let p = 0, last = performance.now(), lastPan = 0, lastIdx = -1, raf = 0, doneAt = 0;
    const step = (now: number) => {
      p = Math.min(1, p + ((now - last) / 1000) * speedRef.current / total);
      last = now;
      const idx = Math.min(playSeq.length - 1, Math.floor(p * (playSeq.length - 1)));
      trail.setLatLngs(playSeq.slice(0, idx + 1).map((x) => x.ll));
      head.setLatLng(playSeq[idx].ll);
      if (idx !== lastIdx) { setPlayPhoto(playSeq[idx].m); lastIdx = idx; }
      if (now - lastPan > 400) { map.panTo(playSeq[idx].ll); lastPan = now; }
      if (p >= 1) {
        if (!doneAt) doneAt = now;
        if (now - doneAt > 1800) { setPlaying(false); return; }   // 终点停 1.8s 收尾
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(raf); trail.remove(); head.remove(); setPlayPhoto(null); };
  }, [playing, playSeq]);
  // 首次载入相簿后默认选最后一天
  useEffect(() => {
    if (days.length && selDays.size === 0) setSelDays(new Set([days[days.length - 1][0]]));
  }, [days.length]);

  // 初始化地图(一次)
  useEffect(() => {
    if (!boxRef.current || mapRef.current) return;
    const map = L.map(boxRef.current, {
      center: [35, 105], zoom: 4, zoomControl: false, attributionControl: false,
      preferCanvas: true,
    });
    L.control.zoom({ position: "bottomright" }).addTo(map);
    L.tileLayer("https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}", {
      subdomains: ["1", "2", "3", "4"], maxZoom: 18, className: "amap-dark",
    }).addTo(map);
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
      if (pts.length >= 2) {
        L.polyline(pts, { color, weight: 3, opacity: 0.5 }).addTo(layer);
        L.polyline(pts, { color, weight: 1.5, opacity: 0.95 }).addTo(layer);
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
    if (allPts.length) {
      boundsRef.current = L.latLngBounds(allPts as any);
      map.fitBounds(boundsRef.current.pad(0.15));
    }
  }, [days, selDays]);

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
              </>
            )}
          </>
        )}
        <button
          className={cn("ml-auto h-7 rounded-full px-2.5 text-[11px] transition-colors",
            darkMap ? "bg-white/[0.1] text-slate-200" : "bg-white/[0.04] text-slate-500 hover:text-slate-300")}
          title="暗色氛围模式(默认原色地图)"
          onClick={() => setDarkMap((v) => !v)}>
          {darkMap ? "🌙 暗色" : "☀️ 原色"}
        </button>
      </div>

      {/* 地图 */}
      <div className="relative min-h-0 flex-1">
        <div ref={boxRef} className={cn("absolute inset-0 overflow-hidden rounded-2xl ring-1 ring-white/10",
          darkMap && "jm-dark")} />
        {/* 播放时:走到哪张照片,哪张浮出(点它看大图) */}
        {playing && playPhoto && (
          <button
            className="absolute bottom-4 left-4 z-[1100] flex items-center gap-2.5 rounded-xl bg-slate-950/88 p-2 pr-3.5 text-left shadow-[0_8px_28px_rgba(0,0,0,0.55)] ring-1 ring-white/15 backdrop-blur-sm"
            onClick={() => setDetail(playPhoto)}>
            <img src={playPhoto.thumb} className="h-16 w-16 rounded-lg object-cover" />
            <div className="text-[11.5px] leading-relaxed text-slate-300">
              <div className="font-medium text-white">
                {String(playPhoto.taken_at).replace("T", " ").slice(5, 16)}
                {playPhoto.type === "VIDEO" && <span className="ml-1 text-[10px] text-slate-400">▶ 视频</span>}
              </div>
              {playPhoto.city && <div className="text-slate-400">📍 {playPhoto.city}</div>}
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
        .jm-head { width:18px; height:18px; border-radius:50%; background:#fff;
                   box-shadow:0 0 0 4px rgba(255,255,255,.25), 0 0 18px 6px rgba(56,189,248,.8);
                   animation: jm-pulse 1.2s ease-in-out infinite; }
        @keyframes jm-pulse { 50% { box-shadow:0 0 0 7px rgba(255,255,255,.15), 0 0 22px 8px rgba(56,189,248,.9); } }
      `}</style>
    </div>
  );
}

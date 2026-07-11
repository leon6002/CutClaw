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
    setAlbumId(id); setSelDays(new Set()); setDetail(null);
    if (id) loadItems(id);
  };
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
          </>
        )}
      </div>

      {/* 地图 */}
      <div ref={boxRef} className="min-h-0 flex-1 overflow-hidden rounded-2xl ring-1 ring-white/10" />

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
        .amap-dark { filter: grayscale(35%) brightness(0.62) contrast(1.05) saturate(0.7); }
        .jm-pin { width: 44px; height: 44px; border-radius: 10px; overflow: hidden;
                  border: 2px solid; box-shadow: 0 2px 10px rgba(0,0,0,.55); background:#0f172a;
                  transition: transform .12s; position: relative; }
        .jm-pin:hover { transform: scale(1.6); z-index: 1000; }
        .jm-pin img { width: 100%; height: 100%; object-fit: cover; display:block; }
        .jm-play { position:absolute; right:2px; bottom:1px; font-size:9px; color:#fff;
                   text-shadow:0 1px 3px rgba(0,0,0,.9); }
        .leaflet-container { background:#0b0f1a; }
      `}</style>
    </div>
  );
}

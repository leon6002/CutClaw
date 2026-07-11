/** 旅程轨迹开场 — 网页实时预览 + 调参(§21 二期)。
 *  canvas 复刻 Python 渲染器的关键视觉(底图调色/描画节奏/缓推/照片冒泡),
 *  滑杆即时生效,「保存参数」写回 route_intro.json,正式渲染读同一份。 */
import { useEffect, useRef, useState } from "react";
import { Loader2, RotateCcw, Save } from "lucide-react";
import { api } from "../api";

// WGS84→GCJ02(与后端 amap_geo.py 同公式;预览轨迹必须转火星坐标才能对上底图)
function wgs2gcj(lat: number, lon: number): [number, number] {
  if (!(lat >= 0.8293 && lat <= 55.8271 && lon >= 72.004 && lon <= 137.8347)) return [lat, lon];
  const a = 6378245.0, ee = 0.00669342162296594323, PI = Math.PI;
  const t = (x: number, y: number, m: "lat" | "lon") => {
    let r = m === "lat"
      ? -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x))
      : 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2 / 3;
    if (m === "lat") {
      r += (20 * Math.sin(y * PI) + 40 * Math.sin(y / 3 * PI)) * 2 / 3;
      r += (160 * Math.sin(y / 12 * PI) + 320 * Math.sin(y * PI / 30)) * 2 / 3;
    } else {
      r += (20 * Math.sin(x * PI) + 40 * Math.sin(x / 3 * PI)) * 2 / 3;
      r += (150 * Math.sin(x / 12 * PI) + 300 * Math.sin(x / 30 * PI)) * 2 / 3;
    }
    return r;
  };
  const dlat0 = t(lon - 105, lat - 35, "lat"), dlon0 = t(lon - 105, lat - 35, "lon");
  const rl = lat / 180 * PI, magic = 1 - ee * Math.sin(rl) ** 2, sm = Math.sqrt(magic);
  return [lat + (dlat0 * 180) / ((a * (1 - ee)) / (magic * sm) * PI),
          lon + (dlon0 * 180) / (a / sm * Math.cos(rl) * PI)];
}

const CANVAS = 2048;
const smooth = (x: number) => { x = Math.max(0, Math.min(1, x)); return x * x * (3 - 2 * x); };

export default function RouteIntroPreview({ shotPoint }: { shotPoint: string }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState("");
  const [dur, setDur] = useState(10);
  const [bright, setBright] = useState(0.62);
  const [bubbles, setBubbles] = useState(true);
  const [excl, setExcl] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState(false);
  const cvRef = useRef<HTMLCanvasElement>(null);
  const imgsRef = useRef<{ map?: HTMLImageElement; thumbs: Record<string, HTMLImageElement> }>({ thumbs: {} });
  const t0Ref = useRef(performance.now());

  useEffect(() => {
    setData(null); setErr("");
    api<any>(`/api/render/route_full?shot_point=${encodeURIComponent(shotPoint)}`)
      .then((d) => {
        const s = d.style || {};
        if (s.duration) setDur(Number(s.duration));
        if (s.map_brightness) setBright(Number(s.map_brightness));
        if (s.bubbles === false) setBubbles(false);
        setExcl(new Set((s.bubble_exclude || []) as string[]));
        const store = imgsRef.current;
        if (d.map?.url) { const im = new Image(); im.src = d.map.url; store.map = im; }
        for (const b of d.bubbles || []) { const im = new Image(); im.src = b.thumb_url; store.thumbs[b.id] = im; }
        setData(d);
        t0Ref.current = performance.now();
      })
      .catch((e) => setErr(e.message));
  }, [shotPoint]);

  useEffect(() => {
    if (!data) return;
    const cv = cvRef.current;
    if (!cv) return;
    const ctx = cv.getContext("2d")!;
    const W = cv.width, H = cv.height;               // 540×960(9:16 @2x)
    // 投影到 2048 画布(与 Python 完全同公式)
    const m = data.map;
    let full: [number, number][] = [];
    if (m?.center && m?.zoom) {
      const n = 256 * Math.pow(2, m.zoom) * 2;
      const merc = (la: number, ln: number): [number, number] => {
        const r = (la * Math.PI) / 180;
        return [((ln + 180) / 360) * n,
                ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * n];
      };
      const [cx0, cy0] = merc(m.center[1], m.center[0]);
      full = data.points.map((p: any) => {
        const [gla, gln] = wgs2gcj(p.lat, p.lon);
        const [x, y] = merc(gla, gln);
        return [x - cx0 + CANVAS / 2, y - cy0 + CANVAS / 2] as [number, number];
      });
    } else {
      const mid = (data.points.reduce((a: number, p: any) => a + p.lat, 0) / data.points.length) * Math.PI / 180;
      const xs = data.points.map((p: any) => p.lon * Math.cos(mid));
      const ys = data.points.map((p: any) => p.lat);
      const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
      const s = (CANVAS * 0.52) / Math.max(x1 - x0, y1 - y0, 1e-9);
      full = xs.map((x: number, i: number) =>
        [(x - (x0 + x1) / 2) * s + CANVAS / 2, CANVAS / 2 - (ys[i] - (y0 + y1) / 2) * s] as [number, number]);
    }
    // 冒泡的画布坐标 + 弧长占比
    const bubPx = (data.bubbles || []).map((b: any, k: number) => {
      let px: [number, number];
      if (m?.center) {
        const n = 256 * Math.pow(2, m.zoom) * 2;
        const [gla, gln] = wgs2gcj(b.lat, b.lon);
        const r = (gla * Math.PI) / 180;
        const x = ((gln + 180) / 360) * n;
        const y = ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * n;
        const rc = (m.center[1] * Math.PI) / 180;
        const cx0 = ((m.center[0] + 180) / 360) * n;
        const cy0 = ((1 - Math.log(Math.tan(rc) + 1 / Math.cos(rc)) / Math.PI) / 2) * n;
        px = [x - cx0 + CANVAS / 2, y - cy0 + CANVAS / 2];
      } else { px = full[0]; }
      let best = 0, bd = Infinity;
      full.forEach((q, i) => { const d2 = (q[0] - px[0]) ** 2 + (q[1] - px[1]) ** 2; if (d2 < bd) { bd = d2; best = i; } });
      return { id: b.id, px, frac: Math.min(best / Math.max(1, full.length - 1), 0.985), side: k % 2 };
    });
    // 裁剪窗口
    const xs2 = full.map((p) => p[0]), ys2 = full.map((p) => p[1]);
    const bcx = (Math.min(...xs2) + Math.max(...xs2)) / 2, bcy = (Math.min(...ys2) + Math.max(...ys2)) / 2;
    const aspect = W / H;
    let cw0 = Math.max((Math.max(...xs2) - Math.min(...xs2)) / 0.7,
                       ((Math.max(...ys2) - Math.min(...ys2)) / 0.7) * aspect, 420 * Math.max(1, aspect));
    let ch0 = cw0 / aspect;
    const fscale = Math.min(CANVAS / cw0, CANVAS / ch0, 1);
    cw0 *= fscale; ch0 *= fscale;

    let raf = 0;
    const drawT0 = 0.7;
    const loop = () => {
      const D = dur;
      const t = ((performance.now() - t0Ref.current) / 1000) % (D + 0.6);
      const drawT1 = D - 1.6, textT0 = D - 2.3;
      const k = 1.05 - 0.11 * smooth(t / D);
      const cw = cw0 * k, ch = ch0 * k;
      const cx = Math.min(Math.max(bcx, cw / 2), CANVAS - cw / 2);
      const cy = Math.min(Math.max(bcy, ch / 2), CANVAS - ch / 2);
      const T = (p: [number, number]): [number, number] =>
        [((p[0] - (cx - cw / 2)) * W) / cw, ((p[1] - (cy - ch / 2)) * H) / ch];

      ctx.filter = "none";
      ctx.fillStyle = "#0b0f1a"; ctx.fillRect(0, 0, W, H);
      const mapIm = imgsRef.current.map;
      if (mapIm && mapIm.complete && mapIm.naturalWidth > 0) {
        ctx.filter = `saturate(28%) brightness(${Math.round(bright * 100)}%)`;
        const sc = mapIm.naturalWidth / CANVAS;
        ctx.drawImage(mapIm, (cx - cw / 2) * sc, (cy - ch / 2) * sc, cw * sc, ch * sc, 0, 0, W, H);
        ctx.filter = "none";
        ctx.fillStyle = "rgba(10,16,32,0.18)"; ctx.fillRect(0, 0, W, H);   // 蓝移近似
      }
      const p = smooth((t - drawT0) / (drawT1 - drawT0));
      const nVis = Math.max(2, Math.floor(full.length * p));
      const seg = full.slice(0, nVis).map(T);
      ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.strokeStyle = "rgba(125,211,252,0.28)"; ctx.lineWidth = 7;
      ctx.beginPath(); seg.forEach((q, i) => (i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]))); ctx.stroke();
      ctx.strokeStyle = "rgb(125,211,252)"; ctx.lineWidth = 2.5;
      ctx.beginPath(); seg.forEach((q, i) => (i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]))); ctx.stroke();
      const s0 = T(full[0]);
      ctx.strokeStyle = "rgba(255,255,255,0.85)"; ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.arc(s0[0], s0[1], 4, 0, 7); ctx.stroke();
      const head = seg[seg.length - 1];
      ctx.fillStyle = "#fff";
      ctx.beginPath(); ctx.arc(head[0], head[1], p < 1 ? 3.5 : 5, 0, 7); ctx.fill();
      // 冒泡
      if (bubbles) {
        for (const bb of bubPx) {
          if (p < bb.frac || excl.has(bb.id)) continue;
          const im = imgsRef.current.thumbs[bb.id];
          if (!im || !im.complete || !im.naturalWidth) continue;
          const S = H * 0.115;
          const [bx, by] = T(bb.px);
          const ox = bb.side ? 10 : -10 - S;
          const px2 = Math.min(Math.max(bx + ox, 4), W - S - 4);
          const py2 = Math.min(Math.max(by - S - 8, 4), H - S - 4);
          ctx.save();
          ctx.translate(px2 + S / 2, py2 + S / 2);
          ctx.rotate(((bb.side ? -6 : 6) * Math.PI) / 180);
          ctx.fillStyle = "#f6f6f4"; ctx.fillRect(-S / 2 - 3, -S / 2 - 3, S + 6, S + 6);
          const cs = Math.min(im.naturalWidth, im.naturalHeight);
          ctx.drawImage(im, (im.naturalWidth - cs) / 2, (im.naturalHeight - cs) / 2, cs, cs, -S / 2, -S / 2, S, S);
          ctx.restore();
          ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(bx, by, 2.5, 0, 7); ctx.fill();
        }
      }
      // 文字
      const ta = smooth((t - textT0) / 0.7);
      if (ta > 0) {
        ctx.fillStyle = `rgba(5,8,16,${0.6 * ta})`;
        ctx.fillRect(0, H * 0.765, W, H * 0.135);
        ctx.textAlign = "center";
        ctx.fillStyle = `rgba(255,255,255,${0.92 * ta})`;
        ctx.font = `600 ${Math.round(H * 0.034)}px system-ui`;
        const title = data.start_label && data.end_label && data.start_label !== data.end_label
          ? `${data.start_label} → ${data.end_label}` : (data.start_label || data.end_label || "");
        ctx.fillText(title, W / 2, H * 0.815);
        ctx.fillStyle = `rgba(148,163,184,${0.8 * ta})`;
        ctx.font = `${Math.round(H * 0.021)}px system-ui`;
        ctx.fillText(`${data.date_range || ""} · 全程 ${Math.round(data.total_km || 0)} 公里`, W / 2, H * 0.855);
      }
      // 首尾淡入淡出
      const g = Math.min(1, t / 0.4, Math.max(0, (D - t) / 0.5));
      if (g < 1) { ctx.fillStyle = `rgba(0,0,0,${1 - g})`; ctx.fillRect(0, 0, W, H); }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [data, dur, bright, bubbles, excl]);

  const save = async () => {
    setErr(""); setSaved(false);
    try {
      await api("/api/render/route_style", {
        method: "POST",
        body: JSON.stringify({ shot_point: shotPoint, style: {
          duration: dur, map_brightness: bright, bubbles, bubble_exclude: [...excl] } }),
      });
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
    } catch (e: any) { setErr(e.message); }
  };

  if (err) return <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">{err}</div>;
  if (!data) return <div className="py-8 text-center"><Loader2 className="mx-auto h-4 w-4 animate-spin text-slate-500" /></div>;

  return (
    <div className="flex flex-wrap items-start gap-4">
      <canvas ref={cvRef} width={540} height={960}
        className="w-[240px] shrink-0 rounded-xl ring-1 ring-white/10" />
      <div className="min-w-[240px] flex-1 space-y-4 pt-1 text-[12.5px] text-slate-300">
        <div>
          <div className="mb-1 flex justify-between text-slate-400">
            <span>时长</span><span className="tabular-nums text-slate-200">{dur.toFixed(1)} 秒</span>
          </div>
          <input type="range" min={5} max={18} step={0.5} value={dur} className="w-full accent-cyan-400"
            onChange={(e) => { setDur(Number(e.target.value)); t0Ref.current = performance.now(); }} />
        </div>
        <div>
          <div className="mb-1 flex justify-between text-slate-400">
            <span>地图亮度</span><span className="tabular-nums text-slate-200">{Math.round(bright * 100)}%</span>
          </div>
          <input type="range" min={0.3} max={1.1} step={0.02} value={bright} className="w-full accent-cyan-400"
            onChange={(e) => setBright(Number(e.target.value))} />
        </div>
        <label className="flex cursor-pointer items-center gap-2 text-slate-400">
          <input type="checkbox" checked={bubbles} className="accent-cyan-400"
            onChange={(e) => setBubbles(e.target.checked)} />
          走到地点弹出照片(沿途等距挑选,点缩略图剔除不想要的)
        </label>
        {bubbles && (data.bubbles || []).length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {(data.bubbles || []).map((b: any) => {
              const off = excl.has(b.id);
              return (
                <button key={b.id}
                  title={off ? "已剔除,点击恢复" : "点击剔除这张"}
                  className={off ? "opacity-30 grayscale" : "ring-1 ring-white/20"}
                  onClick={() => setExcl((s) => {
                    const n = new Set(s); n.has(b.id) ? n.delete(b.id) : n.add(b.id); return n;
                  })}>
                  <img src={b.thumb_url} className="h-12 w-12 rounded-md object-cover" />
                </button>
              );
            })}
          </div>
        )}
        <div className="flex items-center gap-2 pt-1">
          <button className="flex h-8 items-center gap-1.5 rounded-lg bg-cyan-500/20 px-3 text-[12px] font-medium text-cyan-200 transition-colors hover:bg-cyan-500/30"
            onClick={save}>
            <Save className="h-3.5 w-3.5" /> 保存参数
          </button>
          <button className="flex h-8 items-center gap-1.5 rounded-lg bg-white/[0.06] px-3 text-[12px] text-slate-300 transition-colors hover:bg-white/10"
            onClick={() => { t0Ref.current = performance.now(); }}>
            <RotateCcw className="h-3.5 w-3.5" /> 重播
          </button>
          {saved && <span className="text-[11.5px] text-emerald-400">✓ 已保存,渲染时生效</span>}
        </div>
        <div className="text-[11px] leading-relaxed text-slate-600">
          预览为近似效果(浏览器绘制);正式渲染用同一份参数逐帧生成,画质更细。
          比例按 9:16 预览,其他比例构图规则相同。
        </div>
      </div>
    </div>
  );
}

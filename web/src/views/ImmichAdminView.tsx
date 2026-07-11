/** Immich 库管理(§19)— 照片优先设计 + 架构升级(2026-07-11):
 *  zustand 缓存(重进相簿秒开、刷新恢复现场)、Shift 范围选择、
 *  类型/状态筛选片、相簿搜索;详情灯箱 ← → 翻页 / Esc。 */
import { Component, memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ArrowDownUp, ArrowLeft, Check, ChevronLeft, ChevronRight, Copy,
  ExternalLink, Heart, Loader2, MapPin, Search, Sparkles, X,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { api } from "../api";
import { useImmichStore, type ImItem } from "../store";

const fmtTime = (t?: any) => (t ? String(t).replace("T", " ").slice(0, 16) : "—");
const fmtDur = (d?: any) => {
  if (d == null || d === "") return "";
  if (typeof d === "number" || /^\d+$/.test(String(d))) {
    const sec = Math.round(Number(d) / 1000);
    return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  }
  const s = String(d);
  return s.includes(":") ? s.replace(/^0:/, "").replace(/\.\d+$/, "") : s;
};
const WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const dayLabel = (iso: string) => {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "未知日期";
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 · ${WEEK[d.getDay()]}`;
};

const FILTERS = [
  ["all", "全部"], ["image", "照片"], ["video", "视频"],
  ["nogps", "无 GPS"], ["nogeo", "未写地名"], ["unscored", "未评分"],
] as const;
type FilterKey = typeof FILTERS[number][0];
const applyFilter = (items: ImItem[], f: FilterKey) => {
  switch (f) {
    case "image": return items.filter((m) => m.type === "IMAGE");
    case "video": return items.filter((m) => m.type === "VIDEO");
    case "nogps": return items.filter((m) => !m.has_gps);
    case "nogeo": return items.filter((m) => m.has_gps && !m.geo_done);
    case "unscored": return items.filter((m) => !m.score_done);
    default: return items;
  }
};

/** 单个缩略格 — memo 化:500 格的相簿里,点筛选/勾选/任务轮询只重渲染
 *  真正变化的格子,而不是全量 500×8 层 DOM。注意不要在这里用
 *  backdrop-blur(500 份合成层是页面掉帧主因)。 */
const Tile = memo(function Tile({ m, on, onOpen, onCheck }: {
  m: ImItem; on: boolean;
  onOpen: (id: string) => void;
  onCheck: (id: string, shift: boolean) => void;
}) {
  return (
    <div
      className={cn("group relative aspect-square cursor-pointer overflow-hidden rounded-[3px] bg-slate-900",
        on && "rounded-lg ring-2 ring-cyan-400 ring-offset-2 ring-offset-[#0a0f1c]")}
      onClick={() => onOpen(m.id)}>
      <img src={m.thumb} loading="lazy" decoding="async"
        className={cn("h-full w-full object-cover transition-transform duration-200", on && "scale-[0.88]")} />
      <button
        className={cn("absolute left-1.5 top-1.5 z-10 flex h-5 w-5 items-center justify-center rounded-full transition-opacity",
          on ? "bg-cyan-400 text-slate-950"
            : "bg-black/60 text-white/80 opacity-0 hover:bg-black/80 group-hover:opacity-100")}
        onClick={(e) => { e.stopPropagation(); onCheck(m.id, e.shiftKey); }}>
        <Check className="h-3 w-3" strokeWidth={3} />
      </button>
      {m.type === "VIDEO" && (
        <span className="absolute bottom-1 right-1.5 rounded bg-black/65 px-1 py-px text-[9.5px] font-medium tabular-nums text-white/90">
          {fmtDur(m.duration)}
        </span>
      )}
      {m.favorite && <Heart className="absolute bottom-1 left-1.5 h-3 w-3 fill-rose-400 text-rose-400" />}
      <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-end gap-1 bg-gradient-to-b from-black/55 to-transparent px-1.5 pb-4 pt-1 opacity-0 transition-opacity group-hover:opacity-100">
        <span className="mr-auto pl-6 text-[9.5px] tabular-nums text-white/75">{fmtTime(m.taken_at).slice(11)}</span>
        {(m.rating ?? 0) > 0 && <span className="text-[9px] text-amber-300">★{m.rating}</span>}
        {m.geo_done && <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" title="已写地名" />}
        {m.score_done && <span className="h-1.5 w-1.5 rounded-full bg-fuchsia-400" title="已评分" />}
      </div>
    </div>
  );
});

class Boundary extends Component<{ children: ReactNode }, { err: string }> {
  state = { err: "" };
  static getDerivedStateFromError(e: any) { return { err: String(e?.message || e) }; }
  render() {
    if (this.state.err) {
      return (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          页面渲染出错:{this.state.err}
          <button className="ml-3 text-xs underline" onClick={() => this.setState({ err: "" })}>重试</button>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function ImmichAdminView() {
  return <Boundary><Inner /></Boundary>;
}

function Inner() {
  const { albums, itemsByAlbum, currentAlbumId, loadAlbums, openAlbum, loadItems } = useImmichStore();
  const album = albums.find((a) => a.id === currentAlbumId) ?? null;
  const cached = currentAlbumId ? itemsByAlbum[currentAlbumId] : undefined;
  const items = cached?.items ?? [];
  const loading = !!currentAlbumId && !cached;

  const [wallQuery, setWallQuery] = useState("");
  const [filter, setFilter] = useState<FilterKey>("all");
  const [desc, setDesc] = useState(false);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [task, setTask] = useState<any>(null);
  const [err, setErr] = useState("");
  const lastClick = useRef<string | null>(null);

  // 首载:相簿列表(缓存新鲜则零请求);恢复上次浏览的相簿
  useEffect(() => {
    loadAlbums();
    if (currentAlbumId) loadItems(currentAlbumId);
  }, []);

  const enterAlbum = (id: string) => {
    openAlbum(id); setSel(new Set()); setFilter("all"); lastClick.current = null;
    loadItems(id);              // 有缓存秒开,过期后台刷新
  };

  const openDetail = useCallback(async (id: string) => {
    setDetailId(id); setDetail(null);
    try { setDetail(await api<any>(`/api/immich/mgmt/asset/${id}`)); }
    catch (e: any) { setErr(e.message); }
  }, []);

  const filteredItems = useMemo(() => applyFilter(items, filter), [items, filter]);
  const days = useMemo(() => {
    const g = new Map<string, ImItem[]>();
    for (const m of filteredItems) {
      const k = String(m.taken_at || "").slice(0, 10) || "未知";
      if (!g.has(k)) g.set(k, []);
      g.get(k)!.push(m);
    }
    const keys = [...g.keys()].sort();
    if (desc) keys.reverse();
    return keys.map((k) => {
      const its = g.get(k)!;
      // 当天主城市:O(n) 众数(以前是 O(n²) 且写在 render 里,每帧白算)
      const cnt = new Map<string, number>();
      for (const m of its) if (m.city) cnt.set(m.city, (cnt.get(m.city) ?? 0) + 1);
      let city: string | undefined; let best = 0;
      for (const [c, n] of cnt) if (n > best) { best = n; city = c; }
      return { key: k, items: its, city };
    });
  }, [filteredItems, desc]);
  const flat = useMemo(() => days.flatMap((d) => d.items), [days]);
  const flatRef = useRef(flat);
  flatRef.current = flat;

  // 勾选(支持 Shift 范围选择)— useCallback 保持引用稳定,memo Tile 才不失效
  const clickCheck = useCallback((id: string, shift: boolean) => {
    setSel((s) => {
      const n = new Set(s);
      const all = flatRef.current;
      if (shift && lastClick.current && lastClick.current !== id) {
        const a = all.findIndex((m) => m.id === lastClick.current);
        const b = all.findIndex((m) => m.id === id);
        if (a >= 0 && b >= 0) {
          const on = !n.has(id);
          for (let i = Math.min(a, b); i <= Math.max(a, b); i++) {
            on ? n.add(all[i].id) : n.delete(all[i].id);
          }
          return n;
        }
      }
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
    lastClick.current = id;
  }, []);

  // 灯箱键盘
  useEffect(() => {
    if (!detailId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setDetailId(null); setDetail(null); }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        const i = flat.findIndex((m) => m.id === detailId);
        const j = e.key === "ArrowLeft" ? i - 1 : i + 1;
        if (i >= 0 && j >= 0 && j < flat.length) openDetail(flat[j].id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detailId, flat]);

  // 后台任务轮询;完成后强刷当前相簿(服务端缓存也已作废)
  useEffect(() => {
    if (!task?.running) return;
    const t = window.setInterval(async () => {
      try {
        const s = await api<any>("/api/workspace/task");
        setTask(s);
        if (!s.running) {
          window.clearInterval(t);
          if (currentAlbumId) loadItems(currentAlbumId, true);
          if (detailId) openDetail(detailId);
        }
      } catch { /* keep */ }
    }, 2500);
    return () => window.clearInterval(t);
  }, [task?.running]);

  const startTask = async (path: string, ids: string[], confirmMsg: string) => {
    if (!ids.length || !window.confirm(confirmMsg)) return;
    setErr("");
    try {
      await api(path, { method: "POST", body: JSON.stringify({ ids }) });
      setTask({ running: true, done: 0, total: ids.length });
    } catch (e: any) { setErr(e.message); }
  };
  const geoRefresh = (ids: string[]) => startTask("/api/immich/mgmt/geo_refresh", ids,
    `对 ${ids.length} 个资产刷新高德地名并写入描述?无 GPS 自动跳过,网格缓存下配额消耗很小。`);
  const aiScore = (ids: string[]) => startTask("/api/immich/mgmt/score", ids,
    `对 ${ids.length} 个资产 AI 评分(星级 + 评语回填)?每资产 1 次视觉调用,已评过自动跳过。`);
  const geoInfer = (ids: string[]) => startTask("/api/immich/mgmt/geo_infer", ids,
    `对 ${ids.length} 个无 GPS 资产按时间轴推测坐标?\n取前后 90 分钟内带 GPS 的邻居照片插值,写回 Immich;已有 GPS 的自动跳过。`);

  const shownAlbums = albums.filter((a) => !wallQuery || a.name.toLowerCase().includes(wallQuery.toLowerCase()));

  return (
    <div className="select-none">
      {err && <div className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2 text-sm text-red-400">{err}</div>}

      {/* ══ 相簿墙 ══ */}
      {!album && (
        <>
          <div className="mb-4 flex items-center gap-2">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
              <input
                className="h-9 w-64 rounded-full border border-white/10 bg-white/[0.04] pl-9 pr-3 text-[12.5px] text-slate-200 placeholder:text-slate-600 focus:border-cyan-500/40 focus:outline-none"
                placeholder="搜索相簿…" value={wallQuery} onChange={(e) => setWallQuery(e.target.value)} />
            </div>
            <span className="text-[11px] text-slate-600">{shownAlbums.length} 个相簿</span>
          </div>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-4">
            {shownAlbums.map((al) => (
              <button key={al.id}
                className="group relative aspect-[4/3] overflow-hidden rounded-2xl bg-slate-900 text-left ring-1 ring-white/[0.06] transition-all duration-300 hover:ring-white/25"
                onClick={() => enterAlbum(al.id)}>
                {al.thumb
                  ? <img src={al.thumb} className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-[1.04]" loading="lazy" />
                  : <div className="h-full w-full bg-gradient-to-br from-slate-800 to-slate-900" />}
                <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 via-black/40 to-transparent px-4 pb-3 pt-10">
                  <div className="truncate text-[14px] font-medium text-white">{al.name}</div>
                  <div className="mt-0.5 text-[11px] tabular-nums text-white/55">
                    {al.count} 项{al.start ? ` · ${al.start.split("-").join(".")}` : ""}
                    {al.end && al.end !== al.start ? ` – ${al.end.slice(5).replace("-", ".")}` : ""}
                  </div>
                </div>
              </button>
            ))}
            {albums.length === 0 && !err && (
              <div className="col-span-full py-20 text-center text-sm text-slate-500">
                <Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" /> 加载相簿…
              </div>
            )}
          </div>
        </>
      )}

      {/* ══ 相簿内 ══ */}
      {album && (
        <div>
          <div className="mb-3 flex items-center gap-3">
            <button className="flex h-8 w-8 items-center justify-center rounded-full text-slate-400 transition-colors hover:bg-white/[0.06] hover:text-white"
              onClick={() => { openAlbum(null); setSel(new Set()); }}>
              <ArrowLeft className="h-4 w-4" />
            </button>
            <div>
              <h2 className="text-[17px] font-semibold tracking-tight text-white">{album.name}</h2>
              <div className="text-[11px] text-slate-500">
                {filteredItems.length}{filter !== "all" ? ` / ${items.length}` : ""} 项 · {days.length} 天
                {cached && Date.now() - cached.at > 60_000 && (
                  <button className="ml-2 text-cyan-500 hover:underline" onClick={() => loadItems(album.id, true)}>刷新</button>
                )}
              </div>
            </div>
            <button
              className="ml-auto flex h-8 items-center gap-1.5 rounded-full px-3 text-[12px] text-slate-400 transition-colors hover:bg-white/[0.06] hover:text-white"
              onClick={() => setDesc((v) => !v)}>
              <ArrowDownUp className="h-3.5 w-3.5" /> {desc ? "最新在前" : "最早在前"}
            </button>
          </div>

          {/* 筛选片 */}
          <div className="mb-3 flex flex-wrap gap-1.5">
            {FILTERS.map(([k, label]) => (
              <button key={k}
                className={cn("h-7 rounded-full px-3 text-[11.5px] transition-colors",
                  filter === k ? "bg-cyan-500/20 font-medium text-cyan-200" : "bg-white/[0.04] text-slate-400 hover:bg-white/[0.08] hover:text-slate-200")}
                onClick={() => setFilter(k)}>
                {label}
              </button>
            ))}
            {filteredItems.length > 0 && (
              <button className="h-7 rounded-full bg-white/[0.04] px-3 text-[11.5px] text-slate-400 transition-colors hover:bg-white/[0.08] hover:text-slate-200"
                onClick={() => setSel(new Set(filteredItems.map((m) => m.id)))}>
                全选筛选结果
              </button>
            )}
            <span className="self-center text-[10.5px] text-slate-600">提示:按住 Shift 点勾选圆可范围选择</span>
          </div>

          {loading ? (
            <div className="py-24 text-center"><Loader2 className="mx-auto h-5 w-5 animate-spin text-slate-500" /></div>
          ) : days.map((day) => {
            const dayIds = day.items.map((m) => m.id);
            const allSel = dayIds.every((id) => sel.has(id));
            return (
              // content-visibility: 屏幕外的天整段跳过排版与绘制,长相簿滚动/交互不再全页结算
              <section key={day.key} className="mb-6"
                style={{ contentVisibility: "auto", containIntrinsicSize: "auto 500px" } as any}>
                <div className="sticky top-0 z-10 -mx-2 mb-2 flex items-baseline gap-2.5 bg-[#0a0f1c]/95 px-2 py-2">
                  <h3 className="text-[13.5px] font-medium text-slate-200">{dayLabel(day.key)}</h3>
                  {day.city && <span className="flex items-center gap-0.5 text-[11px] text-slate-500"><MapPin className="h-3 w-3" />{day.city}</span>}
                  <span className="text-[11px] text-slate-600">{day.items.length}</span>
                  <button className="ml-auto text-[11px] text-slate-500 transition-colors hover:text-cyan-300"
                    onClick={() => setSel((s) => {
                      const n = new Set(s);
                      dayIds.forEach((id) => allSel ? n.delete(id) : n.add(id));
                      return n;
                    })}>
                    {allSel ? "取消全选" : "选择当天"}
                  </button>
                </div>
                <div className="grid grid-cols-[repeat(auto-fill,minmax(118px,1fr))] gap-1">
                  {day.items.map((m) => (
                    <Tile key={m.id} m={m} on={sel.has(m.id)} onOpen={openDetail} onCheck={clickCheck} />
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      )}

      {/* ══ 悬浮选择操作条 ══ */}
      {sel.size > 0 && (
        <div className="fixed bottom-6 left-1/2 z-40 flex -translate-x-1/2 items-center gap-1 rounded-full border border-white/10 bg-slate-950/90 py-1.5 pl-4 pr-1.5 shadow-[0_8px_32px_rgba(0,0,0,0.6)] backdrop-blur-xl">
          <span className="mr-2 text-[13px] font-medium text-white">已选 {sel.size}</span>
          <button className="flex h-8 items-center gap-1.5 rounded-full px-3 text-[12px] text-emerald-300 transition-colors hover:bg-emerald-500/15"
            disabled={!!task?.running} onClick={() => geoRefresh([...sel])}>
            <MapPin className="h-3.5 w-3.5" /> 刷新地名
          </button>
          <button className="flex h-8 items-center gap-1.5 rounded-full px-3 text-[12px] text-fuchsia-300 transition-colors hover:bg-fuchsia-500/15"
            disabled={!!task?.running} onClick={() => aiScore([...sel])}>
            <Sparkles className="h-3.5 w-3.5" /> AI 评分
          </button>
          <button className="flex h-8 items-center gap-1.5 rounded-full px-3 text-[12px] text-sky-300 transition-colors hover:bg-sky-500/15"
            title="对选中里没有 GPS 的资产,用前后 90 分钟内带 GPS 的照片按时间插值坐标并写回 Immich"
            disabled={!!task?.running} onClick={() => geoInfer([...sel])}>
            🧭 推测 GPS
          </button>
          <button className="flex h-8 w-8 items-center justify-center rounded-full text-slate-400 transition-colors hover:bg-white/10 hover:text-white"
            onClick={() => setSel(new Set())}>
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* ══ 后台任务浮标 ══ */}
      {task && (
        <div className="fixed bottom-6 right-6 z-40 flex items-center gap-2 rounded-full border border-white/10 bg-slate-950/90 px-4 py-2 text-[12px] text-slate-200 shadow-lg backdrop-blur-xl">
          {task.running
            ? <><Loader2 className="h-3.5 w-3.5 animate-spin text-cyan-400" />{task.note || `${task.done}/${task.total}`}</>
            : <>
              <Check className="h-3.5 w-3.5 text-emerald-400" />
              完成 {task.result ? Object.entries(task.result).map(([k, v]) => `${k} ${v}`).join(" · ") : ""}
              <button className="ml-1 text-slate-500 hover:text-white" onClick={() => setTask(null)}><X className="h-3.5 w-3.5" /></button>
            </>}
        </div>
      )}

      {/* ══ 灯箱详情 ══ */}
      {detailId && (
        <div className="fixed inset-0 z-50 flex bg-black/90 backdrop-blur-sm"
          onClick={() => { setDetailId(null); setDetail(null); }}>
          {(() => {
            const i = flat.findIndex((m) => m.id === detailId);
            return (
              <>
                {i > 0 && (
                  <button className="absolute left-3 top-1/2 z-10 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full bg-white/[0.06] text-white/70 transition-colors hover:bg-white/15 hover:text-white"
                    onClick={(e) => { e.stopPropagation(); openDetail(flat[i - 1].id); }}>
                    <ChevronLeft className="h-5 w-5" />
                  </button>
                )}
                {i >= 0 && i < flat.length - 1 && (
                  <button className="absolute right-[364px] top-1/2 z-10 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full bg-white/[0.06] text-white/70 transition-colors hover:bg-white/15 hover:text-white"
                    onClick={(e) => { e.stopPropagation(); openDetail(flat[i + 1].id); }}>
                    <ChevronRight className="h-5 w-5" />
                  </button>
                )}
              </>
            );
          })()}
          <div className="flex min-w-0 flex-1 items-center justify-center p-8" onClick={(e) => e.stopPropagation()}>
            {detail
              ? <img src={detail.thumb} className="max-h-full max-w-full rounded-lg object-contain shadow-2xl" />
              : <Loader2 className="h-6 w-6 animate-spin text-slate-500" />}
          </div>
          <div className="flex w-[352px] shrink-0 flex-col border-l border-white/[0.07] bg-[#0a0f1c]" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-1 px-4 pb-2 pt-3">
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-white" title={detail?.name}>{detail?.name ?? "…"}</span>
              <button className="flex h-8 w-8 items-center justify-center rounded-full text-slate-400 transition-colors hover:bg-white/[0.06] hover:text-white"
                onClick={() => { setDetailId(null); setDetail(null); }}>
                <X className="h-4 w-4" />
              </button>
            </div>
            {detail && (
              <>
                <div className="flex gap-1.5 px-4 pb-3">
                  <button className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg bg-emerald-500/[0.12] text-[12px] text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:opacity-40"
                    disabled={!detail.has_gps || !!task?.running} onClick={() => geoRefresh([detail.id])}>
                    <MapPin className="h-3.5 w-3.5" /> 刷新地名
                  </button>
                  <button className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg bg-fuchsia-500/[0.12] text-[12px] text-fuchsia-300 transition-colors hover:bg-fuchsia-500/20 disabled:opacity-40"
                    disabled={!!task?.running} onClick={() => aiScore([detail.id])}>
                    <Sparkles className="h-3.5 w-3.5" /> AI 评分
                  </button>
                  <a className="flex h-8 w-9 items-center justify-center rounded-lg bg-white/[0.06] text-slate-300 transition-colors hover:bg-white/10 hover:text-white"
                    title="在 Immich 中打开" href={detail.immich_url} target="_blank" rel="noreferrer">
                    <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                </div>
                <ScrollArea className="min-h-0 flex-1">
                  <div className="space-y-5 px-4 pb-6">
                    {detail.geo ? (() => {
                      const levels = [detail.geo.province, detail.geo.city, detail.geo.district, detail.geo.township]
                        .filter((x: string, i: number, a: string[]) => x && a.indexOf(x) === i);
                      let poi = String(detail.geo.formatted || "");
                      for (const l of levels) { if (poi.startsWith(l)) poi = poi.slice(l.length); }
                      const hero = levels.slice(-2).join(" · ");
                      return (
                        <div>
                          <div className="text-[16px] font-semibold leading-snug text-white">{hero || detail.geo.label}</div>
                          <div className="mt-0.5 text-[11.5px] text-slate-500">{levels.slice(0, -2).join(" · ")}</div>
                          {poi && <div className="mt-1.5 text-[12.5px] leading-relaxed text-emerald-300/90">{poi}</div>}
                          <div className="mt-1.5 flex flex-wrap gap-x-3 font-mono text-[10.5px] text-slate-600">
                            <span>{detail.exif?.latitude?.toFixed?.(5)}, {detail.exif?.longitude?.toFixed?.(5)}</span>
                            <span>Immich: {[detail.exif?.city, detail.exif?.state].filter(Boolean).join("·") || "—"}</span>
                          </div>
                        </div>
                      );
                    })() : (
                      <div className="text-[13px] text-slate-500">
                        {detail.exif?.latitude != null
                          ? "有 GPS,尚未解析地名 — 点上方「刷新地名」"
                          : (
                            <span className="flex flex-wrap items-center gap-2">
                              没有位置信息
                              <button className="rounded-lg bg-sky-500/[0.12] px-2.5 py-1 text-[11.5px] text-sky-300 transition-colors hover:bg-sky-500/20 disabled:opacity-40"
                                disabled={!!task?.running}
                                title="用拍摄时间前后 90 分钟内带 GPS 的照片插值坐标,写回 Immich"
                                onClick={() => geoInfer([detail.id])}>
                                🧭 从相邻照片推测 GPS
                              </button>
                            </span>
                          )}
                      </div>
                    )}

                    {(detail.rating > 0 || detail.rating === -1 || detail.favorite || detail.score_done) && (
                      <div className="flex flex-wrap gap-1.5">
                        {detail.rating > 0 && <span className="rounded-md bg-amber-400/[0.12] px-2 py-1 text-[11.5px] text-amber-300">{"★".repeat(Math.min(5, detail.rating))}</span>}
                        {detail.rating === -1 && <span className="rounded-md bg-red-500/[0.12] px-2 py-1 text-[11.5px] text-red-300">已拒绝</span>}
                        {detail.favorite && <span className="rounded-md bg-rose-500/[0.12] px-2 py-1 text-[11.5px] text-rose-300">❤️ 收藏</span>}
                        {detail.score_done && <span className="rounded-md bg-fuchsia-500/[0.12] px-2 py-1 text-[11.5px] text-fuchsia-300">✨ 已评分</span>}
                      </div>
                    )}

                    <div>
                      <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-600">拍摄</div>
                      <div className="space-y-1.5 text-[12.5px]">
                        <div className="text-slate-200">{fmtTime(detail.taken_at)}</div>
                        {(detail.exif?.make || detail.exif?.model) && (
                          <div className="text-slate-400">{[detail.exif?.make, detail.exif?.model].filter(Boolean).join(" ")}</div>
                        )}
                        <div className="flex flex-wrap gap-x-3 font-mono text-[11px] text-slate-500">
                          {detail.exif?.exifImageWidth && <span>{detail.exif.exifImageWidth}×{detail.exif.exifImageHeight}</span>}
                          {detail.size_mb ? <span>{detail.size_mb} MB</span> : null}
                          {detail.type === "VIDEO" && fmtDur(detail.duration) && <span>{fmtDur(detail.duration)}</span>}
                          {detail.exif?.fNumber && <span>f/{detail.exif.fNumber}</span>}
                          {detail.exif?.iso && <span>ISO {detail.exif.iso}</span>}
                          {detail.exif?.exposureTime && <span>{detail.exif.exposureTime}s</span>}
                        </div>
                      </div>
                    </div>

                    <div>
                      <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-600">相簿</div>
                      <div className="flex flex-wrap gap-1.5">
                        {(detail.albums ?? []).map((a: any) => (
                          <span key={a.id} className="rounded-md bg-white/[0.05] px-2 py-1 text-[11.5px] text-slate-300">{a.name}</span>
                        ))}
                        {(detail.albums ?? []).length === 0 && <span className="text-[12px] text-slate-600">不在任何相簿</span>}
                      </div>
                    </div>

                    {detail.description && (
                      <div>
                        <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-600">描述</div>
                        <div className="whitespace-pre-wrap text-[11.5px] leading-relaxed text-slate-400">{detail.description}</div>
                      </div>
                    )}

                    <button className="flex items-center gap-1.5 font-mono text-[10px] text-slate-600 transition-colors hover:text-cyan-400"
                      onClick={() => navigator.clipboard?.writeText(detail.id)} title="复制资产 ID">
                      {detail.id} <Copy className="h-3 w-3" />
                    </button>
                  </div>
                </ScrollArea>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

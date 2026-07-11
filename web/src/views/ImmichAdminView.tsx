/** Immich 库管理页(§19):相簿墙 → 时间线网格 → 资产全景弹层。
 *  能力:高德地名刷新(单个/批量,回填描述 📍 行)、AI 评分(单个/批量,
 *  星级 + 描述块回填)。后台任务经 /api/workspace/task 轮询。 */
import { Component, useEffect, useState, type ReactNode } from "react";
import { ArrowLeft, Copy, ExternalLink, Loader2, MapPin, RotateCw, Sparkles, Star, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { api } from "../api";

type Album = { id: string; name: string; count: number; thumb?: string; start?: string; end?: string };
type Item = {
  id: string; name: string; type: string; taken_at: string; thumb: string;
  duration?: string; rating?: number; favorite?: boolean; has_gps?: boolean;
  city?: string; geo_done?: boolean; score_done?: boolean; size_mb?: number;
};

// 全部防御式:Immich 字段类型不稳(duration 有时是数字,时间偶见非字符串)
const fmtTime = (t?: any) => (t ? String(t).replace("T", " ").slice(0, 16) : "—");
const fmtDur = (d?: any) => {
  if (d == null || d === "") return "";
  // Immich 两种形态并存:字符串 "0:03:27.938" 或 毫秒整数 207938
  if (typeof d === "number" || /^\d+$/.test(String(d))) {
    const sec = Math.round(Number(d) / 1000);
    return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  }
  const s = String(d);
  return s.includes(":") ? s.replace(/^0:/, "").replace(/\.\d+$/, "") : s;
};

/** 局部错误边界:单个脏数据资产不再炸掉整页(白屏)。 */
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

function Stars({ n }: { n?: number }) {
  // Immich 星级可为 -1(拒绝);repeat 负数会抛异常炸整页
  const k = Math.max(0, Math.min(5, Number(n) || 0));
  if (!k) return null;
  return <span className="text-[10px] text-amber-300">{"★".repeat(k)}</span>;
}

export default function ImmichAdminView() {
  return <Boundary><ImmichAdminInner /></Boundary>;
}

function ImmichAdminInner() {
  const [albums, setAlbums] = useState<Album[]>([]);
  const [album, setAlbum] = useState<Album | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [loading, setLoading] = useState(false);
  const [sortAsc, setSortAsc] = useState(true);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [task, setTask] = useState<any>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api<{ albums: Album[] }>("/api/immich/albums").then((r) => setAlbums(r.albums ?? [])).catch((e) => setErr(e.message));
  }, []);

  const openAlbum = async (al: Album) => {
    setAlbum(al); setItems([]); setSel(new Set()); setLoading(true);
    try {
      const r = await api<{ items: Item[] }>(`/api/immich/mgmt/album/${al.id}`);
      setItems(r.items ?? []);
    } catch (e: any) { setErr(e.message); }
    setLoading(false);
  };

  const openDetail = async (id: string) => {
    setDetailId(id); setDetail(null);
    try { setDetail(await api<any>(`/api/immich/mgmt/asset/${id}`)); }
    catch (e: any) { setErr(e.message); }
  };

  // 后台任务轮询(地名/评分共用工作区任务槽)
  useEffect(() => {
    if (!task?.running) return;
    const t = window.setInterval(async () => {
      try {
        const s = await api<any>("/api/workspace/task");
        setTask(s);
        if (!s.running) {
          window.clearInterval(t);
          if (album) openAlbum(album);           // 刷新标记
          if (detailId) openDetail(detailId);
        }
      } catch { /* keep */ }
    }, 2500);
    return () => window.clearInterval(t);
  }, [task?.running]);

  const startTask = async (path: string, ids: string[], confirmMsg: string, extra: any = {}) => {
    if (!ids.length) return;
    if (!window.confirm(confirmMsg)) return;
    setErr("");
    try {
      await api(path, { method: "POST", body: JSON.stringify({ ids, ...extra }) });
      setTask({ running: true, done: 0, total: ids.length });
    } catch (e: any) { setErr(e.message); }
  };

  const geoRefresh = (ids: string[]) => startTask(
    "/api/immich/mgmt/geo_refresh", ids,
    `对 ${ids.length} 个资产刷新高德地名并把完整地址写进描述(📍 行,重复执行会更新)?\n无 GPS 的自动跳过;网格缓存下 API 消耗很小。`);
  const aiScore = (ids: string[]) => startTask(
    "/api/immich/mgmt/score", ids,
    `对 ${ids.length} 个资产 AI 评分(星级 + 描述块回填)?\n计费:每资产 1 次视觉调用;已评过的自动跳过。视频按封面帧评分。`);

  const shown = sortAsc ? items : [...items].reverse();
  const selItems = items.filter((i) => sel.has(i.id));

  return (
    <div>
      {err && <div className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2 text-sm text-red-400">{err}</div>}
      {task && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-cyan-500/25 bg-cyan-500/[0.06] px-4 py-2 text-xs text-cyan-200">
          {task.running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "✓"}
          {task.running
            ? <>后台任务:{task.note || `${task.done}/${task.total}`}</>
            : <>完成:{JSON.stringify(task.result ?? {})}<button className="ml-2 text-slate-400 hover:text-slate-200" onClick={() => setTask(null)}>×</button></>}
        </div>
      )}

      {!album ? (
        // ── 相簿墙 ──
        <div className="grid grid-cols-[repeat(auto-fill,minmax(190px,1fr))] gap-3">
          {albums.map((al) => (
            <button key={al.id}
              className="group overflow-hidden rounded-xl border border-white/[0.07] bg-slate-900/60 text-left transition-colors hover:border-cyan-500/40"
              onClick={() => openAlbum(al)}>
              <div className="h-28 w-full overflow-hidden bg-black/40">
                {al.thumb && <img src={al.thumb} className="h-full w-full object-cover transition-transform group-hover:scale-105" loading="lazy" />}
              </div>
              <div className="p-2.5">
                <div className="truncate text-[13px] font-medium text-slate-200">{al.name}</div>
                <div className="text-[11px] text-slate-500">{al.count} 项 · {al.start}{al.end && al.end !== al.start ? ` ~ ${al.end}` : ""}</div>
              </div>
            </button>
          ))}
        </div>
      ) : (
        // ── 相簿内:时间线网格 ──
        <div>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" className="h-8 gap-1 border-white/10 bg-white/[0.04]"
              onClick={() => { setAlbum(null); setSel(new Set()); }}>
              <ArrowLeft className="h-3.5 w-3.5" /> 相簿
            </Button>
            <span className="text-sm font-semibold text-slate-200">{album.name}</span>
            <span className="text-xs text-slate-500">{items.length} 项 · 按拍摄时间{sortAsc ? "升序" : "降序"}</span>
            <button className="text-xs text-cyan-400 hover:underline" onClick={() => setSortAsc((v) => !v)}>切换排序</button>
            <span className="ml-auto" />
            <Button variant="outline" size="sm" className="h-8 border-white/10 bg-white/[0.04] text-xs"
              onClick={() => setSel(new Set(items.map((i) => i.id)))}>全选</Button>
            <Button variant="outline" size="sm" className="h-8 border-white/10 bg-white/[0.04] text-xs"
              onClick={() => setSel(new Set())}>清空</Button>
            <Button variant="outline" size="sm" className="h-8 gap-1 border-emerald-500/30 bg-emerald-500/[0.08] text-xs text-emerald-300"
              disabled={sel.size === 0 || task?.running}
              onClick={() => geoRefresh([...sel])}>
              <MapPin className="h-3 w-3" /> 批量刷新地名 ({sel.size})
            </Button>
            <Button variant="outline" size="sm" className="h-8 gap-1 border-fuchsia-500/30 bg-fuchsia-500/[0.08] text-xs text-fuchsia-300"
              disabled={sel.size === 0 || task?.running}
              onClick={() => aiScore([...sel])}>
              <Sparkles className="h-3 w-3" /> 批量 AI 评分 ({sel.size})
            </Button>
          </div>
          {loading ? (
            <div className="py-16 text-center text-sm text-slate-500"><Loader2 className="mx-auto h-5 w-5 animate-spin" /></div>
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-2">
              {shown.map((m) => (
                <div key={m.id}
                  className={cn("group relative cursor-pointer overflow-hidden rounded-lg border bg-slate-900/60",
                    sel.has(m.id) ? "border-cyan-400/70" : "border-white/[0.07] hover:border-white/25")}
                  onClick={() => openDetail(m.id)}>
                  <input type="checkbox" checked={sel.has(m.id)}
                    className="absolute left-1.5 top-1.5 z-10 h-4 w-4 accent-cyan-400"
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => setSel((s) => { const n = new Set(s); n.has(m.id) ? n.delete(m.id) : n.add(m.id); return n; })} />
                  <div className="h-24 w-full bg-black/40">
                    <img src={m.thumb} className="h-full w-full object-cover" loading="lazy" />
                  </div>
                  {m.type === "VIDEO" && <span className="absolute right-1.5 top-1.5 rounded bg-black/60 px-1 text-[9px] text-slate-200">▶ {fmtDur(m.duration)}</span>}
                  <div className="px-1.5 py-1 text-[10px] leading-tight">
                    <div className="flex items-center gap-1">
                      <span className="text-slate-400">{fmtTime(m.taken_at).slice(5)}</span>
                      <Stars n={m.rating} />
                      {m.favorite && <span>❤️</span>}
                    </div>
                    <div className="flex items-center gap-1 text-slate-500">
                      {m.has_gps ? <span className={m.geo_done ? "text-emerald-400" : ""}>📍{m.geo_done ? "✓" : ""}</span> : null}
                      {m.score_done && <span className="text-fuchsia-400">评✓</span>}
                      <span className="truncate">{m.city || ""}</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── 资产全景弹层 ── */}
      {detailId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6"
          onClick={() => { setDetailId(null); setDetail(null); }}>
          <div className="flex max-h-[88vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-white/10 bg-slate-950"
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 border-b border-white/[0.07] px-4 py-2.5">
              <span className="truncate text-sm font-semibold text-slate-200">{detail?.name ?? "加载中…"}</span>
              {detail?.type && <Badge variant="outline" className="border-white/15 text-[10px] text-slate-400">{detail.type}</Badge>}
              <span className="ml-auto" />
              {detail && (
                <>
                  <Button variant="outline" size="sm" className="h-7 gap-1 border-emerald-500/30 bg-emerald-500/[0.08] text-[11px] text-emerald-300"
                    disabled={!detail.has_gps || task?.running} onClick={() => geoRefresh([detail.id])}>
                    <MapPin className="h-3 w-3" /> 刷新地名
                  </Button>
                  <Button variant="outline" size="sm" className="h-7 gap-1 border-fuchsia-500/30 bg-fuchsia-500/[0.08] text-[11px] text-fuchsia-300"
                    disabled={task?.running} onClick={() => aiScore([detail.id])}>
                    <Sparkles className="h-3 w-3" /> AI 评分
                  </Button>
                  <a className="flex h-7 items-center gap-1 rounded-md border border-white/10 bg-white/[0.04] px-2 text-[11px] text-slate-300 hover:bg-white/[0.08]"
                    href={detail.immich_url} target="_blank" rel="noreferrer">
                    <ExternalLink className="h-3 w-3" /> Immich
                  </a>
                </>
              )}
              <button className="text-slate-500 hover:text-slate-300" onClick={() => { setDetailId(null); setDetail(null); }}>
                <X className="h-4 w-4" />
              </button>
            </div>
            {!detail ? (
              <div className="py-16 text-center"><Loader2 className="mx-auto h-5 w-5 animate-spin text-slate-500" /></div>
            ) : (
              <div className="flex min-h-0 flex-1 flex-wrap gap-4 overflow-hidden p-4">
                <div className="min-w-[280px] flex-1">
                  <img src={detail.thumb} className="max-h-[52vh] w-full rounded-lg bg-black object-contain" />
                  <div className="mt-1 text-[11px] text-slate-500">
                    {detail.type === "VIDEO" ? "视频封面预览 — 播放请点右上 Immich" : ""}
                  </div>
                </div>
                <ScrollArea className="max-h-[62vh] min-w-[300px] flex-1">
                  <table className="w-full text-[12px]">
                    <tbody>
                      {([
                        ["拍摄时间", fmtTime(detail.taken_at)],
                        ["星级", detail.rating > 0 ? "★".repeat(Math.min(5, detail.rating)) : detail.rating === -1 ? "已拒绝" : "—"],
                        ["收藏", detail.favorite ? "❤️" : "—"],
                        ["大小", detail.size_mb ? `${detail.size_mb} MB` : "—"],
                        ["时长", fmtDur(detail.duration) || "—"],
                        ["相机", [detail.exif?.make, detail.exif?.model].filter(Boolean).join(" ") || "—"],
                        ["尺寸", detail.exif?.exifImageWidth ? `${detail.exif.exifImageWidth}×${detail.exif.exifImageHeight}` : "—"],
                        ["GPS", detail.exif?.latitude != null ? `${detail.exif.latitude?.toFixed?.(5)}, ${detail.exif.longitude?.toFixed?.(5)}` : "无"],
                        ["Immich 地名", [detail.exif?.city, detail.exif?.state].filter(Boolean).join(" · ") || "—"],
                        ["高德地名", detail.geo?.formatted || detail.geo?.label || "—"],
                        ["所属相簿", (detail.albums ?? []).map((a: any) => a.name).join("、") || "—"],
                      ] as const).map(([k, v]) => (
                        <tr key={k} className="border-b border-white/[0.05]">
                          <td className="w-24 py-1.5 pr-2 align-top text-slate-500">{k}</td>
                          <td className={cn("py-1.5 text-slate-300", k === "高德地名" && "text-emerald-300")}>{v}</td>
                        </tr>
                      ))}
                      <tr>
                        <td className="w-24 py-1.5 pr-2 align-top text-slate-500">资产 ID</td>
                        <td className="py-1.5">
                          <button className="font-mono text-[11px] text-cyan-400 hover:underline"
                            title="点击复制"
                            onClick={() => navigator.clipboard?.writeText(detail.id)}>
                            {detail.id} <Copy className="inline h-3 w-3" />
                          </button>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                  {detail.description && (
                    <div className="mt-2 whitespace-pre-wrap rounded-lg border border-white/[0.06] bg-white/[0.02] p-2.5 text-[11.5px] text-slate-300">
                      {detail.description}
                    </div>
                  )}
                </ScrollArea>
              </div>
            )}
          </div>
        </div>
      )}

      {!album && albums.length === 0 && !err && (
        <div className="py-16 text-center text-sm text-slate-500">
          <RotateCw className="mx-auto mb-2 h-5 w-5 animate-spin" /> 加载相簿…
        </div>
      )}
      {!album && <div className="mt-3 text-[11px] text-slate-600">
        <Star className="mr-1 inline h-3 w-3" />
        地名刷新走高德(网格缓存,消耗见「📍 地名日志」);AI 评分每资产 1 次视觉调用,星级/描述直接写回 Immich。
      </div>}
    </div>
  );
}

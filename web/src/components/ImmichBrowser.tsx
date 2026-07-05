/**
 * Immich library browser — browse/CLIP-search the user's real media library
 * (6k+ videos) and import selected assets' playback PROXIES (a few MB each)
 * into the asset root for annotation. Originals are never copied.
 */
import { useEffect, useState } from "react";
import { Check, Download, Loader2, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { api } from "../api";

interface ImmichItem {
  id: string; name: string; duration: string;
  width?: number; height?: number; taken_at?: string; thumb: string;
}

function fmtDur(d: string): string {
  // Immich duration like "00:00:19.569" or millis number
  const m = /^(\d+):(\d+):(\d+)/.exec(d || "");
  if (m) {
    const s = (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
    return s >= 60 ? `${Math.floor(s / 60)}m${s % 60}s` : `${s}s`;
  }
  const n = Number(d);
  return Number.isFinite(n) && n > 0 ? `${Math.round(n / 1000)}s` : "";
}

export default function ImmichBrowser({ onClose, onImported }: {
  onClose: () => void;
  onImported: () => void;   // caller rescans the asset library
}) {
  const [status, setStatus] = useState<{ version?: string; videos?: number } | null>(null);
  const [statusErr, setStatusErr] = useState("");
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<ImmichItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState("");

  const search = async (q: string) => {
    setLoading(true); setResult("");
    try {
      const r = await api<{ items: ImmichItem[] }>("/api/immich/search", {
        method: "POST", body: JSON.stringify({ query: q, size: 36 }),
      });
      setItems(r.items);
    } catch (e: any) { setResult(`搜索失败：${e.message}`); }
    setLoading(false);
  };

  useEffect(() => {
    api<any>("/api/immich/status")
      .then((s) => { setStatus(s); search(""); })
      .catch((e) => setStatusErr(e.message || "无法连接 Immich"));
  }, []);

  const toggle = (id: string) => setPicked((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });

  const doImport = async () => {
    if (picked.size === 0) return;
    setImporting(true); setResult("");
    try {
      const r = await api<{ imported: string[]; skipped: string[]; errors: string[] }>(
        "/api/immich/import",
        { method: "POST", body: JSON.stringify({ ids: [...picked] }) },
      );
      setResult(`✓ 导入 ${r.imported.length} 个代理` +
        (r.skipped.length ? ` · 跳过已存在 ${r.skipped.length}` : "") +
        (r.errors.length ? ` · 失败 ${r.errors.length}: ${r.errors[0]}` : ""));
      setPicked(new Set());
      onImported();
    } catch (e: any) { setResult(`导入失败：${e.message}`); }
    setImporting(false);
  };

  return (
    <Sheet open onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="w-full overflow-y-auto border-white/10 bg-slate-950 p-5 sm:max-w-[880px]"
        style={{ backdropFilter: "none", WebkitBackdropFilter: "none" }}>
        <SheetHeader className="p-0 pb-3">
          <SheetTitle className="flex items-center gap-2 text-sm">
            🖼 Immich 库
            {status && (
              <span className="text-xs font-normal text-slate-500">
                v{status.version} · {status.videos?.toLocaleString()} 个视频
              </span>
            )}
          </SheetTitle>
        </SheetHeader>

        {statusErr ? (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-400">
            {statusErr} — 检查 ⚙ 设置里的 IMMICH_URL / IMMICH_API_KEY
          </div>
        ) : (
          <>
            <div className="mb-3 flex gap-2">
              <div className="relative flex-1">
                <Search className="absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
                <Input
                  className="h-9 border-white/10 bg-black/25 pl-8 text-xs"
                  placeholder="CLIP 语义搜索（英文效果最佳，如 two women running in flower field）— 留空显示最新"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && search(query)}
                />
              </div>
              <Button variant="outline" className="h-9 border-white/10 bg-white/[0.04]"
                onClick={() => search(query)} disabled={loading}>
                {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : "搜索"}
              </Button>
            </div>

            <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 md:grid-cols-4">
              {items.map((it) => (
                <button
                  key={it.id}
                  className={cn(
                    "group relative overflow-hidden rounded-lg border text-left transition-all",
                    picked.has(it.id)
                      ? "border-cyan-400/70 shadow-[0_0_12px_rgba(34,211,238,0.25)]"
                      : "border-white/[0.08] hover:border-white/25",
                  )}
                  onClick={() => toggle(it.id)}
                >
                  <img src={it.thumb} loading="lazy"
                    className="aspect-video w-full object-cover" />
                  {picked.has(it.id) && (
                    <span className="absolute top-1.5 left-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-cyan-400 text-slate-950">
                      <Check className="h-3.5 w-3.5" />
                    </span>
                  )}
                  {fmtDur(it.duration) && (
                    <span className="absolute right-1 bottom-6 rounded bg-black/70 px-1 font-mono text-[10px] text-slate-200">
                      {fmtDur(it.duration)}
                    </span>
                  )}
                  <div className="truncate bg-black/50 px-1.5 py-0.5 text-[10.5px] text-slate-300">
                    {it.name}
                  </div>
                </button>
              ))}
            </div>
            {items.length === 0 && !loading && (
              <div className="py-10 text-center text-sm text-slate-500">没有结果</div>
            )}

            {/* import bar */}
            <div className="sticky bottom-0 mt-4 flex items-center gap-2 rounded-xl border border-white/10 bg-slate-900/95 px-4 py-2.5">
              <span className="text-xs text-slate-400">已选 {picked.size} 个</span>
              {result && <span className="min-w-0 flex-1 truncate text-xs text-emerald-400">{result}</span>}
              <div className="ml-auto flex gap-2">
                {picked.size > 0 && (
                  <Button variant="outline" size="sm" className="h-8 border-white/10 bg-white/[0.04] text-xs"
                    onClick={() => setPicked(new Set())}>
                    <X className="mr-1 h-3 w-3" />清空
                  </Button>
                )}
                <Button size="sm"
                  className="h-8 gap-1.5 bg-cyan-500 text-xs font-semibold text-slate-950 hover:bg-cyan-400"
                  disabled={picked.size === 0 || importing}
                  onClick={doImport}>
                  {importing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                  导入代理到素材库 ({picked.size})
                </Button>
              </div>
            </div>
            <p className="mt-2 text-[11px] text-slate-600">
              导入的是 Immich 转码代理（约 3-10MB/个），标注与剪辑全程使用代理；4K 原片仅在渲染入选片段时按需读取。
            </p>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

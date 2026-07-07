"""Cold-backup manifest: make the UUID blob mirror on the backup disk findable.

The FreeFileSync mirror preserves Immich's content-addressed layout
(upload/<user>/<xx>/<yy>/<uuid>.MP4) — perfect for whole-library disaster
recovery, useless for "find that one aerial master". This script, run while
the cold disk is plugged, writes onto the disk itself:

  F:\\immich-library\\清单.html   searchable offline index (name/date/album →
                                  cold path; works in any browser, no server)
  F:\\immich-library\\清单.csv    same data for Excel/scripts
  F:\\by-name\\<相簿>\\<日期>_<原名>   NTFS HARDLINKS to the video blobs —
                                  a human-readable tree over the same bytes,
                                  ZERO extra space, plays directly

Re-run on every backup session (after FreeFileSync) to pick up new files.
Existing links/entries are refreshed idempotently.

Usage:
  python scripts/cold_manifest.py                # manifest + video hardlinks
  python scripts/cold_manifest.py --no-links     # manifest only
"""
import argparse
import csv
import html
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import config  # noqa: E402

import requests  # noqa: E402

BASE = config.IMMICH_URL.rstrip("/")
KEY = config.IMMICH_API_KEY
COLD_ROOT = r"F:\immich-library"
LINK_ROOT = r"F:\by-name"


def req(path: str, method: str = "GET", body=None):
    r = requests.request(method, BASE + "/api" + path,
                         headers={"x-api-key": KEY}, json=body, timeout=120)
    r.raise_for_status()
    return r.json() if r.text else {}


def cold_path(original_path: str) -> str:
    rel = original_path
    if rel.startswith("/data/"):
        rel = rel[len("/data/"):]
    return os.path.join(COLD_ROOT, rel.replace("/", os.sep))


def sanitize(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip() or "_"


def _next_page(resp_assets) -> int | None:
    """nextPage comes back as a STRING ('2') — feeding it back raw 400s."""
    np = resp_assets.get("nextPage")
    try:
        return int(np) if np else None
    except (TypeError, ValueError):
        return None


SHRINK_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "Output", "asset_index", "shrink_log.jsonl")


def shrunk_master_rows() -> list:
    """Masters replaced by the shrink tool: Immich forgets them once the trash
    empties, but their cold copies are exactly what the user will hunt for —
    the audit log keeps them in the manifest forever."""
    rows = []
    try:
        with open(SHRINK_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not d.get("ok") or not d.get("cold_path"):
                    continue
                rows.append({
                    "id": d.get("old_id") or "",
                    "name": d.get("name") or "",
                    "type": "VIDEO",
                    "taken": d.get("taken") or "",
                    "album": f"{d.get('album') or ''} · 已瘦身母带",
                    "size_mb": round((d.get("bytes_before") or 0) / 2**20, 1),
                    "camera": "",
                    "path": "",
                    "live": False,   # gone from Immich once the trash empties
                    "_cold_override": d["cold_path"],
                })
    except FileNotFoundError:
        pass
    return rows


def fetch_all_assets():
    """All assets (photos + videos) with album names attached."""
    print("拉取相簿成员关系 …", flush=True)
    album_of: dict = {}
    for al in req("/albums") or []:
        name = al.get("albumName") or "未命名相簿"
        page: int | None = 1
        while page:
            r = req("/search/metadata", "POST",
                    {"albumIds": [al["id"]], "size": 1000, "page": page})
            a = r.get("assets") or {}
            for it in a.get("items", []):
                album_of.setdefault(it["id"], name)   # first album wins
            page = _next_page(a)

    print("拉取全部资产 …", flush=True)
    out = []
    page = 1
    while page:
        r = req("/search/metadata", "POST", {"size": 1000, "page": page, "withExif": True})
        a = r.get("assets") or {}
        items = a.get("items", [])
        for it in items:
            ex = it.get("exifInfo") or {}
            out.append({
                "id": it["id"],
                "name": it.get("originalFileName") or "",
                "type": it.get("type") or "",
                "taken": (it.get("fileCreatedAt") or "")[:19].replace("T", " "),
                "album": album_of.get(it["id"], ""),
                "size_mb": round((ex.get("fileSizeInByte") or 0) / 2**20, 1),
                "camera": (str(ex.get("model") or "")).strip(),
                "path": it.get("originalPath") or "",
            })
        page = _next_page(a)
        print(f"  … {len(out)}", flush=True)
    return out


def _cold_of(r: dict) -> str:
    return r.get("_cold_override") or cold_path(r["path"])


def write_manifest(rows: list) -> None:
    csv_path = os.path.join(COLD_ROOT, "清单.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["文件名", "类型", "拍摄时间", "相簿", "大小MB", "相机", "资产ID", "冷盘路径", "在位"])
        for r in rows:
            cp = _cold_of(r)
            w.writerow([r["name"], r["type"], r["taken"], r["album"], r["size_mb"],
                        r["camera"], r["id"], cp, "Y" if os.path.exists(cp) else "N"])

    data = [{**r, "cold": _cold_of(r), "on_disk": os.path.exists(_cold_of(r))}
            for r in rows]
    for d in data:
        d.pop("_cold_override", None)
    html_path = os.path.join(COLD_ROOT, "清单.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write("""<!doctype html><meta charset=utf-8><title>冷备份清单</title>
<style>body{font:13px system-ui;margin:20px;background:#0f172a;color:#cbd5e1}
input{width:420px;padding:6px 10px;font-size:14px;background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:6px}
table{border-collapse:collapse;margin-top:12px;width:100%}
td,th{padding:3px 8px;border-bottom:1px solid #1e293b;text-align:left;white-space:nowrap}
th{color:#67e8f9;position:sticky;top:0;background:#0f172a}
.miss{color:#f87171}.path{color:#64748b;font-family:monospace;font-size:11px}
.hint{color:#64748b;margin:6px 0}
.btn{display:inline-block;padding:1px 8px;margin-right:4px;font-size:11px;color:#67e8f9;
background:#164e63;border:1px solid #155e75;border-radius:5px;text-decoration:none;cursor:pointer}
.btn:hover{background:#155e75}button.btn{font:inherit}
#ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9;
flex-direction:column;align-items:center;justify-content:center;gap:8px}
#ov video{max-width:92vw;max-height:82vh;background:#000;border-radius:8px}
#ov .bar{color:#94a3b8;font-size:12px}#ov .bar b{color:#e2e8f0}</style>
<h2>冷备份清单</h2>
<div class=hint>生成于 """ + time.strftime("%Y-%m-%d %H:%M") + """ · 共 """ + str(len(data)) + """ 项 · 输入即筛(文件名/日期/相簿/相机)</div>
<input id=q placeholder="例如: DJI_2026 / 新疆 / OsmoPocket / 从 Immich 网址复制的资产 ID" autofocus>
<div class=hint>提示: 网址 /photos/ 后面那串就是资产 ID,粘贴直达母带路径 · 点表头排序(再点反向) — 按「大小」降序即得瘦身候选清单</div>
<div id=ov><div class=bar><b id=ovn></b> · 点空白处或按 Esc 关闭 · 播不动时用 📋 复制路径开本地播放器</div><video id=ovv controls></video></div>
<table><thead><tr id=hd>
<th data-k=name>文件名<th data-k=taken>拍摄时间<th data-k=album>相簿<th data-k=size_mb>大小MB<th data-k=camera>相机<th>资产ID<th>操作<th>冷盘路径
</tr></thead><tbody id=tb></tbody></table>
<script>const D=""" + json.dumps(data, ensure_ascii=False) + """;
const IM=""" + json.dumps(BASE) + """;
const tb=document.getElementById('tb'),q=document.getElementById('q');
let sortK=null,sortDir=-1;
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fileUrl(p){return 'file:///'+encodeURI(p.replace(/\\\\/g,'/')).replace(/#/g,'%23')}
function copyPath(btn,p){
  // file:// 页面拿不到 navigator.clipboard — 退回 execCommand
  const ta=document.createElement('textarea');ta.value=p;document.body.appendChild(ta);
  ta.select();try{document.execCommand('copy')}catch(e){}
  document.body.removeChild(ta);
  const t=btn.textContent;btn.textContent='✓ 已复制';setTimeout(()=>btn.textContent=t,1200)}
function render(){const t=q.value.trim().toLowerCase();
let rows=D.filter(r=>!t||(r.name+' '+r.taken+' '+r.album+' '+r.camera+' '+r.id).toLowerCase().includes(t));
if(sortK){rows=[...rows].sort((a,b)=>{const x=a[sortK],y=b[sortK];
return (typeof x==='number'?x-y:String(x).localeCompare(String(y)))*sortDir})}
let n=0,h='';
for(const r of rows){if(++n>500){h+='<tr><td colspan=8 class=hint>…还有更多,请细化搜索</td></tr>';break}
const acts=(r.on_disk?`<button class=btn data-v="${esc(r.cold)}" data-n="${esc(r.name)}" title="页面内播放冷盘文件(MOV/MP4 都走媒体管线,不会触发下载)">▶ 播放</button> `:'')
  +(r.live!==false?`<a class=btn href="${IM}/photos/${r.id}" target=_blank title="在 Immich 中打开">🖼 Immich</a> `:'')
  +`<button class=btn data-p="${esc(r.cold)}" title="复制冷盘完整路径">📋 复制</button>`;
h+=`<tr><td>${esc(r.name)}<td>${esc(r.taken)}<td>${esc(r.album)}<td>${r.size_mb}<td>${esc(r.camera)}<td class=path title="${esc(r.id)}">${esc(r.id.slice(0,8))}<td>${acts}<td class="path${r.on_disk?'':' miss'}">${esc(r.cold)}${r.on_disk?'':' (缺失)'}</td></tr>`}
tb.innerHTML=h;
for(const th of document.querySelectorAll('#hd th')){const k=th.dataset.k;
th.textContent=th.textContent.replace(/ [▲▼]$/,'')+(k===sortK?(sortDir<0?' ▼':' ▲'):'')}}
const ov=document.getElementById('ov'),ovv=document.getElementById('ovv'),ovn=document.getElementById('ovn');
function playVid(p,n){ovn.textContent=n;ovv.src=fileUrl(p);ov.style.display='flex';ovv.play().catch(()=>{})}
function closeOv(){ov.style.display='none';ovv.pause();ovv.removeAttribute('src');ovv.load()}
ov.addEventListener('click',e=>{if(e.target===ov||e.target.classList.contains('bar'))closeOv()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeOv()});
tb.addEventListener('click',e=>{
  const v=e.target.closest('button[data-v]');if(v){playVid(v.dataset.v,v.dataset.n);return}
  const b=e.target.closest('button[data-p]');if(b)copyPath(b,b.dataset.p)});
document.getElementById('hd').addEventListener('click',e=>{
const k=e.target.dataset&&e.target.dataset.k;if(!k)return;
if(sortK===k)sortDir=-sortDir;else{sortK=k;sortDir=k==='size_mb'?-1:1}
render()});
document.querySelectorAll('#hd th[data-k]').forEach(th=>th.style.cursor='pointer');
q.addEventListener('input',render);render();</script>""")
    print(f"清单: {csv_path}")
    print(f"      {html_path}")


def make_links(rows: list) -> None:
    made = skipped = missing = 0
    for r in rows:
        if r["type"] != "VIDEO":
            continue
        src = cold_path(r["path"])
        if not os.path.exists(src):
            missing += 1
            continue
        album = sanitize(r["album"] or (r["taken"][:4] or "未分类"))
        date = (r["taken"][:10] or "").replace("-", "")
        link_dir = os.path.join(LINK_ROOT, album)
        link = os.path.join(link_dir, f"{date}_{sanitize(r['name'])}" if date else sanitize(r["name"]))
        if os.path.exists(link):
            skipped += 1
            continue
        os.makedirs(link_dir, exist_ok=True)
        try:
            os.link(src, link)      # NTFS hardlink — zero extra space
            made += 1
        except OSError as e:
            print(f"  ⚠ 链接失败 {r['name']}: {e}")
    print(f"硬链接树 {LINK_ROOT}: 新建 {made} · 已有 {skipped} · 冷盘缺失 {missing}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--links", action="store_true",
                    help="另建 F:\\by-name 硬链接树(按相簿浏览母带,零额外空间;可选)")
    args = ap.parse_args()
    if not os.path.isdir(COLD_ROOT):
        sys.exit(f"❌ 冷备份盘不在位: {COLD_ROOT}")
    rows = fetch_all_assets() + shrunk_master_rows()
    write_manifest(rows)
    if args.links:
        make_links(rows)


if __name__ == "__main__":
    main()

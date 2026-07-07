"""Immich master shrink (Phase 1): replace huge originals with HEVC transcodes.

The three-tier storage plan (docs in E:\\freefilesync\\备份与恢复指南.md):
masters live on the cold disk, Immich keeps a visually-transparent working
copy, CutClaw analysis stays keyed to content. This tool executes the swap.

SAFETY MODEL (non-negotiable):
- Per file, the COLD copy on the backup disk must exist AND its SHA-1 must
  equal the Immich checksum before anything is touched. FreeFileSync compares
  time+size only — not good enough to justify deleting an original.
- Deleted originals go to the Immich TRASH by default (restorable ~30 days;
  space frees when the trash is emptied). --purge force-deletes immediately.
- Dry-run by default; --execute to act. Every action appends to an audit log.
- Favorites / rating / description / albums / capture time carry over to the
  replacement; faces re-run automatically on the new file.

Usage:
  python tools/immich_replace.py --album 202606                  # dry-run plan
  python tools/immich_replace.py --album 202606 --limit 1 --execute
  python tools/immich_replace.py --album 202606 --preset 1080p --execute
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import config  # noqa: E402  (loads .env)

import requests  # noqa: E402

BASE = config.IMMICH_URL.rstrip("/")
KEY = config.IMMICH_API_KEY
HOT_ROOT = r"E:\apps\immich\library"          # docker /data mount
COLD_ROOT = r"F:\immich-library"              # FreeFileSync mirror of HOT_ROOT
FFMPEG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "tools", "ffmpeg", "ffmpeg.exe")
AUDIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "Output", "asset_index", "shrink_log.jsonl")
IMAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "Output", "asset_index", "immich_map.json")

PRESETS = {
    # visually-transparent 4K working copy — keeps the 9:16 crop headroom
    "4k":    {"vf": None,             "cq": "23"},
    # aggressive: for footage you'll rarely re-edit (vertical crops get soft)
    "1080p": {"vf": "scale=-2:1080",  "cq": "24"},
}


def req(path: str, method: str = "GET", body=None, **kw):
    r = requests.request(method, BASE + "/api" + path,
                         headers={"x-api-key": KEY}, json=body, timeout=60, **kw)
    r.raise_for_status()
    return r.json() if r.text else {}


def host_path(original_path: str, root: str) -> str:
    """Immich container path (/data/upload/…) → host path under root."""
    rel = original_path
    if rel.startswith("/data/"):
        rel = rel[len("/data/"):]
    return os.path.join(root, rel.replace("/", os.sep))


def sha1_b64(path: str, note_cb=None) -> str:
    h = hashlib.sha1()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
            done += len(chunk)
            if note_cb:
                note_cb(done / total)
    return base64.b64encode(h.digest()).decode()


def transcode(src: str, dst: str, preset: dict) -> None:
    cmd = [FFMPEG, "-y", "-v", "error", "-i", src,
           "-map", "0:v:0", "-map", "0:a?",
           "-c:v", "hevc_nvenc", "-rc", "vbr", "-cq", preset["cq"],
           "-preset", "p5", "-b:v", "0",
           "-c:a", "copy",
           "-map_metadata", "0", "-movflags", "use_metadata_tags+faststart"]
    if preset["vf"]:
        cmd += ["-vf", preset["vf"]]
    cmd.append(dst)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError(f"ffmpeg failed: {(r.stderr or '')[-300:]}")


def upload_replacement(path: str, old: dict) -> str:
    """Upload the transcode as a NEW asset; returns the new asset id."""
    name = old.get("originalFileName") or os.path.basename(path)
    fields = {
        "deviceAssetId": f"shrink-{old['id']}",
        "deviceId": "cutclaw-shrink",
        "fileCreatedAt": old.get("fileCreatedAt") or old.get("localDateTime") or "",
        "fileModifiedAt": old.get("fileModifiedAt") or old.get("fileCreatedAt") or "",
        "isFavorite": "true" if old.get("isFavorite") else "false",
    }
    with open(path, "rb") as f:
        r = requests.post(BASE + "/api/assets", headers={"x-api-key": KEY},
                          data=fields, files={"assetData": (name, f, "video/mp4")},
                          timeout=600)
    r.raise_for_status()
    d = r.json()
    if not d.get("id"):
        raise RuntimeError(f"upload returned no id: {d}")
    return d["id"]


def carry_over(old: dict, new_id: str) -> None:
    ex = old.get("exifInfo") or {}
    body = {}
    if ex.get("rating"):
        body["rating"] = ex["rating"]
    # GPS lives in DJI's proprietary udta atoms — ffmpeg's -map_metadata does
    # NOT carry it, so the map pin is restored via the API (city re-geocodes)
    if ex.get("latitude") is not None and ex.get("longitude") is not None:
        body["latitude"] = ex["latitude"]
        body["longitude"] = ex["longitude"]
    desc = str(ex.get("description") or "")
    # camera make/model is exif-read-only via API — keep it in the description
    if ex.get("model") and "📷" not in desc:
        make, model = str(ex.get("make") or "").strip(), str(ex.get("model") or "").strip()
        cam = model if (not make or model.lower().startswith(make.lower())) else f"{make} {model}"
        desc = (desc + "\n" if desc else "") + f"📷 {cam}(原片信息)"
    if desc:
        body["description"] = desc[:4000]
    if body:
        req(f"/assets/{new_id}", "PUT", body)
    # album membership
    try:
        albums = requests.get(BASE + "/api/albums", params={"assetId": old["id"]},
                              headers={"x-api-key": KEY}, timeout=30).json()
        for al in albums or []:
            try:
                req(f"/albums/{al['id']}/assets", "PUT", {"ids": [new_id]})
            except Exception as e:  # noqa: BLE001
                print(f"    ⚠ 加回相簿「{al.get('albumName')}」失败: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"    ⚠ 相簿查询失败: {e}")


def update_immich_map(old_id: str, new_id: str, new_checksum: str) -> None:
    try:
        with open(IMAP, "r", encoding="utf-8") as f:
            imap = json.load(f)
    except Exception:  # noqa: BLE001
        return
    changed = False
    for v in imap.values():
        if v.get("id") == old_id:
            v["id"] = new_id
            v["checksum"] = new_checksum
            v["shrunk"] = True
            changed = True
    if changed:
        with open(IMAP, "w", encoding="utf-8") as f:
            json.dump(imap, f, ensure_ascii=False, indent=2)


def audit(entry: dict) -> None:
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def done_ids() -> set:
    ids = set()
    try:
        with open(AUDIT, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("ok"):
                        ids.add(d.get("old_id"))
                except Exception:  # noqa: BLE001
                    continue
    except FileNotFoundError:
        pass
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--album", required=True, help="相簿名(子串匹配)")
    ap.add_argument("--min-mb", type=int, default=500, help="只处理大于此体积的视频")
    ap.add_argument("--preset", choices=list(PRESETS), default="4k")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理 N 个(0=不限)")
    ap.add_argument("--execute", action="store_true", help="真正执行(默认只演练)")
    ap.add_argument("--purge", action="store_true",
                    help="跳过回收站直接删除(默认进回收站,30天可恢复)")
    args = ap.parse_args()

    if not os.path.isdir(COLD_ROOT):
        sys.exit(f"❌ 冷备份盘不在位({COLD_ROOT})— 校验母带需要它,插盘后重试")
    if not os.path.exists(FFMPEG):
        sys.exit(f"❌ 找不到 ffmpeg: {FFMPEG}")

    albums = req("/albums")
    matches = [a for a in albums if args.album.lower() in (a.get("albumName") or "").lower()]
    if len(matches) != 1:
        sys.exit(f"❌ 相簿匹配到 {len(matches)} 个: {[a.get('albumName') for a in matches]}")
    album = matches[0]
    print(f"相簿: {album['albumName']} ({album.get('assetCount')} 项)")

    assets, page = [], 1
    while page:
        r = req("/search/metadata", "POST",
                {"albumIds": [album["id"]], "type": "VIDEO", "size": 200,
                 "page": page, "withExif": True})
        a = r.get("assets") or {}
        assets.extend(a.get("items", []))
        page = a.get("nextPage")

    skip = done_ids()
    todo = []
    for a in assets:
        sz = (a.get("exifInfo") or {}).get("fileSizeInByte") or 0
        if sz >= args.min_mb * 1024 * 1024 and a["id"] not in skip:
            todo.append(a)
    todo.sort(key=lambda x: -(x.get("exifInfo") or {}).get("fileSizeInByte", 0))
    if args.limit:
        todo = todo[:args.limit]

    total_gb = sum((a.get("exifInfo") or {}).get("fileSizeInByte", 0) for a in todo) / 2**30
    print(f"待处理: {len(todo)} 个视频 · {total_gb:.1f} GB · 预设 {args.preset} "
          f"(预期压到约 {total_gb * (0.25 if args.preset == '4k' else 0.08):.1f} GB)\n")
    if not todo:
        return
    for a in todo:
        sz = (a.get("exifInfo") or {}).get("fileSizeInByte", 0)
        print(f"  {a.get('originalFileName'):44s} {sz / 2**30:5.2f} GB"
              f"{'  ❤️' if a.get('isFavorite') else ''}")
    if not args.execute:
        print("\n(演练模式 — 加 --execute 执行;建议先 --limit 1 试点一个)")
        return

    print()
    for i, a in enumerate(todo):
        aid = a["id"]
        name = a.get("originalFileName") or aid[:8]
        sz = (a.get("exifInfo") or {}).get("fileSizeInByte", 0)
        print(f"[{i + 1}/{len(todo)}] {name} ({sz / 2**30:.2f} GB)")
        entry = {"old_id": aid, "name": name, "bytes_before": sz,
                 "preset": args.preset, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            info = req(f"/assets/{aid}")          # full record incl. checksum
            hot = host_path(info["originalPath"], HOT_ROOT)
            cold = host_path(info["originalPath"], COLD_ROOT)
            if not os.path.exists(hot):
                raise RuntimeError(f"热盘原片不存在: {hot}")
            if not os.path.exists(cold):
                raise RuntimeError(f"冷备副本不存在: {cold} — 先跑一次 FreeFileSync")

            print("    校验冷备 SHA-1 …", end="", flush=True)
            cold_ck = sha1_b64(cold)
            if cold_ck != info.get("checksum"):
                raise RuntimeError("冷备校验不匹配!该文件的备份不可信,跳过")
            print(" ✓ 与 Immich 指纹一致")

            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, re.sub(r"[^\w.-]", "_", name))
                print("    NVENC 转码 …", end="", flush=True)
                t0 = time.time()
                transcode(hot, out, PRESETS[args.preset])
                new_sz = os.path.getsize(out)
                print(f" ✓ {new_sz / 2**30:.2f} GB ({new_sz / sz * 100:.0f}%) · {time.time() - t0:.0f}s")

                print("    上传替换 …", end="", flush=True)
                new_id = upload_replacement(out, info)
                print(f" ✓ {new_id[:8]}")
            carry_over(info, new_id)
            new_info = req(f"/assets/{new_id}")
            update_immich_map(aid, new_id, new_info.get("checksum", ""))

            req("/assets", "DELETE", {"ids": [aid], "force": bool(args.purge)})
            print(f"    旧资产已{'永久删除' if args.purge else '移入回收站'}")
            entry.update({"ok": True, "new_id": new_id, "bytes_after": new_sz,
                          "cold_verified": True})
        except Exception as e:  # noqa: BLE001
            print(f"    ❌ {e}")
            entry.update({"ok": False, "error": str(e)[:300]})
        audit(entry)

    done = [1 for _ in open(AUDIT, encoding="utf-8")]
    print(f"\n完成。审计日志: {AUDIT} ({len(done)} 条)")
    if not args.purge:
        print("空间将在 Immich 回收站清空后释放(管理 → 回收站),或下次带 --purge 运行。")


if __name__ == "__main__":
    main()

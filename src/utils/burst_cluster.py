"""连拍相似照片聚类 + 簇内选优 — 纯本地算法,零 API 成本(§20)。

场景:拍摄时习惯连按多张回家又不整理。双重闸门聚类:
  1) 时间窗 — 相邻拍摄间隔 <= GAP_SEC 秒才算同一波连拍;
  2) 视觉 — dHash 汉明距离 <= HAMMING_MAX 才确认"看起来一样"。
簇内用拉普拉斯方差(清晰度)+ 曝光裁剪惩罚选出最佳一张,供堆叠时做封面。
所有计算在 ~250px 缩略图上进行:同簇画面相同,相对比较足够可靠。
"""
from __future__ import annotations

import cv2
import numpy as np

GAP_SEC = 15          # 相邻两张间隔超过这个秒数,不再算同一波连拍
HAMMING_MAX = 11      # 64 位 dHash 距离上限(<=11 视觉上基本相同)


def _decode_gray(img_bytes: bytes):
    arr = np.frombuffer(img_bytes, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)


def dhash(img_bytes: bytes, size: int = 8) -> int | None:
    """64 位差值哈希;解码失败返回 None(该图跳过聚类)。"""
    img = _decode_gray(img_bytes)
    if img is None:
        return None
    small = cv2.resize(img, (size + 1, size), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    return int(sum(1 << i for i, v in enumerate(diff.flatten()) if v))


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def quality(img_bytes: bytes) -> dict:
    """清晰度 + 曝光裁剪。统一缩放到 256 宽保证同簇可比。"""
    img = _decode_gray(img_bytes)
    if img is None:
        return {"sharp": 0.0, "clip": 1.0, "score": 0.0}
    h, w = img.shape
    if w > 256:
        img = cv2.resize(img, (256, max(1, int(h * 256 / w))), interpolation=cv2.INTER_AREA)
    sharp = float(cv2.Laplacian(img, cv2.CV_64F).var())
    clip = float((img < 12).mean() + (img > 243).mean())   # 死黑+死白占比
    return {"sharp": round(sharp, 1), "clip": round(clip, 3),
            "score": round(sharp * (1.0 - min(0.8, clip * 2)), 1)}


def split_by_visual(hashes: list[int | None]) -> list[list[int]]:
    """把一个时间组按视觉相似切成若干簇(返回下标簇)。
    贪心链式:与簇内最后一张距离 <= HAMMING_MAX 即加入(连拍会缓慢漂移,
    比全簇比对更符合实际)。哈希缺失的照片自成一簇(不参与堆叠)。"""
    clusters: list[list[int]] = []
    for i, h in enumerate(hashes):
        if h is not None and clusters:
            last = clusters[-1]
            lh = hashes[last[-1]]
            if lh is not None and hamming(h, lh) <= HAMMING_MAX:
                last.append(i)
                continue
        clusters.append([i])
    return clusters

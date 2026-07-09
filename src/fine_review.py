"""逐片段精细化评判(标注时一次做好,流水线直接吃结果)。

用户意图(2026-07-09):"我想对每一个视频做精细化评判……标注的时候就做好,
真正跑流水线的时候直接拿结果。"

对一个素材的每个高光池时刻,VLM 按严苛量表分维度打分:
  构图 / 光影 / 主体与瞬间(有没有值得看的事)/ 情绪价值,各 0-10
外加 S/A/B/C 分层 + 一句犀利点评。要点:

- **严苛量表**:prompt 明确"普通素材就是 4-6 分,8+ 必须有过硬理由"——
  抑制 VLM 的老好人倾向;项目池层还有百分位归一兜底(§17),绝对分
  轻微饱和也不伤区分度。
- **两帧对照**(起始/中段):单帧看不出"有没有事发生"。
- 4 个时刻/次调用(8 图),2 路并行;**每组落盘**(付费结论中断零丢失),
  按 (start,end) 键控可断点续评;失败组跳过不重试(重试纪律)。
- 产物 `fine_review.json` 落在 analyzed/{hash}/,项目池合并时随行:
  细评均分替换 content 维度、tier 进编剧菜单(grade 标签),已细评的
  时刻不再花 L3 的钱。
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time

_FR_VERSION = 1
_DIMS = ("composition", "light", "subject_moment", "emotion")
_DIM_LABELS = {"composition": "构图", "light": "光影",
               "subject_moment": "主体瞬间", "emotion": "情绪"}


def review_paths(cache_dir: str) -> tuple[str, str]:
    return (os.path.join(cache_dir, "fine_review.json"),
            os.path.join(cache_dir, "fine_review.progress.json"))


def _mkey(m: dict) -> str:
    return f"{float(m.get('start') or 0):.1f}:{float(m.get('end') or 0):.1f}"


def _frame_b64(path: str, at: float) -> str | None:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        out = tf.name
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", path,
             "-frames:v", "1", "-vf", "scale=640:-2", out],
            capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.getsize(out):
            return None
        with open(out, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


_PROMPT = """你是一位以严苛著称的旅拍选片师,正在为回忆混剪筛选素材。下面给出 %d 个候选片段,每个片段两帧(起始帧、中段帧,按顺序排列)。

对每个片段按以下维度打分(0-10,**严格执行量表:普通素材就是 4-6 分,7 分要求明显优点,8 分以上必须有过硬理由,满分几乎不存在**):
- composition 构图:主体位置、画面平衡、线条引导、地平线
- light 光影:曝光、层次、色彩、氛围(灰蒙蒙平光就是低分)
- subject_moment 主体与瞬间:有没有清晰的主体?有没有值得看的事发生?(空镜/匀速路过=低分,回头/挥手/笑/摔倒/互动=高分)
- emotion 情绪价值:放进回忆混剪里,这个画面能唤起感受吗?

再给综合分层 tier(S=全库前5%%水准/A=优秀/B=可用/C=平庸凑数)和一句 25 字内的点评——说人话,直指问题或亮点,不要客套。

只输出 JSON 数组:
[{"idx": <1-%d>, "composition": n, "light": n, "subject_moment": n, "emotion": n, "tier": "S|A|B|C", "critique": "<点评>"}]"""


def fine_review_source(content_hash: str, workers: int = 2) -> dict:
    """对一个素材的全部池时刻做细评。可续跑;返回 fine_review 数据。"""
    from src import config
    from src.analyzer import get_analysis_path
    try:
        from src.utils.llm_logger import set_llm_stage
        set_llm_stage("fine_review")
    except Exception:  # noqa: BLE001
        pass

    cache_dir = get_analysis_path(content_hash)
    pool_path = os.path.join(cache_dir, "highlight_pool.json")
    if not os.path.exists(pool_path):
        # 池是细评的输入(定义了"哪些时刻值得评")——先建池
        from src.curation import _source_pool
        _source_pool(content_hash)
    with open(pool_path, "r", encoding="utf-8") as f:
        moments = json.load(f).get("moments", [])
    if not moments:
        raise RuntimeError("该素材的高光池为空,没有可评的时刻")

    fr_path, prog_path = review_paths(cache_dir)
    data = {"version": _FR_VERSION, "moments": {}}
    if os.path.exists(fr_path):
        try:
            _old = json.load(open(fr_path, encoding="utf-8"))
            if int(_old.get("version", 0)) >= _FR_VERSION:
                data = _old
        except Exception:  # noqa: BLE001
            pass

    todo = [m for m in moments if _mkey(m) not in data["moments"]]
    if not todo:
        return data
    groups = [todo[i:i + 4] for i in range(0, len(todo), 4)]
    total = len(moments)
    print(f"🔬 [FineReview] {os.path.basename(str(moments[0].get('video_path')))}: "
          f"{len(todo)}/{total} 个时刻待评 → {len(groups)} 次 VLM 调用(计费)")
    _lock = threading.Lock()

    def _progress(note=""):
        try:
            with open(prog_path, "w", encoding="utf-8") as f:
                json.dump({"done": len(data["moments"]), "total": total, "note": note}, f)
        except Exception:  # noqa: BLE001
            pass

    def _flush():
        try:
            with open(fr_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass

    _progress("启动")

    def _judge(gi_grp):
        import litellm
        gi, grp = gi_grp
        packs = []
        for m in grp:
            s, e = float(m["start"]), float(m["end"])
            f1 = _frame_b64(m["video_path"], s + 0.2)
            f2 = _frame_b64(m["video_path"], (s + e) / 2)
            if f1 or f2:
                packs.append((m, [b for b in (f1, f2) if b]))
        if not packs:
            return
        content = [{"type": "text", "text": _PROMPT % (len(packs), len(packs))}]
        for _pi, (_m, frames) in enumerate(packs):
            content.append({"type": "text",
                            "text": f"片段 {_pi + 1}({_m['duration']:.1f}s,画面描述:{str(_m.get('desc'))[:80]}):"})
            for b64 in frames:
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        kwargs = dict(model=config.VIDEO_ANALYSIS_MODEL,
                      messages=[{"role": "user", "content": content}],
                      max_tokens=3000, temperature=0.0, timeout=150)
        if getattr(config, "VIDEO_ANALYSIS_ENDPOINT", ""):
            kwargs["api_base"] = config.VIDEO_ANALYSIS_ENDPOINT
        if getattr(config, "VIDEO_ANALYSIS_API_KEY", ""):
            kwargs["api_key"] = config.VIDEO_ANALYSIS_API_KEY
        try:
            raw = litellm.completion(**kwargs)
            txt = (raw.choices[0].message.content or "").strip()
            arr = re.search(r"\[.*\]", txt, re.S)
            verdicts = json.loads(arr.group(0)) if arr else []
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  [FineReview] 组 {gi + 1}/{len(groups)} 失败(跳过,不重试): {str(e)[:110]}")
            return
        with _lock:
            for v in verdicts:
                try:
                    pi = int(v.get("idx")) - 1
                except Exception:  # noqa: BLE001
                    continue
                if not (0 <= pi < len(packs)):
                    continue
                m = packs[pi][0]
                dims = {}
                for d in _DIMS:
                    try:
                        dims[d] = max(0.0, min(10.0, float(v.get(d))))
                    except (TypeError, ValueError):
                        dims[d] = None
                vals = [x for x in dims.values() if x is not None]
                tier = str(v.get("tier", "")).strip().upper()
                data["moments"][_mkey(m)] = {
                    "dims": dims,
                    "avg": round(sum(vals) / len(vals), 2) if vals else None,
                    "tier": tier if tier in ("S", "A", "B", "C") else "B",
                    "critique": str(v.get("critique") or "")[:80],
                }
            _flush()          # 每组落盘:付费结论一条都不能因中断丢失
            _progress(f"已评 {len(data['moments'])}/{total}")
            print(f"🔬 [FineReview] 组 {gi + 1}/{len(groups)} ✓ ({len(data['moments'])}/{total})")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(_judge, enumerate(groups)))

    data["reviewed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _flush()
    try:
        os.remove(prog_path)
    except OSError:
        pass
    _tc = {}
    for v in data["moments"].values():
        _tc[v["tier"]] = _tc.get(v["tier"], 0) + 1
    print(f"🔬 [FineReview] 完成: {len(data['moments'])}/{total} · 分层 {_tc}")
    return data


def load_fine_review(content_hash: str) -> dict:
    """{ 'start:end': verdict } — 池合并/详情页共用。"""
    from src.analyzer import get_analysis_path
    fr_path, _ = review_paths(get_analysis_path(content_hash))
    try:
        with open(fr_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("moments", {}) if int(d.get("version", 0)) >= _FR_VERSION else {}
    except Exception:  # noqa: BLE001
        return {}

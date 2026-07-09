"""AI 旁白层 — 让成片从"画面集"变成"一段回忆"。

设计原则(对齐 docs/LOGIC.md 的决策瘦身/重试纪律):
- 槽位是确定性算出来的(哪些镜头安静、间距够、开场/收尾),模型只负责
  写文案 + 从给定槽位里挑位置 — 时间轴不交给模型复述。
- LLM 一次调用,失败快速抛错(无盲重试);TTS 用 edge-tts(免费,无计费)。
- 产物 narration.json + narration/*.mp3 落在 shot_point 同目录,渲染器
  按绝对时间混入,BGM 在旁白窗口自动闪避(复用人声闪避机制)。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time

VOICES = {
    "yunxi": "zh-CN-YunxiNeural",       # 男声 · 温和(默认)
    "xiaoxiao": "zh-CN-XiaoxiaoNeural", # 女声 · 温暖
    "yunjian": "zh-CN-YunjianNeural",   # 男声 · 厚重
}
DEFAULT_VOICE = "yunxi"
_TTS_RATE = "-16%"   # 旁白比对话慢,句内标点会产生自然停顿(文案刻意多逗号)
_MIN_SLOT_GAP_SEC = 12.0   # 两句旁白之间至少隔这么久,避免变成"解说"
_MIN_SLOT_DUR = 3.5        # 太短的镜头不当旁白位


def narration_paths(shot_point_path: str) -> tuple[str, str]:
    """(narration.json 路径, 音频目录) — 与成片同键,跟着 shot_point 走。"""
    base = os.path.splitext(os.path.basename(shot_point_path))[0]
    tag = base.replace("shot_point_", "")
    d = os.path.dirname(os.path.abspath(shot_point_path))
    return (os.path.join(d, f"narration_{tag}.json"),
            os.path.join(d, f"narration_{tag}"))


def _timeline(shot_point: list) -> list[dict]:
    """按最终成片顺序展开镜头,算出每个镜头的绝对起点。"""
    out, t = [], 0.0
    for entry in shot_point:
        dur = float(entry.get("total_duration") or 0)
        out.append({"start": t, "dur": dur, "entry": entry})
        t += dur
    return out


def _plan_shots(shot_point_path: str) -> list[dict]:
    plan_path = shot_point_path.replace("shot_point_", "shot_plan_")
    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            pl = json.load(f)
        return [s for sec in pl.get("video_structure", [])
                for s in (sec.get("shot_plan") or {}).get("shots", [])]
    except Exception:
        return []


def build_slots(shot_point_path: str) -> tuple[list[dict], dict]:
    """确定性旁白槽位:安静(无真实人声)、时长够、彼此隔开;首尾必留。

    Returns (slots, context) — context 给 LLM 的画面/主题信息。
    """
    with open(shot_point_path, "r", encoding="utf-8") as f:
        sp = json.load(f)
    tl = _timeline(sp)
    shots = _plan_shots(shot_point_path)
    total = sum(x["dur"] for x in tl)

    theme, logic = "", ""
    plan_path = shot_point_path.replace("shot_point_", "shot_plan_")
    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            pl = json.load(f)
        theme = pl.get("overall_theme") or ""
        logic = pl.get("narrative_logic") or ""
    except Exception:
        pass

    slots, last_at = [], -1e9
    for i, item in enumerate(tl):
        meta = shots[i] if i < len(shots) else {}
        has_voice = bool((meta.get("anchor") or {}).get("sound"))
        content = str(meta.get("content") or "")[:110]
        emotion = str(meta.get("emotion") or "")
        at = item["start"] + 0.4
        is_first = (i == 0)
        is_last_zone = (item["start"] >= total - 20)  # 最后 20s 视为收尾区
        if item["dur"] < _MIN_SLOT_DUR and not (is_first or is_last_zone):
            continue
        if has_voice:          # 不压真实人声/笑声 — 那是比旁白更珍贵的东西
            continue
        if (at - last_at) < _MIN_SLOT_GAP_SEC and not is_last_zone:
            continue
        slots.append({"slot": i, "at_sec": round(at, 1), "shot_dur": round(item["dur"], 1),
                      "content": content, "emotion": emotion,
                      "zone": ("opening" if is_first else "ending" if is_last_zone else "middle")})
        last_at = at
    # 收尾兜底:结尾区镜头全带真实人声时(常见:高潮收在笑声上),用最后一个
    # 镜头当 ending 槽——旁白会压 BGM/环境声,但不至于让片子没有收尾句。
    if tl and not any(s["zone"] == "ending" for s in slots):
        last = tl[-1]
        i = len(tl) - 1
        meta = shots[i] if i < len(shots) else {}
        slots.append({"slot": i, "at_sec": round(last["start"] + 0.4, 1),
                      "shot_dur": round(last["dur"], 1),
                      "content": str(meta.get("content") or "")[:110],
                      "emotion": str(meta.get("emotion") or ""), "zone": "ending"})
    ctx = {"total_sec": round(total, 1), "theme": theme, "logic": logic[:500]}
    return slots, ctx


def _extract_json(content: str):
    """平衡括号取最后一个可解析的 JSON(数组优先——先扫对象会把数组里
    最后一句误当整个结果)。推理模型的思考文本免疫。"""
    for opener, closer in (("[", "]"), ("{", "}")):
        spans = []
        depth, start = 0, -1
        for i, ch in enumerate(content):
            if ch == opener:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == closer and depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(content[start:i + 1])
        for s in reversed(spans):
            try:
                return json.loads(s)
            except Exception:
                continue
    return None


def write_script(slots: list[dict], ctx: dict, instruction: str = "") -> list[dict]:
    """LLM 写 4-6 句第一人称旁白并挑槽位。单次调用,失败抛错(重试纪律)。"""
    import litellm
    from src import config as _cfg
    try:
        from src.utils.llm_logger import set_llm_stage
        set_llm_stage("narration")
    except Exception:
        pass

    slot_lines = "\n".join(
        f"- slot {s['slot']} @{s['at_sec']}s [{s['zone']}] ({s['shot_dur']}s) "
        f"{s['emotion']}: {s['content']}" for s in slots)
    prompt = f"""你是一部旅拍回忆混剪的旁白作者。成片总长 {ctx['total_sec']} 秒。

主题:{ctx['theme']}
叙事:{ctx['logic']}
{f'创作倾向(低权重参考):{instruction}' if instruction else ''}

可用的旁白位置(只能从下面挑,slot 编号 + 画面内容):
{slot_lines}

写 4-6 句第一人称旁白,要求:
- 中文,每句 8~24 个字,像跟朋友看照片时随口说的话——**不是写作,是说话**。允许不完整句、允许口语碎片("说实话,","后来想想,","那天"开头都行);
- 句子内部**刻意用逗号断开**制造停顿(如"风很大,我们没敢说话,就那么滑下去了")——合成语音靠标点呼吸,一逗到底的长句念出来最机械;
- 严禁鸡汤、严禁形容词堆砌、严禁"岁月/时光/美好"这类词;宁可平淡不要文艺腔;
- 说"我们";提具体的东西(雪、缆车、某人的动作),不复述画面(观众看得见),说画面之外的感受、当时的小事、或者记错了也无所谓的细节;
- 必须有一句用 opening 区的槽位(开场定调),必须有一句用 ending 区的槽位(收尾);
- 其余散落中段,不要连续两句挨着。

只输出 JSON 数组,不要解释:
[{{"slot": <槽位编号>, "text": "<旁白文本>"}}]"""

    kwargs = dict(model=_cfg.AGENT_LITELLM_MODEL,
                  messages=[{"role": "user", "content": prompt}],
                  temperature=0.8, max_tokens=8192, timeout=180)
    if getattr(_cfg, "AGENT_LITELLM_URL", ""):
        kwargs["api_base"] = _cfg.AGENT_LITELLM_URL
    if getattr(_cfg, "AGENT_LITELLM_API_KEY", ""):
        kwargs["api_key"] = _cfg.AGENT_LITELLM_API_KEY
    raw = litellm.completion(**kwargs)
    msg = raw.choices[0].message
    content = (msg.content or "").strip() or str(getattr(msg, "reasoning_content", "") or "")
    parsed = _extract_json(content)
    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError(f"旁白 LLM 未返回可解析的句子列表(len={len(content)})")

    by_slot = {s["slot"]: s for s in slots}
    lines = []
    for it in parsed:
        try:
            sl = int(it.get("slot"))
            text = str(it.get("text") or "").strip()
        except Exception:
            continue
        if sl in by_slot and text:
            lines.append({"slot": sl, "text": text, "at_sec": by_slot[sl]["at_sec"]})
    lines.sort(key=lambda x: x["at_sec"])
    if len(lines) < 3:
        raise RuntimeError(f"旁白只有 {len(lines)} 句可用(槽位对不上或文本为空)— 不凑合")
    return lines


def _ffmpeg() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cand = os.path.join(root, "tools", "ffmpeg", "ffmpeg.exe")
    return cand if os.path.exists(cand) else "ffmpeg"


def synthesize(lines: list[dict], out_dir: str, voice_key: str = DEFAULT_VOICE) -> list[dict]:
    """逐句 TTS(edge-tts)→ 响度归一 → 实测时长。跳过 text 未变的已有行。"""
    import edge_tts
    voice = VOICES.get(voice_key, VOICES[DEFAULT_VOICE])
    os.makedirs(out_dir, exist_ok=True)
    fp = _ffmpeg()
    fprobe = os.path.join(os.path.dirname(fp), "ffprobe.exe") if fp.endswith(".exe") else "ffprobe"

    async def _one(text: str, path: str):
        await edge_tts.Communicate(text, voice, rate=_TTS_RATE).save(path)

    for i, ln in enumerate(lines):
        raw = os.path.join(out_dir, f"line_{i}_raw.mp3")
        fin = os.path.join(out_dir, f"line_{i}.m4a")
        sig = os.path.join(out_dir, f"line_{i}.txt")
        prev = ""
        if os.path.exists(sig):
            try:
                prev = open(sig, "r", encoding="utf-8").read()
            except Exception:
                prev = ""
        if prev == f"{voice}|{ln['text']}" and os.path.exists(fin):
            ln["file"] = fin
        else:
            asyncio.run(_one(ln["text"], raw))
            # 响度归一到 -16 LUFS:多句之间音量一致,混音时不用逐句调
            r = subprocess.run([fp, "-y", "-v", "error", "-i", raw,
                                "-af", "loudnorm=I=-16:LRA=8:TP=-1.5",
                                "-c:a", "aac", "-b:a", "128k", fin],
                               capture_output=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(f"旁白响度归一失败 line_{i}: {r.stderr.decode(errors='replace')[-200:]}")
            try:
                os.remove(raw)
            except OSError:
                pass
            with open(sig, "w", encoding="utf-8") as f:
                f.write(f"{voice}|{ln['text']}")
            ln["file"] = fin
        pr = subprocess.run([fprobe, "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", fin],
                            capture_output=True, text=True, timeout=30)
        ln["dur"] = round(float(pr.stdout.strip() or 0), 2)
    return lines


def generate(shot_point_path: str, voice: str = DEFAULT_VOICE,
             instruction: str = "") -> dict:
    """全流程:槽位 → LLM 写稿 → TTS。写 narration.json 并返回。"""
    slots, ctx = build_slots(shot_point_path)
    if len(slots) < 3:
        raise RuntimeError(f"可用旁白槽位只有 {len(slots)} 个(镜头太密/人声太多)")
    lines = write_script(slots, ctx, instruction)
    jpath, adir = narration_paths(shot_point_path)
    lines = synthesize(lines, adir, voice)
    data = {"enabled": True, "voice": voice, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_sec": ctx["total_sec"], "lines": lines}
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def save(shot_point_path: str, lines: list[dict], voice: str, enabled: bool) -> dict:
    """UI 编辑后保存:只对 text 变化的行重新 TTS(synthesize 内部按签名跳过)。"""
    jpath, adir = narration_paths(shot_point_path)
    lines = [{"slot": int(l.get("slot", i)), "text": str(l.get("text") or "").strip(),
              "at_sec": float(l.get("at_sec", 0))}
             for i, l in enumerate(lines) if str(l.get("text") or "").strip()]
    lines.sort(key=lambda x: x["at_sec"])
    lines = synthesize(lines, adir, voice)
    prev = {}
    if os.path.exists(jpath):
        try:
            prev = json.load(open(jpath, encoding="utf-8"))
        except Exception:
            prev = {}
    data = {**prev, "enabled": enabled, "voice": voice,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "lines": lines}
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data

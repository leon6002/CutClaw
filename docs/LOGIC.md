# CutClaw 系统逻辑手册

> 目的：把散落在代码里的设计决策集中记录，方便回溯"为什么这么做"、以及针对某个功能单点优化。
> 每节给出：**设计动机 → 实现要点 → 关键文件/函数 → 调优入口**。
> 最后更新：2026-07-05

---

## 目录

1. [素材标注体系（视频/音频分析）](#1-素材标注体系)
2. [双轨标注：云端 API vs 本地 VLM](#2-双轨标注)
3. [Immich 相册集成](#3-immich-集成)
4. [音频分析：事实层 + LLM 描述层](#4-音频分析)
5. [镜头选取编排（Agent 工作流）](#5-镜头选取编排)
6. [实测画质门槛（防糊防晃）](#6-实测画质门槛)
7. [渲染管线](#7-渲染管线)
8. [Web UI 数据链路](#8-web-ui-数据链路)
9. [任务系统与进程健壮性](#9-任务系统与进程健壮性)
10. [缓存路径全景](#10-缓存路径全景)
11. [关键配置项速查](#11-关键配置项速查)
12. [口味记忆（全局拒绝名单）](#12-口味记忆)
13. [换镜头闭环 UI](#13-换镜头闭环-ui)
14. [视觉去重与前置品控（锚点预算器）](#14-视觉去重与前置品控锚点预算器)
15. [相机运动标注 + 动势匹配 + 正向口味记忆](#15-相机运动标注--动势匹配--正向口味记忆)
16. [卡点精确转场 + 节拍可视化 + BGM 拼接 + 素材红心](#16-卡点精确转场--节拍可视化--bgm-拼接--素材红心)

---

## 1. 素材标注体系

### 动机
每个源视频只分析一次（按内容 SHA-256 缓存），项目换素材不重复消耗 API；任何中断都能续跑。

### 流程（单个视频）
```
镜头检测 (PySceneDetect @2fps, NVDEC 432p 代理加速 5.6×)
  → 片段理解 captioning（VLM，逐镜头，运动感知选帧）
  → 密集描述 dense_caption（VLM，逐镜头时间段描述 dense_segments）
  → 场景合并 scene_merge → 场景分析 scene_analysis（VLM）
  → 汇总注释（质量分/标签/摘要）写入 annotations.json
```

### 可续跑契约（重要，别破坏）
- 每个镜头的 caption 结果写 `captions/ckpt/{start}_{end}.json`，重跑先查 ckpt 命中直接跳过。
- 全部完成后写 `analysis_complete` 标记文件；判断"已完成"看 **ckpt + 场景汇总**，绝不看半成品 captions.json。
- **部分失败必须 raise，禁止把失败烘焙成"已完成"**——否则错误结果被缓存永远不重试。这是历史教训（曾出现 fake "analysis_failed" 注释被当成品）。

### 性能设计
- **文件级并行**：`ProcessPoolExecutor`（进程不是线程——decord 不线程安全 + GIL），`ANNOTATE_VIDEO_WORKERS`（默认 2）。子进程入口 `_annotate_video_in_process`，阶段事件经 Manager 队列回传主进程（`stage_by_hash` 路由到 UI 卡片）。
- **API 并发**：`CAPTION_BATCH_SIZE` 控制并发 VLM 请求。
- **运动感知选帧**：均匀锚点 + 帧差峰值，上限 24 帧/镜头（`VIDEO_CAPTION_MAX_FRAMES`，0=不限）。曾用均匀降采样被否决——1 秒的爆发动作会被漏掉，"不能在输出质量上妥协"。
- **NVDEC 代理**：`_try_gpu_detect_proxy` 生成 432p 代理做镜头检测（fps 透传）；驱动不支持 nvenc 时编码回退 libx264，解码仍走 NVDEC。

### 关键文件
| 文件 | 职责 |
|------|------|
| `src/analyzer.py` | 调度器：`analyze_video()` / `analyze_audio()` / `get_analysis_path(hash, variant)` / 完成标记与恢复检测 |
| `src/asset_manager/annotator.py` | `annotate_asset` / `annotate_video_asset` / `batch_annotate`（多进程分支）/ `_annotate_video_in_process` |
| `src/video/preprocess/video_utils.py` | 镜头检测 + NVDEC 代理 + 心跳进度 |
| `src/video/deconstruction/video_caption.py` | VLM 片段理解，运动感知选帧，ckpt 断点 |
| `src/video/deconstruction/scene_analysis_video.py` | 场景级 VLM 分析 |

### 调优入口
- 提速：`ANNOTATE_VIDEO_WORKERS`↑（显存/内存允许时）、`CAPTION_BATCH_SIZE`↑（API 限速内）。
- 成本：`VIDEO_CAPTION_MAX_FRAMES`↓（有质量损失风险，慎动）。
- 单视频一次完整标注 ≈ 每镜头 1 次 caption 调用 + 1 次 dense + 每场景 1 次，51 帧曾实测 57k token/调用 → 24 帧封顶后大幅下降。

---

## 2. 双轨标注

### 动机
本地 3090 + Ollama qwen2.5vl 免 API 费、低延迟（单帧 1.7s vs 云端 10-60s），但质量待 A/B 验证。两套结果**并行保存互不覆盖**，随时对比。

### 实现
- `variant="local"` 贯穿 `get_analysis_path` → 分析缓存放 `Output/analyzed/{hash}@local/`。
- 注释存 `Output/asset_index/annotations_local.json`（云端在 `annotations.json` + index），server 端 `_save_local_annotations`。
- 执行时临时覆盖 `VIDEO_ANALYSIS_MODEL/ENDPOINT/API_KEY` + `CAPTION_BATCH_SIZE=4`（Ollama 并行槽位），try/finally 恢复；单 GPU 所以本地轨**串行**执行。
- 本地轨结果**不回写 Immich**（实验数据不污染相册描述）。
- 标注队列条目带 `provider`，链式执行只合并同 provider 的请求。
- 模型来源：`src/api_pool.json` 里 endpoint 含 `11434` 的条目（`_local_vlm_entry`）。

### UI
- 视频卡片：青色「标注/重新标注」（云端）+ 紫色「🖥 本地/本地重标」；紫色 `🖥 Q x.x` 徽章表示本地轨有结果。
- 详情页：`☁ 云端 / 🖥 本地` 切换器，详情接口 `GET /api/assets/{hash}/details?variant=local`。
- GPU 面板 `LocalGpuPanel.tsx`：显卡利用率/显存/温度实时条 + 模型加载/卸载/测试一帧按钮（真实计时）。

### 实测参考
4 秒测试片：本地全流程 46s（镜头检测 1.2s / caption 26.9s / dense 11.4s / 场景 4.4s）。

---

## 3. Immich 集成

### 动机
素材真身在 Immich（端口 2284），分析不应重复拷贝原片。

### 实现要点
- **身份绑定**：`Output/asset_index/immich_map.json` 记 `{代理文件名 → immich_id + checksum + original_path…}`（键是本地代理文件名，不是 content_hash）。checksum（Immich 的 base64 SHA-1）是原片内容身份，id 是 API 句柄。
- **代理导入**：分析用 Immich 的 1080p 转码代理（`/assets/{id}/video/playback`，3-10MB）落到 `resource/imports/immich/`，不拉原片。内容级去重按 checksum，但**只有绑定的代理文件仍在盘上才 skip**——用户删代理省空间后再点导入会重新下载（`immich_import`）。
- **代理无关的分析身份（关键）**：代理是转码产物，重下/重转码后**字节 hash 会变**——若按代理 hash 缓存分析，删了重下就白标。所以 Immich 素材的分析键**重写为原片 checksum**：扫描后 `_apply_immich_identity` 把 `content_hash` 覆盖成 `im-<checksum>`（base64→base64url，路径安全无碰撞），贯穿 `analyze_video(content_hash=…)` 到分析目录、annotations、details。**删代理 → 分析全在**（无任何自动清理）；**重下代理 → hash 不变 → 秒复用**，哪怕 Immich 重新转码也认得。首次切换到该逻辑时 `_migrate_analysis_identity` 一次性把旧的按代理 hash 缓存的分析/注释迁移到 checksum 键（`os.replace` 目录 + 挪 index/local 条目），旧数据不丢；迁移后 isdir 短路，稳态零开销。
- **原片渲染（可选项）**：渲染时 `source_quality="original"` → `_materialize_immich_originals()` 生成替换过路径的 shot_point 副本（抖音 1080p 够用，默认 proxy）。
- **描述回写**：标注完成自动 `PUT /assets/{id}` 把摘要写进 Immich 描述（在 Immich 里可搜索）；也有手动"同步标注到 Immich 描述"入口。
- **CLIP 搜索**：素材库「🖼 Immich 库」标签页走 `/search/smart`。搜索结果按 immich_map 的 id + checksum 标「已导入」（绿徽章 + 禁止再勾选），避免重复导入——iPhone 常把不同视频都叫 IMG_xxxx，光看缩略图/文件名分不清，得靠身份判定。
- 认证：`x-api-key` 头，配置 `IMMICH_URL` / `IMMICH_API_KEY` / `IMMICH_PATH_MAP`（宿主挂载路径映射，零拷贝直读原片用，待配置）。

---

## 4. 音频分析

### 核心原则（用户定死）
**能计算出来的值直接算，LLM 只产出描述**。prompt 必须与音乐实测数据结合。

### 事实层（`src/audio/audio_facts.py`，纯信号计算）
- `compute_beat_grid`：madmom 下拍 → 小节长 → **体感 BPM**（240/小节秒数；解决过"报 128 实际体感 70"的错报）。
- `compute_energy_curve` / `find_climax`：RMS 能量曲线、实测高潮点。
- `section_boundaries`：librosa novelty 分段，**吸附到下拍**。
- `section_stats`：每段实测能量/密度。

### LLM 层
只给测好的分段命名和描述情绪（`AUDIO_SECTION_NAMING_PROMPT`），不允许编数值。实测边界跳过 Step 2.6 的吸附纠偏。

### 节奏映射（防"机关枪均匀切"）
- **卡点原则（用户定死）：节拍是切点的网格，不是节拍器**——切换镜头的时刻要落在节拍上，但音乐绝不决定切换频率。切点从"最强 keypoints 里按最小间距贪心挑选"，本来就是"多里选少"。
- 镜头时长锚定小节，锚点按曲目强度缩放：`intensity = 0.6·tempo(70→140bpm 归一) + 0.4·能量对比度`，`anchor = 2 − intensity`——狂躁曲峰值 1 小节，舒缓曲峰值 2 小节 / 平静 4 小节。旅拍回忆 + 舒缓音乐绝不允许出现 <2s 碎片（曾出现 14×1.5s 机关枪切，见下）。
- burst/breath 呼吸感成组（连续 4 段快切后并入一个长镜头）。
- E2E 验证过：9 段结构，Drop 落在实测 climax 上。

### 声音高光与原声闪避（产品定位核心：情感共鸣）
**动机**：旅拍回忆的共鸣点在真实声音——同伴的话、笑声、环境声。BGM 全量覆盖会把它们全部抹掉。

**检测**（`src/audio/sound_highlights.py`，纯信号，零新依赖）：
- 两级：①粗筛 = 人声频带能量比 >0.5 + 频谱平坦度 <0.25 + 低频占比 <0.6 + 自适应响度门（兼容手机压缩音频）；②验证 = pyin 基频周期性。
- **校准记录**：mean voiced_prob 对纯净语音也只有 0.23-0.29（只有元音是浊音帧，均值被辅音/停顿稀释），风噪/引擎 0.01-0.06 → 阈值 0.18。TTS 语音/粉噪混语音/纯粉噪/真实风噪四组地面真值全对。
- 无人机素材通常**没有音轨**（正常现象，静默跳过）；语音高光主要来自手机/手持素材。
- 缓存 `Output/analyzed/{hash}/sound_highlights.json`，`analyze_video` 收尾时自动生成；渲染端对老注释缺文件时按 basename→分析目录映射惰性补算。

**选材**：trim_shot 内部场景带 `sound_highlight` 标注（"真实人声/笑声——优先选这段"），工具说明写明渲染器会闪避放行原声。

**渲染闪避**（render_video.py）：
- `_compute_duck_windows`：clip 源区间 ∩ 高光段 → 换算成成片时间轴窗口（镜像 xfade 缩短逻辑），重叠 ≥0.5s 才算，相邻窗口合并。
- 窗口内 BGM 降到 25%、原声升到 100%，两侧 0.3s 线性斜坡；窗口外一切照旧。
- 前置条件：无声源的切片在提取时补 anullsrc 静音轨（音频流同构，concat/xfade 音频链才不崩）；xfade 路径的音频按转场时长裁尾后硬拼（与画面时间轴对齐）。
- 与 hook 对白模式互斥（对白模式会前插片段挪时间轴，其优先）。
- sidecar `.render.json` 记录 `duck_windows` 供 UI 可视化。
- **E2E 验证**：合成含 TTS 语音的测试源渲染两版对比，窗口外包络差 0.000、窗口内 0.598——闪避精确命中。

### 脏缓存教训（2026-07-05）
Auld Lang Syne 舒缓翻唱被切成 14×1.5s 碎片，根因不是配速公式而是**重构前的旧音频缓存**：LLM 把它瞎猜成 "Upbeat pop-rock"、没有任何实测 BPM，小节锚定从未生效。规则：`analyze_audio` 检查 captions.json 顶层 `facts` 键，缺失 = 事实层之前的毒缓存，**无条件重跑**。任何大重构改变输出格式时，必须同步加缓存版本判据。

---

## 5. 镜头选取编排

### 角色链
```
merge_scene_summaries → Screenwriter（shot_plan：每镜头 content/emotion/时长/related_scene）
  → ParallelShotOrchestrator（编排）→ EditorCoreAgent × N（每镜头一个子 agent）
  → Reviewer（审查）→ shot_point.json（每 clip 带源视频路径 + 局部起止）
```

### 选材优先制（curation-first，架构反转）
- **动机**：旧流程是"编剧虚构理想镜头 → 30 个 agent 找素材匹配虚构"——素材不符警告、agent 失败、兜底全是这个倒置的代价。回忆类视频里**素材本身就是故事**。
- **高光池**（`src/curation.py`）：枚举所有源的 dense_segments，逐段打分 `0.40·VLM内容分 + 0.40·实测画质 + 0.15·声音高光 + 0.05·有人`；VLM<3 分或实测<3.5 分直接出局；带拍摄时刻。按源缓存 `{analysis_dir}/highlight_pool.json`（含实测分，重跑秒回）；项目级合并+映射到 merged scene 索引，写 `Output/Output/{project}/highlight_pool.json`。
- **编剧降级为编排**：`generate_shot_plan` 检测到池文件（按 scene_folder_path 的父目录约定，零签名改动）就注入"真实瞬间清单"（每段 id/时间/时长/实测分/REAL VOICES/拍摄时刻/描述，上限 25 条），要求每个镜头尽量选定一个 `anchor_id`（同一瞬间不得复用）；解析后 `_attach_anchors` 把 id 解析成 `{video_path,start,end}` 挂到 shot 上（未知/重复 id 剥掉走 agent 路径）。
- **锚定镜头零 LLM**：编排器 `_anchored_pick` 在瞬间内滑动目标时长窗口（瞬间不够长则围绕中心对称扩展），避开已用区间+间距，多候选时选实测最清晰的切片；结果标 `"anchored": true`。全部滑动位置都冲突 → 回落到正常 agent 路径。冲突重试轮（guidance_map 非空）不走锚定。
- **效果**：大多数镜头确定性完成（快、零失败、零 token），agent 只处理无锚镜头和冲突重试；"素材可能不符"应大幅消失（content 描述的就是真实画面）。
- 开关 `CURATION_FIRST`（默认 True，False = 旧流程）。

### 时序叙事（旅程时间脊柱）
- **动机**：跑马灯式回忆应当按旅程时间推进（Day 1 → Day N、早 → 晚），此前镜头顺序完全由编剧虚构。
- **拍摄时间提取**（`src/utils/capture_time.py`，零网络）：①文件名模式（DJI_YYYYMMDDHHMMSS / VID_ / PXL_ / 通用 8+6 位）②ffprobe 容器 creation_time（iPhone .MOV）③拿不到 = None（编剧可自由摆放）。文件名时间是当地时间、容器时间常是 UTC——**不做时区换算**，同一旅程内排序一致性才是关键。
- **场景级时刻** = 文件录制起点 + 场景片内偏移，`merge_scene_summaries` 时写入每个场景的 `capture_time`。
- **旅行聚类**：拍摄日期间隔 >14 天 = 新旅程（`build_trip_labeler`），混合素材库标 "Trip 2 · Day 1 · 12-13 15:07" 而不是荒谬的 "Day 411"。
- **进 prompt**：`load_scene_summaries` 给每个场景加 "Shot at:" 行 + JOURNEY CHRONOLOGY 规则（默认时序前进，指令明确要求时可打破）；`generate_shot_plan` 的分段场景描述同样带拍摄时刻。

### 编剧提速 + 稀疏场景 id（2026-07-05，血泪教训）
- **症状**：AI 编剧一跑 6 轮 LLM 调用（选音乐段 + 结构提案×2 + 分镜脚本×3），慢（分镜每次 80-97s）、烧 token、效果还差，模型推理里反复纠结「late section (scenes 2-3) 缺失」和「重复的 Scene 0」。
- **根因一：可用场景 id 是稀疏的，但计数用了文件总数**。合并场景夹 `merged_scenes/` 有 scene_0..3，但 `load_scene_summaries` 会跳过 `importance<3`/不可用/空摘要的场景——实际可用可能是 `{1,2}`。旧代码返回**文件总数 4**，prompt 说「场景 0-3 共 4 个」、`check_scene_distribution` 按稠密 0..3 要求三段覆盖，于是逼模型选它**从没见过**的 scene 0/3 → 永远不可能满足 → 每次重试都空转。
- **根因二：merge 不给场景重编号**，每个 `merged_scenes/scene_N.json` 里 `scene_id` 都是 0，`[Scene {scene_id}]` 展示成一堆「Scene 0」，模型分不清也没法引用（`related_scenes` 其实要的是**文件号**，见 `generate_shot_plan` 读 `scene_{idx}.json`）。
- **修法**：`load_scene_summaries` 改为返回**可用场景的文件 id 列表**（稀疏，如 `[1,2]`），展示用文件号而非 `scene_id`；prompt 用 `AVAILABLE_SCENE_IDS_PLACEHOLDER` 明确列出「只能从这些 id 选」；`check_scene_distribution(proposal, available_ids)` 只针对真实可用 id 校验——≥3 个场景按**位置**分三段各要一个，<3 个只要求「每个都用到」（一定可满足）。
- **顺带修**：`_check_scene_load` 识别「重分配也救不了」（总请求秒数 > 引用场景总预算，或无空闲场景）时直接放行、不再空转重试；spare/budget 只算**被引用的**场景，不会建议模型去用跳过的场景。
- **效果**：少素材项目从 ~6 轮压到 ~3 轮；更关键的是校验从「不可能满足」变「可满足」，反馈不再自相矛盾，输出质量也提升。**规则：任何按「场景数」推导的逻辑，都必须用「可用场景 id 集合」，绝不用文件总数或假设稠密 0..N-1。**

### 分镜输出瘦身：决策 JSON + 确定性回填（2026-07-06）
- **症状**：BGM 融合后项目 235s → 66 个镜头槽位，分镜回复被 `max_tokens` 截断（`finish_reason=length`），24576→49152 翻倍重试，每次重试重付几万 token 输入 + 推理模型思考也翻倍。
- **根因**：旧 prompt 让模型每镜头输出 ~630 字符散文（content/visuals/visual_beat/emotion/time_duration），其中真正的决策只有 `anchor_id + scene` ~20 字符；`time_duration` 更是让模型**抄输入**（prompt 要求"必须精确等于音乐段落时长"）。66 镜头 ≈ 9k token 输出 + 成比例的思考 token。
- **修法**（三处，`src/Screenwriter_scene_short.py` + `src/prompt.py`）：
  1. **决策专用 prompt**（`GENERATE_SHOT_PLAN_PROMPT`）：输出只有 `{"shots":[{"id","anchor_id","scene"}]}`，实测 66 镜头 ~730 token。音乐段落输入也改紧凑单行（`S{i} | 起-止 | tone | energy | rhythm | 描述`），不再 `indent=2` JSON。无锚点池时（curation 关闭）回落 `GENERATE_SHOT_PLAN_PROMPT_LEGACY`（旧全量 schema）。
  2. **`_backfill_shot_fields`**：`time_duration` 从音乐段落实测抄写（权威，顺带消灭抄错）；`emotion`/`visual_beat` 从段落的 `Emotional_Tone`/`energy·rhythm` 回填；`content` 先用段落描述作 agent 路径简报。数量不匹配（镜头数≠段落数）在校验层打回重试——回复很小，重试便宜。
  3. **`_attach_anchors._commit`**：锚定成功即用 moment 的实测 `desc` 覆盖 `content`、`motion.type` 写 `visuals`（`camera pan_left`）、`scene` 权威覆盖 `related_scene`——下游（转场选择器 / agent prompt / UI 焦点卡片）拿到的描述比模型复述更忠实。
- **效果**：输出 9k→<1k token，截断机制上不可能。**规则：模型只输出决策，凡输入里已有或可实测的字段一律确定性回填，禁止让模型复述。**

### 重试纪律（2026-07-06，用户定死）
- **禁止盲重试/翻倍重试**：掩盖 bug + 成倍烧 API。旧的 `finish_reason=length → max_tokens 翻倍重试` 梯子已删——瘦身后截断=真异常信号（模型输出循环/配置错误），立即抛错并把 prompt+截断回复发到工作台。
- **仅网络类瞬时错误可重试**（connection/timeout/5xx/429），最多 2 次，且日志高亮「每次重试都会重新计费整个 prompt」——绝不让用户以为一切正常。其余错误（认证/参数/空回复）一律快速失败，错误信息写明排查方向。
- **修复性重试（带反馈的重发）允许但必须点名**：分镜校验失败/结构提案不合法时带具体反馈重发，日志明示「修复性重试，会重新计费」。原样重发同一请求 = 禁止（结果不变，纯烧钱）。
- **断点续跑**：编剧三步（选音乐段落/结构提案/分镜脚本）逐步写 `{output}.progress.json`（按 instruction 键控，换指令自动失效）；失败后重跑零成本跳过已完成步骤，成功后删除（最终 shot_plan.json 即检查点）。实测：分镜步失败→重跑，前两步零 API 复用。
- **实现**：`_call_agent_litellm`（错误分类+抛错）、`generate_shot_plan_with_retry`（空回复抛错）、`generate_structure_proposal_with_retry`（异常不再吞掉盲重试）、`Screenwriter.run`（progress checkpoint）。

### 槽长-供给匹配：长镜链 + 时长保险丝（2026-07-06）
- **症状**：节奏参数调到 5-9s 后，180s 项目 29 镜中 25 个锚点比槽短（池时刻中位 4.0s、最长 <6.4s），实际镜头平均 6.02s vs 计划 6.4s，累计漂移 ~19s——切点从第 3 镜起全面脱离节拍网格；另有 agent 提交过 0.78s 闪帧镜头（目标 6.5s）无人拦截。
- **根因**：池的时刻长度天花板 = 单个密集描述段的跨度，与节奏参数（槽长）没有联动；agent/兜底路径没有时长下限校验。
- **修法**：
  1. **长镜链（池 v8）**：同一检测镜头（同 ckpt 文件）内连续的密集段合并成 ≤12s 的链候选，逐秒干净扫描照常裁边；**绝不跨 ckpt 文件**——文件边界就是检测到的硬切，"时刻"里不能包含剪辑点。旧池（≤v7）全量重建（v5/v6 只补签名的捷径已废除，否则会把无链池标成当前版）。
  2. **精华加分带宽 3-6s → 3-9s**（稳定运动的长镜正是慢节奏槽要的）。
  3. **编剧菜单 LENGTH FIT 规则**：优先选时长 ≥ 槽长的时刻（菜单本就展示时长），不够长才接受短时刻+未实测补边。
  4. **时长保险丝（orchestrator）**：任何路径（agent/锚定/兜底）提交的镜头 < `MIN_ACCEPTABLE_SHOT_DURATION` 即弃用转确定性兜底；兜底也短则标 FAILED——宁可画布上亮红灯也不出 0.8s 闪帧。
- **顺带**：AI 转场 prompt 明确 hblur 是速度特效，平静曲目几乎只用 fade/dissolve/cut（用户口味）。

### 编排器（`ParallelShotOrchestrator.run_parallel`，src/core.py）
- **按源分组**：同源镜头串行（前面的选择进后面的禁选区 → 结构上杜绝同源重叠），不同源并行（`PARALLEL_SHOT_MAX_WORKERS`）。
- **容量守卫**：派发前按"每源需求 vs 供给"重平衡，把超订源上最小的镜头挪到最空的源（解决"最后一个镜头只剩边角料"）。大镜头先选（长窗口优先）。
- **冲突检测**：硬冲突 = 真重叠（必须重试）；软冲突 = 同源间距 < `SHOT_MIN_GAP_SEC`（重试轮不成就照常提交，**永不因间距丢镜头**）。输掉的按质量分（主角占比 + 时长贴合）判定。
- **重试轮**：`PARALLEL_SHOT_MAX_RERUNS`（默认 1，每轮是完整 agent loop，贵）。
- **自愈**：启动时清理历史 checkpoint 里的同源重叠条目并重选。

### 子 agent 预算（EditorCoreAgent._run_shot_loop）
- 3 次模型调用预算（`AGENT_MAX_ITERATIONS`）：典型 = 场景检索 → 精细修剪 → commit。
- **宽限提交（grace）**：预算耗尽未 commit → 无条件追加一轮，`tool_choice` 锁定 commit；若提供商拒绝强制 tool_choice（DeepSeek 思考模式 400"Thinking mode does not support this tool_choice"）→ 自动去掉该参数重试，靠 `EDITOR_FORCED_COMMIT_PROMPT` 引导。
- 宽限轮审查用 lenient 模式。

### Reviewer 规则（src/Reviewer.py）
| 检查 | 规则 | lenient（最终宽限）时 |
|------|------|------|
| 格式 | `[shot: HH:MM:SS to HH:MM:SS]`，最多 `MAX_SHOTS_PER_CLIP` 段拼接，段间隙 ≤2s | 同 |
| 时长 | 与目标差 > `ALLOW_DURATION_TOLERANCE`(1s) 拒 | 只差时长且 ≥2s 底线 → 放行 |
| 重叠 | 与已用区间重叠必拒 | **仍然必拒**（重复画面比缺镜头更糟） |
| 实测画质 | 低于 `STABILITY_MIN_SCORE` 拒（见 §6） | 跳过（糊片段好过缺镜头） |
| 人脸 | vlog 模式跳过；film 模式 VLM 主角占比 ≥ `MIN_PROTAGONIST_RATIO` | — |

### 100% 成功保证（确定性兜底 `_fallback_pick`）
agent 无论因何失败（模型报错/审查拒绝/冲突重试耗尽）→ 编排器在空闲区间确定性截取：
- 偏好顺序：剧本指定场景 → 同源其他场景 → 任意源；避开已用区间 + 间距；候选窗口先测画质（≥5 分直接用，全糊取最清晰）。
- 结果标 `"fallback": true` 写入 shot_point。
- 唯一合法失败：所有源的空闲区间都 < 2s（素材真用光，属不可抗力）。
- 不经过任何 LLM，**不可能因 API 抖动失败**。

### 工具面（agent 可用）
- `semantic_neighborhood_retrieval`：读场景 JSON（探索范围 `SCENE_EXPLORATION_RANGE`）。
- `fine_grained_shot_trimming`：**缓存优先**——预标注 dense_segments 覆盖 ≥80% 就直接复用零 VLM 调用；内部场景带 `measured_quality`（§6）。VLM 兜底路径带覆盖率校验（≥50%）。
- `commit`：解析/夹紧到源片尾/超目标 ≤1s 自动裁尾/记录已用区间。

---

## 6. 实测画质门槛

### 动机
VLM 的 visual_quality 是**看静帧**打的分，看不见帧间剧烈晃动（无人机修正、手持抖动）。但剧烈晃动有物理指纹：**运动模糊**。

### 指标（`src/utils/stability.py`）
- **相对清晰度** = 区间拉普拉斯方差中位数 ÷ 该源视频自己的 p70 基线（24 帧全片采样，缓存）。**必须按源归一**：雪原天然比秋林纹理少，跨源比绝对值无意义。
- **光流紊乱度** = 稠密光流幅值的 std（w/s），惩罚乱动；顺滑横移/航拍不受罚。
- **视速** / **倾斜**：见下两小节（2026-07 新增维度）。
- 得分 0-10：`10·min(1, rel/0.75)^0.8 × (1 − 0.5·disorder_pen) × (1 − 0.6·speed_pen) × (1 − 0.7·tilt_pen)`；按 (视频, 区间±0.1s) 进程内缓存。

### 视速惩罚（apparent speed，2026-07 新增）
- **动机**：用户实测反馈——顺滑但极快的无人机横扫，清晰度/紊乱度双满分，放进平静的回忆混剪里却让人头晕。清晰≠适合。
- **实现**：`speed` = 稠密光流幅值**中位数**换算成"屏宽/秒"（`median(mag)/320·fps`）。温柔滑移实测 0.02–0.29 w/s（不扣分）；超过 **0.30 w/s** 起罚：`speed_pen = min(1, (speed − 0.30)/0.45)`，乘进总分因子 `(1 − 0.6·speed_pen)`。与紊乱度正交——紊乱度用 std 抓"乱动"，视速用中位数抓"整体太快"。
- **调优入口**：起罚点 `0.30` 与斜率 `0.45`（`stability.py` `_measure` 内）；返回字典带 `speed` 字段可直接观察实测值。

### 倾斜惩罚（camera roll，2026-07 新增）
- **动机**：用户发现一个云台歪斜的镜头（实测竖直结构偏角 9.1°，正常素材 1.8°），清晰度指标完全看不见"画面是歪的"。
- **实现**：每采样帧 Canny(60,160) + `HoughLinesP`(threshold=40, minLineLength=40, maxLineGap=6) 找线段，取与竖直方向夹角 ≤25° 的"近竖直结构"（树/杆/建筑棱），偏离竖直的角度取中位数。**可信度门槛**：单帧 ≥6 条合格线才算测到、≥2 帧测到才判倾斜——开阔水面/天空这类无竖直结构的画面测不到 = 不惩罚（`tilt=None, pen=0`），绝不误杀。偏角 >4° 起罚：`tilt_pen = min(1, (tilt − 4.0)/5.0)`，因子 `(1 − 0.7·tilt_pen)`。
- **调优入口**：起罚角 `4.0°`/斜率 `5.0`、单帧线数门槛 `6`、帧数门槛 `2`；返回字典 `tilt` 字段为实测偏角（None=测不到）。改公式记得**升 `src/curation.py` 的 `_POOL_VERSION`**（v5 就是为倾斜惩罚升的），否则旧高光池缓存里的实测分不会重算。

### 逐秒扫描与干净段裁剪（池构建时，2026-07 新增）
- **动机**：一个 6s 的 dense 段里常藏着 1-2s 的构图调整（猛甩），整段中位数会把它"平均掉"——用户核心诉求是**只保留每个 take 里真正好的部分**，而不是整段要么全收要么全弃。
- **实现**（`stability.py` `quality_per_second` / `longest_clean_run` + `curation.py` `_source_pool`）：
  - `quality_per_second`：区间按 1s 窗逐秒测分（samples=1，窗口走区间缓存——相邻 moment 重叠部分共享计算）。
  - `longest_clean_run(per_sec, floor=3.5)`：找最长连续"干净秒"（≥3.5 分；测不到的秒算干净）。
  - 池构建时每个候选 moment 先逐秒扫描，**trim 到最长干净段**：没有任何干净秒 = 纯调整镜头直接丢弃；干净核心 <1.6s 丢弃；有裁剪则标 `"trimmed": true`，之后再对裁剪后的区间做整段实测（samples=3），<3.5 分仍出局。
- **调优入口**：干净秒门槛 `floor=3.5`（`_source_pool` 调用处）、最短可用时长 `1.6s`。逐秒窗口只采 1 帧对，延迟可控；如嫌慢先怀疑区间缓存是否命中。

### 校准记录（2026-07-05，滑雪项目实测，10/10 人工判断命中）
| 片段 | rel_sharp | 得分 | 判定 |
|------|-----------|------|------|
| 无人机俯冲修正（用户吐槽） | 0.04 | 1.0 | 拒 ✓ |
| 度假村远景（偏糊） | 0.40 | 6.0 | 可用（边缘） |
| 顺滑航拍/跟拍/静止 | 0.81–1.38 | 8.8–10 | 好 ✓ |

### 接入点
1. Reviewer 硬门槛（`STABILITY_CHECK_ENABLED` / `STABILITY_MIN_SCORE=3.5`，lenient 跳过）。
2. trim_shot 缓存分支每个内部场景带 `measured_quality {score, verdict}`（3 采样限延迟），工具说明警告 <4 分会被拒 → agent 事前避开。
3. 兜底选取偏好 ≥5 分窗口。

### 调优入口
- 更严：`STABILITY_MIN_SCORE` 3.5 → 5。
- 误杀艺术性快扫/浅景深：调 `_JERK_HALF_SCORE` 已废弃（v1 平移法失效——POV 前进是径向流，相位相关读不到）；现公式看 `rel/0.75` 拐点与 disorder 的 `0.12/0.25` 起罚带。

---

## 7. 渲染管线

### 结构（`render/render_video.py`）
```
shot_point.json（多源 clip 各带 video_path）
  → 每 clip 独立 ffmpeg 提取重编码（无预合并）
  → 拼接：硬切 = concat demuxer 流拷贝；有转场 = settb=AVTB + 链式 xfade 单次重编码
  → BGM 混音（窗口裁剪 + loudnorm 响度匹配 + apad/atrim 对齐）
  → 成功后写 sidecar {output}.render.json
```

### 血泪规则（都是修过的 bug，别回退）
- **每个切片编码分支必须 `-pix_fmt yuv420p`**：10-bit 源（DJI D-Log yuv420p10le）会让 x264 静默输出 High10 profile，concat 拷贝后流中途换 profile → 浏览器/WMF/NVDEC 全部死在换源那一帧（软件解码全程无错，渲染自检发现不了）。
- **每个提取分支统一 `-r video_fps`**：混帧率进 concat 拷贝会 corrupt 时间轴（片段变速、总时长膨胀）。
- **xfade 前每输入 `settb=AVTB`**：concat filter 输出 1/1000000 时基，裸 h264 是 1/12800，不统一 xfade 直接硬报 "timebase do not match"。
- **音频 AAC 48kHz 封顶**：96kHz WMP 拒播。
- `-ss` 放 `-i` 前 + 重编码 = 帧精确剪切。

### 转场系统
- 调色板 `TRANSITION_PALETTE`：12 种精选 xfade（fade/fadeblack/fadewhite/dissolve/zoomin/radial/circleopen/hblur/smoothleft/smoothright/hlslice/distance），全部 ffmpeg 内建。
- **AI 选择** `_ai_pick_transitions`：把每个切点前后镜头的 content/emotion/visual_beat 给 LLM，规则=快切为主、转场只做情绪转折点缀、同款不连用、时长 0.3-0.6s。主模型（AGENT）失败自动降级 `TRANSLATE_MODEL`（flash）；max_tokens 8000（推理模型思考会吃掉小额度导致正文为空——曾因此静默回退硬切）。
- 统一叠化：全切点 `fade:0.4`。
- xfade 会吞掉每切点 ~duration 时长（重叠淡化），切点相对节拍轻微漂移，已知取舍。

### 电影感层（画面风格 / 黑边 / 淡入淡出）
- **实现原则：全部在切片提取阶段追加滤镜**——切片本来就要重编码，调色/黑边/淡入淡出零额外遍数。三个提取分支在 subprocess.run 前有统一注入点（有 `-vf` 则拼接，无则插入）。
- **调色 `COLOR_GRADES`**：纯 ffmpeg 滤镜链、不依赖外部 LUT 文件——teal_orange（青橙：阴影偏青高光偏橙）/ film（胶片：提黑压高光降饱和）/ warm（暖阳：色温 5400K）。片尾/片头卡不调色。
- **2.35:1 黑边 `LETTERBOX_FILTER`**：crop 到 2.35:1 再 pad 回 16:9（容器仍是平台友好的 16:9）；仅 16:9 比例开放。
- **淡入淡出**：首个主镜头 fade-in 0.5s、最后一个主镜头 fade-out 1.2s（都在切片级，不触发成片级重编码）；BGM 在混音阶段 afade 收尾 1.5s。默认开。
- 渲染请求字段 `color_grade` / `letterbox` / `fades`，sidecar 记录。像素级验证：黑边区亮度 0.0、首帧 0.0、末帧 5.1。

### sidecar 元数据（UI 可视化数据源）
`{output}.render.json`：`transition_mode` / `transitions[]`（每切点实际用的）/ `audio {path,start,duration}` / `clips`。
`GET /api/render/outputs` 附带为 `render_meta`（并 ffprobe 补 `audio.total`，有 mtime 缓存）。

### 其他
- `source_quality`: proxy（默认）| original（Immich 原片替换）。
- 输出文件名 `output_{ratio}_{md5(shot_point名)[:8]}.mp4`——按指令隔离，不同指令不互相覆盖。
- 片尾视频：拼接前统一格式重编码（scale/pad/fps/采样率对齐）。

---

## 8. Web UI 数据链路

### 渲染页（RenderView）
- **成片预览一屏布局**：左 = 播放器（16:9 最大 820px）+ 字幕条 + 时间轴；右 = 固定 320px 高的「当前镜头」焦点卡片 + 单行索引列表（点击跳转）。**所有随播放变化的容器必须固定高度**——min-height/自适应高度会在每个切点把页面顶得一跳一跳（焦点卡片、字幕条都栽过）。
- **字幕条**：dense 描述含多个时间段标记，按 `源时间 = src_start + (playhead − out_start)` 只显示当前段（`parseDenseSegments`）。
- **时间轴（ShotTimeline, Charts.tsx）**：按源分行的 clip 甘特图 + 播放游标 + 琥珀菱形转场标记（tooltip 中文名+时长）+ 音乐窗口条（紫色高亮 = 实际用的那段，含游标）。
- **素材不符标记**：编剧意图（content+visuals+visual_beat+emotion 全字段）与 VLM 描述做词干化词汇重叠，< 6% 且文本够长才标 ⚠（启发式，误报源于"编剧写创意文案 vs VLM 写字面记录"的天然风格差）。
- **列表自动滚动**：只滚容器自身 `scrollTop`，**禁用 scrollIntoView**（它会连页面一起滚）。

### 中文翻译层
- `POST /api/translate`：批量（25/请求）DeepSeek V4 Flash 翻译，时间戳标记保留原样；磁盘缓存 `Output/cache/translations_zh.json` 按内容哈希——**每段文字一生只翻一次**（实测首次 11.8s，命中 0.01s）。
- 前端 `useZh()/useZhFlag()`（ClipInspector.tsx）：全局中文/原文开关（localStorage + 自定义事件跨组件同步）；纯中文/无英文单词的文本跳过。匹配度计算永远用英文原文。
- 扩展方式：任何组件把文本数组传进 `useZh` 即可。

### 项目编辑画布（WorkflowCanvas · 编剧阶段可观测性）
- **动机**：编剧阶段（AI 编剧）过去是黑盒——画布只在第一次 LLM 调用后画出「AI 编剧」节点、只显示调用次数，调用之间/调用前完全沉默，用户不知道进行到哪一步。
- **命名子步骤事件**：`Screenwriter_scene_short.py` 的 `_sw_stage(label)` 在每个真实子步骤开头发一个 **`event="stage"`** 进度事件（选择音乐段落→生成结构提案→生成分镜脚本→挑选开场对白→保存分镜脚本；含缓存复用/补全开场白分支）。走 `@@PROGRESS` → `_apply_progress_ev` 存到 `tasks.screenwriter_llm.stage_label`——**独立 event，不进 traces**，所以不污染 LLM 调用 trace / 提示词-回复 workbench。
- **画布呈现（编剧=单节点时间轴）**：编剧是**一个自绘节点**（`screenwriter`），内部渲染成 station-and-rail 竖向时间轴——每个子步骤一个「站点」，用 CSS rail 串联，当前段有琥珀流光（`.sw-rail-flow`）、当前站点有脉冲光环（`.sw-station-active`）、整节点 `.node-breathe-amber` 呼吸。**故意不用 RF 子节点+边**（那样会暴露丑陋的 handle 圆点）；连接线全内部 CSS 绘制，完全可控。节点在**第一个 stage 事件**就出现。子步骤状态由 `SW_STEPS.indexOf(stage_label)` 推导：之前=done✓、当前=running⟳、之后=pending，`total>0`（编辑阶段已开始）时全部 done。整节点自成左列，与右侧「AI 编辑」用间距 + 虚线连边隔开。
- **每个子步骤的数据**：`_sw_emit` 给每个 LLM 调用事件盖上当前 `stage` 标签（`_SW_STAGE["cur"]`，`_apply_progress_ev` 存进 trace 的 `stage` 字段）。前端 `groupSteps` 把调用按 `stage` 归组，每个站点显示「N 次调用 · Xs」；点击节点打开 Agent 工作台，工作台里每条调用都带 `stage` 徽章，等于把调用按子步骤组织展示。
- **上游素材列（视频理解 / 音乐分析）**：画布最左边一列是本项目的**源素材节点**（`asset` 节点：视频=天蓝、音频=紫）。视频显示**缩略图海报**（`/api/assets/thumb` 的 ffmpeg 首帧 JPEG，磁盘缓存 + 浏览器缓存 + 节点 memo，**绝不嵌 `<video>`**——多个视频解码器会卡）+ Q 分角标 + 「视频理解」标签 + hover 播放提示；音频显示装饰波形。缩略图端点在 `hash` 缺省时**从 path 自动算 hash**，未标注素材也能出图。数据来自 `POST /api/assets/by_paths`。
  - **实时分析状态**：流水线「视频理解」阶段 `local_run.py` 逐个 `analyze_video(vp)` 前后发 `video_analysis` start/done 事件（`label=basename`；音频发 `audio_analysis_asset`）。前端按 basename 匹配到素材节点：**正在分析的那个视频显示「分析中」+ 天蓝呼吸边框 + 脉冲遮罩**，已分析显示「已分析」，其余静态（Q 分/未标注）。画布顶部提示也从「等待编剧阶段」改成「正在分析素材：<文件名>」。缓存命中的视频瞬间 done，只有真正在跑的会停留在「分析中」。**点击素材节点** → overlay 弹出 `AssetPanel`（媒体预览 + 标注表 + 主色调色块点击复制）。布局：assets(x10) → 编剧(x286) → 编辑(x640) → 镜头列(X_ROOT 900)。
- **每个镜头的最终选片（clip 结果节点 + 预览）**：每个镜头 lane 之后插一个 `clip` 节点，显示 AI 选定的**源素材名 + 时间片**（`00:12–00:18 6.0s`，多片段/兜底都标出），接 lane → clip → merge。数据来自 `GET /api/pipeline/shots?project_id`（读 shot_point.json，运行时轮询 2.5s）。**点击 clip 节点** → overlay 弹出 `ClipPlayer`，用 `<video>` seek 到 start、播到 end 停，可重播。clip 节点还有一条**细淡曲线**连回左侧对应的源素材节点（按 basename 匹配；`clip` 节点专用 `back` 左侧 source handle → asset 右侧 target handle，走 clip 左侧不绕圈）。overlay 优先级：clip 预览 > 素材标注 > Agent 工作台。
- **布局**：`X_ROOT`（镜头列）与 `EDITOR_X`（编辑 hub）整体右移，给编剧框腾出左列；框高 `SW_HEADER_H + SW_STEPS.length·SW_ROW_H + SW_PAD`。子步骤「运行中」判定用 `total===0`（还没产出镜头 = 编剧阶段），比窥探最后一条 trace 的 phase 更可靠（子步骤间会静默）。
- 新增子步骤时：`_sw_stage` 的 label 必须与 `nodes.tsx` 的 `SW_STEPS` 数组保持一致（子节点按它生成、状态按它排序）。

### 素材页（AssetsView）
- 全页详情视图（弃用抽屉——用户明确否决抽屉交互）；海报卡片墙（缩略图 `/api/assets/thumb`、状态色条、按哈希路由的实时阶段显示）；命令栏 ⚙ 模型/并发设置;JobDock 全局任务坞。
- 标注状态按 `content_hash` 键控（`meta.files`: p/r/d/f），**绝不用文件名匹配**（曾致"标注一个全部显示标注中"）。

### 扫描持久化（刷新不重扫）
**动机**：素材列表原先只存 React 内存 state，一刷新就回到"点击扫描"空状态，必须重扫；而扫描本身很贵——每个文件 SHA-256 读 1MB + `_probe_structured` 为每个视频跑 ~8 次 ffprobe 子进程（Windows 上每次 spawn 50-100ms，单视频 ≈0.5-1s），且文件没变也照跑。刷新即重扫 = 双重浪费。

**三层解法**（缺一不可）：
1. **磁盘元数据缓存**（`src/asset_manager/scanner.py`）：`scan_asset_directory` 按绝对路径缓存 `{size, mtime, hash, meta}` 到 `Output/asset_index/scan_cache.json`。文件 `(size, mtime)` 未变 → 直接复用缓存对象，**跳过 hash + 全部 ffprobe**；变了/新增才全量 probe。收尾一次性落盘，删掉的文件自动剔除。带 `_CACHE_VERSION`（元数据结构变更时旧缓存作废）；`use_cache=False` 可强制全量重扫。**mtime 存 int 秒**（避免 JSON 浮点精度误判），配合 size 双条件足够稳。Streamlit/CLI 走同函数一并受益。
2. **后端恢复端点**：`GET /api/assets/scan`——服务内存有上次扫描（`SCANNED`）就秒回；重启后内存空了就对配置根走一次缓存扫描（因①已很快）。与既有 `POST /api/assets/scan`（用户手动扫描）复用 `_assets_with_annotations()` 合并云端+本地注释。
3. **前端挂载恢复**：`AssetsView` mount 时先 `GET /api/assets/scan` 拉回上次结果直接铺网格，不再回到空状态。

**效果**：刷新→内存秒回；服务重启→磁盘缓存秒级；文件真变→只重 probe 变动的那几个；唯一全量成本是**首次冷扫描**（不可避免，仅一次）。缓存在 `Output/` 下，已被 gitignore 覆盖。

---

## 9. 任务系统与进程健壮性

### Job 模型（server/main.py）
- 内存 JOBS + 磁盘快照 `Output/jobs/*.json`（保留 12 个）；`GET /api/jobs/{id}?since=N` 增量拉日志。
- 重启恢复：快照里 status=running 的标记为 error + "[服务重启] 任务跟踪中断"提示（跟踪丢失≠工作丢失，见下）。
- 中断批次浮出：`/api/jobs/latest/annotate` + unfinished 计数 → 前端横幅提示续跑。

### 进程拓扑与代码热更新
| 代码 | 运行位置 | 改完生效方式 |
|------|---------|-------------|
| `local_run.py` / `render/render_video.py` / `src/core.py` / `src/Reviewer.py`（流水线、渲染链） | 每次任务新 spawn 的子进程 | **无需重启**，下次任务自动用新代码 |
| `server/main.py` / 标注线程链 | 常驻 API 进程 | 必须重启（start_api.bat 自带清端口） |

### 断管保护（关键）
后端重启 → 子进程 stdout 管道死亡 → 下一次 print() 抛 BrokenPipe **杀死本来健康的渲染/流水线**。
两处都套了 `_SafeStream`（吞写错误）：`local_run.py`、`render/render_video.py` main。孤儿任务丢日志不丢结果。

### 标注队列
- 批次运行中新请求进 `_ANNOTATE_QUEUE`（卡片显示"排队中"），当前批完成后在 finally 里链式起新批；**取队列在锁内、执行在锁外**（防死锁）；只合并同 provider 的请求。

---

## 10. 缓存路径全景

| 产物 | 路径 | 失效方式 |
|------|------|---------|
| 视频/音频分析（云端轨） | `Output/analyzed/{sha256}/` | 强制重新标注（force） |
| 视频分析（本地轨） | `Output/analyzed/{sha256}@local/` | 本地重标 |
| 片段 caption 断点 | `Output/analyzed/{hash}/captions/ckpt/*.json` | 随所属分析 |
| 完成标记 | `Output/analyzed/{hash}/analysis_complete` | — |
| 扫描元数据缓存 | `Output/asset_index/scan_cache.json` | 文件 (size, mtime) 变化自动重 probe；`_CACHE_VERSION` 升级作废 |
| 注释索引（云端） | `Output/asset_index/annotations.json` | — |
| Immich 素材分析键 | `Output/analyzed/im-<原片checksum>/`（非代理 hash） | 键随原片身份，删/重下代理不失效 |
| 注释（本地轨） | `Output/asset_index/annotations_local.json` | — |
| Immich 绑定 | `Output/asset_index/immich_map.json` | — |
| 中文翻译 | `Output/cache/translations_zh.json` | 内容哈希，永久 |
| 项目产物 | `Output/Output/{project_id}/`（shot_plan_/shot_point_ 按指令哈希命名） | 换指令自动新文件 |
| 渲染成片 | 同上 `output_{ratio}_{sp_tag}.mp4` + `.render.json` sidecar | 重渲覆盖 |
| 任务快照 | `Output/jobs/*.json` | 自动裁剪到 12 |
| LLM 调用明细 | `Output/logs/llm_calls_*.jsonl` | — |

---

## 11. 关键配置项速查（src/config.py）

| 配置 | 默认 | 说明 |
|------|------|------|
| `VIDEO_ANALYSIS_MODEL/ENDPOINT/API_KEY` | Gemini Flash (bboluo) | 标注 VLM（本地轨运行时临时覆盖为 Ollama） |
| `AGENT_LITELLM_MODEL` | deepseek-v4-pro | 编剧/编辑/选材 agent |
| `TRANSLATE_MODEL` | deepseek-v4-flash | UI 翻译 + AI 转场降级 |
| `ANNOTATE_VIDEO_WORKERS` | 2 | 标注文件级并行进程数 |
| `CAPTION_BATCH_SIZE` | 64（本地轨强制 4） | VLM 并发请求数 |
| `VIDEO_CAPTION_MAX_FRAMES` | 24 | 每镜头送 VLM 帧数上限（0=不限） |
| `AGENT_MAX_ITERATIONS` | 3 | 每镜头模型调用预算（+1 宽限） |
| `PARALLEL_SHOT_MAX_WORKERS` | 4 | 不同源镜头并行数 |
| `PARALLEL_SHOT_MAX_RERUNS` | 1 | 冲突重试轮数 |
| `SHOT_MIN_GAP_SEC` | 2.0 | 同源镜头最小间距（软约束） |
| `ALLOW_DURATION_TOLERANCE` | 1.0 | commit 时长容差（秒） |
| `MIN_ACCEPTABLE_SHOT_DURATION` | 2.0 | 镜头时长绝对底线 |
| `STABILITY_CHECK_ENABLED` | True | 实测画质门槛开关 |
| `STABILITY_MIN_SCORE` | 3.5 | 低于此分的 commit 被拒（0-10） |
| `IMMICH_URL/API_KEY/PATH_MAP` | :2284 | Immich 接入 |
| `AUDIO_SEGMENT_MIN/MAX_DURATION_SEC` | 目标±5s | 成片时长区间 |
| `ASR_BACKEND` | litellm / whisper_cpp | 无对白素材用 whisper_cpp |

---

## 12. 口味记忆

### 动机
用户对某个镜头说"不要"，这个偏好不该只活在一次会话里——同一段素材换个项目又被选中，等于让用户重复教 AI。拒绝要**全局持久**，且区分力度：多数不满是"这段不太行"（降权），少数是"这段永远别用"（硬禁）。

### 实现
- **存储**：`Output/asset_index/rejections.json`（`src/curation.py` `REJECTIONS_PATH`），**全局跨项目**。每条 `{video_path, start, end, reason, ban, ts}`，由换镜接口追加（见 §13）。
- **软拒 = 池内降权**（`build_highlight_pool`）：moment 与拒绝区间重叠 >0.3s → 综合分 **−0.15/次，叠加封顶 −0.45**；原因文本记入 `rejected_overlap`（最多 3 条）供 UI 展示。**惩罚施加在项目池合并阶段而非按源缓存**——新拒绝立即生效，不需要重建池。
- **永久 ban**：措辞含 **不再/别再/拉黑/永久/never** 或以 **`!` 开头**（`server/main.py` `replace_shot` 判定）→ `ban: true`。两处强制执行：
  1. 池构建时**直接剔除**（不进候选，打印 `🚫 excluded by permanent bans`）。
  2. `core.py` `run_parallel` 开头把所有 ban 区间注入 `global_keep_ranges`（全局禁选区）——agent commit、锚定选取、确定性兜底**三条选取路径全部避开**，不是只防高光池一条路。
- **换镜选取时**：所有拒绝区间（不分软硬）+ 其他镜头已用区间一起构成 forbidden 列表，替代镜头按 `SHOT_MIN_GAP_SEC` padding 避让。

### 调优入口
- 软拒力度：`curation.py` 池合并处的 `0.15`/封顶 `0.45`、重叠判定 `0.3s`。
- ban 触发词表：`server/main.py` `replace_shot` 内 `_ban` 判定。
- 手工管理：直接编辑 `rejections.json`（删条目 = 撤销拒绝，改 `ban` 字段 = 升降级），下次构建池/跑流水线即生效。

---

## 13. 换镜头闭环 UI

### 动机
看成片时发现某个镜头不满意，此前只能改指令重跑整条流水线（慢、贵、其他镜头也会变）。需要**单镜头级**的"换掉这个"闭环：说一句原因 → 立即换上更好的 → 偏好被记住（§12）。

### 链路
```
渲染页焦点卡片「换掉」按钮（ClipInspector.tsx，琥珀色）
  → window.prompt 收原因（提示语教用户措辞语义）
  → POST /api/shots/replace {shot_point, section_idx, shot_idx, reason}（RenderView.replaceShot）
  → 后端：写 rejections.json + 从高光池选替代 + 改写 shot_point.json
  → 前端 reloadClipMap + alert 展示新片段（id/分数/描述）
  → 用户重新渲染后生效（渲染读的就是 shot_point.json）
```

### 后端选取逻辑（`server/main.py` `replace_shot`）
- **前置**：项目必须有 `highlight_pool.json`（旧流水线产物返回 400 提示重跑）。
- **原因关键词导向**：
  - 含 **晃/抖/快/晕/歪** → 替代镜头要求实测稳定分 **≥7**（不只是"能用"，要明显更稳）。
  - 含 **重复/一样/相似/雷同** → 同源避让半径从 20s 扩到 **60s**（避开相似画面）。
- 候选按池内综合分降序：时长 ≥ 目标−1s；在 moment 中心对称取目标时长窗口；避开 forbidden（全部拒绝区间 + 其他镜头 clips，padding `SHOT_MIN_GAP_SEC`）。全池无解 → 409（提示换措辞或重跑扩充素材）。
- **写入前备份**：`shot_point.json` → 同名 `.bak`（`shutil.copy2`），改坏可手工回滚。替换后的 shot 标 `replaced: true` + `replace_reason`。

### UI 细节
- 「换掉」按钮只在传入 `onReplace` 时渲染（焦点卡片场景）；prompt 提示语明确教措辞：晃/歪/晕→更稳、重复→避相似、"不再使用"或 `!` 前缀→永久拉黑、其余原因=负分。取消 prompt = 整个操作取消（不写任何东西）。
- 成功 alert 明示"重新渲染后生效；被换掉的区间已进入拒绝名单"——换镜只改 shot_point，不自动触发渲染。

### 调优入口
- 稳定分门槛 `7.0`、相似半径 `60/20s`、时长容差 `1.0s`：都在 `replace_shot` 内。
- 关键词表与 prompt 提示语（`RenderView.tsx` `replaceShot`）需**同步维护**——提示语教的措辞必须真的被后端识别。

---

## 14. 视觉去重与前置品控（锚点预算器）

### 动机
用户反馈（2026-07-05 成片）：一支缓慢航拍的雪山视频被穿插了非常多次——不是同一段重复，而是**整支视频视觉同质**，隔 60 秒取的段落在观众眼里仍是"同一张照片"。暴露两个结构性问题：
1. 既有防重复全是**时间距离**逻辑（相邻同源 20s），而慢航拍上时间距离≠视觉差异——与"平滑快速运镜骗过稳定分"同类的指标盲区。
2. 品控靠**事后打回**（Reviewer 拒绝、剥锚重选）会给 agent 加压、推高失败率。核心原则（用户定死）：**品控前置，让非法选择在菜单里根本不存在，而不是选了再罚**。

### 架构（三层，全部纯计算、零新增 LLM 调用）
```
高光池 v6      每 moment 抽 3 帧算 dHash → 项目池贪心聚类（汉明距 ≤ 阈值 = 同一"视觉场景"）
               → 每簇只保留 top (簇配额+1) 个候选（留一个替补）
    ↓
锚点预算器     build_anchor_budget()：按分数贪心选锚点菜单，硬约束在此一次性消化——
               同簇 ≤ VISUAL_CLUSTER_MAX_USES(2)、同源 ≤ SOURCE_VIDEO_MAX_USES(4)、人声优先保额。
               数学性质：配额是上限 ⇒ 菜单的任何子集自动满足配额 ⇒ 编剧怎么选都合法。
               素材不足时按固定顺序放宽（簇+1 → 源+1 循环）并打日志——唯一合法失败=素材耗尽。
    ↓
编剧           anchors_block 只列预算菜单（带 look C{n} 簇标签）；VARIETY 从硬规则降级为
               摆位建议（同簇两条离远点）——菜单已经干净，规则可以温和，省思考预算。
    ↓
装配修复       _attach_anchors：未知/重复/同簇过近 → 先换预算余量里的异簇替补（修复），
               实在无替补才剥锚回 agent（最后手段）。修复是确定性毫秒级，不弹回 LLM。
    ↓
保险丝         装配完成后校验配额，超额只打警告日志（说明前置环节有 bug），不拒绝不重试。
```

### 实现要点
- **dHash**（`src/utils/stability.py`）：每 moment 取 3 帧，灰度缩到 9×8，相邻像素比较出 64-bit 指纹。两 moment 距离 = 双方帧指纹的最小汉明距。本地毫秒级，帧读取复用 OpenCV。
- **池 v5→v6 升级路径**：只补算 dHash，不重跑逐秒扫描/稳定度测量（那部分很贵且结果不变）。
- **聚类**是项目池级贪心 leader 聚类（按分数降序，距离 ≤ 阈值并入，否则立新簇），跨 clip、跨源都算。
- **预算菜单大小**随预计镜头数缩放（音乐时长 / 平均段长 × 1.5，下限 25）——菜单太小会逼出 `anchor_id: null` 的 agent 自由行，那正是要消灭的压力源。

### 调优入口（都在 src/config.py，参数面板可调）
- `VISUAL_CLUSTER_MAX_USES = 2` —— 同一视觉场景全片最多出场次数（用户体感：两三次到顶）。
- `SOURCE_VIDEO_MAX_USES = 4` —— 同一源视频全片配额（画面多样的源可以多给）。
- `VISUAL_CLUSTER_HAMMING = 12` —— 判定"同一场景"的汉明距阈值；调大=聚得更狠。
- `VISUAL_CLUSTER_MIN_GAP_SHOTS = 6` —— 同簇两次出现的最小镜头间距（治 A-B-A-B 穿插感）。

### 踩坑规则
- 别把配额检查加回编辑器提交路径——那是打回机制回魂。前置环节出 bug 就修前置环节，保险丝日志会指出来。
- 剥锚（strip → agent 自由选）永远是最后手段：agent 自由行不受簇配额保护，选回同质画面等于白干。
- 池 v6 的 dHash 字段是"换镜头闭环"（§13）按簇降权的地基——拒绝一个镜头时可顺带压掉同款机位（待接线）。

---

## 15. 相机运动标注 + 动势匹配 + 正向口味记忆

### 动机
用户看片反馈（2026-07-05）三连：
1. 特别满意的航拍 = 稳定平移/左移/右移/推进、3-5s，"跟专业旅拍博主非常接近"——这些精华应该在**标注阶段**就被识别出来。
2. 镜头切换有个技巧：上一镜头左移，下一镜头也接着左移，不跳跃（= 专业剪辑的**动势匹配** motion continuity，用户自己悟到的，判断正确）。用户点名 #12→#13（向前飞→继续向前推湖面+叠化）为完美示范。
3. 想给满意的镜头**点赞**并给出理由，AI 必须真正理解为什么好，作为以后剪辑的参考。

### 运动测量（src/utils/stability.py，纯本地计算）
- `_global_motion()`：LK 特征点跟踪 + 双信号——RANSAC 相似变换（旋转平移+缩放）和**底部三分之一视差漂移**（低空掠过远景时全局拟合锁在远山上读数为零，近场视差才是真信号）+ 位移散度（推进的扩张率）。
- 基线取 `min(1s, 时长×0.4)`——用户最爱的缓飞在 320px 宽下每帧亚像素位移，相邻帧读不出来。
- `_classify_motion()`：chaotic（紊乱度>0.30，乱晃废料）/ push_in / pull_out / pan_left/right / tilt_up/down / static。**不可感知的缓移诚实标 static**（<0.7px/s@320，对剪辑而言它就是"稳"）——不给 LLM 编方向的机会。
- 实测校准：0373 明显推进→push_in(zoom 0.097)✓；0680 开场→push_in✓；观景台缓移→tilt_down✓；用户最爱的超缓飞行→static（正确：光学上近乎定格）。
- ⚠️ **np.polyfit/LAPACK 在 decord/cv2 之后加载会硬崩(0xc06d007f)**——斜率一律用逐元素运算 `_elem_slope`（铁律10 新款）。

### 池 v7 与"博主精华"加分（src/curation.py）
- moment 带 `motion` 字段；v5/v6→v7 升级只补指纹+运动，不重跑逐秒扫描。
- **gem 加分** +0.06：实测稳定≥7 + 运动非 chaotic + 时长 3-6s（用户校准的专业旅拍节奏）。
- **正向口味记忆** `Output/asset_index/likes.json`（与 rejections 对称）：点赞区间在每次池构建时 +0.15/次（封顶+0.45，`liked_overlap` 字段）。

### 动势匹配接线
- 编剧菜单行带 `cam {motion_type}` 标签 + MOTION CONTINUITY 规则（延续方向或落定为静，禁止相邻反向）。
- `_attach_anchors` 把 motion 写进 shot（`camera_motion`）→ 存入 shot_plan → 渲染时 `_ai_pick_transitions` 的 boundaries 带上双侧运动 → 转场规则：同向流动时用 cut/fade 保动势，smoothleft/right 只许顺着共同方向划，双静用叠化托住宁静。

### 点赞闭环（server `/api/shots/like` + RenderView）
- 焦点卡片 👍满意 / 👎换掉（👎走原 replace 流程）。👍收理由（可空）→ `_analyze_like()` 用 agent 模型(降级 flash)提炼成结构化剪辑原则 JSON（summary/principles/applies_to）→ 存 likes.json → alert 展示"AI 的理解"给用户确认。
- **原则进编剧**：generate_shot_plan 聚合全部点赞原则(去重取最近8条)为 `[USER TASTE]` 块注入 prompt——用户的审美随点赞持续积累。

### UI
- 成片时间轴色块/转场菱形**点击跳播**（ShotTimeline onSeek → video.currentTime）。

### 调优入口
- gem 加分 0.06 / 点赞加分 0.15：`curation.py` `build_highlight_pool`。
- 运动分类阈值（chaotic 0.30 / static 0.0022 w/s / zoom 0.008）：`stability._classify_motion`。
- 口味原则条数上限 8：`Screenwriter_scene_short.py` taste block。

---

## 16. 卡点精确转场 + 节拍可视化 + BGM 拼接 + 素材红心

### 卡点精确转场（渲染器,2026-07-05 用户体感驱动）
**旧病根**：xfade 让相邻切片重叠 ~0.5s，每过一个转场后续所有切点提前 0.5s——34 个转场累计 ~17s，编剧排在节拍上的切点到片尾完全脱离节拍网格（用户："切换跟节奏没完全对上"+"时间轴对不上"，同一根因）。
**修法**：提取每个切片时尾部多取 `xfade_tail`（=该切点转场重叠时长）；xfade `offset=规划累计时刻`（淡化吃掉尾巴，进镜头正点开始）；音频 atrim 回规划时长；duck 窗口改纯规划累计。成片时长=规划总长（实测 179.95s vs 规划 ~178s，残差为帧率取整）。
- 素材不够取尾巴时：该处转场时长缩到实际尾巴，<0.2s 降级为硬切（trim 掉残尾），确定性降级不失败。
- ⚠️ **concat 滤镜输出不声明帧率(1/0)，下游 xfade 在 ffmpeg 7.x 直接报错**——每个 concat 后必须 `fps=N,settb=AVTB`（血泪：真实渲染全军覆没，合成复现定位）。

### 节拍可视化
`GET /api/render/beats?path&start&duration`：读音频缓存 `_keypoints_detail`（madmom 实测关键点），映射进渲染音乐窗口 → 成片时间轴上画紫色虚线（深色=Downbeat）。切点色块边界 vs 节拍线一眼可对。注意：**`/api/audio/keypoints` 已被素材详情页占用**（按方法分组格式），勿复用路径。

### BGM 拼接（独立工具,不跑流水线）
- `src/audio/bgm_stitch.py` + `POST /api/bgm/stitch`；素材页「拼接 BGM」按钮（音频≥2 时可用）。
- 实测无缝三件套：起止吸附各曲**小节线**（facts.bar_sec）；段间 `acrossfade` **2 小节封顶 4s**；各段先 loudnorm 再接。
- **物化哲学**：产物是 `resource/imports/` 里的普通 mp3（+同名 .bgmmix.json 记录成分）——重新扫描后流水线把它当"一首歌"分析使用，**零下游改动**就获得多 BGM 能力。
- **AI 融合模式**（`plan_bgm_mix`，面板默认）：LLM 拿到各曲**实测段落**（section_stats 的能量均值/趋势 + BPM + 小节长 + climax），只做编排决策——哪首的哪几段承担 开场→铺垫→高潮→收尾，**按段落序号引用，禁止编时间**；物化仍走小节吸附+低谷衔接+loudnorm。LLM 失败 → 确定性兜底（各曲能量峰值段贪心扩展），100% 成功。推理模型空回复要读 reasoning_content + 放大 token 预算（与转场选择器同款处理）。facts 读取兜底到标注轨缓存（asset_index/audio_captions），刚标注完没跑过流水线的歌也能融合。
- 局限：无调性检测，风格相近的歌拼接效果最好。

### 素材红心 ❤️（与镜头级 👍 互补的粗粒度口味）
- `Output/asset_index/asset_hearts.json`（content_hash 键，全局跨项目）；`POST /api/assets/heart`；素材卡片海报角 ❤️。
- 权重（用户："稍稍多一点"）：红心源的所有瞬间池内 **+0.05**（vs 镜头级点赞 +0.15）；预算器红心源配额 **+1**；BGM 拼接面板红心曲排前。

---

## 17. 叙事层（旁白 + 字幕卡）与节奏呼吸

> 动机(2026-07-09,用户:"成片没那么能打动我,可能缺文字/旁白/…"):数据诊断白山成片——26 镜头里 19 个 5-8s(节拍器节奏)、21 个同情绪、0 个静止镜头、两对字面重复,且全片无一句话。缺的不是特效,是叙事层和呼吸。

### AI 旁白(src/narration.py)
- **槽位确定性**:安静镜头(anchor.sound=False)、间距≥12s、开场/收尾必留(收尾全是人声时兜底用最后一镜);模型只写文案+挑槽位,时间轴不让模型复述(决策瘦身同款铁律)。
- **一次 LLM 调用**(agent 模型,stage=narration),失败快速抛错;文案要求:第一人称、8-22 字、口语克制、说画面之外的记忆,严禁鸡汤。
- **TTS**:edge-tts(免费),声线 yunxi/xiaoxiao/yunjian,rate -12%,逐句 loudnorm -16 LUFS;`voice|text` 签名缓存——改一句只重配一句。
- **产物**:`narration_{tag}.json` + `narration_{tag}/` 跟着 shot_point 走;渲染器 `--narration` 按绝对时刻 adelay+amix 混入,BGM 压到 NARRATION_BGM_LEVEL(0.45,比人声闪避的 0.65 深),环境原声再让 50%。
- **UI**:渲染页紫色卡片——生成/逐句编辑/换声线/启用开关;渲染时 sidecar enabled 才混入。

### 字幕卡
- 片头(地点·年月)叠在**第一个镜头**上(0.8s 渐显/3s/0.8s 渐隐,下三分之一),片尾(日期范围)随**最后一镜**淡出居中浮现——都画在切片提取阶段,**时间轴不动,卡点不破坏**。中文字体 msyh.ttc,textfile 免转义;UI 自动按素材拍摄时间+Immich 地点填充,可改可清空。

### 节奏呼吸(reslot_by_energy,Screenwriter)
- madmom 段落天然是 2-3 小节的均匀颗粒 → 成片节奏像节拍器。重切槽全部**按小节线**(measured_tempo.bar_sec):高能段(energy_mean≥0.55/文本 high…)先并成 run 再走 **1-1-2 小节循环**(按段各切会每段重启循环退化成机关枪);低能段相邻合并上限 12s。PACING_RESLOT=False 可关。
- **保险丝相对化**(core.py):MIN_ACCEPTABLE_SHOT_DURATION 是绝对 4s,会把 1 小节快切槽(~2.6s)全误判成闪帧——地板改为 `min(配置值, 槽位×0.8)`。

### 评分 v9:百分位归一 + 事件性 + 稀缺度 + 空镜否决(_score_reform_v9)
- 动机(用户:"片段质量评判不行,很多镜头无意义"):旧公式绝对值相加,白山 top15 全部饱和在 ~1.03(极差 0.032),且 13/14 是同款"粉衣滑雪者跟拍"。
- 项目池层重算(聚类后,吃已存字段,**零缓存重建**):content/stability/rarity 按**项目内百分位**归一;**event**=密集描述动词挖掘(转身/挥手/笑/摔/指/停=1.0,转弯/靠近/揭示=0.5);**rarity**=1/√簇规模(第 15 个同款自动掉价);权重 0.30/0.25/0.15/0.15 + 人声 0.10 + 有人 0.05(人脸小权重系用户定夺:风景为主)。
- **空镜否决**:无人+无事件+无人声 → ×0.6 + `empty_shot` 标记(呼吸位可用,前列免谈)。
- 旧分中的口味/宝石/红心加成以 **delta 形式原样保留**(新分 = 组合百分位 + 旧加成增量)。
- 强制分层 `tier`:S≤10%/A≤30%/B≤70%/C。白山实测:top15 极差 0.032→0.115,新 top14 = 挥手/回头/比耶/合影等人物瞬间(6 个不同源),67 个空镜降权。

### 重复镜头根治 + 呼吸镜头保底(curation/Screenwriter)
- **同源区间互斥**:精华时刻与长镜链常覆盖同段画面(白山 #7/#21 = 0357 的 134-138s 与 134-146s),重叠>30%(按较短者)不进菜单(build_anchor_budget),换锚修复侧同款检查(_attach_anchors 的 _valid,因为修复备选来自全池而非菜单)。
- 同簇间距 6→8 镜头(相隔 7 个的同 look 仍被识别为"刚看过")。
- **STATIC_BREATH_MIN(3)**:评分偏爱运镜,静止固定机位被系统性挤出;菜单保底注入高分 static 时刻(守区间互斥,不吃簇配额)。

---

## 附：历史决策否决清单（别再提议）

- ❌ 均匀降帧省 token —— 损失画面信息，被明确否决（"不能在输出质量上妥协"）。
- ❌ 抽屉式详情 UI —— 交互被否决，一律全页视图。
- ❌ 用兜底/回退掩盖 bug —— 必须暴露修根因（兜底只用于"agent 失败不许丢镜头"这类产品级保证）。
- ❌ LLM 产出可计算的数值（BPM、时长、边界）—— 一律信号计算，LLM 只写描述。
- ❌ 文件名匹配任务状态 —— 一律 content_hash 键控。

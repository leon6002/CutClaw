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
- **身份绑定**：`Output/asset_index/immich_map.json` 记 `{content_hash → immich_id + checksum}`。checksum（Immich 的 SHA-1）是内容身份，id 是 API 句柄。
- **代理导入**：分析用 Immich 的 1080p 转码代理（`/assets/{id}/video/playback`，3-10MB）落到 `resource/imports/`，不拉原片。
- **原片渲染（可选项）**：渲染时 `source_quality="original"` → `_materialize_immich_originals()` 生成替换过路径的 shot_point 副本（抖音 1080p 够用，默认 proxy）。
- **描述回写**：标注完成自动 `PUT /assets/{id}` 把摘要写进 Immich 描述（在 Immich 里可搜索）；也有手动"同步标注到 Immich 描述"入口。
- **CLIP 搜索**：素材库「🖼 Immich 库」标签页走 `/search/smart`。
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
- 得分 0-10：`10·min(1, rel/0.75)^0.8 × (1 − 0.5·disorder_penalty)`；按 (视频, 区间±0.1s) 进程内缓存。

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

## 附：历史决策否决清单（别再提议）

- ❌ 均匀降帧省 token —— 损失画面信息，被明确否决（"不能在输出质量上妥协"）。
- ❌ 抽屉式详情 UI —— 交互被否决，一律全页视图。
- ❌ 用兜底/回退掩盖 bug —— 必须暴露修根因（兜底只用于"agent 失败不许丢镜头"这类产品级保证）。
- ❌ LLM 产出可计算的数值（BPM、时长、边界）—— 一律信号计算，LLM 只写描述。
- ❌ 文件名匹配任务状态 —— 一律 content_hash 键控。

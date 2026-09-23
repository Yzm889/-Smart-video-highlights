# 一帧成片 · 成片质量升级规划

> 基于《成片质量升级计划.md》+ 项目当前架构分析  
> 生成时间：2026-09-09  
> 状态：规划阶段（不改代码，只给清单）

---

## 一、已完成的升级（另一个 AI 所做）

之前 AI 已完成了架构重构和质量提升两部分：

### ✅ 架构层
- webui_server.py 从 430KB 拆为 9 个模块（movie_narrator/video_render/handler/workflows/tts_engines/ffmpeg_utils/beat_analysis/bili_downloader/ai_providers/cache_utils/text_utils）
- ETA 进度预估、键盘快捷键、音量推子、多入口视频复用

### ✅ 成片质量（已落地的 P1 项）
| 项 | 改了什么 | 状态 |
|---|---|---|
| **P1-1** | 单遍滤镜图：剪切+setpts+烧字幕+缩放合并为 1 次编码 | ✅ |
| **P1-2** | setpts 画面微变速适配配音时长（±15% 内） | ✅ |
| **P1-3** | BGM sidechain 闪避解说 | ✅ |
| **P1-4** | BGM 有限次 -stream_loop N（不再播完静音） | ✅ |
| **P2-2** | 段落边界交叉淡化（200ms xfade/acrossfade） | ✅ |
| **P2-4** | 画质档位 UI（速度优先/均衡/画质优先） | ✅ |

---

## 二、后续可做的成片增强（按 ROI 排序）

基于《成片质量升级计划.md》P0 项 + 实际架构分析后的可执行清单：

### 🔴 P0 — 纯参数改动，零结构风险

| # | 改动位置 | 现状 → 改后 | 预期收益 |
|---|---|---|---|
| **P0-1** | `video_render.py` 中间产物编码参数 | `-crf 23`（两遍）→ ①用 `-crf 12` 接近无损，③才压到目标质量 | 消除两遍有损累积 |
| **P0-2** | `video_encode_args()` NVENC 参数 | `-preset p4 -rc constqp -qp 23` → `-preset p5 -tune hq -rc vbr -cq 20 -b:v 8M -maxrate 12M -bufsize 16M -rc-lookahead 32` | 暗部色带显著减少 |
| **P0-3** | `video_encode_args()` CPU 回退档 | `-preset veryfast -crf 23` → `-preset medium -crf 19 -tune film` | 同码率清晰度 +12-18% |
| **P0-4** | `_compose_narration_video` 音频链路 | `amix:normalize=0` → 加 `alimiter=limit=0.95,loudnorm=I=-16:TP=-1.5:LRA=11` | 消除削波爆音 + 响度统一到 -16 LUFS |
| **P0-5** | `tts_engines.py` 或 `video_render.py` 中间态格式 | narr{i}_fit.mp3 → 改用 PCM/WAV | 配音二次有损消失 |
| **P0-6** | `make_video_clip` scale 滤镜 | `scale=w:h` → `scale=w:h:flags=lanczos` | 降采样不再发糊 |
| **P0-7** | `video_render.py` 字幕 force_style | 补 `Shadow=1,BorderStyle=1` | 亮画面字幕可读 |

### 🟠 P1 — 需要结构性改动

| # | 改动 | 说明 | 收益 |
|---|---|---|---|
| **P1-1** | 画质档位映射到实际档位代码 | `P2-4` UI 已做但后端 `qualityTier` 与档位匹配需验证 | 档位切换真实生效 |
| **P1-2** | VFR→CFR 归一化 | 入口加 `fps=fps=30`（手机素材防音画漂移） | 消除手机素材漂移 |
| **P1-3** | 智能剪辑去黑场/重复帧 | 无声段自动跳过 + 重复帧去重 | 成片节奏更紧凑 |
| **P1-4** | 自动音量均衡（段落间） | `loudnorm` 前分析各段 LUFS + 增益对齐 | 消除段落间忽大忽小 |

### 🟡 P2 — 进阶增强

| # | 改动 | 说明 |
|---|---|---|
| **P2-1** | 10-bit 中间管线 | `p010le`/`yuv420p10le` 彻底消除渐变色带；默认关 |
| **P2-2** | 片头 hook + 片尾卡片 | 前 1.5s 节奏强化 + 结尾关注/subscribe 卡 |
| **P2-3** | 智能 BGM 推荐 | 按影片情绪（VADER/LLM）从 music_library 自动挑选配乐 |
| **P2-4** | AI 配音情感标记渲染 | `{慢}{停顿:0.6}{悲伤}` → TTS 参数映射（语速/音高/音量包络） |
| **P2-5** | 关键帧自动构图 | 切点避开闭眼/模糊/偏移主体帧 |

### 🟢 P3 — 锦上添花

| # | 内容 |
|---|---|
| **P3-1** | HDR→SDR 色调映射（对抗过曝源片） |
| **P3-2** | 自动去抖/防抖（拍摄不稳的手机素材） |
| **P3-3** | 画面增强（超分/降噪/锐化）—— GPU 可选 |
| **P3-4** | 多音轨输出（纯人声/纯 BGM/混合 三轨） |

---

## 三、验证改进是否有效

| 维度 | 工具 | 达标线 |
|---|---|---|
| 画质 | ffmpeg `libvmaf` | VMAF ≥ 93（均衡档） |
| 削波 | `ffmpeg -af astats` | Peak ≤ -1.0 dBFS |
| 响度 | `ffmpeg -af ebur128` | 综合 −16 ± 1 LUFS，真峰值 ≤ -1.5 dBTP |
| 人声 | 解说段 vs 音乐段侧链深度 | 压低 6-8 dB |
| 同步 | 逐段边界与配音时长比对 | 偏差 < 80 ms |
| 编码一致性 | ffprobe `r_frame_rate` vs `time_base` | CFR 30fps 恒定 |

---

## 四、建议执行路径

```
第一阶段（1-2 天）纯参数改动
  ├ P0-1 中间产物近无损
  ├ P0-2 NVENC 参数升级
  ├ P0-3 CPU 回退档升级
  ├ P0-4 混音限幅+响度归一
  ├ P0-5 配音中间态改 WAV
  ├ P0-6 lanczos 降采样
  └ P0-7 字幕阴影
  验证：同一段素材前后对照

第二阶段（3-5 天）结构改动
  ├ P1-1 档位代码验证
  ├ P1-2 VFR→CFR
  ├ P1-3 智能剪辑（无声跳过）
  └ P1-4 段落音量均衡

第三阶段（1-2 周）进阶
  └ P2 系列按优先级挑做
```

注：所有改动集中在 `video_render.py`（502 行）+ `ffmpeg_utils.py`（473 行）+ `tts_engines.py`（1195 行）三个文件内，其余模块不动。

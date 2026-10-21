# FrameCut 工程审计报告 + 升级路线图（2026-10-21）

> 背景：M1 / P0-1「真机全链路出片」已完成并验收。本报告基于**真机跑通后的实测数据**
> 与只读代码审计，给出全景现状、已修缺陷、待修清单与分阶段升级路线。
> 原则不变：**可信 > 智能 > 花哨**；AI 建议，人拍板。

---

## 一、P0-1 验收结论：通过

**成片已产出**（本项目首次跑通阶段②）。10 分钟真实对白片段，21 段分析 → 保留 4 段。

| 验收项 | 期望 | 实测 | 结论 |
|---|---|---|---|
| 产出成片 | 存在且 ≥1KB | 正式 18.6MB / 草稿 47MB | ✅ |
| 真剪生效 | 剪掉未保留段 | 剪除 **226.00s**，与后端报告偏差 **0.01s** | ✅ |
| 成片时长 = 保留段总和 | 374.27s | **374.41s**（偏差 0.14s） | ✅ |
| 字幕逐字 = 定稿 | 一字不差 | 4 条字幕逐字一致，按剪辑后新时间轴定位 | ✅ |
| 配音存在 | 有音轨 | 音轨存在，align 表 4/4 有声 | ✅ |
| 留剪集合 | 与编辑台一致 | [0, 3, 10, 17]，其余 17 段剪除 | ✅ |
| 失败不静默 | 降级可见 | SAPI 降级有明确 warn 提示 | ✅ |

**草稿 vs 正式档位正确**：草稿 854x480（speed 档，64s 出片），正式 640x360（原始分辨率，44.9s）。

---

## 二、已修复缺陷（6 次提交，全部经真机定位到具体行）

| # | 缺陷 | 根因 | 修复 | 提交 |
|---|---|---|---|---|
| 1 | ASR 静默全灭（98 分钟视频 0 台词 / 0 草稿） | PyAV 19 移除 `av.open` 的 `metadata_errors`，faster-whisper 1.2.1 必抛 TypeError | 改 stdlib `wave` 读自产 PCM 转 numpy | `31a1e19` |
| 2 | 失败原因完全不可见 | 裸 `except Exception: return []` | 写入 `PROGRESS[runid]['warnings']` | `31a1e19` |
| 3 | 有显卡但 GPU 推理必崩 | `_cuda_available` 只查 nvidia-smi，不查 CUDA 运行时库 | 加 `_cuda_runtime_present`；缺库走 CPU | `31a1e19` |
| 4 | 装了 CUDA 库仍报 cublas 找不到 | DLL 在 `site-packages/nvidia/*/bin/`，不在 DLL 搜索路径（`add_dll_directory` 对 ctranslate2 无效） | 前置到**本进程** PATH | `e523639` |
| 5 | 草稿全灭（21 段 0 草稿） | qwen3 默认开 thinking，`max_tokens` 是思考+正文共享配额，思考吃光 | `_llm_payload` 统一加 `think=false`（**5.7s vs 28.9s**） | `e523639` |
| 6 | 多对象 JSON 输出全丢 | `_try_parse_json` 贪婪正则把 3 个对象连成非法块 | 引号感知的括号配对，逐个提取 | `e523639` |
| 7 | 配音静默降级 SAPI | 降级无任何告知，tts_log 仍显示 success=4 | `_warn_tts_engine_degraded` 写入 progress | `e74862c` |
| 8 | 0 字节 mp3 残留 | edge 命令先建文件再写，失败留空壳 | `_drop_empty` | `e74862c` |
| 9 | **正式合成卡死 36 分钟** | edge 子进程超时 180s × 3 轮 × 4 段 | `_EDGE_SUBPROC_TIMEOUT = 25`（**→44.9s**） | `4735a83` |
| 10 | 连通探测误判 | 只建 TCP（本机 443 通但 TLS 被拦）；域名用「任一通即可」 | 改 TLS 握手，只探实际依赖域名 | `4735a83` |
| 11 | ASR 临时 wav 沉淀 **258MB** | 只在异常分支删，成功路径直接 return | `try/finally` + `_silent_remove` | `8158f17` |
| 12 | **任意目录写**（安全） | 三处 `X if not os.path.isabs(X) else X` 信任请求绝对路径 | 新增 `_safe_dir`，三处统一校验 | `8158f17` |

**测试：310 passed / 4 failed → 336 passed / 0 failed**（新增 26 条回归）。

---

## 三、待修清单（按严重度）

### 致命

| # | 问题 | 位置 | 影响 | 改动量 |
|---|---|---|---|---|
| F1 | **长视频阶段①实际不可用**：缩略图每段起 1 个 ffmpeg 子进程串行；草稿每段 1 次 LLM 请求不批量。98 分钟→235+ 段，仅缩略图就 10–30 分钟 | `analyze_pipeline.py:194-224`、`movie_narrator.py:754` | 长视频无法使用 | 中 |
| F2 | `new_spans` 与 `narr` 长度从不校验 | `render_pipeline.py:129`、`video_render.py:334` | 少一段 → 解说词与画面**整体错位**，成片「第 3 段词配第 4 段画面」 | 小 |
| F3 | TTS 本地引擎子进程不受 GPU 互斥保护 | `tts_engines.py:294,400`、`movie_narrator.py:3156` | 12GB 显存下 CUDA OOM → 整段配音失败 | 小 |

### 严重

| # | 问题 | 位置 | 影响 | 改动量 |
|---|---|---|---|---|
| F4 | `/api/video_frame` 绝对路径穿越 + 无缓存 + 句柄不关 | `handler.py:498,504,512` | `HOST=0.0.0.0` 时任意文件读写；拖时间轴高频起 ffmpeg | 小 |
| F5 | 缩略图整批静默丢失（双层裸吞） | `handler.py:1703-1707`、`workflows.py:290-295` | 所有分镜缩略图空白但接口返回 `ok:True` | 小 |
| F6 | concat 兜底把 N 路 `-i` 塞进单条命令 | `video_render.py:398-410` | 段数多时 ffmpeg 崩 → 回退原片 = **成片完全没剪** | 中 |
| F7 | edge 探测缓存无锁 | `tts_engines.py:118,142` | 并发 worker 各起一次 25s 探测子进程 | 小 |
| F8 | ASR 音频全量载入内存 | `ai_providers.py:190` | 98 分钟峰值约 560MB | 小 |
| F9 | 排队任务缺 active_task 登记 | `workflows.py:1139-1157` | 崩溃恢复与 UI 活动态对排队任务失效 | 小 |

### 中等 / 轻微（技术债）

| 项 | 说明 |
|---|---|
| 巨石拆分 | `webui_server.py` 4338 行中约 **1370 行（32%）是模型/环境安装簇**，零请求路径耦合，唯一能「搬走即通过」的块 → 优先抽 `env_setup.py` |
| 性能 | `_asr_text_in` O(段×台词) 应改 bisect；存储面板对 `models/`（多 GB）全量 walk 应缓存；进度写盘每次 phase 变更都 fsync 应节流 |
| 冗余 | 两处重复的逐段抽帧实现；`_resolve_music` 重复调用；孤儿端点 `/api/material/tags` 前端零引用 |
| 一致性 | `dispatch_analyze` 的 docstring 写在赋值之后（实为无用表达式）；`open()` 未用 `with` |

---

## 四、升级路线图

### M1 能稳定出片 ✅ **已完成**
- P0-1 真机全链路跑通并验收
- 附带修复 12 个缺陷，测试全绿

### M2 长视频可用（建议下一步）
> 现在的瓶颈已从「能不能出片」变成「长视频能不能用」。

| 序 | 任务 | 收益 | 改动量 |
|---|---|---|---|
| M2-1 | `_segment_timeline` 加段数硬顶（上限 ~60 段） | 98 分钟从 235 段降到可控 | 小 |
| M2-2 | 缩略图并发 + 统一 scale 滤镜 | 缩略图阶段 10–30 分钟 → 1–2 分钟 | 中 |
| M2-3 | 草稿 LLM 合并为批量请求 | 草稿阶段耗时降一个量级 | 中 |
| M2-4 | `new_spans` 长度一致性校验 | 消除字幕/配音错位风险 | 小 |
| M2-5 | ASR 音频分块喂入 | 内存峰值 560MB → 可控 | 小 |

### M3 AI 真看懂（核心价值，对应原 P0-2）
> **关键判断**：本次素材的解说词是「请不吝点赞订阅转发」这类字幕，
> 草稿质量天然一般——这**恰恰印证了接入 VLM 的必要性**：
> 只喂 ASR 台词，模型无从得知画面内容。

- 复用已有函数（`movie_narrator._plot_brief` / `_seg_visual_captions` /
  `local_vlm_narrate` / `_vlm_sample_timeline`），**不要重写**
- 每段：画面描述 + 前后段台词 + 整体剧情梗概 → 草稿 LLM
- 填充 `emotion / key_info / semantic_tag`（当前仍为空串）
- 无 VLM 时降级回纯台词草稿并写 warning，不允许崩
- VLM 调用需缓存、可取消、控帧（12GB 显存按 6GB/12GB 分档）

### M4 失败可恢复
局部重试（复用已有 `narr*.mp3`）、编辑态存 localStorage、分段级错误面板、
配音超长一键延长出点。

### M5 协同智能化
`/api/assist`、拖拽排序（后端 `order` 字段）、渲染参数面板、分段精度可调。

### M6 收尾
BGM/转场、多语言、模型中心 UI 重做、README 更新、技术债偿还。

---

## 五、CI 状态（对应原 P0-3）

**本地已全绿**（336 passed），但 CI（Linux）上的环境相关断言尚未处理：

- `test_core.py::test_ai_status_includes_whisper_vlm`
- `test_image_clip_stream.py::test_make_image_clip_streaming_output`

需改为 `pytest.mark.skipif` 或 mock，让 CI 在无 GPU / 无模型 / 无网络时也能过。
**建议与 M2 并行推进**，合并前必须绿。

---

## 六、环境结论（重要，影响后续判断）

1. **edge-tts 在本沙箱不可用**：TLS 层拦截 `speech.platform.bing.com`。
   这是环境限制而非代码问题；用户机器网络正常时 edge 应可用。
   本次所有出片配音实为 SAPI（机械感重），修复已确保这种情况会**明确告知用户**。
2. `login.live.com` 能 TLS 握手但朗读服务被拦 → 连通探测必须**只对准实际依赖的域名**，
   绝不能用「任一通即可」。
3. 已装依赖：`nvidia-cublas-cu12` / `nvidia-cudnn-cu12`（GPU 推理必需，否则 ASR 降级 CPU）。
4. 真机素材已备于 `C:\Users\19891\Desktop\framecut_testsrc\`（项目外，不入库）：
   `clip_265m.mp4`(98min) / `clip_1500m.mp4`(66min) / `clip_dialogue.mp4`(10min，对白密集，推荐)。

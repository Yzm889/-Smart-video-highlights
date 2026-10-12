# 交接提示词 · FrameCut（一帧成片 / 智能视频高光）— 2026-10-13

> 用法：把本文件**整体**作为第一条消息发给接手 AI。文件自包含：项目定位、真实现状、数据契约、要做的任务、技术坑、验收标准都在里面。
> 配套清单见同目录 `PRIORITY_20261013.md`（先读它确定顺序）。

---

## 0. 你拿到的第一条指令（请严格照做）

1. 先**通读本文档 + `PRIORITY_20261013.md`**，不要急着写代码。
2. 用只读命令核对项目真实结构（列目录、读关键文件），确认现状与文档一致；如发现不一致，**以代码为准并告诉我**，不要照文档臆测。
3. 从 **M1 里程碑**开始：先把「真机全链路稳定出片」跑通（P0-1），再让 CI 全绿（P0-3），**一次只做一个任务**。
4. 每个任务闭环：**改代码 → 本地验证（语法/最小回归）→ 浏览器真机验证 → 提交（commit 信息写清根因与改动）**。做不完的任务不要留半成品。
5. 所有判断遵循一条原则——**"这行代码是在帮人做决策，还是在帮人执行决策？"** 执行决策，AI 做到最好；做决策，AI 只能建议、人拍板。

---

## 1. 这是什么项目

本地运行的短视频解说/剪辑 Web 工具「FrameCut」，Python 后端 + 原生静态前端（无框架），端口 **8765**。

已从早期的"一键全自动出片"重构为**「人机协同 · 两段式」**：

- **阶段① 自动分析（AI 充分发挥）**：上传视频 → 镜头切分 + ASR 字幕 + 每段缩略图 + 每段多版 AI 草稿解说词 + 段落分析（情绪/关键信息/重要性）+ 整体叙事线建议。
- **中间「段落编辑台」（人决策、AI 辅助）**：人看缩略图+台词+草稿，选一版草稿或自己改，勾选保留/剪除，调入点出点，（未来）排序，实时对齐预检。
- **阶段② 合成（AI 只执行，不改变决策）**：按人定稿的留剪/解说词/出入点真剪、逐字 TTS、混音、烧字幕、导出。

**一句话原则**：AI 可以主动思考、建议、生成草稿；但定稿解说词、留剪、顺序的最终决定权永远在人。

**不可越过的红线（写进代码语义）**：
- ❌ 禁止自动改写用户定稿的解说词——人写的字，TTS 必须逐字念。
- ❌ 禁止自动决定保留/剪除——AI 只能给 `keep/cut_candidate` 建议，人不勾选不生效。
- ❌ 禁止在用户没点"开始合成"时预跑合成。
- ❌ 禁止把 AI 草稿直接送 TTS——必须经过编辑台确认。
- ✅ 允许：多版草稿、叙事建议、情绪/关键信息标注、用户主动触发的辅助建议、阶段②的语气/音量/字幕时机等技术优化。

---

## 2. 环境与项目速览

- **项目根**：`C:\Users\<用户名>\Desktop\-Smart-video-highlights-main`（接手时先 `cd` 确认实际路径；用户名可能是 19891 或 XOS）。
- **Python**：Windows 自带 Python 3.13（路径形如 `C:\Users\<用户名>\AppData\Local\Programs\Python\Python313\python.exe`）。**本机没有装 pytest，也没有 venv**（回归测试主要在 GitHub CI 的 Linux 上跑；本地可用独立脚本验证单测断言）。
- **GitHub**：`https://github.com/Yzm889/-Smart-video-highlights`，分支 `main`。
- **启动**：`Start-Process -FilePath <python> -ArgumentList "-u","webui_server.py" -WorkingDirectory <项目根> -WindowStyle Hidden`，等 7–8 秒，用 `Get-NetTCPConnection -LocalPort 8765 -State Listen` 确认。**改后端必须重启服务**；改前端静态文件浏览器刷新即可（但要 bump 版本号）。
- **停止**：`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ?{$_.CommandLine -like '*webui_server.py*'} | %{Stop-Process -Id $_.ProcessId -Force}`。

主要文件（行数越大越是巨石，新逻辑优先放小模块）：

| 文件 | 职责 |
|---|---|
| `webui_server.py`（3625） | 服务启动、配置、模型状态、上传会话、任务队列、大量工具函数 |
| `handler.py`（1790） | HTTP 路由：上传 init/chunk/done、`/api/analyze`、`/api/render`、模型/状态等 |
| `analyze_pipeline.py`（201） | **阶段①**：切分+ASR+缩略图+多版草稿+叙事建议（小而清晰，阶段①改动主要在这里） |
| `render_pipeline.py`（158） | **阶段②**：按定稿真剪+逐字 TTS+混音+字幕+导出（已较健壮，勿大改） |
| `movie_narrator.py`（3288） | 旧一键成片解说引擎；**含可复用的 VLM/切分/TTS 能力** |
| `video_render.py`（665） | ffmpeg 裁剪/合成/字幕/混音底层 |
| `ai_providers.py` | ASR（faster-whisper）、LLM/VLM 调用 |
| `tts_engines.py`（1072） | edge-tts / CosyVoice / ChatTTS / MeloTTS / SAPI |
| `workflows.py` | 旧一键成片任务分发（与协同台并存，勿混） |
| `static/app.js`（6569）、`index.html`（1206）、`style.css`（1693） | 原生前端；协同台逻辑在 app.js 的 `collab*` 函数（约 L6500 之后） |

产物目录：`webui_output/<task_id>/`（`source`/`src.mp4`、`thumbnails/`、`analyze_result.json`、`render_draft/`、`render/`）；上传暂存 `webui_workspace/uploads/<upload_id>/`（24h/100 会话自动清理）。

---

## 3. 数据契约（已实现，改动要向后兼容）

### 阶段① `POST /api/analyze`（handler `_post_analyze`）
请求：`{video:{name,data(base64)} | {name,upload_id} | {video_path}, options:{max_seg,draft}}`，返回 `{task_id}`，轮询 `GET /api/analyze/{task_id}`。
输出（`analyze_result.json`）：
```json
{
  "task_id": "run-1-…", "video": {"duration": 123.4, "path": "…/src.mp4"},
  "narrative_suggestion": {"main_line": [0,1,3], "suggested_cuts": [2], "reason": "…"},
  "segments": [{
    "segment_id": 0, "start": 0.0, "end": 4.5,
    "thumbnail_url": "/media/<task>/thumbnails/0.jpg",
    "asr_text": "台词", "semantic_tag": "", "emotion": "", "key_info": "",
    "ai_importance_hint": "keep|cut_candidate", "importance_reason": "",
    "ai_draft_narrations": [{"style":"简洁","text":"…"},{"style":"悬念","text":"…"},{"style":"口语","text":"…"}],
    "default_keep": true
  }],
  "warnings": []
}
```
**当前短板（P0-2 要改）**：`_drafts_for_segment()` 只喂 ASR 台词，没画面没上下文；`emotion/key_info/semantic_tag` 基本为空；阶段①完全没调 VLM。

### 阶段② `POST /api/render`（handler `_post_render`，逻辑 `render_pipeline.dispatch_render`）
请求：`{task_id, segments:[{segment_id,keep,narration,start,end,order?}], params:{w,h,resolution,qualityTier,draft}, draft?}`。
行为：严格按 `keep=true` 段、按 start/end 真剪（`skip_no_cut=True`）、`narration` 逐字 TTS（不改写）、真实 TTS 时长对齐表 `prog['align']`、成片硬校验。
**注意**：段落顺序现在按 `segment_id` 升序（`render_pipeline.py` L69），`order` 字段尚未实现（P2-2）。

### 阶段②辅助（待新增）`POST /api/assist`（P2-1）
`{task_id, action:"rewrite"|"polish"|"suggest_order"|"check_cut", segment_id, current_text, context:{previous_segment,next_segment,thumbnail_url}}` → 返回**建议**，前端必须弹"采纳/忽略"，不自动应用。

---

## 4. 要做的任务（按里程碑，细节见 `PRIORITY_20261013.md`）

**M1 能稳定出片（先做）**
- P0-1 真机全链路：真实大视频走完上传→分析→编辑→快速草稿→正式合成，确认字幕/配音/真剪/留剪与编辑台所见一致。
- P0-3 CI 全绿：`.github/workflows/ci.yml` 跑 `pytest tests`。当前失败是因为测试假设本机有 edge-tts 网络、whisper 模型、ffmpeg 产出（如 `test_core.py::test_ai_status_includes_whisper_vlm`、`test_image_clip_stream.py::test_make_image_clip_streaming_output`）。把环境相关断言改为 `pytest.mark.skipif`/mock，CI 无 GPU/模型/网络也能过。

**M2 AI 真看懂（核心价值）**
- P0-2 让阶段①调用 VLM。**复用已有函数，不要重写**：`movie_narrator.py` 里的 `_plot_brief()`(L47)、`_seg_visual_captions()`(L293)、`local_vlm_narrate()`(L437)、`_vlm_sample_timeline()`(L1630)。把每段画面描述 + 前后段台词 + 整体剧情梗概喂给草稿 LLM；无 VLM 时必须降级回现在的纯台词草稿并写 warning，不允许崩。填上 `emotion/key_info/semantic_tag`。VLM 调用要缓存、可取消、控帧（显存有限，参考 MiniCPM 96x 压缩的采样策略）。
- P1-5 草稿连贯：跨段带上上下文，不要每段独立割裂。

**M3 失败可恢复**
- P1-1 单段配音/片段局部重试（复用已生成的 `tts/narr*.mp3`，只补失败段）。
- P1-2 编辑态存 localStorage（`framecut_edit_{task_id}`），刷新恢复。
- P1-3 分段级错误面板。
- P1-4 红色对齐（配音>画面）一键"按配音时长延长出点"，消除硬截断。

**M4/M5**：`/api/assist`、拖拽排序（后端 order）、渲染参数、多语言、模型中心 UI、BGM/转场、README 更新。

---

## 5. 技术坑（血泪，务必遵守）

- **Windows 宿主，没有 Bash**：命令行一律 PowerShell。
- **改前端必须 bump 版本**：`static/index.html` 里 `app.js?v=NN`（当前 v60）、`style.css?v=NN`；改完用 `node --check static/app.js` 校验语法，否则浏览器加载旧缓存。
- **改后端必须重启服务**（见第 2 节命令）。静态文件 `/media/...` 由服务直接读产物目录。
- **上传成品已改为 copy 不再 move**（`webui_server._resolve_upload_video`），同一 upload_id 可重复分析；不要再改回 move。
- **阶段②不改写解说词**：`render_pipeline.py` 是"执行层"，任何"智能优化"只允许在 TTS 语气/停顿/音量/字幕时机层面，**禁止改文本、禁止改留剪**。
- **不要去碰旧一键成片管线的行为**（`workflows.py`/`movie_narrator.py` 的 dispatch_*），但可以**复用其中的纯函数**（VLM、切分、TTS、ffmpeg）。
- **测试**：本项目用 pytest（CI 环境）。import 顺序：先 `import webui_server` 再 `import video_render`/`movie_narrator`（晚绑定宿主符号，直接 import 会循环）。本地无 pytest 时，用独立 Python 脚本复现单测断言验证。
- **网络（中国大陆）**：
  - GitHub/ollama.com/registry.ollama.ai 直连被墙；git 已配代理 `http://127.0.0.1:7877`（代理软件没开会 `Connection refused`，push 失败时先确认代理开启，或临时 `git -c http.proxy= -c https.proxy= push`）。
  - 模型下载走 **hf-mirror.com**（带正常 UA、`HF_HUB_DISABLE_XET=1`；Xet 协议 `cas-server.xethub.hf.co` 在镜像下会 401，必须禁用）。
  - Ollama 安装优先**微软商店**（不限速），其次官网跳转；分片下载要支持断点续传、进度%、网速、取消。
- **硬件基线**：RTX 3060 Laptop 12GB / 16GB 内存；已装模型 minicpm-v4.5、qwen2.5:14b、qwen3:14b-q4_K_M、Whisper large-v3，TTS 走 edge-tts。VLM 控帧/显存要按 6GB/12GB 分档。
- **不静默**：任何失败/降级都要写进 `progress['warnings']` 且前端可见；最终文件必须硬校验（存在+非空），不能"进度走完但没成片"。

---

## 6. 验收标准（每个任务做完前自检）

1. 有针对性的回归测试（或本地等价脚本断言）；既有用例不被破坏；M1 后 CI 必须绿。
2. 任何失败/降级都可见、可恢复，不允许静默 return 假装成功。
3. 编辑台"所见即所得"：用户勾选/定稿/出入点/顺序，成片必须逐条对应；人改过的字成片里逐字对应。
4. 前端改动 bump 版本号 + `node --check` 通过；后端改动重启后浏览器实测页面与请求正常。
5. 只提交基础代码：`models/`、`webui_output/`、`webui_workspace/`、`ai_config.json`、视频/音频、日志均已在 `.gitignore`，不要提交产物或模型。
6. 一个任务一个 commit，信息写清根因和改动；完成后更新进度文档。

---

## 7. 用户偏好（长期，务必遵守）

- **代码质量与稳定性优先**：从第一个功能逐步做精做细，避免频繁 bug；不要为炫技引入不稳定依赖。
- **人机协同，不是全自动**：人做最终决策，AI 做重复苦力和高质量草稿。
- **透明化、可调 > 一键化**：中间产物可见、可干预。
- **解说连贯、聚焦剧情与画面含义**：不要逐镜头独立解说，不要过度解读、不要堆无关细节。
- **国内镜像优先、尽量免代理**；硬件按四档给多个模型选择，不要一锤定音。
- 全权委托、少要确认，但遇到会改变架构方向或不可逆的操作，先说明再动手。

---

## 8. 请你现在开始

回复我：①你核对到的项目实际结构与本文档是否一致；②M1（P0-1 真机全链路）的具体执行计划（要点、风险、如何验证）；③确认后再动手。**不要一上来就改代码。**

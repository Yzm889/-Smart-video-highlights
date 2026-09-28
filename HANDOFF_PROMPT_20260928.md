# 交接提示词 · spring_video（一帧成片 FrameCut）— 给下一个 AI 的完整上下文

> 用法：把本文档整体（或按需裁剪）粘贴给接手 AI，作为它的第一条消息。它包含项目全貌、已完成改造、未完成待办、技术坑与验收标准。

---

## 一、你是谁 / 在做什么

你在接手一个**本地运行**的 Python 视频解说/剪辑 web 工具「一帧成片 · FrameCut」。用户已把它从「一键化黑盒」转型为「**细节化、透明化、可调整**」的方向——核心诉求是：**每个处理环节（剪辑决策、解说词↔画面映射、配音状态）都要可见、可干预，任何失败/降级都要明确告知，不能静默**。

你的任务：继续完善它。**优先解决用户长期抱怨的 4 个问题**，并按用户偏好（代码质量与稳定性优先、从第一个功能逐步做细、避免频繁 bug）推进。

## 二、项目速览（先读这些文件再动手）

- 主目录：`C:\Users\XOS\Desktop\spring_video`
- 入口：`webui_server.py`（PORT 环境变量默认 8765，OUTDIR=`webui_output`）
- 后端模块：`video_render.py`（渲染：剪辑→配音→混音→烧字幕→配乐）、`movie_narrator.py`（解说分析/剧情驱动/配音）、`workflows.py`（任务分发）、`handler.py`（HTTP 路由）、`ffmpeg_utils.py`、`tts_engines.py`、`ai_providers.py`
- 前端：`static/index.html` + `static/app.js` + `static/style.css`（**无框架**，原生 JS，动态拼串渲染）
- 仓库：`https://github.com/Yzm889/-Smart-video-highlights`，分支 `main`
- 进度存档（必读）：`PROGRESS_NOTE_20260923.md`（S1-S9/P1P2/i18n/两步走修复）、`PROGRESS_NOTE_20260928.md`（透明化第一轮）

## 三、用户最在意的 4 个长期问题（本轮已修一半，另一半是后续主线）

| 问题 | 根因（已查实） | 当前状态 |
|---|---|---|
| ③剪辑失效/不剪辑 | `_condense_segs` 的 keep=False 依赖 LLM 标过渡微段（很少标）→ 保留段首尾覆盖无空隙 → `_no_cut` 跳过剪辑；剪辑异常静默返回原片 | ✅ 已修：cut_plan.removed>0 强制真剪；降级写 warnings；前端可见剪辑方案 |
| ①解说词对不上画面 | 剧情驱动依赖 LLM 语义对齐，失败静默回退 bigram；对齐结果不可见不可改 | ⏳ 部分：cut_plan 展示每段解说词↔画面对照；**可改/重对齐未做** |
| ④画面对不上解说词 | 同①；剪辑后时间轴用 piece_durs 逐段累计（有缩放修复），但用户不可调 | ⏳ 部分：时间轴可见；**手动调整入口未接到本地解说流程** |
| ②配音丢失/不合成 | 两步走 UI 误导（已修）+ TTS 失败仅写 tts_failures + 合成「完成」不校验成片 | ✅ 已修：final 文件硬校验（存在+≥1KB）失败明确报错；降级红字 |

## 四、已完成改造（别重复做，先读代码确认现状）

1. **cut_plan（剪辑方案）落盘**：`movie_narrator.py` 的 `_narrate_analysis`（本地解说）与 `_narrate_by_plot`（剧情驱动）都在分析阶段写 `progress['cut_plan']`，结构：
   ```json
   { "src_dur": 10.0, "coverage": 100.0, "kept": 2, "removed": 0,
     "removed_sec": 0.0, "segments": [
       {"i":0,"start":0.0,"end":5.0,"keep":true,"importance":"advance",
        "reason":"主线推进","caption":"解说词…","asr":""} ] }
   ```
2. **渲染真剪**：`video_render.py` 的 `_cut_video_by_spans` / `_cut_and_burn_video` 新增 `skip_no_cut` 参数；`_render_narrate` 从 progress 读 cut_plan.removed>0 → 强制真剪；`compose_movie_from_tts`（两步走第二步）从 tts_state.json 恢复 cut_plan。
3. **失败不静默**：`_warn(progress, msg)`（去重写入 `progress['warnings']`）；剪辑降级/P1-1 回退均写 warning；`dispatch_narrate`/`dispatch_movie_compose`/`dispatch_movie` 有 final 文件硬校验。
4. **前端**（app.js v36 / style.css v15）：`renderCutPlan(el,p)` 渲染「🎬 剪辑方案」卡片（时间轴色块绿保留/红剪除 + 每段时间/标签/原因/解说词对照）；`renderWarnings(el,p)` 红字展示降级；容器 id：`narCutPlan/narWarn/movieCutPlan/movieWarn`。
5. **i18n**：中/EN 切换（app.js `I18N_DICT` + `toggleLang` + localStorage `framecut_lang`；右上角 langToggle 按钮）。
6. **两步走修复**：narrate_movie tts_only 第一步不再把 cover.jpg 写进 `progress['file']`（封面误当成品）；前端第一步提示「去⑥手动调整确认后合成」。

## 五、下一步待办（按优先级，可先做 1，或听用户安排）

1. **剪辑方案「可勾选」**（用户点名的"可调整"核心）：目前 cut_plan 只读展示。把「⑥手动调整」的勾选/改稿能力接到**本地解说流程（/api/narrate）**——用户取消勾选某段 → 渲染按勾选真剪。参考现有 `/api/plan` 编辑器与 `compose_movie_from_tts` 的 `skip_set` 机制。
2. **解说词逐段可改 + 重新对齐**：暴露 `/api/narrate/align`（按新解说重匹配分镜）到本地解说流程；对齐失败时**不再静默**（写 warnings 告知用户回退到了 bigram）。
3. **README 项目主页更新**：GitHub 主页仍是老版本内容（用户 9-23 点名过，未做）。更新功能列表（透明化/剪辑方案/多语言/i18n 等）+ 使用说明 + 镜像下载说明（qwen GGUF 走 hf-mirror.com，用户指定）。
4. **剧情驱动对齐质量**：`_llm_align_and_refine`/`_llm_align_beats_to_scenes` 失败回退 bigram 时质量差——考虑失败时明确提示 + 提供手动对齐入口。
5. **配音段状态可见**：tts_failures 目前前端展示弱，可并入 warnings 体系。

## 六、技术备忘（血泪坑，务必遵守）

- **Windows 宿主**：没有 Bash，命令行一律用 PowerShell 工具。
- **改 Python 文件用「Write 一个 .py 脚本 → PowerShell 执行 python 脚本」做精确替换**：Edit 工具在 CRLF 文件上会报 "File has not been read yet"/匹配失败（已验证多次）。脚本内用 `io.open(p, encoding='utf-8', newline='')` 保持行尾一致。
- **前端改后必须 bump 版本**：`static/index.html` 里 `<script src="/static/app.js?v=36">`、`<link ... style.css?v=15">`；改 app.js 必 bump；校验用 `node --check static/app.js`。
- **服务启动**：`cd C:\Users\XOS\Desktop\spring_video; $env:PORT='8765'; python webui_server.py`（首次运行会 pip 装 yt-dlp）。
- **测试**：`python -m pytest tests/test_cut_plan_transparency.py tests/test_plan_plot_smoke.py tests/test_plan_cut_smoke.py tests/test_run_cleanup.py`（基线 4+7 passed, 1 skipped）。新逻辑必须补测试。
- **import 顺序**：测试里**先 `import webui_server` 再 `import video_render`**（video_render 经 `_host()` 晚绑定宿主符号，直接 import 会循环导入报错）。
- **push 需直连**：本机 git 全局代理 `socks5://127.0.0.1:10808` 常因代理软件未开而失败——用 `git -c http.proxy= -c https.proxy= push origin main` 直连（已验证可用）。
- **进度存档**：每轮结束更新 `PROGRESS_NOTE_YYYYMMDD.md`（格式见 20260928 存档）。
- **端到端验证脚本**：`tools/e2e_cutplan.py`（真实跑 /api/narrate 检查 cut_plan 落盘+成片产出，用 `spring10s.mp4` 样片）。

## 七、验收标准（做完一轮必须满足）

1. 你改的每个功能**有测试**（至少一个针对性回归测试，4 passed 风格），且既有测试全绿。
2. 任何降级/失败路径**写 progress.warnings 且前端可见**——不允许静默 return 假装成功。
3. 剪辑方案/解说词对照**在界面可见、可干预**（勾选/改稿），改动后成片按用户选择生成。
4. 前端改完 bump 版本号并 `node --check` 通过；浏览器实测页面 200。
5. 交付时更新 PROGRESS_NOTE，说明验证方式与覆盖范围。

## 八、用户偏好（长期，务必遵守）

- **代码质量与稳定性优先**：从第一个功能逐步做细，避免频繁 bug，不要为了炫技引入不稳定依赖。
- **解说连贯**：不要每个镜头都单独解说；解说聚焦剧情与画面含义，避免过度解读和不必要的细节。
- **模型下载走国内镜像**：qwen GGUF 从 hf-mirror.com 下载（README/UI 已体现，保持一致）。
- **透明化 > 一键化**：用户明确不满"一键智能生成不可干预"——所有中间产物可见、可调。

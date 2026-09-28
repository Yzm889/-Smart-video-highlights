# PROGRESS_NOTE 2026-09-28 · 透明化改造（第一轮）

> 承接 2026-09-23 存档（S1-S9 / P1P2 / 两步走修复 / i18n 中英）。
> 本轮主线：用户明确「太一键化效果差」→ 4 个长期问题（①解说词对不上画面 ②配音丢失不合成 ③剪辑失效不剪辑 ④画面对不上解说词）→ 方向确认为 **透明化 + 可调整**。

## 一、根因诊断（4 个长期问题，代码级证据）

| 用户问题 | 根因 |
|---|---|
| ③剪辑失效/不剪辑 | `_condense_segs`（movie_narrator.py）的 keep=False 依赖 LLM 标出过渡微段，LLM 很少标 → 几乎全 keep=True → 保留段首尾覆盖无空隙 → `_no_cut` 判定成立（video_render.py）→ 跳过剪辑，成片=原片全部画面；且 `_cut_video_by_spans` 异常时**静默 return 原片**（用户完全不知道没剪） |
| ①解说词对不上画面 / ④画面对不上解说词 | 剧情驱动 `_narrate_by_plot` 依赖 LLM 语义对齐（`_llm_align_and_refine`→`_allocate_script_spans`），对齐失败静默回退 bigram 匹配；**对齐结果用户不可见、不可改**；分析阶段还把 keep=False 段静默剔除，渲染拿到已无空隙的 segs 促成 no_cut |
| ②配音丢失/不合成 | 两步走 UI 误导（9-23 已修）+ TTS 失败段仅写 `tts_failures` 前端展示弱 + 合成「完成」未校验成片文件是否存在 |

**共同根因**：中间产物（剪辑决策/解说词-画面映射/配音状态）不可见 + 失败静默降级。

## 二、本轮改动（已落盘 + 已验证）

### 后端
1. **cut_plan（剪辑方案）落盘**：
   - `_narrate_analysis`（本地解说）：保留段（带解说词/重要性/原因）+ 被剪段（keep=False+原因）全表 → `progress['cut_plan']`
   - `_narrate_by_plot`（剧情驱动）：解说词↔画面区间对照（narr_map）+ 智能剪辑被剪段 → `progress['cut_plan']`
   - cut_plan 含：src_dur / coverage / kept / removed / removed_sec / segments[]
2. **渲染按方案真剪（修复③）**：
   - `_render_narrate`：`cut_plan.removed>0` → `skip_no_cut=True` 强制真剪（不再按首尾覆盖跳过）
   - `_cut_video_by_spans` / `_cut_and_burn_video`：新增 `skip_no_cut` 参数
   - `compose_movie_from_tts`：tts_state 携带 cut_plan，第二步恢复并传 skip_no_cut
3. **失败不静默**：
   - `_warn(progress, msg)` 辅助（去重写入 `progress['warnings']`）
   - `_cut_video_by_spans` 降级写 warning；P1-1 回退两遍管线写 warning
   - `dispatch_narrate` / `dispatch_movie_compose` / `dispatch_movie`：**final 文件校验**（存在+≥1KB），否则明确报错，杜绝「显示✅完成却无视频」
   - 修复降级分支 `(_ce or '')[:120]` 对异常对象切片的 TypeError（真实剪辑失败会崩在这，已修）

### 前端（app.js v36 / style.css v15 / index.html）
1. `renderCutPlan(el, p)`：🎬 剪辑方案卡片——覆盖率、保留/剪除统计、**时间轴色块条**（绿=保留/红=剪除）、每段时间+保留/剪除标签+原因+解说词对照
2. `renderWarnings(el, p)`：⚠️ 降级/失败红字展示（不静默）
3. 4 处接入：本地解说完成 / 本地解说两步走第一步 / 电影解说两步走第一步 / 电影解说合成完成
4. index.html 新增容器：narCutPlan / narWarn / movieCutPlan / movieWarn

## 三、验证
- 真实端到端（spring10s.mp4 → /api/narrate）：cut_plan 落盘 ✅（coverage=100%、2 段保留、每段带解说词）；成片产出 ✅；final 校验通过
- 新回归测试 `tests/test_cut_plan_transparency.py`：4 passed（removed>0 强制真剪 / no_cut 维持 / 降级写 warning / _warn 去重）
- 既有冒烟 7 passed, 1 skipped（含 plan 链路）
- 前端：浏览器实测 4 容器在 DOM、app.js?v=36 加载、函数就位

## 四、本轮效果（对 4 个问题）
- **③剪辑失效**：分析剪了什么 → 前端时间轴可见（绿/红）；removed>0 → 渲染必真剪；剪辑失败 → 红字告知
- **①④解说/画面错位**：每段解说词与画面时间范围对照展示，错位一眼可见；后续可手动改
- **②配音丢失/不合成**：成片文件硬校验 + 明确报错；配音失败进 warnings
- **透明化**：coverage 显示「成片保留画面 X%」——若 100% 用户能直接看懂「为什么没剪」

## 五、未完成 / 下一步（第二轮候选）
1. **剪辑方案「可勾选」**：目前 cut_plan 只读展示；把「⑥手动调整」的勾选/改稿能力接到本地解说流程（/api/narrate）——用户可直接取消勾选段 → 渲染真剪
2. **解说词逐段可改 + 重新对齐**：把 /api/narrate/align 暴露为「按新解说重匹配分镜」
3. **README 项目主页更新**（9-23 用户点名，仍未做——可并入交付时处理）
4. **本轮改动未 commit/push**（含 9-23 晚的两步走修复+i18n，用户确认后再推）

## 六、技术备忘
- 服务启动：`cd C:\Users\XOS\Desktop\spring_video; $env:PORT='8765'; python webui_server.py`（OUTDIR=webui_output）
- 前端改后必须 bump：app.js?v=36、style.css?v=15；校验 `node --check static/app.js`
- Windows 下 Edit 工具曾 CRLF 匹配失败 → 用「Write 脚本 + PowerShell 执行 python」做精确替换（已验证可靠）
- 测试基线：`python -m pytest tests/test_cut_plan_transparency.py tests/test_plan_plot_smoke.py tests/test_plan_cut_smoke.py tests/test_run_cleanup.py`

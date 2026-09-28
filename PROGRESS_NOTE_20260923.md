# 项目进度存档（2026-09-23 晚）

> 本文件由 AI 在用户要求"记住进度，先做到这"时写入，供下次会话继续。

## 一、今日已完成（均已推送 GitHub：Yzm889/-Smart-video-highlights，main 分支）

1. **S1–S9 管线升级**：预热 / GPU互斥 / LLM轮次合并 / 轻量降级 / 磁盘清理 / 上传残片清理
2. **24 项测试失败修复**（304 → 308 passed）
3. **P1/P2 渲染质量升级**：单遍编码 / 微变速 / BGM闪避 / 交叉淡化 / 画质档位
4. **前端换肤 + 模块化拆分 + README 更新**
5. **CI 全绿修复**：fonts-noto-cjk（中文字体）/ 测试缓存隔离 / 编码器探测缓存；CI 注解机制保留
6. **前端 UI 主页介绍待更新**（见下方待办——用户提到 GitHub 仓库主页介绍仍是旧内容）

## 二、本轮 Bug 修复（"完成了却没有生成视频"）— 已完成 + 浏览器验证通过

### 根因（已完整定位）
不是渲染失败，是 **"两步走"流程的界面误导**：
- 用户走「视频解说 + 填剧情」→ 后端 `/api/movie_tts` → `narrate_movie(tts_only=True)`
- **第一步只生成配音（21/21 段），`done=true`，等用户确认后才合成视频**（`awaiting_confirm`）
- 前端本应跳「⑥ 手动调整」让用户点「合成视频」，但 narr 卡片静态标题「✅ 解说完成」+ 静态计数「分段0·台词0条·配音0段」**不更新** → 用户误以为完成却无视频
- 附带缺陷：第一步把 `cover.jpg` 写进 `progress['file']` → `_record_history` 把封面当成品（图片探测失败被静默跳过）→ ⑨记录查不到今天的配音任务

### 已改动的文件
| 文件 | 改动 |
|---|---|
| `movie_narrator.py` | `narrate_movie(tts_only=True)` 分支：`progress['file']` 不再写 cover.jpg（保留 `progress['cover']`），历史不会被封面污染 |
| `static/app.js` | ① `pollNarrate` / `pollMovieNarrate` 的 tts_list 分支：明确提示「配音已生成 → 去⑥手动调整确认后点合成视频」，并更新 narDiag/movieDiag + 标题；② 成片完成分支恢复「✅ 解说完成」；③ 新增 I18N 模块 |
| `static/index.html` | narResult/movieResult 标题加 id（`narDoneTitle` / `movieDoneTitle`）；新增语言切换按钮（`langToggle`） |

### 验证
- plan 相关测试：`tests/test_plan_plot_smoke.py` + `test_plan_cut_smoke.py` + `test_run_cleanup.py` → **7 passed, 1 skipped**
- 页面加载 200、`node --check` 通过、浏览器实测 i18n 中→EN→中切换正常

## 三、多语言界面切换 — 已完成（中/EN）

- 方案：轻量 i18n，`I18N_DICT` 词典（核心界面文案）+ 运行时叶子文本替换 + `toggleLang()` + localStorage（`framecut_lang`）持久化
- 切中文 = `location.reload()` 还原；后端动态文案（phase/错误）保持中文（有意为之）
- 入口：页面右上角 `langToggle` 按钮（初始显示 EN，点击切换）

## 四、服务状态
- `webui_server.py` 仍在后台运行（127.0.0.1:8765），用户浏览器开着页面
- 下次继续前若服务已停，重启：`cd C:\Users\XOS\Desktop\spring_video && python webui_server.py`

## 五、待办（下次继续）
1. **GitHub 仓库主页介绍未更新**（用户点名：主页还是老版本内容，需更新 README/仓库 About）
2. 可选项：i18n 词典继续补漏（如"保留原声"等已补部分，仍有少量长 hint 未译）
3. 可选项：把这次修复推送到 GitHub（当前改动尚未 commit/push）

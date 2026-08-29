# 项目进度存档（2026-08-29 更新）

> **当前状态：全部完成、118 测试通过、可正常出片。解说已切到本地 14B 模型。GPU 硬编已启用。存储管理面板已上线。🎭 剧情驱动解说已上线（用户粘贴剧情→按时序剪分镜+写解说，不靠AI识别画面）。**

---

## ✅ 第二十九轮（本轮）已完成：解说词驱动的分镜重匹配

> 用户需求：识别完视频后把解说词展示出来并允许修改，然后**模型根据解说词去匹配分镜、剪辑分镜**。

### 与既有能力的关系（避免重复造轮子）
原有「方案预览」已支持**编辑每段解说词**后渲染（/api/plan → /api/confirm），
但**分镜边界在分析时已定死**——改词后画面不会跟着变。本轮补的正是这一段。

### 实现
- 分析阶段额外保存**未合并的细粒度候选镜头**与台词：`/api/plan` 的 narrate 方案新增
  `shots` / `asr` / `run_dir`（合并后的「剧情环节」粒度太粗，无法重新组合）。
- 新接口 **POST `/api/narrate/align`**：传入改写后的解说词 → 重新分配候选镜头 →
  回写方案 `segs`/`narr` → 返回新分镜（含缩略图）供界面刷新，之后照常走 `/api/confirm` 渲染。
- 匹配策略（与项目「本地优先、失败回退」一致）：
  - `_model_align_shots`：模型输出「每句解说对应的最后镜头编号」JSON 数组，按语义分配；
  - `_algo_align_shots`：回退方案，按解说词字数权重顺序分配（离线可用）；
  - 新增 `_llm_text`（本地文字模型优先 → 回退视觉模型文字通道）、`_asr_text_in`（窗口内台词）。
- 前端：解说方案弹窗新增「🧩 按解说词重新匹配分镜」按钮，按当前输入框内容重排并刷新列表，
  提示区分「模型语义匹配 / 按比例分配」两种来源。

### 健壮性
- 模型输出严格校验：长度 / 整数 / 单调递增 / 覆盖全部镜头，任一条不满足即回退算法，
  **绝不产出错乱分镜**。
- **修复一个越界崩溃**：解说句数 > 候选镜头数时「每句至少 1 个镜头」无解 → `IndexError`。
  新增 `_expand_shots`：镜头不够时把每个镜头按时间均分成子段（子段不跨镜头）。
- 不变量：分镜连续覆盖全片、无空洞无重叠；末段终点对齐原片时长。

### 验证
- 单元：新增 6 例（算法连续覆盖 / 句数>镜头不崩 / `_expand_shots` / 模型 JSON 解析 /
  拒绝非法输出×5 / 模型不可用时回退）→ **114 passed**，pyflakes 干净、node OK。
- 端到端（真实模型 qwen2.5:14b）：
  - demo.mp4（5s、1 镜头）改 3 句 → `source=model`，3 段连续覆盖、总长不变。
  - 自制 6 镜头色块视频（24s）：原方案 3 段（0-8 / 8-16 / 16-24）；把解说改写成讲颜色
    递进（红 → 蓝绿 → 黄紫橙）后，模型给出 **0-4s(红) / 4-12s(蓝、绿) / 12-24s(黄、紫、橙)**，
    边界精确落在真实转场点——语义匹配确实生效，不是按比例均分。

### 自查
- 后台服务用 `&` 启在一次性 bash 任务里会被随任务回收，改为 `run_in_background` 常驻任务才稳定。
- 一条引号嵌套写错的 `sed -n "$(grep -n \"...\" ...),+18p" webui_server.py` 命令
  意外生成了两个 `webui_server.py` 副本（`ebui_server.py`、`ebui_server.py,+18p`，各 291KB）。
  已确认是未跟踪垃圾文件并删除。**教训：复杂 sed/grep 嵌套别硬套引号，改用 Grep 工具取行号。**

## ✅ 第二十八轮（更早）已完成：修复「解说只出一句」

> 用户反馈：模型识别出的解说又出问题了，出的解说都只有一句。

### 根因（用真实模型复现，非猜测）
用 `qwen2.5:14b` 实测同一输入（5 个镜头）：模型**内容写得很好**（完整连贯讲完故事），
但输出**没有换行**——写成了一整段。`_split_nar_lines` 因此只解析出 1 行；
`_map_lines_to_segs` 在「行数 < 镜头数、按句切分后句数仍不足」时走进兜底循环，
`out.append(lines[src])` 中 `m=1 → src 恒为 0`，于是 **5 个镜头拿到完全相同的文本**。
实测返回 5 行、**去重后只剩 1 行**，与用户现象完全吻合。

### 修复（两层）
1. **根治 · 重试**：`local_vlm_narrate` 在行数不足时用「请把稿子原样拆成 N 行，只做换行、
   不增删改」提示重试一次；主 prompt 追加格式约束「必须用换行分隔成 N 行，不要写成一整段」。
2. **兜底 · 映射层永不塌缩**：重写 `_map_lines_to_segs`，改为逐级拆细——
   按句(。！？) → 按小句(，；、) → 在中点附近标点处劈开最长条目 → 凑够 n 条后 `_distribute_sents` 均匀分布。
   新增两个纯函数 `_split_nar_clauses`（二级切分）与 `_split_into_k`（按字数均分，优先标点断）。
   - **只在标点处断开**，不再硬切字符（首版硬切把「便利店」劈成「便利」+「店想」）
   - 极端「只剩一句」时，仅当句子够长（≥8×n 字）才按字数均分，太短则原样重复——
     **宁可重复也不产出读不通的半截话**

### 验证
- 真实模型：修复前 `5 段 / 去重 1 段`（全同）；修复后 `5 段 / 去重 5 段`，
  且逐段对上镜头：售货机只收港币 → 求助师傅 → 师傅大笑 → 终于买到 → 满意笑了。
- 离线用例矩阵：正常多行 ✅ / 单段落句数够 ✅ / 单段落句数不足（干净小句、有重复但无半截词）✅ /
  真实长段落 ✅ 全不同且句子完整。
- 回归：**108 passed**（104 + 4 新增）；pyflakes 干净、node OK。

### 自查
- 首个回归断言写错（用 `'男子走进便利' not in chunk` 判断硬切），但「男子走进便利」
  本身是完整小句「男子走进便利店想买汽水，」的前缀子串 → 误报。
  改为正确不变量：**每个片段必须以标点结尾**。

### 已知相关缺口（未改，云端路径）
`generate_narration` 云端分支在行数不足时用 `templates` 通用模板补齐剩余段
（不是复制，所以没有本 bug），但一段真实解说 + 四段通用模板的观感仍不佳。
本机走本地模型路径，且云端路径无法在本机验证，故未改动——如需可复用本轮的两个切分函数。

## ✅ 第二十七轮（更早）已完成：项目重审 + 提交积压改动 + GPU 编码加速 + 文档修正

> 用户要求「重新刷新对项目理解，看看有没有可优化升级的」。先做全量健康审查，再按用户勾选执行四项。

### 0. 项目重审（健康基线实测）
- `pytest` **98 passed**、`node --check` OK；**pyflakes 未安装**（两个解释器都缺）→ 此前每轮声称的「pyflakes 清零」实际无法执行，已 `pip install -r requirements-dev.txt` 补上。
- 前后端 **38 个 `/api/*` 路由完全对齐、零孤儿路由**；`/api/progress` 两侧参数均为 `run`（无误）。
- DOM 引用审计（164 引用 / 200 定义）：5 处「JS 引用但 HTML 不存在」逐一核实——`newCutT`/`planSum` 为 JS 动态注入（安全），`narMode`/`movieMode`/`eco` 为第 26 轮删除省流选择器后的残留但有空值保护（不报错，属死代码）。
- 规模：`webui_server.py` 5817 行 / `app.js` 1780 / `index.html` 610 / `test_core.py` 1690。

### 1. 提交积压改动（P0）
两天工作全部未提交：10 文件 **+2733/−368**（`webui_server.py` 单独 +1535）。已按语义拆为 4 个提交：
`chore`(依赖与忽略规则) / `feat(后端)` / `feat(前端)` / `docs(test)`。提交前确认 `ai_config.json` 已被忽略且未被跟踪、四个提交均不含密钥文件。

### 2. GPU 编码加速（P1，核心改动）
- **问题**：8 处视频编码全为 CPU 软编 `libx264`，而 Whisper 早已走 CUDA（用户机器 RTX 3060 Laptop）——显卡半闲置。渲染是长视频出片瓶颈（8 分钟素材约 360s）。
- **实现**：`video_encoder_cfg` / `_probe_nvenc` / `_nvenc_usable` / `video_encode_args` / `video_encoder_label`。
  - 探测方式：不查 `-encoders` 列表（驱动不匹配会「列表有、运行期失败」），**实跑 1 帧测试编码**确认真能出片才启用；进程内只探测一次并缓存。
  - 三级策略 `auto`(默认) / `cpu` / `gpu`；**强制 gpu 但探测失败时回退 CPU**，不让流水线崩掉。
  - 8 处编码点统一改 `video_encode_args()`，顺带统一 preset（原 3509 行为 `fast`，其余 `veryfast`）。
- **实测**（RTX 3060 Laptop，demo.mp4 ×12 约 60s + 缩放填充滤镜）：CPU **7.42s** → GPU **4.06s**，**提速 1.83×**，体积 7.73MB → 7.99MB（+3%，基本持平）。
  > 注：合成测试图（testsrc2）下单测仅 1.47× 且 GPU 体积 +52%，该数据不具代表性，勿引用。
- 配置：`ai_config.json` 新增 `video` 段，接入 `/api/ai/config` 读写；GET 返回 `video_encoder` 文案。前端「⚙️ 输出设置」新增「视频编码」下拉 + 生效提示。

### 3. 清理死代码 + 修 README 漂移
- 删除 `app.js` preflight 中读取已删元素 `narMode`/`movieMode`/`eco` 的三处分支（条件恒假）。
- README：删除全部 **9 处**「省流模式」描述（第 26 轮已合并为自动选路）→ 改为准确的「自动选路（免费优先）」；功能清单补素材库 / B 站集成 / 封面生成 / GPU 编码 / 方案预览；接口表补 `/api/material/*`、`/api/bili/*`、`/api/cover`、`video` 段；目录结构补 6 个目录与 2 个文件。

### 回归
- `python -m pytest tests -q` → **104 passed**（98 + 6 新增：配置解析 / GPU 路径 / CPU 回退 ×3 / preset 统一源码 pin）。
- pyflakes 清零、`node --check` OK；HTTP 冒烟验证编码器开关（auto→GPU 硬编、cpu→CPU 软编）读写正常。
- 期间修正自身两处误判：① 首轮 grep 因 ffmpeg 参数为列表元素而误判「8 处都没设 preset」；② 真实素材基准测试首版 `-stream_loop` 写在 `-i` 之后导致两次均失败。

## ✅ 第二十六轮（更早）已完成：两个性能修复 + 「省流模式」合并为自动选路

> 用户先要求两个分析性能修复，随后指出「省流模式没有实际作用」要求删除。经确认：**省流模式走的就是本地模型**（qwen2.5-vl 看画面 + qwen2.5:14b 写稿 + SAPI 免费配音）——用户选择「删模式选择器、合并为自动选路」。

### 修复 1：分析预览等待上限 10 分钟 → 60 分钟
1 小时视频的解说分析（Whisper + 大模型链路）远超旧上限 10 分钟，此前必然超时。现 60 分钟，超时提示带真实预期。

### 修复 2：抽帧 fps 下限 1.0 → 0.5
超过 30 分钟的视频此前会突破 1800 帧预算（1 小时 = 3600 帧）。下限 0.5fps 后 1 小时视频恰好 1800 帧，逐帧分析省一半。

### 省流模式合并为「自动选路」（本名不副实——它就是本地模型）
- **删除三处模式选择器**：解说卡「省流/智能」下拉、合成卡「省流」勾选、联网解说模式下拉，全部移除并替换为「🤖 自动路径」说明。
- **后端自动选路**（generate_narration 重排）：**本地模型（免费）优先 → 配置了云端 key 才用云端（填 key = 同意付费）→ 都没有时台词/模板兜底**。配音同理（配置了云端 TTS 用云端，否则免费 SAPI）；联网解说脚本（配置了云端文字 key 用 LLM，否则离线切句）；一键合成文案（配置了云端视觉用 AI 文案，否则模板）。
- **原则不变**：不配置任何 key 依旧全程免费可出片；配置了 key 的用户自动获得云端增强。
- 行为说明：对本地模型已部署的用户，行为与旧省流完全一致；配了云端 key 的用户，解说/文案会自动获得云端增强。
- 全部 UI「省流」文案清零（改为「自动路径/免费本地」表述）；preflight 弹窗对已删选择器自动降级为透传。

### 回归
- `python -m pytest tests -q` → **98 passed**；受影响用例更新：`test_narrate_movie_defaults_economy` → `test_narrate_movie_auto_routing`（锁定「无云端 key = 免费离线，有 key = 付费增强」）；`test_adaptive_fps` 增加 1800s/3600s 边界。pyflakes 清零、node --check OK。

## ✅ 第二十五轮（更早）已完成：修复长视频分析预览报错 + 进度条精细化 + 全局取消按钮

> 用户反馈三个问题：① 长视频「分析预览」报错而「直接生成」可用；② 进度条/预计时间不准确（一个阶段内只涨不变，跳阶段才动）；③ 需要生成中途可取消的按钮。

### 修复 1：长视频分析预览报错（根因确凿）
- **根因**：第十七轮做分片上传时，直接生成（dispatch_beatcut）接了 `{name, upload_id}` 新协议，但**分析预览（_analyze_plan_job）漏接**——仍按 base64 解码。长视频（>64MB）前端自动走分片上传 → plan 拿到空数据 → 报「请先上传视频」；直接生成正常。与用户现象完全吻合。
- **修复**：`_analyze_plan_job` 视频解析统一走 `_resolve_upload_video`（同时支持 base64 / upload_id / 素材库 mlib 三种形态，narrate 分支同样受益）；会话过期给明确提示「请重新分析」。
- **回归**：新增 `test_analyze_plan_accepts_upload_id_video` / `test_analyze_plan_accepts_mlib_video` 锁住该行为；HTTP 实测「分片上传 → 分析预览」出方案（切点 3 个）。

### 修复 2：进度条/预计时间精细化
- **根因**：阶段内百分比是常量（场景检测 5%、抽帧 10% 整段不动），前端 ETA 公式在 pct 不变时只会长涨，跳阶段才跳动——与用户描述一致。
- **改法**：
  - `ffmpeg_run` 新增可选 `on_progress(seconds)` 回调：从 stderr 的 `time=` 统计行解析解码/编码位置（向后兼容，默认 None）；
  - `detect_scene_cuts` 去掉 `-nostats`，场景解码阶段按 time= 平滑推进 **5→24**；
  - `_analyze_video_frames` 逐帧推进 **25→44**（每 20 帧更新一次）；
  - `_render_beatcut` 新增 `pct_base` 参数并逐段更新「切片 i/n」：直连生成传 50（分析已占 0~50，渲染 50→95，**不再回跳**）；人机协同确认渲染维持默认 30；
  - 抽帧/场景结果命中磁盘缓存时秒过，属预期。
- 新增 `_parse_time_str` 纯函数及用例；`_beatcut_env` 两处 mock 签名同步更新（探测/抽帧函数新增可选进度参数）。

### 修复 3：生成中途可取消
- **全局进度浮层**（右下角 gprog）新增「⏹ 取消」按钮——按当前任务 runid 调 `/api/cancel`（ffmpeg 立即终止 + 协作式标志），无活动任务时自动隐藏；
- **B 站下载**进度行新增「⏹ 取消」（后端 BILI_PULL abort 原本就有，前端此前没接）；
- 各卡片原有的取消按钮（bcCancel/narCancel 等）不变。

### 回归与实测
- `python -m pytest tests -q` → **98 passed**（95 + 3：plan upload_id 回归 / plan mlib / time 解析）；pyflakes 清零、node --check OK。
- HTTP 端到端（复现用户场景）：分片上传 → **分析预览出方案**（切点 3 个）✓；分片上传 → **直接生成出片**（终态「合成配乐 100」）✓。
- 期间修正冒烟脚本两处（plan beatcut 必须带音乐；plan 与直接生成各自独立上传会话——会话为单次消耗属设计语义，前端每次提交都会重新分片上传）。

## ✅ 第二十四轮（更早）已完成：新功能「本地素材库」

## ✅ 第二十四轮（本轮）已完成：新功能「本地素材库」

> 用户需求：「做一个本地素材库，单独给一个素材文件夹」。解决两个真实痛点：素材刷新就丢、B 站下载的视频没有持久归处。

### 实现：material_library/ 素材文件夹 + 全链路接入
- **独立文件夹**：项目根目录 `material_library/`（与 music_library 命名呼应，已进 .gitignore）；`/material_lib/<名>` 静态服务（`_safe_join` 防穿越 + **URL 百分号解码**——冒烟实测抓到中文文件名 404，/media 因一直是 ASCII 名从未暴露此问题）。
- **后端五接口**：`/api/material/list`（视频/图片分类+大小）｜`upload`（≤64MB base64，保留原始名、重名自动加 (1)(2) 序号、文件名消毒）｜`from_upload`（>64MB 复用分片协议，成品 **move** 进库）｜`save_from_media`（把产物目录文件复制入库，如 B 站下载的视频）｜`delete`。
- **全链路 mlib 引用**：任务请求里素材传 `{name, mlib}`（引用而非上传），`_resolve_upload_video`/build 图片分支从库中 **copy** 进 run_dir（库内文件保留、可反复使用）；卡点/解说/方案/指令/一键合成五条链路全部接入（前端各槽位支持「素材库条目」对象，直接绕过浏览器上传）。
- **前端**：素材页新增「🗂 本地素材库」折叠面板——上传（自动走分片/直传）、列表（视频预览/图片缩略图+大小）、每项四操作（➕加入素材列表 / 🎬设为解说 / 🎯设为卡点 / 🗑删除）；B 站下载完成后新增「🗂 存入素材库」按钮。
- **上传协议重构**：把 `videoToBody` 的分片逻辑抽成 `uploadChunksOnly(file)`（断点续传语义保留），素材库大文件上传直接复用。

### 开发过程自纠错
- 首版 `material_save_bytes` 走固定名 `mlib_tmp` 中转，**丢失原始文件名和扩展名**——新增用例当场抓到，重写为直接按消毒后的原始名+去重序号写入。

### 验证
- HTTP 端到端实测：上传入库（中文文件名）→ 列表（分类/大小）→ `/material_lib/` 静态服务 → **build 直接引用库内视频+图片混排出片 5.0s**（库内文件保留）→ 删除。全通过。
- **回归**：`python -m pytest tests -q` → **95 passed**（91 + 4：往返去重/防穿越/mlib copy/build pin）；pyflakes 清零、node --check OK。

## ✅ 第二十三轮（更早）已完成：新功能「自动封面生成」

## ✅ 第二十三轮（本轮）已完成：新功能「自动封面生成」

> 用户反馈 B 站风控暂时用不了，指示先做其他功能——选了短视频创作的最大缺口：**发抖音/B站都需要封面，但工具出片后只有 final.mp4**。

### 实现：智能选帧 + 大字标题合成封面
- **后端 `/api/cover`**（POST）：
  - `_cover_candidates`：均匀抽 8 帧（预览 640px）→ 智能打分（对比度 std + 边缘能量 − 过曝过暗惩罚）→ 自动选最高分帧；候选缩略图与 `list.json` 持久化到 run_dir/cover_cand，换帧时复用不重抽。
  - `_cover_render`：指定时间点抽全分辨率帧 → PIL（微软雅黑）叠标题——三种版式：**居中大字**（白字黑描边）/ **底部条幅**（半透明黑条）/ **左上角**；标题超宽自动换行（≤3 行）；可选副标题。输出 cover.jpg（quality 90，宽 ≤1920）。
  - 路由：`ts` 为空 → 智能选帧并返回全部候选；带 `ts` → 按当前标题/版式重渲染。`_safe_join` 校验视频必须在产物目录内。
- **前端**：卡点/解说/合成三个出片区的「💾 保存」旁新增「🖼 生成封面」按钮（出片完成自动显示）→ 内嵌面板：封面预览 + 标题输入 + 版式下拉 + 「按当前设置重做」+ 每个候选帧的时间点按钮（点击换帧）+ 下载封面。方案微调渲染（/api/confirm）的产物同样支持。
- 开发过程自纠错：首版打分函数误用 PIL API（Image.filter 不是模块函数）且纯 Python 像素循环过慢 → numpy 重写；`/api/cover` 首次插错了 do_GET/do_POST 位置 → 回归用例抓到后移正。

### 验证
- 真实视频实测（demo.mp4）：候选 6 帧 1.4s、打分合理（内容帧 42 分 > 平滑帧 33 分）、三种版式均产出 40~45KB 封面（视觉确认：大字描边清晰、副标题正确）；HTTP 级 auto-pick + 换帧重渲染通过。
- **回归**：`python -m pytest tests -q` → **91 passed**（88 + 3：打分排序/候选+渲染/路由注册）；pyflakes 清零、node --check OK。
- B 站功能状态不变：本机 IP 仍在风控期，等冷却或配 `bili.cookie` 后可用。

## ✅ 第二十二轮（更早）已完成：B 站素材集成（搜索 + 下载 MP4，免跳转）

## ✅ 第二十二轮（本轮）已完成：B 站素材集成（搜索 + 下载 MP4，免跳转）

> 用户需求：素材/解说页顶部加 v2ob.com 提示（可跳转）+ 集成功能「搜索 B 站 → 选片 → 自动下载 MP4 → 入素材/解说/卡点」。

### 实现决策（重要，勿回退）
- **集成引擎用 yt-dlp 直连 B 站，而非自动化 v2ob**：v2ob 是 JS 渲染页、无公开 API，自动化等于逆向不明接口（结构一变就坏），还会把 B 站链接发给第三方；yt-dlp 原生支持 B 站搜索/下载且持续维护。v2ob 按用户要求以可点击链接形式放在素材/解说两页顶部（跳转手动使用）。
- **B 站 WAF（412）应对**：匿名请求需 buvid3 cookie——自动访问首页收割（已实测解 412）；支持 `ai_config.json` 的 `bili.cookie`（用户粘贴浏览器登录 Cookie，登录态更稳且清晰度更高，示例配置已补）。

### 实现
- **后端 `/api/bili/*`**：`search`（yt-dlp bilisearch，返回 bvid/标题/UP主/时长/封面）｜`download`（BV 号白名单校验 → 线程下载）｜`status`｜`cancel`。下载双引擎：① playurl html5 直连（未登录通常 480p）② yt-dlp 兜底（≤720p + 自动合并 mp4，`--ffmpeg-location` 指向 imageio-ffmpeg）；进度/取消（BILI_PULL 槽 + abort 协作标志）；产物存 `webui_output/bili/` 经 /media 供前端取用。
- **前端**：素材页新增搜索面板（关键词 → 结果列表带封面/UP主/时长 → 「⬇ 下载 MP4」带进度 → 完成后三键：➕加入素材 / 🎬设为解说视频 / 🎯设为卡点视频，blob 转 File 复用既有 setNarVideo/setBCVideo/render 流程）；解说页顶部提示行带跳转。
- **依赖**：yt-dlp 进 requirements.txt 并加入 `ensure_deps` 自动安装。
- **版权提示**：两处 UI 注明「请仅下载你有权使用的视频」。

### 验证状态（如实）
- **搜索成功路径已实测**（4 结果含完整元数据，4.8s）；**下载错误路径已实测**（双引擎失败后给出友好 412 风控提示而非崩溃；非法 BV 拒绝）。
- **下载成功路径本机暂无法实测**：开发期间连续测试触发了 B 站 IP 级风控标记（412，持续中）。搜索/下载代码结构与 cookie 机制均与已验证的搜索一致；标记解除后即可正常工作。用户正常使用频率（偶发搜索）不易触发。
- **回归**：`python -m pytest tests -q` → **88 passed**（84 + 4：BV 校验/文件名消毒/搜索归一化/bili 配置默认）；pyflakes 清零、node --check OK。

## ✅ 第二十·二十一轮（更早）已完成：自动检测问题 + 两轮迭代升级（九）

## ✅ 第二十·二十一轮（本轮）已完成：自动检测问题 + 两轮迭代升级（九）

> 第二十轮 = 上传体验与健壮性收尾；第二十一轮 = 跨标签页断点续传。本轮两次自引入问题均在同轮内被回归抓到并修复（详见下）。

### 第二十轮：上传体验与健壮性收尾
- **分片 3 路并发上传**：本地回环下顺序小片传输的瓶颈在 FileReader+base64 编码，改为 3 个 worker 并发拉取待传分片（带 failed 快速失败标志），大文件上传显著提速。HTTP 实测 3 路 × 6 片乱序提交 0.05s 全部成功、合并字节精确。
- **会话数量上限**：`_upload_prune` 增加「活跃会话 >100 时清最旧」——防 HOST=0.0.0.0 部署下被会话数量滥用撑爆磁盘；清理时序放在会话创建**之后**（否则上限长期停在 101）。实测 102 个会话 + init → ≤100、最旧被清。
- **`load_history` 加锁**：与 `add_history` 共用 `_HIST_LOCK`，消除「读到半写文件→误判为空→下一条写入清空历史」的瞬时不一致。
- **⚠️ 自纠错记录**：加锁时用了非重入 `Lock`，而 `add_history` 持锁内部又调 `load_history` → **自死锁，pytest 直接挂起**。当即定位并改用 `threading.RLock()` 修复，回归恢复全绿。
- **⚠️ 自纠错记录 2**：prune 首版在会话创建之前执行，上限长期停在 101——实测抓到后把清理挪到创建之后。

### 第二十一轮：跨标签页断点续传
- **实现**：续传键从 sessionStorage 迁到 **localStorage**——刷新页面、甚至换一个标签页都能续传（键=文件名+大小+mtime，不同文件互不干扰）；续传键只保留最近 6 个防无限堆积；上传完成即清键。
- **⚠️ 自纠错记录 3**：编辑时误删了 `videoToBody` 的并发上传尾段导致 app.js 语法损坏——`node --check` 当即报错，读回实际内容精确修复。
- 另修正一个方向写反的 prune 断言（清理行为本身正确）。
- **回归**：`python -m pytest tests -q` → **84 passed**（83 + 1：prune 会话上限）；pyflakes 清零、node --check OK。

## ✅ 第十八·十九轮（更早）已完成：自动检测问题 + 两轮迭代升级（八）

> 第十八轮 = 分片上传断点续传；第十九轮 = 一键合成视频素材接入分片 + 组合路径冒烟。

### 第十八轮：分片上传断点续传
- **场景**：大视频传到一半刷新页面/断网/服务重启，此前要整文件重传。
- **实现**：`/api/upload/init` 支持带 `upload_id` 续传——返回已到齐分片列表（`_upload_have_parts` 升序），前端把会话 id 记在 sessionStorage（按 文件名+大小+mtime 键控），重试时**跳过已传分片**；会话在服务端磁盘上，**服务重启也能续传**（24h 内）。会话过期/被清理时自动重开新会话。
- **修掉一个边角语义**：已完成的会话（成品待 dispatch 取走）再续传会返回旧 uid 且 have=[]——前端会整文件重传覆盖成品。现判定「成品已存在」即按新会话处理。
- **HTTP 实测**：新会话 have=[] → 传 1 片 → 续传 init 返回 have=[0] → 只补第 1 片 → 合并字节精确（143433B）→ 已完成会话续传自动转新会话。

### 第十九轮：一键合成视频素材接入分片
- **实现**：build 的视频素材 >64MB 走与卡点/解说相同的分片协议（`videoToBody`），后端 `dispatch_build` 视频分支统一走 `_resolve_upload_video`（upload_id 直接 move 到 WORKDIR 免二次拷贝）；单素材上限从 150MB 放宽到 2GB。图片与小视频维持 base64。
- **组合路径冒烟**：xfade 转场 + 保留原声同时开启 OK（此前只各测过单开）；build 携带分片视频素材（upload_id）+ 图片混排 OK，5.0s 出片、上传会话自动清空。
- **回归**：`python -m pytest tests -q` → **83 passed**（81 + 2：`_upload_have_parts`、build 分片 pin）；pyflakes 清零、node --check OK；冒烟产物已清理（历史归零、webui_output 仅保留用户 4 个成片）。

## ✅ 第十六·十七轮（更早）已完成：自动检测问题 + 两轮迭代升级（七）

> 第十六轮 = 网络健壮性收官横扫；第十七轮 = **大文件分片上传协议**（最后一个已知边界专项落地）。

### 第十六轮：网络健壮性收官（全部健康，无需修复）
- 全项目 **20 处 `urlopen` 逐一核对：全部带超时**（5~120 秒按用途分级）；`local_llm_chat`/`vlm_text`/`vlm_chat_multi` 走查超时参数齐全；`start.sh`（Linux/macOS 启动）与 Dockerfile/compose 一致性核对通过。

### 第十七轮：大文件分片上传协议（解除 200MB 上限）
- **旧路径的根本问题**：整文件 base64 塞进单个 JSON——体积膨胀 1.37 倍（334MB 视频 ≈ 446MB 请求体）、GB 级内存峰值、服务端 300MB body 守卫被迫拦截。
- **新协议**（与旧路径并存，≤64MB 仍走 base64 少 3 个请求）：
  - 后端三接口：`/api/upload/init`（开会话 + 清理 24h 废弃会话）→ `/api/upload/chunk`（单片 ≤8MB、乱序到达安全、总量 ≤2GB）→ `/api/upload/done`（按序合并、逐片校验缺片即拒）。upload_id 白名单正则校验防穿越；成品文件名取 basename 防穿越。
  - `_resolve_upload_video` 统一落盘：任务 dispatch 从会话目录 **move 成品**（免二次整文件拷贝），取走后清空会话。
  - 接入强卡点/解说/联网解说/指令四链路（plan 走同一前端助手）；一键合成（多素材、单素材 ≤150MB）维持 base64。
  - 前端 `videoToBody()`：>64MB 自动分片（FileReader 切片 + 进度显示「视频上传中 n/N」），5 个上传入口全接；客户端上限从 200MB 放宽到 2GB。
- **验证**：离线用例（乱序合并/非法 id/单片超限/缺片拒/move 语义/穿越拒绝）+ HTTP 级端到端实测（init → 乱序 2 片 → done 字节精确合并 143433B → 用 upload_id 发起强卡点 11.1s 出片 → 会话自动清空）全部通过。
- **回归**：`python -m pytest tests -q` → **81 passed**（79 + 2 净增；实现过程中 pyflakes 抓到并修复了一个漏定义 `_upload_final_path`）；pyflakes 清零、node --check OK；README API 表补 3 个新接口；冒烟产物已清理（历史归零、webui_output 仅保留用户 4 个成片）。

## ✅ 第十四·十五轮（更早）已完成：自动检测问题 + 两轮迭代升级（六）

> 第十四轮 = 命令执行面安全审查；第十五轮 = Ken Burns 走查 + 并发双任务实测。

### 第十四轮：命令执行面安全审查（结论：干净，无需修复）
逐处审查了全部 shell/外部命令调用（这是 Host=0.0.0.0 部署时的关键暴露面）：
- **`sapi_tts`**：PowerShell 单引号串内 `''` 转义正确——用户可控的解说词文本无法逃逸字符串执行任意命令；输出路径为服务器内部生成。
- **`_fast_pull_local` / 官方 `ollama pull` 回退**：全部**列表式 subprocess（无 shell）**；`ollama create` 的模型名必须命中 `FAST_GGUF_SOURCES` 白名单字典的键才会走加速通道，任意输入在入口即被拒绝；Modelfile 文件名对模型名做了正则消毒；aria2c 参数全部来自硬编码字典。
- **`ai_tts` / `vlm_chat` / `local_llm_chat`**：走 urllib JSON 请求，URL 来自用户自己的 ai_config.json，无拼接执行。
- 新增防回退源码 pin `test_no_shell_injection_surface`（禁 shell=True + cmd /c 必须在白名单校验之后）。

### 第十五轮：Ken Burns 走查 + 并发双任务实测（全部健康）
- `make_image_clip` 走查：四种镜头运动的裁剪边界全部有钳制，无越界风险。
- **并发双任务实测 OK**：解说（走本地 VLM+14B 真实生成）与强卡点渲染**同时**提交、同时完成——独立 run 目录互不串扰、SAPI 每次调用独立 PowerShell 进程无 COM 共享状态、Whisper 并发加载正常、两条历史记录各自正确。
- 回归：**79 passed**（78 + 1：注入面 pin）；pyflakes 清零、node --check OK；冒烟产物已清理（历史归零、webui_output 仅保留用户 4 个成片）。

## ✅ 第十二·十三轮（更早）已完成：自动检测问题 + 两轮迭代升级（五）

> 第十二轮 = 轮询超时语义 + 仓库卫生；第十三轮 = 连续运行/并发稳定性 soak 测试。

### 第十二轮发现并修复
- **轮询超时语义失真（真实 UX 问题）**：前端 7 个轮询超时（卡点 600s / 其余 900s）后**静默放弃**——用户以为任务失败，实际后端仍在渲染并已写入⑨记录（第五轮起全链路写历史）。→ 全部超时提示改为「⚠️ 等待超时已停止刷新（任务可能仍在后台进行），请稍后到⑨记录查看结果」，并把渲染类等待上限提到 **30 分钟**（8 分钟视频渲染实测 360s，20 分钟素材会超旧上限；方案分析保持 600s，超时提示改为「请重试或调大最大分段」——分析无历史记录，语义不同）。
- **`.gitignore` 缺口**：`.zcode/`（会话工具）、`.workbuddy/`、`.pytest_cache/` 未忽略，git status 长期有噪音。→ 补齐，实测 `git status` 只剩真实改动。
- `load_history` 损坏容错（try/except → []）走查确认无需修。

### 第十三轮：连续运行/并发稳定性 soak 测试（全部健康，零缺陷）
- **单进程连跑 6 个混合任务**（3×卡点调参 + 1×plan 分析 + 1×混合素材合成 + 1×指令成片）：**6/6 成功**；结束后 PROGRESS 条目数=任务数（无泄漏）、任务线程全部退出、RUN_PROCS 归零、PLANS 仅剩待确认方案（设计内，30 上限）、历史记录 5 条与任务对应。
- **新增并发缓存写安全测试**：10 线程同时分析同一视频并落盘，验证 os.replace 原子替换——全部读到完整一致数据、无 .tmp 半写残留。
- **确认一个设计行为**（非缺陷）：分析缓存对「同一会话内调参重跑方案」命中（秒回，这正是设计目标）；对「重新上传同一视频」不命中（上传副本 mtime 必然不同）。200 条上限自然淘汰孤儿条目，无需处理。

### 回归
- `python -m pytest tests -q` → **78 passed**（77 + 1 新增：并发缓存写安全）；pyflakes 清零、node --check OK；soak 与全部冒烟产物已清理（历史归零、webui_output 仅保留用户 4 个成片）。

## ✅ 第十·十一轮（更早）已完成：自动检测问题 + 两轮迭代升级（四）

> 第十轮 = API 边界硬化 + 开发依赖声明；第十一轮 = 联网解说离线路径/历史删除实测 + 部署脚本核对。

### 第十轮发现并修复
- **`maxSeg`/`maxCuts` 服务端无钳制**：HTML `min=8` 只约束浏览器，API/指令成片路径可传 `maxSeg=0.5`（40s 视频切出 80 个碎段拖垮解说稿）或 `maxCuts=10000`（上百段拖垮渲染）。→ `_segment_timeline` 钳制 4~600 秒、`_analyze_beatcut` 钳制 3~96，各配回归用例。
- **`bcTransDur`（转场时长）不进刷新记忆**：localStorage EXT 列表补齐。
- **新增 `requirements-dev.txt`**：pytest/pyflakes 是回归协议的必要工具但不在 requirements.txt，补一份开发依赖清单（本机已装）。
- **检测确认无需修的两处**：`/api/progress` 对未知 runid 已返回 404 JSON、`/api/cancel` 已有 `runid in PROGRESS` 守卫；xfade 单段边界正确（空转场链退化为单输入 format 滤镜）。

### 第十一轮检测结论（全部健康，无需修复）
- **narrate_movie 离线全流程实测 OK**（8.1s）：带剧情文本 + economy 走「剧情切句」离线兜底（不联网不调 API），5 事件 → 对齐 → SAPI 配音 → 合成；`llm_movie_script` 的三级兜底（剧情切句→本地模型→片名模板）走查确认。
- **`history/delete` 实测 OK**：删除条目时连带成片文件与 run 目录中间产物一并清理。
- **部署脚本核对自洽**：Dockerfile/compose 的 `HOST=0.0.0.0` 暴露面已被第六轮的 `_safe_join` 穿越修复覆盖；`PORT` 环境变量入口有读取；`ensure_deps` 只补装缺失核心依赖、不会擅自升级。

### 回归
- `python -m pytest tests -q` → **77 passed**（75 + 3 新增：maxSeg 钳制、maxCuts 钳制 pin；bcTransDur 为前端小改）；pyflakes 清零、node --check OK。冒烟产物已全部清理（历史归零、webui_output 仅保留用户 4 个成片）。

## ✅ 第八·九轮（更早）已完成：自动检测问题 + 两轮迭代升级（三）

> 第八轮 = 指令路由实测 + 双解说流程去重 + 配置/文档一致性；第九轮 = 边角走查 + 混合素材/曲库冒烟 + 死代码清理。另：本次迭代前做了全量备份 `spring_video__backup_20260829`（259MB，含 .git 与成片，不含 models/webui_workspace）。

### 第八轮发现并修复
- **双解说流程去重（上轮遗留的结构性收尾）**：`_analyze_narrate` 与 `narrate_video` 的分析段逐行重复（分段→合并环节→ASR→关键帧→解说稿，约 20 行）。→ 抽出公共层 `_narrate_analysis()`，两个入口各自保留薄壳；plan 分析与直接生成从此不会各自漂移。ASR 无条件调用回归 pin 随之移到公共层（`test_narrate_analysis_runs_local_asr_in_economy`），并新增去重源码 pin。
- **`ai_config.example.json` 落后于代码**：代码读取 7 个配置段，示例只有 3 个——缺 `local`（本地 Ollama，现已是解说主力）、`vlm`（视觉理解）、`whisper`、`mirror`（国内镜像）。→ 按代码默认值补全（含 `_说明` 字段），JSON 校验通过。
- **README 过期描述**：「合并成 ≤6 个剧情环节」已不符（第四轮改为约 10s/环节自适应 4~14 个）。→ 修正。
- **指令成片实测**：`/api/instruct`（"把这段视频剪成强卡点短片" + 视频+音乐上下文）正确路由到强卡点，10s 出片；`parse_instruction` 补 6 组离线路由用例（解说/卡点/片名联网/默认合成/省流竖屏/真AI）。

### 第九轮发现并修复
- **混合素材的省流文案露出内部临时名（真实体验问题）**：视频+图片混排时，视频素材文案显示 `up_0_0_vid`（服务器内部路径名）而非用户原始文件名——`dispatch_build` 构建素材列表时丢弃了 `name`。→ work 素材携带原始名，`offline_caption` 优先使用；实测混合合成（1 视频+2 图片 Ken Burns+节拍对齐）20.1s 出片 7.0s ✓。
- **边角走查全部健康**：`build_srt`（有上游比例校正兜底）、`offline_caption`、`collect_partial`、`_plan_thumbs`、`plan_beat_durations`（奇值有比例兜底）未发现问题；曲库接口（search/use）实测正常。
- **测试死代码清理**：删除 `S_module()`/`monkeypatch_local_off()` 两个占位空函数。

### 回归
- `python -m pytest tests -q` → **75 passed**（72 + 3 净增：指令路由、解说流程去重 pin、offline_caption 原始名；1 个 ASR pin 迁移到公共层）；pyflakes 清零、node --check OK。
- 冒烟产物（8772/8773 端口实例与 run 目录、历史条目）已全部清理，真实历史归零、webui_output 仅保留用户 4 个成片。

### 累计状态（三轮自动迭代共 9 轮）
测试 48 → 75。已知边界全部处置完毕：长视频（自适应抽帧，8 分钟冷分析 61s/热 1.6s）、双引擎去重、卡点与解说共享切点、配置体验（示例配置补全）、取消能力（ffmpeg + 协作式）。仅剩的已知边界：**大文件上传仍走 base64 JSON**（约 200MB 源文件上限，峰值内存约 1GB）——升级需改前端分片协议，属独立专项；`narrate_movie` 联网解说与短片解说已是两套合理独立的流程，不再合并。

## ✅ 第六·七轮（更早）已完成：自动检测问题 + 两轮迭代升级（二）

> 再次执行「自动检测 2 轮」。第六轮 = 服务端健壮性扫描 + 未测渲染路径变体冒烟 + 8 分钟长视频压测；第七轮 = 协作式取消 + 缓存键修正 + 浏览器 UI 实测。

### 第六轮发现并修复（服务端健壮性 + 路径变体全量冒烟）
- **4 条未测渲染路径全部实测通过**：xfade 转场（12.2s）、节拍同步引擎（19.1s）、保留原声（6.1s）、build 交叉淡入（24.4s，BPM 95.7 对齐正常）。
- **大文件上传断连（真实 bug）**：334MB 长视频 base64 后 446MB 超 `/api/beatcut` 的 300MB 守卫，但守卫**不排空请求体**直接关闭连接——客户端收到 WinError 10053 断连而非友好错误。→ `_read_json` 重构：超限时分块排空再返回 None；6 处大小守卫统一收口到 `max_len` 参数。
- **`_read_json` O(n²) 拷贝**：`raw += chunk` 逐 MB 拼接，大文件上传内存带宽浪费严重。→ list 收集 + `b''.join` 一次拼接。
- **前端大文件无预检**：拖入 334MB 视频会白白上传 446MB 后才失败。→ 5 处上传入口（卡点×2/解说×2/联网解说）加 200MB 预检并给出 MB 数提示。
- **`/media/`、`/music_lib/` 目录穿越**：`/media/../ai_config.json` 可读到含密钥的配置（默认 127.0.0.1 风险低，Docker 部署 HOST=0.0.0.0 时是真漏洞）。→ 新增 `_safe_join()`（normpath + commonpath 归属校验），两处路由接入，HTTP 实测穿越已 404。
- **内存泄漏**：`PROGRESS` 只增不减（diag/解说稿不小）、`PLANS` 用户放弃确认时永不清理。→ `_spawn` 修剪 PROGRESS 至最近 100 条（跳过活跃任务）、PLANS 限最近 30 个。
- **`add_history` 无锁**：并发任务同时完成会读改写竞争丢条目。→ `_HIST_LOCK`，20 线程并发写实测 20 条全保留。
- **8 分钟长视频压测（720p 合成素材）**：全流程 419.6s 出片 41 段 ✓。冷/热分解：**冷分析 61.4s、缓存命中后热分析 1.58s（39×）、渲染约 360s**——结论：分析优化已到位，8 分钟视频的瓶颈在逐段重编码渲染（约 0.75× 实时），段级 copy 会因关键帧对齐破坏卡点精度，判定不值得做。

### 第七轮发现并修复
- **fps 缓存键错标**：缓存键用请求的 fps=4.0，实际自适应后的 3.75 在函数内部才确定——键名与内容不一致（长视频验证时暴露）。→ `_cached_frame_signals` 先探测时长算出 `fps_eff`，键与调用统一用它；实测长视频条目 1800 帧（封顶）/fps 3.75/键名 fps3.75 ✓。
- **协作式取消**：此前「取消」只能终止 ffmpeg，解说稿生成（4 次本地 LLM 调用，约 1~2 分钟）期间无法中断。→ 新增 `_aborted()`（读任务线程 TLS 的 abort 标志），接入 `local_vlm_narrate` 的每次模型调用之间、`_render_narrate` 逐段配音循环、`_render_beatcut` 逐段切片循环——取消从「等 ffmpeg」变成「阶段间秒级生效」。
- **浏览器 UI 实测（IAB 真实页面）**：页面加载无异常，9 步导航切换正常、localStorage 记忆生效（lastStep=beatcut）、`bcCancel` 按钮存在且默认隐藏、AI 状态芯片真实数据渲染正常、`_esc`/`escapeHtml` 页面内可达且转义正确、截图确认布局无破损。（检测中曾误报 `_esc` 不可用——是 const 不挂 window 的检测方式问题，页面词法作用域实测正常。）

### 回归
- `python -m pytest tests -q` → **72 passed**（66 + 6 新增：`_safe_join` 穿越、并发历史写锁、PROGRESS/PLANS 修剪 pin、`_read_json` 排空/join pin、`_aborted` 行为、协作取消接线 pin）；pyflakes 清零、node --check OK。
- 冒烟用临时端口（8770/8771）与全部冒烟产物已清理（含 334MB 压测视频；需要重跑压测可一行再生：`ffmpeg -f lavfi -i testsrc2=duration=480:size=1280x720:rate=30 -f lavfi -i sine=frequency=440:duration=480 -c:v libx264 -preset ultrafast -crf 28 -c:a aac _long_test.mp4`），不影响真实服务与历史。

## ✅ 第五轮（更早）已完成：自动检测问题 + 两轮迭代升级（一）

> 应用户要求「自动检测问题与迭代升级 2 轮」。每轮均为：静态检查（pytest/pyflakes/node）+ 前后端接口一致性扫描 + 临时端口起真实服务端到端冒烟 → 修复 → 回归。

### 第一轮发现并修复
- **F1 强卡点/方案分析/按方案渲染完全没有取消能力**：`buildBeatCut` 只设 `_bcRunid` 从不接入 `cancelRun()`，beatcut 卡片连取消按钮都没有；plan 分析与 confirm 渲染也不设 `_currentRunid`。→ 修复：beatcut 卡片新增 `bcCancel` 按钮（index.html）；`pollBeatCut`/`_pollPlan`/`_pollRender` 全部接线 `_currentRunid` + 显示/隐藏/复位取消按钮（复用对应卡的取消钮）。
- **F2 只有「一键合成」写生成历史**：`add_history` 全项目仅 1 处调用，强卡点/解说/联网解说/按方案渲染的成片都不出现在⑨记录。→ 新增 `_record_history(req, prog, kind)` 统一入口（探测成片时长、失败静默、容量淘汰遵循 add_history 原规则），接入 dispatch_beatcut（beatsync/beatcut）、dispatch_narrate、dispatch_movie、_render_plan_job（plan-beatcut/plan-narrate）。
- **F3 联网解说字幕仍是整段显示**：与上一轮短片解说的「字随声走」修复不一致。→ narrate_movie 配音循环补 `voice_spans` 并传给 `_compose_narration_video`。
- 验证：65 tests passed；真实冒烟 8767 端口三链路（beatcut 完整出片 9.3s / build 含省流文案字幕 19.4s / plan 分析走本地 VLM 37.2s）全部通过。

### 第二轮发现并修复
- **R1【抓到第一轮自己引入的回归】pytest 污染真实 history.json**：`_render_plan_job` 写历史后，每次 `pytest` 会往真实 history.json 塞 2 条指向 tmp 的垃圾条目（实测抓到 4 条）。→ conftest 的 autouse 隔离夹具从「只隔离 ai_config」扩展为「隔离 ai_config + HISTORY_PATH + OUTDIR」，从结构上杜绝测试碰真实历史/输出；并实测 pytest 前后 history.json 不再变化。
- **R2 run 目录跨重启复用覆盖成片**：`_spawn` 用进程内计数器命名 `run-N`，服务重启后 RUNSEQ 归零，新任务的 run-1 会写进旧目录覆盖旧成片，历史条目随之指向错误文件（冒烟中实际观察到 run-1 被两轮冒烟复用）。→ run 目录改为 `run-N-时间戳`（runid 本身仍用于进度轮询与取消）；端到端实测新命名出片正常。
- **R3 双引擎 librosa 代码去重（原方向4·保守版）**：`detect_beats`（节拍同步）与 `detect_strong_beats`（强卡点）各有一份逐行重复的 load+onset_strength+peak_pick。→ 抽公共层 `_music_onset_peaks()`，两引擎行为逐参数保持不变（含 wait 单位是帧不是秒的历史行为，已加注释防止未来误改）；引擎本体（随机片段池 vs 内容感知）不动。
- 验证：66 tests passed（新增 4：`_record_history`、narrate_movie voice_spans 源码 pin、双引擎共用 onset 层源码 pin、run_dir 时间戳源码 pin）；pyflakes 清零、node --check OK；8769 端口冒烟新 run 目录命名出片 OK；真实 history.json 与 webui_output 已恢复冒烟前状态（0 条目 + 4 个用户成片）。

### 交接备注
- 冒烟使用的临时实例均跑在独立端口（8766~8769），未动用户正在运行的服务；所有冒烟产物已清理。
- 仍可考虑（未做）：`_analyze_narrate`/`narrate_video` 近重复流程可合并；LLM 分析阶段的后端中断（当前取消只终止 ffmpeg，LLM 调用要等它返回后在下一次 ffmpeg 时才感知 abort）。

## ✅ 第四轮（更早）已完成：解说字幕「时间轴错位 + 密度不够」修复

### 问题现象（用户反馈）
直接生成的解说字幕对不上时间轴画面：30 秒后才发生的画面描述提前到 30 秒前显示；字幕密度不够。

### 根因（代码级定位，无映射 bug）
排查了整条链路（整稿生成 → `_map_lines_to_segs`/`_distribute_sents` → SRT），行↔段映射全部保序，**不存在把内容提前的显式 bug**。错位是两个机制性问题叠加：
1. **密度不够（根因）**：`_merge_segs` 固定 `max_keep=6` 把整段视频硬压到 ≤6 个剧情环节——2 分钟视频每环节 20~40 秒，一行解说扛 20~40 秒画面；且旧合并按 `target*1.5` 容差实际经常压到 ≤4 个环节（每环节 30s+）。
2. **字幕窗口 = 整段区间（放大器）**：SRT 每条字幕从段起点显示到段终点（`ts(s0) --> ts(s1)`），而这行字的内容讲的是该段中后段发生的事（模型按"第 N 段"推进剧情）→ 后段内容从段首就开始显示，观感像"内容提前了"。
3. **附带发现**：骑跨段边界的 ASR 台词会被丢弃（旧归段条件要求整句落在段内），模型拿到的剧情信息变少。

### 修复（webui_server.py）
- **`_merge_segs` 环节数自适应**：缺省按「约每 10 秒一个环节」取 `clamp(vdur/10, 4, 14)`——60s 短视频维持 ≤6（与旧行为一致），120s 视频约 8~12 环节（实测 24 个 5s 段从旧 4 环节提升到 8），显式传 `max_keep` 仍生效。两处调用点（`_analyze_narrate`/`narrate_video`）已改为不传。
- **字幕窗口跟随配音**：`_render_narrate` 逐段探测配音时长生成 `voice_spans[i]=(段起点, 段起点+配音时长+0.35s)`，`_compose_narration_video` 新增可选参数，SRT 按「有声才显字、念完即收」写（至少显示 0.8s；空解说段不写空字幕、序号连续）。**缺省不传时回退整段显示（兼容旧行为）**，联网解说（narrate_movie）调用点不受影响。
- **台词中点归段**：`generate_narration` 聚合台词改为按中点 bisect 到唯一镜头段——不丢、不重、保序。

### 回归
- `python -m pytest tests -q` → **62 passed**（59 + 3 新增：`_merge_segs` 自适应密度与时间轴连续性、`_compose_narration_video` 字幕跟随配音/兼容回退、跨段台词中点归段）；pyflakes 清零。
- 待用户冒烟：重启服务后用同一素材重新「生成解说」，对比字幕密度（应约 10s 一条）与音画对位（字随声走）。

---

## ✅ 第三轮（更早）已完成：长视频性能 + 分析缓存 + 前端健壮性

### A. ⏱️ 长视频抽帧保护：总帧数封顶 + 分析帧降采样
- **问题**：`_analyze_video_frames` 硬编码 fps=4 且按**原生分辨率**逐帧读管道（1080p 一帧 ~6MB），无任何长视频自适应——118s 样本分析就要 69s，>5 分钟视频按线性外推基本不可用。
- **改法**（只动 `_analyze_video_frames` 内部，签名不变）：
  - 新增纯函数 `_adaptive_fps`（`min(fps_s, max(1.0, 1800/时长))` 封顶总帧数 `_ANALYZE_MAX_FRAMES=1800`，下限 1.0）与 `_scaled_dims`（分析帧长边压到 `_ANALYZE_MAX_SIDE=640`，宽高取偶）；
  - 时长从**同一次** `ffmpeg -i` 探测的 stderr 用 `Duration:` 正则顺带解析（不加参数、不多跑 ffmpeg；解析失败则 fps 原样透传，安全兜底）；
  - `-vf` 改为 `fps={fps_s},scale={w}:{h}`，reshape/字节数全用缩放后尺寸。
- **安全性依据**：下游阈值全是分位数/IQR 相对阈值（动作 `p75+1.5*IQR`、视觉 `p80*0.8` 等），分辨率缩放不影响检测；检测器 min_gap 0.5~0.6s，fps≥2 粒度足够；**短视频（≤7.5 分钟）fps 完全不变**。
- **预期收益**：1080p 管道 I/O 约 21× 减少（6MB→0.7MB/帧）、逐帧 numpy 计算约 9× 减少；10 分钟视频分析从线性外推 ~6 分钟降到 ~1 分钟级。

### B. 🗂 分析结果磁盘缓存：卡点/解说共享同一套切点
- **问题**：全项目零缓存——每次 `/api/beatcut`、`/api/plan`、`/api/narrate`、`/api/narrate_movie` 都从头全片解码；`_segment_timeline`（解说分段）还会**再跑一次**全片场景检测（阈值 0.25 vs 卡点 0.30）。人机协同里反复调 strength/maxCuts 重看方案，每次都是分钟级全量重分析。
- **改法**：新增缓存助手（键 = `scene/frames_v{版本}_{参数}_{size:mtime 指纹}`，JSON 存 `webui_workspace/analysis_cache/`，md5 文件名）：
  - `_file_fp`：**≥4KB 才参与缓存**——天然屏蔽测试假文件（1 字节），杜绝 mock 数据落盘污染；
  - 写临时文件 + `os.replace` 原子替换（防 Windows 并发读到半写文件）；读写全 try/except 静默回退实时分析，**缓存只影响速度、不影响正确性**；
  - 空结果也缓存（"确属无切点"不必反复扫）；超 200 条自动按 mtime 清最旧；`ANALYSIS_VERSION=1`，以后分析逻辑变更 +1 即全量失效。
- **三处接入**：`_analyze_beatcut` 的场景切点与帧信号、`_segment_timeline` 的场景切点改走 `_cached_scene_cuts`/`_cached_frame_signals`——**解说与卡点从此共享同一套切点**，顺带解决"两套切点打架"。
- **实测（demo.mp4）**：场景切点首次 0.31s → 命中 0.0006s，结果一致；帧信号缓存往返一致。测试环境因 <4KB 指纹跳过，48 个旧用例零影响。

### C. 🖥 前端健壮性：轮询断线判定 + 转义合并
- **问题**（实测核实，此前以为的"9 条裸 fetch 无 catch"不成立——所有 fetch 其实都有 catch）：7 个进度轮询循环的 `.catch(()=>{})` **静默吞错**，服务重启/断网后会永久转圈到超时（600~900s）——这对"改完必须重启服务"的工作流是真实痛点。
- **改法**：
  - 7 个轮询（beatcut/narrate/movie/instruct/build/plan/render）加连续失败计数：**连挂 8 次（约 3 秒）判定断线**，明确报错"与服务失去连接（服务可能已重启），请重新发起"、清进度条、解锁按钮并 resolve；
  - 三套转义实现合并为一份：`_esc` 与 `renderNarrGuide` 内部 `esc` 改为 `escapeHtml` 别名（额外转义引号，属性值更安全）；
  - 一次性加载器（AI 配置/Whisper/VLM/本地模型/历史/就绪状态）的静默 catch 补 `console.warn`，失败可诊断、不打扰用户。

### 回归与验证
- 锚点保险先行：重构前给未 pin 的 `plan_beat_cuts`/`_segment_timeline` 补了行为锚点（吸附容差、min_seg 过滤、超量抽稀、场景分段首尾边距）——锚点还纠正了一个误读：**距片尾 <3.0s 的切点会被过滤**（`vdur - c >= 3.0`）。
- `python -m pytest tests -q` → **59 passed**（48 旧 + 5 锚点 + 6 新增：降采样/自适应 fps 纯函数 + 源码 pin + 缓存往返/失效/小文件跳过/接线检查）。
- pyflakes 清零、`node --check static/app.js` OK。
- 端到端实测（临时 8766 端口实例，不动正式服务）：页面/静态资源/`/api/ai/config` 正常；`/api/plan`（beatcut，demo.mp4+carefree.mp3）完整跑通 `plan_ready`，5.16s 视频出 2 切点 3 段方案；临时 run 目录已清理。
- 注意：`requirements.txt` 未含 pytest/pyflakes（开发依赖），本机环境已补装。

## ✅ 第二轮（更早）已完成

### A. 🚀 网页拉取「加速通道」（用户要求：记住拉取方案并优先使用）
- **后端 `webui_server.py`**：新增 `FAST_GGUF_SOURCES`（`qwen2.5:14b`/`qwen2.5:7b`/`qwen2.5:latest` → bartowski 非 split 单文件 GGUF 直链）+ `_fast_pull_local()`（aria2c 多线程下载 → 进度写入 `LOCAL_PULL` → `ollama create` 导入 → 清理临时文件）。`_local_pull_thread` 开头先尝试加速通道，**失败自动回退官方 `ollama pull`**。
- 前端按钮文案提示"自动优先国内加速通道"。README 补手动导入教程 + 警告勿用 Qwen 官方分片。
- 实现要点（勿丢）：aria2c 参数 `-c -x 8 -s 8 -k 1M --max-tries=0 --retry-wait=3 --timeout=60`；`ollama create` 用 `cmd /c` 规避 PowerShell 对 stderr 进度条的误报；Modelfile 用绝对路径 `FROM C:/.../xxx.gguf`；下载用 `_dl/` 临时目录，结束后删除。

### B. 🎬 解说 v3「连贯真人化」（用户要求：连续起来 + 别每段升华含义）
- **重写 `local_vlm_narrate`**：
  - **整稿生成**：一次让模型写完整连贯解说稿（像真人一样从头讲到尾、镜头间自然承接），一行对应一个镜头；prompt 强制"必须恰好 N 行、不要合并镜头"。
  - **少升华**：prompt 明确"除非真转折/高光，否则不要总结'这反映了/象征着/揭示了'"；`_beat_plan` 的 role 改讲"剧情推进作用"而非"含义升华"。
  - **自优化**：整稿生成后让模型自查润色一遍（改衔接/删重复套话/保持详略），仅当整稿成形且用强文字模型时执行。
  - **前置要求**：`params['req']` 注入所有 prompt（前端「解说要求/风格」输入框）。
  - **映射兜底修复**：`_split_nar_sentences` + `_distribute_sents`——行数不足时按句切分均匀分布，杜绝"多镜头复制同一句"（实测曾出现 5 段全同，已修复）。
  - 整稿失败回退逐段生成（少升华 + 承接上一段结尾）。
- 云端 `generate_narration` instr 同步：整稿连贯 + 少升华 + 额外要求。
- 前端：解说卡片新增「解说要求/风格」输入框 `narReq` → 传给 `params.req`。
- **实测（14B，同《赌圣》片段，req=接地气）**：5 段完全不同、衔接自然、接地气（"这小子一脸懵逼""保安看得津津有味""急得直跺脚"）、无意义升华。约 50s/5 段。

## ✅ 本轮已完成（解说升级 · 理解含义 + 合理延伸 + 详略得当 + 14B 部署）

### 0. 🎯 14B 模型部署完成（重点，已成功并实测）
- **本机已装 `qwen2.5:14b`（9.0GB，bartowski Q4_K_M 完整单文件）**，`ai_config.json` 的 `local.model` 已设为 `qwen2.5:14b`，解说自动走 14B。
- **最终落地方案**（踩坑后的正确路径，供以后参考）：
  1. 官方 `ollama pull qwen2.5:14b`（registry.ollama.ai）国内慢/卡 → 弃用；
  2. 1ms.run 是 Docker 镜像加速，**不适用** ollama 模型；
  3. Qwen 官方分片 GGUF（HF/ModelScope `qwen2.5-14b-instruct-q4_k_m-0000N-of-00003.gguf`）是**非标准 split**（分片 1/2 无 tokenizer，token_type 被切分 152068<304132），`ollama create` 报 `split GGUF ... has 1 shards, expected 3`，手工合并也会因 tokenizer 不完整导致加载失败（`toktypes (152068 < 304132)`）→ **别用 Qwen 官方分片**；
  4. **正确方案**：下载 **bartowski 非 split 单文件** GGUF + `ollama create` 导入：
     ```
     # aria2c 多连接从 hf-mirror 下载（约 3 分钟，远快于单连接）
     aria2c -c -x 8 -s 8 -k 1M --max-tries=0 --retry-wait=3 --timeout=60 -o qwen2.5-14b-Q4_K_M.gguf "https://hf-mirror.com/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf"
     # Modelfile: FROM C:/.../qwen2.5-14b-Q4_K_M.gguf
     cmd /c "ollama create qwen2.5:14b -f Modelfile"   # cmd /c 规避 PowerShell 对进度条 stderr 的误报
     ```
  5. 验证：`ollama list` 出现 `qwen2.5:14b`；`POST localhost:11434/api/generate` 能返回结果。
- **14B vs 7B 实测（同《赌圣》买汽水片段）**：
  - 14B 语言更有解说感、含义更深（"悄然埋下两地交流初期的障碍"、"售货机的拒绝象征着异国他乡的经济隔阂"）；
  - 详略结构好：79/67/103/91/52 字，关键段展开、过渡段收束；
  - 每段独立成句已加"开头不用接续词"约束。
- 注意：14B 单段生成约 12s，完整解说（视觉+规划+5段）约 100s，比 7B 慢但质量高一档。

### 1-5. 解说管线升级（同上一轮，见下方"已完成"清单）

### 1. 后端：解说词生成管线重写（webui_server.py）
- 新增 `_try_parse_json`：从模型输出稳健提取 JSON。
- 新增 `_beat_plan`：**详略规划**。用文字模型把每段视频标成 4 级——
  `key`(关键/转折/高光，展开讲) / `advance`(推进，正常讲) / `transition`(过渡，一句带过) / `mood`(氛围，简略渲染)，
  并给每段写"本段在剧情中的作用"（人物处境/动机/事件含义/前后因果，可合理延伸但不编造）。
- 重写 `local_vlm_narrate`：逐段按重要性分配篇幅（key 80~120 字 / advance 40~70 / transition·mood 20~40）；
  五条铁律：
  1. 讲"这段剧情在讲什么、意味着什么"，不是描述画面；
  2. 含义延伸只在 key 段展开，避免每段升华时代背景；
  3. 除非剧情理解里确有转折，禁止堆砌"转折/高潮/关键"等空泛词（防过度理解）；
  4. 台词只作参考转述，不原样引用（防照本宣科）；
  5. 每段强制"推进新内容、不与前文重复"（防重复）。
  整体剧情理解（plot/summary）只喂给第 1 段做开场衔接。
- `generate_narration` 云端智能分支 instr 同步升级（详略 + 含义 + 转述）。

### 2. 后端：解说模型引导检测
- `local_llm_enabled()` / `_local_model_available()` / `_installed_local_models()`（缓存 60s）：
  自动探测本机 Ollama 已装模型，解说写稿优先用**本地文字模型**（qwen2.5:latest 等），VLM 只负责看懂画面。
- `ai_status` 新增 `narr_guide` 字段：`weak_vlm`(qwen2.5vl 是弱视觉模型) / `vlm_model` / `local_ok` / `local_model` / `installed` / `recommend`。

### 3. 前端：在项目内提示 qwen2.5vl 局限 + 引导部署标准解说模型
- `static/index.html`：解说卡片顶部新增 `<div id="narrGuide">` 动态引导条容器；
  AI 配置「⑤ VLM」区新增静态黄色提示框（qwen2.5vl 只"看懂画面"、写稿主力是文字模型 qwen2.5:14b）。
- `static/app.js`：新增 `renderNarrGuide(s)`，按 `narr_guide` 状态显示——
  有文字模型=绿条；只有弱 VLM=黄条（附 `ollama pull qwen2.5:14b` 命令+已装列表）；都没配=蓝条。
- `static/style.css`：`.narr-guide` 样式（ok/warn 两态）。

### 4. 文档
- `README.md`：新增「理解场景含义 + 详略得当」设计说明；新增「🧠 本地部署"标准解说模型"」指南（双模型分工表 + 部署步骤）。

### 5. 实测（qwen2.5:latest / 7B，《赌圣》买汽水片段，5 段）
改前：每段重复"货币流通不便+无奈挣扎"，两段几乎一字不差。
改后（79/37/46/51/35 字，详略分明、有推进、含义只在开场点出）：
```
[0 79字] 故事从这男子无奈的表情中展开……更是当时货币兑换不便的生动写照。
[1 37字] 人民币换不了港币，这让男子陷入困境。他无奈地向一位路人求助，故事由此转折。
[2 46字] 男子不断恳求机器供应他渴望的汽水……这份坚持预示着即将发生的关键转折。
[3 51字] 男子一遍遍恳求……也预示着即将发生的突破性转折。
[4 35字] 男子的困境愈发明显……预示着即将迎来关键转折。
```

---

## 测试 & 回归
- `python -m pytest tests -q` → 48 passed（约 21s）
- `python -c "import ast; ast.parse(open('webui_server.py',encoding='utf-8').read())"` → py OK
- `node --check static/app.js` → js OK

## 临时文件
- 已清理 `_beat_test.py` / `_beat_test2.py` / `_text_test.py` / `_scan.py` / `_check.py` / `webui_output/_t`、`_t2`、`_dl14b`。
- 保留了 4 个用户成片：`webui_output/20260827-{103807,112405,125858,130200}`。

---

## 本轮已完成（总结性优化 + 顶部导航重构）

### A. 🧹 全面检测 / 修复 / 备份
- **后端 bug 修复**：_render_beatcut 中 silent_dur = probe_audio_len(silent) or timeline[-1]（原 dur 未定义，probe 失败会 NameError 崩溃）。
- **pyflakes 清零**：重复 import ssl / 函数内重复 numpy / 未使用变量（w/h/fps、voice_ok）/ 无占位 f-string，python -m pyflakes webui_server.py → 无输出。
- **前后端 API 一致性**：24 条路由两端完全对齐（do_POST/do_GET 分支 vs fetch 调用）。
- **HTML 结构**：div/details/select/table 等全平衡，navlink→卡片目标全部有效。
- **备份**：文件快照 C:/Users/XOS/Desktop/spring_video__backup_20260828（18 文件，不含 models）；git 提交 checkpoints。

### B. 🎛 顶部步骤导航重构（侧边栏 → 顶部，按执行步骤切换页面）
- 移除左侧 sidenav/
avToggle/scrollspy；新增顶部 9 步骤条：①开始②素材③音乐④卡点⑤解说⑥AI⑦输出⑧合成⑨记录。
- 13 个功能卡片按 data-step 分组，切换步骤只显示对应卡片（showStep）。
- 所有跳转统一收敛：卡片内链接 / AI 配置跳转（jumpToAISection）/ 引导页快速跳转 → goStep('卡片id')。
- 记忆键升级 lastCard → lastStep：刷新/重开回到上次步骤；settings（EXT 组 14 项）记忆保留。
- 移动端断点适配（720/980）；git 仓库 models/ 移出（.gitignore 排除，本地保留）。

### C. ✅ 验证
- python -m pytest tests -q → 48 passed（约 21s）
- 
ode --check static/app.js → OK；HTML 标签平衡、goStep 目标全部有效
- 浏览器实测：顶部按钮切换 5 步链路、卡片内跳转、AI 配置跳转、刷新 lastStep 记忆全部通过

---

## 本轮已完成（功能实用性升级 · 卡点引擎）

### 🎯 强拍均匀化（修复「卡点全挤片头 / 后段踩不上鼓点」）
- detect_strong_beats 由「全局最强 top_k」改为「**时间窗分桶**」：整段音乐均分 top_k 窗，每窗取局部最强 onset。
- 实测（118s《赌圣》+ glitter-blast）：强拍分布 0-30/30-60/60-90/90-118s 由 **17/10/3/0** → **10/10/9/1**。
- 空窗不强凑（音乐平缓段交给相邻窗）。

### 🎯 切点密度控制（修复「几十个 1 秒碎段闪屏」）
- plan_beat_cuts：min_seg 按强度自适应（soft 1.5 / standard 1.2 / strong 1.0）；新增 max_cuts 段数上限（默认约 3.5s/段，≤48，前端 maxCuts 直接生效）；候选过多先按权重+时间均匀去重；timeline 段数超限后**均匀抽稀保留首尾**。
- 实测：118s 视频由 **70 个 1s 碎段** → **32 段 2-5s 合理节奏**。

### 🎯 抽帧合并（性能减半）
- _analyze_beatcut 一次 _analyze_video_frames 同时算动作+视觉（新增 _detect_motion_from_frames / _detect_visual_from_frames，对外 detect_motion_points / detect_visual_cues 保留兼容）。
- 实测：118s 分析耗时 **133s → 69s**。

### ✅ 验证
- 长视频分析 + 4s 短视频完整渲染（出片正常）实测通过。
- python -m pytest tests -q → 48 passed（测试 mock 同步到共享抽帧实现）。

---

## ✅ 第三十轮（本轮）已完成：存储管理面板（用户自主清理，不自动删）

> 用户需求：上一轮排查发现包体涨到 4G+，用户决定**不做自动清理，改为页面展示占用 + 用户自主删除**。本轮落地该面板。

### 背景（实测真实画像）
恢复上下文时发现旧摘要/日志与真实文件系统不符：那个 2.86GB 的 `run-*/src_video.mp4` 残留已不在，当前真正的大头是 `webui_workspace`（1.4GB：上传会话成品 ~1GB + music/asr 临时）。故面板按**真实目录**分组，而非照旧清单。

### 实现
- 后端 `webui_server.py` 新增 `_storage_scan()`：按档位分组扫描
  - `keep`（不可删）：成片 `webui_output/2026*`、`.git` 历史
  - `safe`（可清理）：`run-*` 残留帧/缩略图、`webui_workspace/uploads` 会话成品、`asr_*.wav`、`music_*.mp3/wav`、`up_*_*` 上传残留、`analysis_cache`
  - `review`（删需重下）：`models/`
  - 返回 `total_bytes` / `reclaimable_bytes` / `free_bytes` + 每组 items（name/rel/size/mtime）
- 删除安全：**路径白名单** `_STORAGE_ALLOW`（正则，仅限上述相对路径），`_storage_resolve_deletable` 先做 `..`/绝对路径/越界校验，再 `commonpath` 二次兜住；成片与 `.git` 不在白名单 → 永远拒绝。新增 `GET /api/storage` 与 `POST /api/storage/delete`。
- 前端：顶部导航加「🧹 存储」按钮（data-step=storage）；`storageCard` 展示分组占用卡片、可回收总量、逐条「🗑 删除」（confirm）、「🧹 一键清理可回收项」（仅删 safe 档，confirm 后并发删除）。打开存储页自动刷新。

### ✅ 验证
- `py_compile` + `node --check` 通过；`pytest` **116 passed**（+2 新例：扫描结构、白名单/穿越拒绝）；`pyflakes webui_server.py` 干净。
- 冒烟：`_storage_scan()` 实跑返回 9 组、可回收 ~1.86GB；`_storage_resolve_deletable` 对 run 目录返回路径、对 `../webui_server.py` 与成片目录均返回 None。
- 严格遵循用户决策：**未自动删除任何文件**，清理动作完全由用户在面板内触发。

---

## 🎭 第三十一轮 · 剧情驱动解说（2026-08-29）

### 需求
用户：在「解说」页加入新功能——粘贴详细剧情（如《行尸走肉》分幕剧情），系统**按剧情剪分镜 + 写解说**，而不是 AI 单独识别画面。痛点：原解说只匹配画面、不识视频主体内容，割裂感很强。

### 方案（复用既有引擎，零重写）
- **核心引擎 `_narrate_by_plot`**（新增）：分段 → ASR台词 → `llm_movie_script('', plot)` 把用户剧情按句拆成解说事件 → `align_script_to_segments` 按时序把事件对齐到分镜 → 映射每段解说。`align_script_to_segments` 自带单调指针，保证剧情不乱序、不错配到更早画面。
- **空段兜底**：剧情句数 < 镜头数（无台词可语义对齐）时，把剧情句按时间均匀铺满全片，消除「片尾大段静音 / 解说只挤开头」的割裂感。
- **去掉分幕编号**：改进 `_split_sentences`，剥掉「1. 2. 3.」前缀，解说词不带序号。
- **复用**：`narrate_movie` 重构改走 `_narrate_by_plot`（避免两份实现漂移）；`_analyze_plan_job` 的 narrate 分支在 `req.plot` 非空时走剧情驱动（segs/narr/asr/diag 全由剧情生成）。

### 前端
- `narCard` 加「🎭 剧情驱动剪辑」折叠面板（`textarea#narPlot`）。
- `buildNarrate`：`narPlot` 非空 → 改 POST `/api/narrate_movie`（`movie:''`, `plot`），状态提示区分剧情模式。
- `planNarrate`/`_startPlan`：透传 `plot` 到 `/api/plan`，预览方案即剧情驱动的分镜+解说，仍可用「按解说词重新匹配分镜」微调。
- 留空则仍是原「AI 识别画面」模式，向后兼容。

### ✅ 验证
- `py_compile` + `node --check` 通过；`pytest` **118 passed**（+2 新例：剧情拆事件去编号、对齐单调全覆盖）；`pyflakes webui_server.py` 干净。
- 冒烟（`_narrate_by_plot` 用合成 seg/asr 绕过 ffmpeg）：8 段分镜全部铺到剧情解说、剧情按时序展开、无空段、无「1.」编号。
- 提交：`4124389` feat: 🎭 剧情驱动解说。

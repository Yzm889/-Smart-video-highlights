# AI 交接与更新清单（每个 AI 必读必更新）

> **铁律：每一轮工作（哪怕只改一行）完成后，必须在本文档「四、更新清单」顶部追加一条记录（日期 + 版本/commit + 一句话改了什么 + 踩了什么坑），并 commit。**
> 这是主人定下的规矩。本文档是下一个 AI 的眼睛——不更新 = 挖坑给后来人。

---

## 一、项目一句话

「一帧成片 (FrameCut)」：本地免费 AI 解说视频工具。上传电影/剧集 → AI 写解说稿（或用户贴稿）→ TTS 配音 → 按台词剪画面 → 烧字幕 → 出片。**两条前端共存、同一个后端**：

- `static/index.html` + `static/app.js` —— 网页版（**已冻结：只修不改**，主人已拍板用桌面版替换它）
- `static/desktop/`（index.html / desktop.js / desktop.css）—— 桌面版 UI，**产品主线**，由 `desktop.py` 打包成 EXE 分发
- 后端：`webui_server.py` + `handler.py` + `workflows.py` + `movie_narrator.py` + `video_render.py` 等，两版共享

## 二、当前状态（2026-11-08）

- **版本 v1.4.3**（commit a2c13d3），CI 走 tag 发布：推 `v*` 标签 → `.github/workflows/release.yml` 自动构建挂 Release
- 主人的日常用机：`C:\Users\19891\Desktop\FrameCut\`（v1.4.0 本地构建 + 后续静态热更新）；升级前备份在 `Desktop\FrameCut_old\`（models 已被迁走，仅旧程序壳）
- **模型现状（主人的 8765 EXE 实例）**：Whisper distil-large-v3 就绪；ollama 已装 qwen3:8b / minicpm-v4.5 / nomic-embed-text / qwen3-vl:8b（含 mmproj）；CosyVoice 项目侧已装（主人音色已克隆），**EXE 副本侧未装**（每目录独立）
- 网页版基线：《惩罚者》S01E01 481s/16% 精剪基线（见 tests/）

## 三、参考升级路线（发新版时照此执行）

1. 改完代码 → `node --check static/desktop/desktop.js` + `py -3.13 -c "import ast; ast.parse(open(f).read())"`（handler/webui 等改动时）→ 8899 验证实例浏览器实测（**不要占 8765，那是主人的实例**）
2. commit → push main → 打 `v*` 标签推 tags（CI 自动出 EXE Release）
3. **本地构建**（不等 CI 也行）：`.venv_build/Scripts/python.exe -m PyInstaller FrameCut.spec --noconfirm` → 产物 `dist/FrameCut/`（~381MB）→ 先跑 `dist/FrameCut/FrameCut.exe --selftest`
4. **升级主人桌面副本**（`Desktop\FrameCut`）：
   - 旧目录整体改名 `FrameCut_old`（保留备份）→ 新 `dist/FrameCut` 复制过去 → headless 迁移：
     ```python
     import webui_server; webui_server.HERE = r'C:\Users\19891\Desktop\FrameCut\_internal'
     import desktop; desktop.DesktopApi().migrate_from(r'C:\Users\19891\Desktop\FrameCut_old')
     ```
     （migrate_from 会：导入 ai_config.json/history.json、递归并入 webui_output 等、models 是 junction 则重建 / 实体则同卷改名）
   - 或让主人走产品化路径：新包首跑的「升级导入横幅」自己选旧目录
   - 只改了 `static/desktop/` 的轮次可以热更新：直接覆盖 `Desktop\FrameCut\_internal\static\desktop\` 然后重启 EXE
5. 真机验证：EXE 启动后探 `127.0.0.1:8765/api/whisper/status`；UI 点击由主人做，状态栏左下角有**模式徽章**（● 桌面直连 / ● 桥连接中… / ● 浏览器模式）

## 四、更新清单（新记录在最上）

- **2026-11-08 待发版（本轮·源码态基线修复）** 干净 clone 首次全量回归 + 真实素材实测：① 修 CI 静态门禁两处 F401（`webui_server.py` ollama_service_state 多余的 `os`、`test_desktop_mode.py` 多余 `sys`）；② 修三处测试隔离——缺 `spring10s.mp4` 由 assert 改 `pytest.skip`、cosy 转写优先级用例把 `tts_engines.HERE` 指到临时假模型目录（原本干净 clone/CI 因 `models/` 被 gitignore 排除而必挂 2 条）；③ **真缺陷**：`ai_providers.parse_subtitle_file` 原先硬编码 `utf-8-sig + errors=ignore`，主人《惩罚者》S01E01 官方 **GB18030** 字幕被吞成乱码且 593 条只解析出 557 条——改为按 UTF-8(BOM)/GB18030/UTF-16(BOM) 逐个严格解码，新增 `tests/test_subtitle_encoding.py` 5 条钉死四种编码。坑：`movie_narrator` 直连 import 会触发 `webui_server ↔ video_render` 循环导入，必须先 import webui_server。验证（源码态，未打包）：`flake8 --select=F` 0 告警、`node --check` 通过、全量 pytest **737 passed / 12 skipped**（原 731 passed / 3 failed / 15 skipped）；真实素材 `前二十分钟.mp4` 切 10s 样片让 3 条剪辑测试从 skip 转真跑；解说稿 docx 2542 字 → 79 句 → 95 段画面 → **481.2s 成片（命中项目《惩罚者》481s 基线）**、时间轴单调递增；GB18030 字幕修复后 593 条全还原、0 乱码。数据（配置/成片/模型）由主人主动清空，样片 `spring10s.mp4` 已被 gitignore 排除、不入库。
- **2026-11-08 v1.4.3（a2c13d3）** 桌面桥自愈：桥初始化持续重试 + 状态栏模式徽章（桌面直连/桥连接中/浏览器模式）+ 选片等四入口 `_ensureBridge()` 先确保桥再走原生。坑：EXE 里桥未连上会**静默退化成上传模式**（主人 5GB 视频白传），已根治。
- **2026-11-08 v1.4.2（6f8e971）** 上传分片 60s 超时+重试（5GB 卡 26% 根因：单片挂起无超时）；>300MB 引导走 EXE；引擎安装接 TTS_SETUP 实时进度（阶段+百分比）。
- **2026-11-08 v1.4.1（11a0ee2）** 导入字幕提到新建卡显眼位置（主人：字幕是命脉）；拖拽 .srt 进窗口即导入（绕开原生对话框）；EXE 原生对话框路径加可见反馈。坑：「点不动」其实是 pywebview 对话框被压/静默失败。
- **2026-11-08 v1.4.0（b77c41a + 74ae3ab + 1f954ae）** 新包首跑自动迁移横幅（migrate_from：配置/历史合并/成片/工作区/models 三态）；4K 预览代理接入（720p 副本热切换）；合成中可取消；不烧录交付 .srt 入口；硬件一键配置（守卫：CosyVoice 在用不接管——首版曾覆盖主人配音引擎，已修+已恢复）；历史「✏️ 编辑」入口。**事故**：迁移测试覆盖过真实 history.json 致索引丢失（成片未丢），已从 webui_output 重建 16 条；migrate_from 因此改为 history 按键合并。
- **2026-11-07 v1.3.x（125a5b4..a2c13d3）** 首页装机引导卡（渐进渲染）；合成链三重断线修复（EXE 点合成无反应的根因：绑定不存在的 btnCompose 元素令脚本中断）；解说主链补齐（高级参数/查剧情/最近项目/配音设置/成片历史）；美术现代化（SVG 图标/动效/深色控件）；浏览器模式兜底（分片上传+base64）；ollama 环境卡服务状态解耦（已装点「启动」不再重装 1.5GB）；**无声成片三层根因修复**（配音相对路径未解析 / cleanup 误删 narr+ srt / 无声兜底报成功）——此修复网页版同样受益；下载进度条（pct+MB+网速）；localhost IPv6 慢解析修复（状态接口 8.3s→0.15s）；模型中心（主人拍板：第一页只看状态，装引擎收进设置）。
- **2026-11-07 v1.2.0（9319441..1b2eaf6）** 桌面 UI v2 完整功能版发布（SRT/配乐直连/设置对话框/工具栏/自检）。
- **2026-11-06 v1.1.x（HANDOFF_20261106.md）** 桌面版首次成包发布；详见该文件。

## 五、已拍板不改清单（别推翻）

1. **主人三原则**：不开倒车 / 不乱做 / 证据说话（每轮必须实测，截屏或断言为证）
2. 一键查剧情：走 `/api/deepseek_plot` 同一条链，只加 UI 不动链
3. 精剪默认开启；《惩罚者》基线 481s/16%，bigram/向量语义匹配实测无提升、默认关，别再提
4. 配音音色：主人自己的声音已克隆验收（models/cosyvoice/voices/主人音色），这条线已收官
5. 第一页只留引导 + 状态展示；装引擎/选模型收进设置「模型中心」
6. 网页版冻结（只修不改）；桌面版是唯一产品主线
7. 硬件一键配置**不得接管**在用的自然配音引擎（cosyvoice/chattts/edge）

## 六、已知的坑（前人踩过，后人别再踩）

1. **frozen 下 HERE = `<EXE目录>\_internal\`**：ai_config.json / history.json / webui_output / models 全在那里；没有任何 env 覆盖（测试隔离是改模块属性）
2. **localhost IPv6 慢解析**：Windows 每次探测白等 ~2s——探测 URL 一律 `_probe_url()` 走 127.0.0.1
3. **desktop.js 顶层绑定不存在的元素 id** → 该行之后全部顶层代码静默死亡，node --check 查不出；改完必须点全编辑器路径
4. **按钮动态改文案用 textContent 会吃掉内嵌 SVG** → 用 `setBtnIcon()`
5. **任务成功后 cleanup 不删 narr*.wav 和 *.srt**（改词重配与不烧录交付的必需品，曾全丢）
6. **ollama pull 官方源对已装模型会整包重下** → 已装走「启动/跳过」；加速通道白名单在 FAST_GGUF_SOURCES / VLM_FAST_GGUF_SOURCES
7. **CosyVoice 安装**：需要系统 Python 3.11/3.12（PyTorch 不支持 3.14），装到 `<HERE>/.venv_cosyvoice`——每个安装目录独立，EXE 副本要自己装一遍；纯 EXE 用户最稳的配音是「下载离线模型 MeloTS」（纯文件下载）
8. **history.json 只能合并不能覆盖**（migrate_from 已实现按 file 键合并）
9. 模型中心/环境卡的探测有成本：拉取中跳过推理探针；轮询间隔拉取中 2.5s / 平时 15s
10. 8899 是验证实例端口；8765 是主人实例；`_verify_env_8899.py` 是现成的验证服务启动器（带桌面令牌）

## 七、下一步候选（主人体验后拍板）

- A. 真机 EXE 全程回归（主人正在做 UI 点击部分；服务端健康已验）
- B. 启动速度优化（EXE 导入全部引擎层，可懒加载）+ 新版本检查提示（GitHub Releases latest 对比）
- C. 编辑器增强：B-roll/AI 推荐画面（/api/recommend_segments 现成）、双轨预览、音效库
- D. 多工作流：图文成片/智能卡点/B站下载/素材库（大工程，分多轮，需主人拍板）

# 一帧成片 · FrameCut

[English](README.en.md) · 当前版本 **v1.4.26**

本地运行的 AI 影视解说 / 短视频剪辑工具。给它一部片子和一段剧情（或你自己写的稿子），它会**写解说词、逐句配音、按内容挑画面、配音念完再切镜头**，最后出成片。

全程本地推理：不花 API 费用、素材不上传、断网可用。也内置强卡点、素材库、B 站下载、自动封面等短视频工具。

<p align="center">
  <img src="demo.gif" alt="一帧成片演示" width="420">
</p>

---

## 目录

- [功能特性](#功能特性)
- [桌面版（下载即用）](#桌面版下载即用)
- [源码运行](#源码运行)
- [使用流程](#使用流程)
- [配置说明](#配置说明)
- [模型与显存](#模型与显存)
- [中文字体](#中文字体)
- [目录结构](#目录结构)
- [开发与测试](#开发与测试)
- [常见问题](#常见问题)
- [贡献](#贡献)
- [许可证](#许可证)

---

## 功能特性

### 影视解说（主线）

| 能力 | 说明 |
|---|---|
| **剧情驱动剪辑** | 先出完整解说稿，再按每句需要的画面时长从原片挑镜头。解说没提到的画面自然被剪掉，成片明显短于原片 |
| **整稿生成** | 开场钩子 / 因果推进 / 结尾升华，一次写完整篇，不是把画面描述拼起来 |
| **自带稿子** | 粘贴你写好的稿子，工具只负责「按稿子找画面 + 逐句配音 + 烧字幕」，一个字都不改你的稿 |
| **配音念完再切** | 画面按真实配音时长补齐，每句留 0.5 秒气口（可调），不再出现「话没说完就切镜头」 |
| **引用台词让位** | 稿子里引用台词的句子（`弗兰克回了一句：“我不想交朋友。”`）不念稿，直接切原片那句台词的镜头 + 原声 + 台词字幕 |
| **留白标记** | 某句只写「（留白）」，这句就不配音、不出字幕，原声全量，把话筒交回给画面 |
| **两步走确认** | 先生成全部配音供试听，确认后再合成视频；不满意可单段重配，不用整片重跑 |
| **手动调整时间轴** | PR 式编辑器：拖拽片段、改入点出点、删除、插入 B-roll、调原声/解说音量、撤销重做 |

### 配音

| 引擎 | 说明 |
|---|---|
| **IndexTTS2**（默认） | 零样本克隆。用一条固定参考音，整片同一把嗓子；支持 8 维情感控制 |
| **Qwen3-TTS · VoiceDesign** | 用文字描述造音色。默认只用来「铸」一条参考音，也作为克隆失败时的兜底 |
| 旧引擎（可选） | edge-tts / sherpa-onnx / 系统 SAPI / CosyVoice / ChatTTS，仍可在设置里显式选用 |

语速统一由 `rate` 控制（默认 1.12×，ffmpeg atempo 保音高），试听与成片同参。

### 其他

- **强卡点**：场景切换 / 大幅动作停顿帧匹对音乐鼓点，自动硬切 + 配乐
- **素材库与 B 站**：本地素材持久化；关键词搜片 → 下载 → 一键入库
- **自动封面**：智能选帧 + 大字标题，三种版式
- **GPU 编码**：自动探测 NVENC，不可用回退 libx264，三档画质
- **显存守门**：配音/写稿/看图三个模型按需换入换出，12GB 卡也能跑完整链路
- **记录与清理**：生成历史带文案可搜索、可回改；中间产物定时回收

---

## 桌面版（下载即用）

**[⬇️ 下载最新版（Windows 64 位）](https://github.com/Yzm889/-Smart-video-highlights/releases/latest)**

- 解压后双击 `FrameCut.exe`，独立窗口，无需装 Python、无需命令行
- 首次使用点「📖 新手引导」装模型（写稿 / 看图 / 台词 / 配音按显存推荐）
- 本地文件免上传直读

---

## 源码运行

**环境要求**：Python 3.9+（CI 覆盖 3.9 / 3.11，打包用 3.11）；ffmpeg 由 `imageio-ffmpeg` 自带，无需单独安装。

```bash
git clone https://github.com/Yzm889/-Smart-video-highlights.git
cd -Smart-video-highlights
pip install -r requirements.txt
```

`torch` 请按 CUDA 版本单独装：

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

**启动**

```bash
# Windows：双击 启动视频工坊.bat
# macOS / Linux：
chmod +x start.sh && ./start.sh
# 通用：
python webui_server.py        # 然后打开 http://127.0.0.1:8765/
```

---

## 使用流程

### 一、做一条解说

1. 拖入片子（或从素材库 / B 站选）
2. 填**剧情**（不知道就点「🔎 一键查剧情」），或直接粘贴**你的稿子**
3. 有官方字幕就一并导入——会跳过 Whisper 识别，快很多
4. 点开始 → 进入**两步走**：
   - 第一步：生成全部配音，逐段试听
   - 第二步：确认后合成，出成片
5. 想改就进**手动调整**：改文案、换画面、插 B-roll、调音量，再合成

### 二、做一条卡点

拖入图片 / 视频 + 选一首音乐 → 「⚡ 一键强卡点」→ 先出方案预览（可删切点、加切点、取消段落）→ 按调整合成。

---

## 配置说明

配置写在 `ai_config.json`（已被 `.gitignore` 忽略，别提交）。参考 `ai_config.example.json`。

```json
{
  "local": { "enabled": true, "base_url": "http://localhost:11434/v1" },

  "vlm": {
    "enabled": true,
    "mode": "ollama",
    "base_url": "http://localhost:11434",
    "model": "minicpm-v4.5:latest"
  },

  "tts_local": { "engine": "auto", "rate": "+0%", "instruct": "" },

  "tts": { "provider": "", "base_url": "", "api_key": "", "model": "", "voice": "" }
}
```

| 段 | 作用 | 备注 |
|---|---|---|
| `local` | 本地写稿模型（Ollama） | Ollama 的 OpenAI 兼容端点在 `/v1` 上 |
| `vlm` | 看图模型 | Ollama **原生**端点在根上，`base_url` 不要带 `/v1`；旧配置写 `vision` 也认 |
| `tts_local` | 本地配音 | `engine: auto` = IndexTTS2 克隆；`rate` 正数更快；`instruct` 是音色描述 |
| `tts` | 云端配音（可选） | 填了 `api_key` + `model` 才走云端 |
| `video` | 编码策略 | `encoder`: `auto` / `cpu` / `gpu` |

> 不配任何 Key 也能全程出片——本地模型 + 本地配音默认就是完整链路。

---

## 模型与显存

12GB 显存（如 RTX 3060）实测可跑通全链路，推荐组合：

| 用途 | 模型 | 体积 | 说明 |
|---|---|---|---|
| 写稿 | `qwen3:8b` | ~5.0G | 14B 会和看图/配音抢显存，12GB 不建议 |
| 看图 | `minicpm-v4.5` | ~6.1G | 也可用 `qwen3-vl:8b`（~5.8G） |
| 台词 | faster-whisper | ~0.2G | 有官方字幕时完全跳过 |
| 配音 | IndexTTS2 | ~6.6G | 与写稿模型串行换入换出 |

三个模型不会同时驻留：管线按阶段释放显存（例如画面理解完成后立刻卸载 VLM，把卡让给写稿模型）。

---

## 中文字体

烧字幕和封面标题需要真正含中文字形的字体，探测顺序：

1. 环境变量 `SPRING_VIDEO_FONT`（字体文件绝对路径）
2. 项目自带 `assets/fonts/`（`.ttf` / `.otf` / `.ttc`）
3. 系统中文字体（Windows 雅黑 / macOS 苹方 / Linux Noto CJK）
4. 扫描系统字体目录

探测不到会**直接报错并给修复指引**，不会静默画出一片方块。

```bash
sudo apt-get install -y fonts-noto-cjk          # Debian / Ubuntu
sudo yum install -y google-noto-sans-cjk-fonts  # CentOS / RHEL
apk add --no-cache font-noto-cjk                # Alpine
```

> 商业发布建议用 OFL 协议的思源黑体（丢进 `assets/fonts/` 即可，无需改代码）。

---

## 目录结构

```
webui_server.py      # 服务入口：HTTP + 路由 + 调度（启动后浏览器操作）
desktop.py           # 桌面窗口（pywebview），打包入口
handler.py           # HTTP 端点（端点 = 方法 + 登记一行）
workflows.py         # 任务工作流与队列（dispatch_*）

movie_narrator.py    # 解说主线：写稿 / 对齐 / 引用让位 / 配音 / 合成
video_render.py      # 渲染：切片 / 微变速 / 混音 / 字幕烧录 / 单遍编码
beat_analysis.py     # 强卡点：场景切分 / 节拍分析 / 切点规划
render_pipeline.py   # 一键合成管线
analyze_pipeline.py  # 预分析（ASR / 抽帧 / 预热）

tts_engines.py       # 配音调度（引擎排序 / 兜底 / 语速）
tts2_engines.py      # IndexTTS2 + Qwen3-TTS 常驻 worker 适配
*_worker.py          # 各 TTS 引擎的常驻子进程
ai_providers.py      # 模型接入（本地 Ollama / 云端 / hf-mirror）
ffmpeg_utils.py      # ffmpeg 封装（超时 / 取消 / 探针）
cache_utils.py       # 分析缓存（指纹 + 参数 + 版本）
text_utils.py        # 文本工具（清洗 / 切句 / 留白标记）

static/              # 前端（index.html / desktop/ / app.js / style.css）
tests/               # 回归测试
assets/              # 图标、字体
FrameCut.spec        # PyInstaller 打包配置
```

运行时数据（均已被 `.gitignore` 排除）：`material_library/` `models/` `webui_output/` `webui_workspace/` `music_library/`

---

## 开发与测试

```bash
pip install -r requirements-dev.txt

py -m pytest tests -q          # 897 passed / 12 skipped
py -m pytest tests -q -k tts   # 只跑配音相关
```

**打包桌面版**

```bash
py -m PyInstaller FrameCut.spec --noconfirm
# 产物 dist/FrameCut/，其中 _internal/ 与 exe 同级
```

**主要接口**（前后端契约）

| 接口 | 说明 |
|---|---|
| `POST /api/movie_tts` | 第一步：写稿 + 全部配音（body: `video`, `plot` 或 `script`, `subtitle_file?`, `params`） |
| `POST /api/movie_compose` | 第二步：按确认的配音合成（body: `run_dir`, `params`） |
| `POST /api/narrate_movie` | 一步直出解说（含合成） |
| `POST /api/plan` / `POST /api/confirm` | 先出方案 → 按调整合成 |
| `POST /api/upload/init` → `chunk` → `done` | 大视频分片上传（>64MB 自动分片） |
| `GET/POST /api/material/*` | 素材库：列表 / 上传 / 入库 / 删除 |
| `GET /api/bili/*`、`POST /api/bili/download` | B 站搜索与下载 |
| `POST /api/cover` | 自动封面（智能选帧 / 指定时间点重渲染） |
| `GET /api/vlm/status` | 看图模型状态（是否启用、是否就绪、已装列表） |

---

## 常见问题

**配音听起来每句换一个人？**
默认引擎是 IndexTTS2 克隆，整片共用一条参考音。若在设置里显式选了 Qwen3-TTS，它按文字描述现场造音色，逐句调用就会每句不同——这是引擎特性，不是 bug。

**配音语速偏慢？**
`tts_local.rate` 调成 `+20%` 即可（保音高变速，不影响音色）。

**成片比原片短很多，正常吗？**
正常。解说覆盖到哪，画面留到哪；未被解说的镜头会被剪掉。这是剧情驱动剪辑的设计目标。

**稿子里引用的台词没换成原声？**
两个前提：引号前要有冒号或说话动词（`回了一句：`），且该台词必须在**片源时长内**。超出片源的引用会照常念稿，不会播不存在的画面。

**设置里选了看图模型，但状态还是「未启用」？**
旧版本存在配置键不一致（写 `vision`、读 `vlm`）与 `base_url` 多带 `/v1` 的问题，已在 v1.4.26 修复。升级后重新选一次模型即可。

**显存不够 / 跑着突然变慢？**
先关掉其他占用显存的程序（尤其是常驻的 Ollama 模型）。配音与写稿模型是串行换入换出的，同时驻留会触发 WDDM 换页，表现为 GPU 占用高但利用率 0%。

---

## 贡献

1. Fork → 建分支 `feature/xxx`
2. 改动后跑 `py -m pytest tests -q`，保持全绿
3. 提交 PR，说明改了什么、为什么

欢迎的方向：更强的卡点/对齐算法、更多 TTS 与视觉模型适配、字幕样式与转场模板、前端交互打磨。

---

## 许可证

[MIT](LICENSE)

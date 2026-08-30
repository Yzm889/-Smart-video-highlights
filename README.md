# 一帧成片 · FrameCut

[English version](README.en.md)
一个**本地运行、AI 智能**的短视频创作工具。上传图片 / 视频 + 选一首音乐，自动生成**强卡点**短片——画面切换点精确匹对到音乐鼓点，支持 AI 看图写文案 + 中文配音 + 字幕烧录。

> ⚡ 智能强卡点、场景/动作分析、节拍对齐全部**本地计算**，不消耗 API 余额。

<p align="center">
  <img src="demo.gif" alt="一帧成片演示" width="400">
</p>

## ✨ 功能

- 🎯 **智能强卡点**：分析视频的**场景切换 / 大幅动作停顿帧**，匹对音乐**大鼓点**，自动硬切 + 配乐成片
- 🖼️ **多素材混排**：图片（自动 Ken Burns 镜头运动）+ 视频 自由组合
- 🎵 **免费踩点音乐库**：内置 CC.BY 商用曲库，在线搜索、一键使用
- 🎵 **节拍对齐**：每 0.5 / 1 / 2 拍可调切换，硬切 / 交叉淡入淡出
- 🤖 **AI 文案 + 字幕 + 中文配音**：支持 DeepSeek（看图写话）、小米 MiMo / 通义（配音），可分别配置
- 💸 **自动选路（免费优先）**：本地模型优先 → 配了云端 Key 才用云端 → 都没有时用离线模板兜底。**不配任何 Key 也能全程免费出片**（不再需要手动切模式）
- 🗂 **本地素材库**：`material_library/` 持久化存放视频/图片，刷新不丢，可反复引用；B 站下载的视频可一键入库
- 🔎 **B 站素材集成**：关键词搜索 → 选片 → 自动下载 MP4 → 直接送入解说/卡点（基于 yt-dlp，尊重版权）
- 🖼 **自动封面生成**：出片后智能选帧（对比度+边缘能量打分）+ 大字标题合成封面，三种版式可换帧
- ⚡ **GPU 编码加速**：输出设置可选「自动 / 仅 CPU / 强制 GPU」，自动探测 `h264_nvenc` 硬编（实测渲染约 **1.8× 提速**，体积基本持平），不可用时自动回退 `libx264`
- 🧠 **本地模型选择卡**：写稿/看图模型在网页里三选一（标清体积/显存/区别），点卡片即下载启用；支持 qwen3 系列（思考段自动剥离，不污染解说稿）与 whisper large-v3-turbo
- 🎛 **方案预览与微调**：卡点切点/解说词先在面板里看得到、改得动，确认后再出片
- 🕘 最近生成历史、可取消合成、竖屏发布规格、多种转场

## 📁 目录结构

```
.
├── webui_server.py        # 后端：本地 HTTP 服务 + 合成/卡点引擎 + AI 接入
├── static/
│   ├── index.html         # 前端页面
│   ├── style.css          # 样式
│   └── app.js             # 前端逻辑
├── ai_config.example.json # AI 配置模板（复制为 ai_config.json 使用）
├── requirements.txt       # Python 运行依赖
├── requirements-dev.txt   # 开发/回归依赖（pytest + pyflakes）
├── material_library/      # 本地素材库（视频/图片持久化，.gitignore 排除）
├── music_library/         # 踩点曲库（.gitignore 排除）
├── models/                # Whisper 权重缓存（.gitignore 排除）
├── webui_output/          # 成片与中间产物（.gitignore 排除）
├── webui_workspace/       # 分析缓存等运行时数据（.gitignore 排除）
├── 启动视频工坊.bat        # Windows 一键启动（双击打开浏览器）
├── _restart_server.py     # 重启启动器（不弹浏览器，用于加载最新代码）
└── start.sh               # macOS/Linux 启动脚本
└── img1~4.png             # 内置春景示例图
```

## 🚀 快速开始

### 环境要求
- Python 3.9+
- （无需单独安装 ffmpeg，依赖 `imageio-ffmpeg` 自带）

### 安装
```bash
git clone <你的仓库地址>
cd <仓库目录>
pip install -r requirements.txt
```

### 启动
**Windows**：双击 `启动视频工坊.bat`（自动打开浏览器）。

**macOS / Linux**：
```bash
chmod +x start.sh
./start.sh
```

或通用方式：
```bash
python webui_server.py
```
然后在浏览器打开 `http://127.0.0.1:8765/`。

### 使用
1. 拖入**图片 / 视频**
2. （可选）选一首**音乐**——从「🔎 免费踩点音乐库」搜一首点「使用」，或上传本地 mp3
3. （可选）打开「🤖 AI」配置你的 API Key —— 不配也能用，会自动走免费本地路径
4. 点「🎬 开始合成」或「⚡ 一键强卡点」

## 🤖 AI 配置（可选，不配也能用）

复制 `ai_config.example.json` 为 `ai_config.json`，填入你的 Key（视觉与 TTS 可分别用不同供应商）：

```json
{
  "vision": { "base_url": "https://api.deepseek.com/v1", "api_key": "你的Key", "model": "deepseek-v4-flash-vision-exp" },
  "tts":    { "provider": "mimo", "base_url": "https://api.xiaomimimo.com/v1", "api_key": "你的Key", "model": "mimo-v2.5-tts", "voice": "Mia" }
}
```

> ⚠️ `ai_config.json` 已被 `.gitignore` 忽略，**不要提交到仓库**。

## 🔤 中文字体（字幕 / 封面标题）

烧字幕和封面标题时，程序会**自动探测**一个真正含中文字形的字体，探测顺序：

1. 环境变量 `SPRING_VIDEO_FONT`（显式指定字体文件路径）
2. 项目自带的 `assets/fonts/` 目录（`.ttf` / `.otf` / `.ttc`）
3. 系统常见中文字体（Windows 雅黑 / 黑体 / 宋体，macOS 苹方，Linux Noto CJK / 文泉驿等）
4. 扫描系统字体目录（最多 400 个文件或 8 秒）

⚠️ **探测不到时不会「凑合」**：程序会中止渲染并给出修复指引，**不会**静默改用不含中文的字体画出一片「豆腐块」——那种图看起来能出片，实际全是方框。

修复方式任选其一：

```bash
# 1) 装一个开源中文字体（思源黑体 Noto Sans SC，SIL OFL 协议，可商用）
sudo apt-get install -y fonts-noto-cjk          # Debian / Ubuntu
sudo yum install -y google-noto-sans-cjk-fonts # CentOS / RHEL
apk add --no-cache font-noto-cjk                # Alpine

# 2) 或把字体文件放进 assets/fonts/ 后重启

# 3) 或用环境变量指定（Windows PowerShell 示例）
$env:SPRING_VIDEO_FONT = "C:/path/to/NotoSansSC-Regular.otf"
```

> 注：Windows 默认命中的微软雅黑版权归方正 / 微软，若成片用于商业发布，建议换成 OFL 协议的思源黑体（丢进 `assets/fonts/` 即可，无需改代码）。

## 🤝 如何参与贡献

1. Fork 本仓库
2. 创建功能分支：`git checkout -b feature/xxx`
3. 提交改动：`git commit -am 'feat: xxx'`
4. 推送到你的分支：`git push origin feature/xxx`
5. 发起 Pull Request

**建议的优化方向**：
- 更强的卡点算法（小节对齐、强拍权重调优）
- 更多转场 / 字幕样式 / 片头片尾模板
- 更多 TTS / 视觉模型供应商适配
- 前端界面打磨

## 🎬 电影解说三阶段（Phase 2 / 3 / 4）

工具在「强卡点」之外，内置了完整的**电影/动漫剧情解说**流水线，分三阶段渐进：

### Phase 2 · 电影解说 v1（本地短片版，少量花钱）✅ 已就绪
- **输入**：拖入 1~5 分钟短片/预告/动漫片段（整部电影暂不推荐，见预算说明）。
- **处理链（本地为主）**：
  1. **分段**：场景切分（`detect_scene_cuts`）→ N 个镜头段。
  2. **台词识别**：`faster-whisper`（本地免费、中文模型 `base`）识别每段台词 + 时间戳（首次会自动 `pip install faster-whisper` 并下载 ~140MB 权重）。
  3. **剧情解说生成（剧情旁白，不是画面描述）**：本地 VLM 先多帧理解整段剧情（人物/事件/台词）→ 生成**连贯的电影解说稿**——开场引入 + 剧情推进，面向观众讲"发生了什么"；配了云端 Key 会自动用云端 LLM 基于台词+画面线索写剧情解说。**未配 Key** 时用离线模板，0 元跑通。
  4. **时间轴**：解说稿按镜头段时间轴自动编号。
  5. **成片**：按时间轴配音（未配 TTS Key 用 Windows 免费 SAPI 中文 TTS；配了则用云端付费 TTS）+ 原声压低 + 可选**配乐**（勾选「加配乐」）+ 烧录解说字幕。
- **产出**：输入短视频 → 输出「带 AI 剧情解说 + 字幕」的解说视频。
- **成本**：本地 ASR 免费；**未配云端 Key 时全程 0 元**，配了则 LLM 按分钟计费（几毛/分钟）。

### Phase 3 · 联网搜索 + 全自动剧情解说 ✅ 已就绪
- **目标**：输入「帮我解说《XXX》」→ 自动完成。
- **做法**：
  1. **联网搜索（多源容灾）**：`web_search` 免费免 Key，优先**百度/必应**（大陆可达），DuckDuckGo 兜底；任一源成功即用，避免单一源被墙导致整功能挂掉。返回 `[(title, snippet, url)]`。无网络时也可直接粘贴剧情文本。
  2. **自动匹配电影**：上传正片 → 用 LLM 生成的「剧情事件」结合**本地 ASR 台词关键词 + 场景时间轴**做语义对齐（`_char_bigrams` 重叠度匹配）。
  3. **时间轴自动对齐（时序保持）**：解说事件按剧情顺序、镜头段按时间顺序**单调分配**，杜绝「第3段解说词错配到更早画面」的乱序问题；未命中事件按顺序补到空闲段，保证全覆盖。
- **难点**：整片 ASR 耗时大；「解说稿↔片段」自动对齐是核心，已用时序保持的加权语义匹配实现（免费、无需向量模型）。
- **LLM 配置**：解说/剧情类纯文本走 `ai_config.chat` 段（见 `ai_config.example.json`），未配 `chat` 时自动回退 `vision` 段（兼容旧配置）；画面描述仍走 `vision`。未配 Key（或指令走免费路径）时用离线切句，0 元跑通。
- **成本**：整部电影 LLM 分析会超 2 元，建议限制时长或用搜索到的剧情梗概替代全片理解。未上传视频时只产出解说稿。

### Phase 4 · 指令化 AI 整合 ✅ 已就绪
- **目标**：自然语言指令一键成片。
- **做法**：顶部「💬 指令成片」输入框是一个**指令解析层**（`parse_instruction`）：
  - `帮我解说《XXX》配这首音乐` → 路由到 Phase 3 联网解说。
  - `解说这段电影 / 解说这段视频（用真AI）` → 路由到本地短片解说（有上传视频时）。
  - `把这段视频剪个强卡点 / 强卡点配乐` → 路由到强卡点。
  - `用春景图合成一个竖屏短视频` → 路由到通用合成。
- **免费优先**：指令默认走免费本地路径；明确含「真AI / 智能 / 花钱」才调用付费接口；含「竖屏/横屏」自动设分辨率。
- **实现**：所有工作流复用同一组 `dispatch_*` 后台函数，前端把当前已选素材（视频/音乐/图片）作为上下文一并提交。

### 相关接口
| 接口 | 说明 |
| --- | --- |
| `POST /api/narrate` | Phase 2 本地短片解说（body: `video`, `params{economy,maxSeg}`, 可选 `music`） |
| `POST /api/narrate_movie` | Phase 3 联网解说（body: `movie`, `plot?`, `video?`, `params`, 可选 `music`） |
| `POST /api/instruct` | Phase 4 指令路由（body: `instruction`, `context{video,music,items}`） |
| `POST /api/beatcut` / `POST /api/build` | 强卡点 / 通用合成（与指令层共用） |
| `POST /api/upload/init` → `chunk` → `done` | Phase 6 大视频分片上传：>64MB 的视频前端自动分片（每片 4MB、单文件上限 2GB），任务请求里 `video` 传 `{name, upload_id}`；≤64MB 仍走 base64 旧路径。三接口分别开会话 / 乱序写分片 / 按序合并，废弃会话 24h 自动清理。 |
| `GET/POST /api/material/*` | 本地素材库：`list` 分类列表 / `upload`(≤64MB) / `from_upload`(>64MB 复用分片) / `save_from_media`(产物入库) / `delete`。任务请求里素材可传 `{name, mlib}` 直接引用库内文件。 |
| `GET /api/bili/search`、`POST /api/bili/download`、`/status`、`/cancel` | B 站素材：关键词搜索（返回 bvid/标题/UP主/时长/封面）→ 下载 MP4（双引擎：playurl 直连 + yt-dlp 兜底）→ 进度与取消。遇 B 站 412 风控时在 `ai_config.json` 配 `bili.cookie`。 |
| `POST /api/cover` | 自动封面：`ts` 为空则智能选帧并返回全部候选；带 `ts` 按当前标题/版式重渲染（居中大字 / 底部条幅 / 左上角）。 |
| `GET/POST /api/ai/config`（`video` 段） | 视频编码策略：`encoder` = `auto`(默认·GPU 可用则用) / `cpu` / `gpu`。GET 另返回 `video_encoder` 字段说明当前实际生效的编码器。 |

## 🎛️ 人机协同 · 方案预览与微调（Phase 5）✅ 已就绪

不再「一键跑到底、出片才知道好坏」——**卡点 / 解说在合成前先出方案，你看得见、改得动**。

### 工作流
1. 点「⚡ 一键强卡点」/「🎙 解说」→ 先走 `/api/plan` 分析，弹出**方案预览面板**（不是直接出片）。
2. 面板里**可视化微调**，满意后点「✅ 按我的调整合成」→ 走 `/api/confirm` 按你的版本出片。

### 卡点方案（可微调）
- **时间轴预览**：所有切点按秒显示在时间轴上，每个切点旁有 `✕`，点一下即删除该处切换（与前段合并）。
- **＋ 添加切点**：手动输入秒数，在任意位置新增一个切换点。
- **段落勾选**：列表每段可取消勾选 = 该处不切换（与前段合并）。

### 解说方案（可微调）
- **逐段解说词可编辑**：直接在方案里改某段的解说文案，改完按你的词配音 + 烧字幕。
- **✂ 减词**：一键把长段解说缩短为一句。
- **🔒 锁定必要段**：把关键片段锁为「必要」，不可误删。
- **段落勾选**：取消勾选 = 去掉该段画面与解说。

### 相关接口
| 接口 | 说明 |
| --- | --- |
| `POST /api/plan` | 先出方案（body: `type=beatcut|narrate`, `video`, 可选 `music`, `params`） |
| `POST /api/confirm` | 按用户微调后的 edits 渲染成片（body: `runid`, `edits{segs[{start,end,caption,on}]}`, `params`） |

> 本次修复：解说 + 背景乐时 `_compose_narration_video` 中 `-stream_loop -1` 与 `apad(whole_dur)` 组合会让 ffmpeg 永久挂起（无限循环流无 EOF，apad 永不输出）——已改为单次输入 + `atrim/apad` 截齐视频时长，实测 10s 短片 9 秒内出片。

## 🎬 解说词 = 剧情旁白（不是画面描述）

解说稿生成已从「逐帧看图说话」升级为「剧情解说」：

- **先理解整段剧情**：把整片关键帧（多图）+ 各段 ASR 台词一次性喂给视觉模型，先判断内容类型、识别作品、重构剧情事件，再写旁白。
- **连贯电影解说稿**：开场白引入（确认作品时点片名/年代，如"19XX年，一部《XXX》横空出世……"；不确定则不编造，用"故事从……"引入）→ 按剧情推进逐段叙述 → 面向观众口播风格。
- **合并过碎镜头段**：影视片段常被切成一堆 4~8 秒短镜头，逐段解说必然重复；现在按时长自适应合并成「剧情环节」（约 10 秒/环节，4~14 个），每环节一句解说，更贴近真人解说节奏。
- **理解场景含义 + 详略得当（像写作文）**：写稿前先用文字模型给每段打「详略标签」（关键/推进/过渡/情绪）——关键/转折/高光段展开讲透并延伸含义，过渡/铺垫段一句带过。解说讲"这段剧情在讲什么、意味着什么"，可合理延伸（人物处境、事件含义、前后因果），但**不照本宣科**（台词只作参考转述，不原样引用）、**不过度理解**（不编造剧情外事实、不堆砌"转折/高潮"等空泛词）、**不重复**（每段强制推进新内容，整体剧情理解只用于开场衔接）。
- **连贯真人解说（整稿生成 + 自优化）**：不再"每个镜头独立生成、各说各的"——而是让模型**一次写出完整连贯的解说整稿**（像真人解说一样从头讲到尾，镜头间用"此时/紧接着/可没想到"等自然承接，一行对应一个镜头），再让模型**自查润色一遍**（改衔接、删重复与套话、保持详略）。行数不足时按句自动补位，杜绝"多镜头复制同一句"。
- **少升华**：只有真正的转折/高光镜头才点含义，**绝大多数镜头只讲剧情本身**（人物做了什么、事态怎么变），不每段都总结"这反映了/象征着/揭示了"——避免夸张、空泛。
- **前置要求（解说要求输入框）**：解说卡片新增「解说要求/风格」输入框，内容作为前置条件注入模型提示词（如"多点幽默 / 少升华 / 语气接地气 / 强调兄弟情"），生成前即可控制风格。
- **关键词（本地/云端两条路径都支持）**：
  - 本地路径：`ai_config.json` 配 `vlm`（如 ollama qwen2.5vl），0 元跑剧情解说；
  - 云端智能：配 `chat` + `vision`（如 DeepSeek），解说质量更高（更长、更会点出作品背景）。未配 `chat` 时自动走本地路径。

> ⚠️ 本地小视觉模型（如 qwen2.5vl）能输出连贯剧情旁白，但对"这是哪部作品、深层剧情冲突"的识别有限；想要《赌圣》示例那种"点片名、讲人物背景、有年代感"的解说，建议配置云端 `chat`（DeepSeek 按量计费，几毛钱）或换用更大本地模型。

### 🧠 本地部署"标准解说模型"（推荐，免费、隐私、效果接近云端）

解说靠**两个模型分工**，别指望一个 `qwen2.5vl` 全包：

| 职责 | 模型 | 说明 |
| --- | --- | --- |
| 👁 **看懂画面**（人物/场景/事件/剧情理解） | `qwen2.5vl`（视觉模型） | 只负责"看图"，不擅长写词 |
| 📝 **写剧情解说稿**（主力，决定解说质量） | `qwen2.5:14b` 等**文字模型** | 点片名、讲人物背景、有年代感，靠它 |

**部署步骤**（Ollama 已装的前提下，装法见界面「路径 A」）：
```bash
# 1. 拉取文字模型（写解说稿主力，推荐 14B；电脑配置一般可换 qwen2.5:7b）
ollama pull qwen2.5:14b

# 2. 拉取视觉模型（看图，仅当还没有）
ollama pull qwen2.5vl:latest
```

> ⚡ **国内拉取优先走"加速通道"**（网页内点「📥 拉取模型」会自动优先执行，无需手动）：
> 官方 `ollama pull` 走境外 registry 源，国内慢/卡。本项目对 `qwen2.5:14b` / `qwen2.5:7b` 内置了**加速通道**——从 hf-mirror 拉 **bartowski 非 split 单文件 GGUF**（Q4_K_M）+ `aria2c` 多线程下载（几 GB 约 3 分钟）+ `ollama create` 本地导入，网页拉取自动使用；加速通道失败时自动回退官方源。
>
> **手动加速导入（网页拉取也失败时）**：
> ```bash
> # ① 多线程下载单文件（免科学上网，hf-mirror 直连）
> aria2c -c -x 8 -s 8 -k 1M --max-tries=0 --retry-wait=3 --timeout=60 -o qwen2.5-14b.gguf ^
>   "https://hf-mirror.com/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf"
> # ② 用 ollama 导入（Modelfile 一行：FROM 上面文件的绝对路径）
> ollama create qwen2.5:14b -f Modelfile
> ```
> 7B 同理把 URL 换成 `Qwen2.5-7B-Instruct-GGUF` 的 `Qwen2.5-7B-Instruct-Q4_K_M.gguf`。
> ⚠️ 别用 Qwen 官方分片 GGUF（`-00001-of-00003`）：其分片格式非标准，`ollama create` 会报错或加载失败。
然后在界面「🤖 AI 配置 → ③ 本地模型」把**模型 model 填成文字模型**（如 `qwen2.5:14b`），「⑤ VLM」保持视觉模型 `qwen2.5vl:latest`。程序会自动用「视觉模型看画面 + 文字模型写解说」，无需改代码。

> 💡 界面「短片解说」卡片顶部会实时提示：当前视觉模型是否偏弱、是否已部署好文字模型，并给出推荐命令。

## 📄 License

[MIT](LICENSE)

## 🙏 致谢

- 音乐库部分曲目来自 [Incompetech](https://incompetech.com)（Kevin MacLeod，CC.BY 4.0）
- 视频处理基于 ffmpeg，节拍分析基于 [librosa](https://librosa.org)

## 🐳 Docker 一键部署

适合服务器 / 远程部署，无需关心本地 Python 环境。镜像内已装好 ffmpeg、libsndfile 与中文字体（保证烧字幕中文正常）。

### 方式一：docker compose（推荐）
```bash
docker compose up -d --build
```
启动后访问 `http://<服务器IP>:8765/`。产物（`webui_output`）与曲库（`music_library`）已挂载到宿主机持久化；如需 AI 功能，把你的 `ai_config.json` 放到项目根目录（已挂载进容器）。

### 方式二：docker run
```bash
docker build -t spring-video .
docker run -d -p 8765:8765 -e HOST=0.0.0.0 \
  -v "$PWD/webui_output:/app/webui_output" \
  -v "$PWD/music_library:/app/music_library" \
  -v "$PWD/ai_config.json:/app/ai_config.json" \
  spring-video
```

> 容器内服务绑定 `0.0.0.0`（通过环境变量 `HOST` 控制；本地直接运行 `python webui_server.py` 时默认 `127.0.0.1`，并自动打开浏览器）。首次运行电影解说会联网下载 whisper 模型权重（~140MB），请保证容器可访问外网。

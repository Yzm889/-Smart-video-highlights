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
- 💸 **省流模式**：默认开启，用离线文案模板、跳过付费配音，几乎不花钱
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
├── requirements.txt       # Python 依赖
├── 启动视频工坊.bat        # Windows 一键启动（双击打开浏览器）
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
3. （可选）打开「🤖 AI」配置你的 API Key，或用默认「省流模式」的离线文案
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

## 📄 License

[MIT](LICENSE)

## 🙏 致谢

- 音乐库部分曲目来自 [Incompetech](https://incompetech.com)（Kevin MacLeod，CC.BY 4.0）
- 视频处理基于 ffmpeg，节拍分析基于 [librosa](https://librosa.org)

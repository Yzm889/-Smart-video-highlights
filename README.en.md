# FrameCut · AI-Powered Short Video Maker

A **locally running, AI-powered** video creation tool. Upload images/videos + choose a piece of music, and it automatically generates a **strong beat‑synchronised** short video – every cut lands exactly on the musical downbeats, with optional AI-generated descriptions, Chinese voiceover, and burned‑in subtitles.

> ⚡ All intelligent beat detection, scene analysis, and music alignment are performed **locally** – no API credits consumed.

<p align="center">
  <img src="demo.gif" alt="FrameCut demo" width="400">
</p>

## ✨ Features

- 🎯 **Precise beat‑matching** – analyses scene changes / major motion pauses in your video, matches them to strong musical downbeats, and automatically hard‑cuts with synchronized music (xfade transitions / original audio retention / 0.5–1–2 beat alignment)
- 🎬 **Movie narration pipeline** – scene segmentation → Whisper transcription → **one-shot coherent narration script** (hook / grounded visuals / restrained uplift / deliberate pauses) → TTS → subtitles → mixed output; supports "script‑driven editing" (shorten footage to match narration)
- 🌐 **Online narration** – search the plot by movie title → fully automatic narration (offline fallback, zero cost)
- 💬 **Instruction‑driven creation** – route natural‑language instructions to the right workflow (narration / beat‑cut / composition)
- 🎛 **Human‑in‑the‑loop editing** – plan preview panel (timeline drag / delete cuts / edit narration / insert B‑roll / regenerate single‑segment voice) + 50‑step undo/redo
- 🖼️ **Mixed media support** – combine images (with automatic Ken Burns motion) and videos freely
- 🎵 **Free royalty‑free music library** – built‑in CC.BY commercial music collection, search and use with one click
- 🤖 **AI scripts + subtitles + Chinese voiceover** – supports DeepSeek (image‑to‑text), Xiaomi MiMo / Tongyi (TTS) – configure each separately
- 💸 **Free‑first auto routing** – local models first → cloud only if you configure a key → offline template fallback. **Fully free without any key**
- 🧠 **Local model picker** – writing: `qwen3:14b` ⭐ / `qwen2.5:14b` / `qwen3:8b`; vision: `qwen3-vl:8b` ⭐ / `qwen2.5vl`; click a card in the UI to download & enable; Qwen3 thinking segments auto‑stripped
- ⚡ **CN mirror acceleration** – UI pulls automatically use **hf‑mirror + aria2c multi‑thread** (qwen3:14b‑q4_K_M / qwen3‑vl:8b dual files / qwen2.5 series), GB‑scale in ~2–3 min, with automatic fallback to the official source
- ⚡ **GPU encoding** – auto‑detects `h264_nvenc` (~1.8× faster), falls back to `libx264`; three quality presets
- 🔇 **Local TTS engines** – edge‑tts (key‑free, multi‑voice) / sherpa‑onnx offline models / system SAPI with auto‑fallback and circuit breaker
- ⚡ **Upload warm‑up** – ASR+VLM pre‑analysis runs in idle time right after upload; narration hits the cache instantly
- 🎙 **Concurrent TTS** – 3‑way parallel edge voices (resume / retry / circuit breaker)
- 🧮 **GPU resource isolation** – global mutex for local inference (ASR/LLM/VLM share one slot; remote APIs run in parallel)
- 🧠 **Merged LLM rounds + light model** – alignment+refinement in one call; genre/outline/story tasks use a lighter 8B model (script writing keeps the main model)
- 🗂 **Local material library + Bilibili integration** – persistent `material_library/`; search → select → download MP4 from Bilibili → one‑click library entry
- 🖼 **Auto cover generation** – smart frame picking (contrast + edge energy) + large‑title cover compositing, three layouts
- 🧹 **Auto disk cleanup** – run dirs / warm‑up dirs / upload leftovers recycled on a 6‑hour sweep; analysis cache capped at 200 entries (fingerprint+params+version keyed)
- 🕘 Generation history, cancel‑able synthesis (immediate ffmpeg kill + cooperative flags), vertical formats, multiple transitions
- 🧪 **Regression assurance** – **308 tests / 0 failures** + pyflakes zero‑warning gate + GitHub Actions CI (Python 3.9/3.11 matrix)

## 📁 Project Structure

```
.
├── webui_server.py        # Backend entry: HTTP service + route tables + background scheduler
├── handler.py             # HTTP endpoint handlers (table‑driven routing)
├── workflows.py           # Task workflows & persisted queue
├── movie_narrator.py      # Movie narration: scripting/alignment/story/VLM analysis (resumable)
├── video_render.py        # Rendering: cutting/transitions/mixing/subtitles/single‑pass encode
├── beat_analysis.py       # Beat‑matching: scene cuts/beat analysis/frame sampling
├── ffmpeg_utils.py        # ffmpeg wrapper (timeout / cooperative cancel / probe / proc cleanup)
├── cache_utils.py         # Analysis cache (fingerprint+params+version, 200‑entry cap)
├── ai_providers.py        # AI access: local Ollama / cloud providers / hf‑mirror fast pull
├── tts_engines.py         # TTS engines: edge‑tts / sherpa‑onnx / SAPI / cloud / concurrency
├── text_utils.py          # Text utilities (narration cleaning / splitting / alignment)
├── bili_downloader.py     # Bilibili material: search/download
├── static/                # Frontend (index.html / style.css / app.js)
├── tests/                 # Regression tests (308 cases, pytest)
├── .github/workflows/     # CI: pyflakes gate + pytest matrix
├── ai_config.example.json # AI config template (copy to ai_config.json)
├── requirements.txt       # Python runtime dependencies
├── requirements-dev.txt   # Dev dependencies (pytest + pyflakes)
├── Dockerfile             # Container image (ffmpeg / CJK fonts / HEALTHCHECK)
├── docker-compose.yml     # One‑click deployment with persisted volumes
├── material_library/      # Local material library (.gitignore‑excluded)
├── music_library/         # Music library (.gitignore‑excluded)
├── models/                # Whisper weight cache (.gitignore‑excluded)
├── webui_output/          # Outputs & intermediates (.gitignore‑excluded)
├── webui_workspace/       # Runtime data such as analysis cache (.gitignore‑excluded)
├── 启动视频工坊.bat        # Windows one‑click launcher (double‑click to open browser)
├── _restart_server.py     # Restart launcher (no browser popup)
├── start.sh               # macOS/Linux startup script
└── img1~4.png / demo.gif  # Built‑in sample images & demo
```

text

## 🚀 Quick Start

### Requirements
- Python 3.9+
- No separate ffmpeg installation required – `imageio-ffmpeg` is bundled

### Installation
```bash
git clone https://github.com/Yzm889/-Smart-video-highlights.git
cd -Smart-video-highlights
pip install -r requirements.txt
Launch
Windows: Double‑click 启动视频工坊.bat (automatically opens your browser).

macOS / Linux:

bash
chmod +x start.sh
./start.sh
Or use the generic method:

bash
python webui_server.py
Then open http://127.0.0.1:8765/ in your browser.

Usage
Drag in images / videos

(Optional) Choose a music track – search the built‑in "🎵 Free Music Library" and click "Use", or upload your own local MP3

(Optional) Open the "🤖 AI" section to configure your API keys, or stick with the default "Economy mode" offline scripts

Click "🎬 Start Synthesis" or "⚡ One‑click Beat‑matching"

🤖 AI Configuration (Optional – works without it)
Copy ai_config.example.json to ai_config.json and fill in your keys (you can mix different providers for vision and TTS):

json
{
  "vision": { "base_url": "https://api.deepseek.com/v1", "api_key": "your-key", "model": "deepseek-v4-flash-vision-exp" },
  "tts":    { "provider": "mimo", "base_url": "https://api.xiaomimimo.com/v1", "api_key": "your-key", "model": "mimo-v2.5-tts", "voice": "Mia" }
}
⚠️ ai_config.json is already in .gitignore – never commit it to the repository.

🤝 How to Contribute
Fork this repository

Create a feature branch: git checkout -b feature/xxx

Commit your changes: git commit -am 'feat: xxx'

Push to your branch: git push origin feature/xxx

Open a Pull Request

Suggested improvements:

Smarter beat‑matching algorithms (bar‑level alignment, downbeat weighting)

More transition effects / subtitle styles / intro/outro templates

Support for additional TTS / vision model providers

UI polish and enhancements

📄 License
MIT

🙏 Acknowledgements
Some music tracks provided by Incompetech (Kevin MacLeod, CC.BY 4.0)

Video processing powered by ffmpeg, beat analysis by librosa


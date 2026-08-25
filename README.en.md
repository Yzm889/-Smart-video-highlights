# FrameCut · AI-Powered Short Video Maker

A **locally running, AI-powered** video creation tool. Upload images/videos + choose a piece of music, and it automatically generates a **strong beat‑synchronised** short video – every cut lands exactly on the musical downbeats, with optional AI-generated descriptions, Chinese voiceover, and burned‑in subtitles.

> ⚡ All intelligent beat detection, scene analysis, and music alignment are performed **locally** – no API credits consumed.

<p align="center">
  <img src="demo.gif" alt="FrameCut demo" width="400">
</p>

## ✨ Features

- 🎯 **Precise beat‑matching** – analyses scene changes / major motion pauses in your video, matches them to strong musical downbeats, and automatically hard‑cuts with synchronized music
- 🖼️ **Mixed media support** – combine images (with automatic Ken Burns motion) and videos freely
- 🎵 **Free royalty‑free music library** – built‑in CC.BY commercial music collection, search and use with one click
- 🎵 **Tempo‑aligned cuts** – adjustable cut intervals (0.5 / 1 / 2 beats) with hard cuts or cross‑dissolve transitions
- 🤖 **AI scripts + subtitles + Chinese voiceover** – supports DeepSeek (image‑to‑text), Xiaomi MiMo / Tongyi (TTS) – configure each separately
- 💸 **Economy mode** – enabled by default; uses offline script templates and skips paid voiceovers, costing almost nothing
- 🕘 Recent generation history, cancel‑able synthesis, vertical publishing formats, and multiple transitions

## 📁 Project Structure
.
├── webui_server.py # Backend: local HTTP service + synthesis/beat engine + AI integrations
├── static/
│ ├── index.html # Frontend page
│ ├── style.css # Styling
│ └── app.js # Frontend logic
├── ai_config.example.json # AI config template (copy to ai_config.json to use)
├── requirements.txt # Python dependencies
├── 启动视频工坊.bat # Windows one‑click launcher (double‑click to open browser)
├── start.sh # macOS/Linux startup script
└── img1~4.png # Built‑in spring scenery example images

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


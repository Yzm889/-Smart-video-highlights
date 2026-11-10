# FrameCut · 一帧成片

[中文文档](README.md) · v1.4.29

A local-first AI movie-commentary, live-clip and short-video editor for Windows. Give it a film and a plot summary (or your own script), and it writes the narration, voices every line, picks shots by content, cuts only after each line finishes, and renders the final video.

All AI runs locally: no API bills, no uploads, works offline.

> This is a condensed English overview. The [Chinese README](README.md) is the authoritative, always up-to-date document.

---

## Features

**Movie commentary**

- **Plot-driven editing** — writes the full script first, then allocates footage per sentence; unused shots are cut away, so the result is shorter than the source
- **Bring your own script** — the tool only finds shots, voices lines and burns subtitles; your text is never rewritten
- **No truncation** — every shot is extended to fit its real voice-over, plus a configurable 0.5 s tail
- **Quoted-dialogue takeover** — a line quoting dialogue (`He replied: "I don't want to make friends."`) is replaced by the original shot, original audio and the actual subtitle
- **Silence markers** — a line containing only `（留白）` is not voiced or subtitled; the original audio plays at full volume
- **Two-step workflow** — generate all voice-over first, preview it, then compose; regenerate single lines without redoing the film
- **Timeline editor** — drag clips, trim in/out points, delete, insert B-roll, adjust volumes, undo/redo

**Voice-over**

| Engine | Notes |
|---|---|
| **IndexTTS2** (default) | Zero-shot cloning from one fixed reference clip — one voice for the whole film; 8-D emotion control |
| **Qwen3-TTS · VoiceDesign** | Designs a voice from text; used to mint the reference clip, and as a fallback |
| Legacy (optional) | edge-tts / sherpa-onnx / SAPI / CosyVoice / ChatTTS |

Speech rate is controlled by `tts_local.rate` (default 1.12×, pitch-preserving via ffmpeg `atempo`).

**Live clipping**

Turn a stream recording (OBS capture / platform VOD) into ready-to-post short clips. Two routes: fully automatic, or **review-and-adjust** (delete segments, re-trim in/out points, add your own) before export. Highlights are scored from three relative signals — loudness, motion, speech density + hype keywords — so a quiet or a loud-only stream both work, and every clip ships with a human-readable reason. Vertical 9:16 output (blurred-background pad or center crop), auto covers trimmed to 9:16, fixed dialogue language. A 90-minute recording finishes in ~4 minutes with +6 MB RAM.

**Also included**: beat-synced cutting, local material library, Bilibili search/download, auto cover generation, NVENC encoding, a VRAM guard that swaps models in and out (and silences the inference probe while a task runs), and searchable render history.

---

## Desktop build

**[⬇️ Download for Windows x64](https://github.com/Yzm889/-Smart-video-highlights/releases/latest)**

Unzip and run `FrameCut.exe`. No Python required. The toolbar has three entries — **Narrate / Live clip / Environment check**; when the environment is ready the app opens straight into the narration page. Upgrading from an older build? The first-launch banner imports your config, renders, history and models from the old folder.

---

## Run from source

Requires Python 3.9+ (CI covers 3.9 / 3.11; packaging uses 3.11). ffmpeg ships with `imageio-ffmpeg`.

```bash
git clone https://github.com/Yzm889/-Smart-video-highlights.git
cd -Smart-video-highlights
pip install -r requirements.txt
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

python webui_server.py     # then open http://127.0.0.1:8765/
```

On macOS/Linux use `./start.sh`; on Windows double-click `启动视频工坊.bat`.

---

## Configuration

Settings live in `ai_config.json` (git-ignored). See `ai_config.example.json`.

```json
{
  "local": { "enabled": true, "base_url": "http://localhost:11434/v1" },
  "vlm":   { "enabled": true, "mode": "ollama",
             "base_url": "http://localhost:11434", "model": "minicpm-v4.5:latest" },
  "tts_local": { "engine": "auto", "rate": "+0%", "instruct": "" }
}
```

- `local` — writing model via Ollama's OpenAI-compatible endpoint (`/v1`)
- `vlm` — vision model via Ollama's **native** endpoint (no `/v1`); the legacy `vision` key is still accepted
- `tts_local.rate` — positive values speak faster (`+20%`); `instruct` describes the voice
- No keys are required for the fully local pipeline

## Models on a 12 GB GPU

`qwen3:8b` (~5.0 GB) for writing, `minicpm-v4.5` (~6.1 GB) or `qwen3-vl:8b` (~5.8 GB) for vision, `faster-whisper` (~0.2 GB) for dialogue, IndexTTS2 (~6.6 GB) for voice. Models are swapped serially — never resident at the same time.

---

## Development

```bash
pip install -r requirements-dev.txt
py -m pytest tests -q        # 1090 passed / 12 skipped

py -m PyInstaller FrameCut.spec --noconfirm   # desktop build → dist/FrameCut/
```

---

## License

[MIT](LICENSE)

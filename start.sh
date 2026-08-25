#!/usr/bin/env bash
# ============================================================
#  一帧成片 FrameCut · 启动脚本 (macOS / Linux)
#  用法：
#    chmod +x start.sh
#    ./start.sh
#  启动后会自动打开浏览器 http://127.0.0.1:8765/
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

# 找 Python（优先 python3，再 python）
PYEXE=""
if command -v python3 >/dev/null 2>&1; then
  PYEXE=python3
elif command -v python >/dev/null 2>&1; then
  PYEXE=python
else
  echo "[错误] 未找到 Python，请先安装 Python 3 并加入 PATH。"
  exit 1
fi

echo "正在启动「一帧成片」FrameCut ..."
echo "若首次运行，会自动安装依赖（Pillow/numpy/imageio-ffmpeg/librosa）。"
echo "启动后浏览器将自动打开 http://127.0.0.1:8765/ ，按 Ctrl+C 停止。"

# 首次自动安装依赖
if ! "$PYEXE" -c "import PIL, numpy, imageio_ffmpeg, librosa" >/dev/null 2>&1; then
  echo "首次运行：安装依赖 ..."
  "$PYEXE" -m pip install --disable-pip-version-check --no-input -r requirements.txt
fi

exec "$PYEXE" webui_server.py

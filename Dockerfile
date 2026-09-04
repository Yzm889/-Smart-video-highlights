# 一帧成片 FrameCut · 容器镜像
# 用法：
#   docker build -t spring-video .
#   docker run -p 8765:8765 -e HOST=0.0.0.0 -v "$PWD/webui_output:/app/webui_output" -v "$PWD/music_library:/app/music_library" spring-video
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8765 \
    HOST=0.0.0.0

WORKDIR /app

# 系统依赖：ffmpeg（librosa / 部分音频处理）、libsndfile（soundfile）、
# 中文字体（保证烧字幕中文不乱码）。noto-cjk 较大但一次性。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8765

# 健康检查：/api/tasks 只读且零磁盘开销，容器不健康时便于编排层自动重启
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,sys,urllib.request; p=int(os.environ.get('PORT', 8765)); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%d/api/tasks' % p, timeout=4).status == 200 else 1)"

CMD ["python", "webui_server.py"]

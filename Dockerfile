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

CMD ["python", "webui_server.py"]

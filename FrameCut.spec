# -*- mode: python ; coding: utf-8 -*-
# 一帧成片 · Windows 桌面版打包配置（PyInstaller onedir）
# 构建: pyinstaller FrameCut.spec --noconfirm
# 产物: dist/FrameCut/FrameCut.exe（整个目录即绿色版，可 zip 分发）

from PyInstaller.utils.hooks import collect_data_files

# 【v1.4.8 修的真缺陷】faster_whisper 1.2.x 的 VAD 模型是**数据文件**
# （faster_whisper/assets/silero_vad_v6.onnx），不在模块图里，少了它运行时直接：
#   NoSuchFile: [ONNXRuntimeError] : 3 : NO_SUCHFILE :
#   Load model from ...\_internal\faster_whisper\assets\silero_vad_v6.onnx failed
# 后果：**打包版里 Whisper 转写全废**，只以 warning 形式提示「ASR 转写失败」，
# 有字幕的用户完全无感（字幕优先于 ASR），没字幕的用户等于没有台词轨。
# 之前只 datas=[('static','static')]，现补上 faster_whisper 的全部数据文件。
_whisper_datas = collect_data_files('faster_whisper')

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    # 静态前端资源打进包内（服务按 HERE/static 寻址；frozen 下 HERE=包目录）
    datas=[('static', 'static')] + _whisper_datas,
    hiddenimports=[
        # 重依赖的次级导入，模块图常跟不全（缺一个运行时就是 err/弹窗）
        'faster_whisper',
        'ctranslate2',
        'tokenizers',
        'sherpa_onnx',
        'soundfile',
        'imageio_ffmpeg',
        'imageio_ffmpeg.binaries',
        'huggingface_hub',
        'psutil',
        'PIL',
        'PIL.ImageDraw',
        'PIL.ImageFont',
        'PIL.ImageFilter',
        # 桌面窗口（pywebview，Windows 后端走 WebView2/CLR）
        'webview',
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        'clr',
        'clr_loader',
        # 本地引擎模块（webui_server 直接 import，正常会收；显式列出防漏）
        'app_version',
        'ai_providers',
        'movie_narrator',
        'video_render',
        'workflows',
        'handler',
        'beat_analysis',
        'analyze_pipeline',
        'tts_engines',
        'text_utils',
        'ffmpeg_utils',
        'cache_utils',
        'bili_downloader',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 主环境没有也不需要：torch 系走各自 venv 子进程（CosyVoice/ChatTTS）
        'torch', 'torchaudio', 'torchvision',
        'tkinter', 'matplotlib', 'scipy', 'pandas',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FrameCut',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX 会误伤 ctranslate2/ctrust DLL，禁用
    console=True,        # v1 保留控制台便于排障；稳定后再改 False
    icon='assets/icon.ico' if __import__('os').path.exists('assets/icon.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='FrameCut',
)

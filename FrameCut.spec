# -*- mode: python ; coding: utf-8 -*-
# 一帧成片 · Windows 桌面版打包配置（PyInstaller onedir）
# 构建: pyinstaller FrameCut.spec --noconfirm
# 产物: dist/FrameCut/FrameCut.exe（整个目录即绿色版，可 zip 分发）

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    # 静态前端资源打进包内（服务按 HERE/static 寻址；frozen 下 HERE=包目录）
    datas=[('static', 'static')],
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
        # 本地引擎模块（webui_server 直接 import，正常会收；显式列出防漏）
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

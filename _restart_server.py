# -*- coding: utf-8 -*-
"""重启启动器：以 open_browser=False 启动 webui_server（不弹浏览器），确保加载磁盘最新代码。"""
import os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
import webui_server as ws
ws.ensure_deps()
ws.start_server(port=int(os.environ.get('PORT', '8765')), open_browser=False)

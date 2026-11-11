# -*- coding: utf-8 -*-
"""桌面桥（window.pywebview.api）必须真的挂上窗口。

背景（2026-11-11 主人实报）：左下角状态栏**一直**显示「● 桥连接中…」。
`desktop.js` 的判据是「window.pywebview 存在但 get_desktop_info 没成功」，
而 `desktop.py` 从 1c2ed6b 引入原生选片那天起，只定义了 `DesktopApi` 类、
`create_window()` 从未传 `js_api` —— 整个类是死代码。后果不只是文案：
原生选片失效（选 20 分钟片退化成上传 5GB）、打开文件夹失效、_TOKEN 拿不到。

本文件钉住「类必须被实例化并挂到窗口」以及「前端调用的每个方法都要存在」，
防止以后改 create_window 时又把参数弄丢。
"""
import io
import os
import re

import desktop as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, 'desktop.py')
JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')


def _src(path):
    return io.open(path, encoding='utf-8').read()


def test_create_window_actually_passes_js_api():
    """只 import 类不算数：create_window 必须写 js_api=DesktopApi()。"""
    src = _src(PY)
    i = src.index('webview.create_window(')
    chunk = src[i:i + 500]
    assert 'js_api=DesktopApi()' in chunk, (
        '桌面桥没挂到窗口：window.pywebview.api 将为空，'
        '状态栏恒显示「桥连接中…」、原生选片全部失效')
    # 同时确认不是只写在注释里：注释里那句不带括号调用形态
    assert re.search(r'js_api\s*=\s*DesktopApi\(\)', chunk)


def test_desktop_api_implements_every_frontend_call():
    """前端 window.pywebview.api.X 的每个 X，DesktopApi 都必须有可调用方法。"""
    names = set(re.findall(r'pywebview\.api\.([A-Za-z0-9_]+)', _src(JS)))
    assert names, '没抓到前端桥调用，探针失效'
    missing = [n for n in sorted(names) if not callable(getattr(D.DesktopApi, n, None))]
    assert not missing, '前端调用了后端没有的桥方法：%s' % missing


def test_get_desktop_info_returns_usable_token():
    """前端 initDesktop 的判据是 info.token，拿不到就永远停在「桥连接中…」。"""
    info = D.DesktopApi().get_desktop_info()
    assert info and info.get('token') == D._TOKEN
    assert info.get('version'), '桌面信息应带版本，供状态栏显示'

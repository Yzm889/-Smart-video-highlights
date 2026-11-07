# -*- coding: utf-8 -*-
"""desktop.py 惰性导入回归钉（v1.4.7 真缺陷）。

背景：`DesktopApi.open_folder` 用了 `webui_server.OUTDIR` 但函数内没导入
`webui_server`（`main()` 里的 `import webui_server as S` 是 main 的局部名，
不是模块全局），每次调用都抛
    NameError: name 'webui_server' is not defined
而前端 `open_folder(...).catch(() => {})` 把它吞了 —— 主人看到的就是
「点『打开文件夹』没反应」。`_launch_app_window` 同样漏导 `subprocess`，
NameError 被 except 吃掉，Edge --app 独立窗口永远不生效。

约束：不能把 `webui_server` 提到模块顶层（它一 import 就初始化全部引擎，
会拖慢冷启动、也会让 --selftest 跑起服务）。所以必须「函数内惰性导入」。
"""

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import desktop  # noqa: E402


def _fn_src(fn):
    src = inspect.getsource(fn)
    return '\n'.join(line.split('#')[0] for line in src.splitlines())


def test_open_folder_imports_webui_server_before_use():
    src = _fn_src(desktop.DesktopApi.open_folder)
    assert 'import webui_server' in src, 'open_folder 又漏导 webui_server（会 NameError）'
    assert src.index('import webui_server') < src.index('webui_server.OUTDIR'), \
        '导入必须在首次使用之前'


def test_launch_app_window_imports_subprocess():
    src = _fn_src(desktop._launch_app_window)
    assert 'import subprocess' in src, '_launch_app_window 又漏导 subprocess'
    assert src.index('import subprocess') < src.index('subprocess.Popen')


def test_webui_server_still_lazy_at_module_level():
    """webui_server 必须保持惰性导入，不许提到模块顶层（冷启动 / selftest 依赖这点）。"""
    path = os.path.join(ROOT, 'desktop.py')
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    for i, line in enumerate(lines, 1):
        if line.startswith('import ') or line.startswith('from '):
            assert not line.startswith('import webui_server'), \
                'desktop.py 第 %d 行把 webui_server 提到了模块顶层' % i

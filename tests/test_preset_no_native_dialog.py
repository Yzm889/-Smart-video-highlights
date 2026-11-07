# -*- coding: utf-8 -*-
"""参数预设不得使用 pywebview 原生 JS 对话框（v1.4.1 事故回归钉）。

AI_CHANGELOG v1.4.1 记录：EXE 里 pywebview 原生对话框被压到窗口后 / 静默失败，
主人只看到「点了没反应」。当时专门做了拖拽导入字幕来绕开。
参数预设的「命名保存」「删除确认」必须走页面内 .modal-mask 模态。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')
HTML = os.path.join(ROOT, 'static', 'desktop', 'index.html')


def _load(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def _strip_js_comments(js):
    js = re.sub(r'/\*[\s\S]*?\*/', '', js)
    js = re.sub(r'(?m)^\s*//.*$', '', js)
    return js


def test_preset_path_has_no_native_dialog():
    """预设代码块内不得出现 window.prompt / window.confirm / window.alert。"""
    src = _load(JS)
    start = src.index('const PRESET_KEY')
    end = src.index('/* ── 查剧情')
    block = _strip_js_comments(src[start:end])
    for native in ('window.prompt', 'window.confirm', 'window.alert',
                   'prompt(', 'confirm(', 'alert('):
        assert native not in block, '预设路径又用了原生对话框 %s：会静默失败' % native


def test_page_modal_elements_exist():
    html = _load(HTML)
    for i in ('presetMask', 'presetModalTitle', 'presetModalBody',
              'presetNameInput', 'presetModalCancel', 'presetModalOk'):
        assert ('id="%s"' % i) in html, '缺模态元素 %s' % i


def test_modal_is_escapable_and_cancellable():
    js = _strip_js_comments(_load(JS))
    assert 'presetModalClose(null)' in js, '取消/Esc 必须能关闭并 resolve(null)'
    assert "$('presetMask')" in js and 'presetModal' in js


def test_cache_version_bumped():
    """前端改了就必须 bump 缓存版本，否则 EXE 里仍加载旧 JS。"""
    html = _load(HTML)
    vers = re.findall(r'desktop\.(?:js|css)\?v=(\d+)', html)
    assert len(vers) == 2, 'js/css 都应带版本号：%r' % vers
    assert vers[0] == vers[1] == '3', '版本未 bump 到 3：%r' % vers

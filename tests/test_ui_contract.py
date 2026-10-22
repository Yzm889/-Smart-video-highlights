# -*- coding: utf-8 -*-
"""静态 UI 契约测试（task #24）：无浏览器依赖地拦住两类最常见的前端断裂。

本项目前端是 7040 行 vanilla JS + 大量 inline onclick，且**从未在浏览器里跑过**
（沙箱无浏览器）。于是两类断裂长期无人发现：
  A. 按钮点了没反应 —— index.html 里的 inline onclick 调用了 app.js 中已改名/已删除的函数；
  B. 元素为 null 报错 —— app.js 里 $('xxx') 引用了 index.html 中不存在的 id。

本文件用静态解析把这两类变成可自动拦截的门禁（纯标准库，零新增依赖）：
  ① index.html 每个 inline 事件属性里调用的函数名，必须在 app.js 中有定义；
  ② app.js 里每个 $('id') / getElementById('id') 引用的 id，必须能在
     index.html 或 app.js 动态模板里找到（动态创建的元素也算数）。
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(ROOT, 'static', 'index.html')
JS_PATH = os.path.join(ROOT, 'static', 'app.js')

# JS 内建/语言关键字：inline 处理器里出现它们不算「调用了 app.js 的函数」
BUILTINS = {
    'alert', 'confirm', 'prompt', 'require', 'return', 'if', 'for', 'while', 'switch',
    'catch', 'function', 'typeof', 'new', 'void', 'delete', 'in', 'of', 'do', 'else',
    'try', 'finally', 'throw', 'instanceof', 'setTimeout', 'setInterval', 'clearTimeout',
    'clearInterval', 'requestAnimationFrame', 'parseInt', 'parseFloat', 'isNaN',
    'encodeURIComponent', 'decodeURIComponent', 'encodeURI', 'decodeURI', 'fetch',
    'JSON', 'Math', 'Date', 'String', 'Number', 'Boolean', 'Array', 'Object', 'RegExp',
    'Map', 'Set', 'Promise', 'Error', 'TypeError', 'parse', 'stringify', 'this', 'event',
    'arguments', 'undefined', 'null', 'true', 'false', 'eval', 'escape', 'unescape',
}

# inline 事件属性（onclick / onchange / oninput ...）
_EVENT_ATTR = re.compile(r'\son([a-z]+)\s*=\s*"([^"]*)"', re.I)
# 处理器里被调用的标识符：前面不是 . : 也不是字母数字下划线
_CALL = re.compile(r'(?<![.\w$])([A-Za-z_$][A-Za-z0-9_$]*)\s*\(')
# app.js 中函数定义的各种写法
_DEF_PATTERNS = [
    re.compile(r'\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\('),
    re.compile(r'\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?(?:function\b|\()'),
    re.compile(r'\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?(?:function\b|\()'),
    re.compile(r'\bwindow\.([A-Za-z_$][A-Za-z0-9_$]*)\s*='),
    re.compile(r'\basync\s+function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\('),
]
# 元素 id：HTML 属性 或 JS 动态模板里的 id="..."
_ID_HTML = re.compile(r'\bid\s*=\s*["\']([^"\']+)["\']', re.I)
_ID_JS = re.compile(r'''\bid\s*=\s*["']([^"']+)["']''')
_GET_BY_ID = re.compile(r'''\$\(\s*['"]([^'"]+)['"]\s*\)|getElementById\(\s*['"]([^'"]+)['"]\s*\)''')
# 不安全访问：紧跟元素查询取属性（缺元素即 null.value → TypeError）
_UNSAFE_ACCESS = re.compile(
    r'''\$\(\s*['"]([^'"]+)['"]\s*\)\s*\.|getElementById\(\s*['"]([^'"]+)['"]\s*\)\s*\.''')


@pytest.fixture(scope='module')
def src():
    with open(HTML_PATH, encoding='utf-8') as f:
        html = f.read()
    with open(JS_PATH, encoding='utf-8') as f:
        js = f.read()
    return html, js


def _defined_functions(js):
    names = set()
    for pat in _DEF_PATTERNS:
        names.update(pat.findall(js))
    return names


def _known_ids(html, js):
    ids = set(_ID_HTML.findall(html))
    ids.update(_ID_JS.findall(js))
    return ids


def test_inline_handlers_reference_existing_functions(src):
    """A. index.html 的 inline 事件处理器调用的函数必须在 app.js 中存在。"""
    html, js = src
    defined = _defined_functions(js)
    missing = {}
    for attr, body in _EVENT_ATTR.findall(html):
        for name in _CALL.findall(body):
            if name in BUILTINS or name in defined:
                continue
            missing.setdefault(name, []).append('%s="%s"' % (attr, body[:60]))
    assert not missing, '页面 inline 事件引用了 app.js 中不存在的函数（按钮点了没反应）：%s' % missing


def test_dom_ids_referenced_by_js_exist(src):
    """B. app.js 里**未做空值保护**的元素访问，其 id 必须真实存在。

    只判「不安全的直接属性访问」——`$('x').value` / `getElementById('x').textContent`
    这类在元素缺失时会抛 TypeError 并中断整个函数；而 `($('x')||{}).value`、
    `const b=$('x'); if(b){...}` 是安全写法，缺失也无妨，不应误报。
    （初版把两者混在一起报，噪音大到会被无视，等于没门禁。）"""
    html, js = src
    known = _known_ids(html, js)
    missing = {}
    for m1, m2 in _UNSAFE_ACCESS.findall(js):
        for i in (m1, m2):
            if i and i not in known:
                missing.setdefault(i, 0)
                missing[i] += 1
    assert not missing, ('app.js 对不存在的元素做了无保护访问（null.value 会抛 TypeError）：%s'
                         % sorted(missing))


def test_key_ui_anchors_present(src):
    """关键交互锚点不能被重构误删（剧情框、一键查剧情按钮、离线模型选择等）。"""
    html, _js = src
    for anchor in ('id="narPlot"', 'id="narDsBtn"', 'id="moviePlot"', 'id="movieDsBtn"',
                   'id="ttsSherpaModel"', 'id="ttsLocalState"', 'id="ttsSetupBar"'):
        assert anchor in html, 'index.html 缺少关键锚点：%s' % anchor


def test_new_endpoints_have_ui_entry(src):
    """本次新增能力必须有 UI 入口：DeepSeek 一键查剧情两个页面都要有按钮。"""
    html, _js = src
    assert 'fetchDeepSeekPlot' in html, '缺少一键查剧情按钮（nar/movie 两处）'
    assert html.count('fetchDeepSeekPlot') >= 2, 'nar 与 movie 两处都应接入一键查剧情'

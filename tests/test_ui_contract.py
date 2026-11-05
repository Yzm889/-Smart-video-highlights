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


def test_js_toggled_classes_defined_in_css():
    """JS 里切换的 class 必须在 style.css 里有定义。

    同类「两边各写一份」风险：改了 CSS 类名忘了改 JS（或反之）→ 样式不生效、
    页面看着「就是不对」，但没有任何报错。
    容忍动态前缀（'toast-' 这种拼在后面的后缀式类名不算缺失）。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'style.css'), encoding='utf-8') as f:
        css = f.read()
    with open(os.path.join(root, 'static', 'app.js'), encoding='utf-8') as f:
        js = f.read()
    defined = set(re.findall(r'\.([a-zA-Z][\w-]*)', css))
    used = set()
    for pat in (r"classList\.(?:add|remove|toggle)\('([^']+)'\)",
                r'classList\.(?:add|remove|toggle)\("([^"]+)"\)',
                r"className\s*=\s*'([^']+)'"):
        for m in re.finditer(pat, js):
            used.update(m.group(1).split())
    missing = []
    for c in used:
        if not c or c in defined:
            continue
        # 动态前缀：toast-xxx 这类，CSS 里有 toast-ok / toast-warn 即可
        if any(d.startswith(c) for d in defined):
            continue
        missing.append(c)
    assert not missing, 'JS 切换的 class 在 CSS 里没有定义（样式不生效）：%s' % sorted(missing)


# ---------------------------------------------------------------------------
# 页面2 左栏工具卡布局（2026-11-05）
#
# 背景：图文成片 / 智能强卡点 / 解说出片 三张卡原来纵向平铺堆在同一页。
# 用户要滚很久才知道有哪些功能，也分不清当前在用哪个。改成左栏三张卡、
# 点谁展谁、同一时刻只显示一个面板。
#
# 这个改法最容易被后续改动毁掉的两个点，正是下面两条测试：
#   ① 面板显隐规则的特异性必须高于 `.card[data-step].active`（0,3,0）。
#      否则 showStep('narrate') 给四张卡全加 .active 后，三张卡又同时铺开——
#      改了一圈回到原点。
#   ② 左栏卡片、面板、JS 里的面板名必须三方一致。少一处就是「点了没反应」。
# ---------------------------------------------------------------------------

def _spec(sel):
    """粗略计算选择器特异性 (id, class+attr+pseudo, element)。"""
    s = re.sub(r':not\(([^)]*)\)', r' \1 ', sel)
    n_id = len(re.findall(r'#[\w-]+', s))
    n_cls = len(re.findall(r'\.[\w-]+', s)) + len(re.findall(r'\[[^\]]+\]', s))
    n_el = sum(1 for t in re.split(r'[\s>+~]+', s)
               if re.fullmatch(r'[a-zA-Z][\w-]*', t))
    return (n_id, n_cls, n_el)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def test_nar_panel_rules_beat_card_active(src):
    """面板显隐规则必须压过 `.card[data-step].active`。

    这是本布局能成立的前提：showStep('narrate') 会无条件给 narCard/movieCard/
    drop/beatcutCard 四张卡加 .active（它们都是 .card[data-step]），
    若面板规则特异性不够，三张卡仍会同时显示。
    """
    css = _read('static/style.css')
    base = '.card[data-step].active'
    assert base in css, '基础显隐规则不在了，请复核本测试前提'

    # 找出面板隐藏/显示两条规则
    hide = '.nar-page-main .card[data-step][data-narpanel]'
    show = '.nar-page-main .card[data-step][data-narpanel].nr-on'
    for sel in (hide, show):
        assert sel in css, '面板规则缺失：%s（改布局时被删了？）' % sel

    assert _spec(hide) > _spec(base), (
        '面板隐藏规则特异性 %s 不高于 %s —— showStep 加了 .active 后三张卡会同时铺开'
        % (_spec(hide), _spec(base)))
    assert _spec(show) > _spec(hide), (
        '选中态特异性 %s 不高于隐藏规则 %s —— 选中了也显示不出来'
        % (_spec(show), _spec(hide)))


def test_nar_rail_panels_are_consistent(src):
    """左栏卡片 data-panel、面板 data-narpanel、JS 允许值三方必须一致。

    少一处就是「点了没反应」：左栏写着 beatcut，面板写 beatcutCard，
    selectNarPanel 一比对不上就静默回落到解说面板——用户以为按钮坏了。
    """
    html, js = src
    rail = set(re.findall(r'class="nar-rail-card[^"]*"[^>]*data-panel="([^"]+)"', html))
    rail |= set(re.findall(r'data-panel="([^"]+)"[^>]*class="nar-rail-card', html))
    assert rail == {'drop', 'beatcut', 'nar'}, '左栏卡片集合不对：%s' % sorted(rail)

    panels = set(re.findall(r'data-narpanel="([^"]+)"', html))
    assert panels == {'drop', 'beatcut', 'nar'}, '面板集合与左栏不匹配：%s' % sorted(panels)

    i = js.find('function selectNarPanel')
    assert i > 0, 'selectNarPanel 不见了（左栏点击会变成无响应）'
    body = js[i:i + 700]
    allowed = re.search(r"\[\s*'nar'\s*,\s*'beatcut'\s*,\s*'drop'\s*\]", body)
    assert allowed, 'selectNarPanel 的允许值列表不见了或被改动，请与 data-panel 同步'


def test_nar_panel_keeps_subtab_mechanism(src):
    """narCard / movieCard 不得挂 data-narpanel。

    它们的显隐由「剪成片 / 只要解说稿」子标签（switchNarrTab 设 inline display）
    独占控制。若也挂 data-narpanel，会被 `.nar-page-main [data-narpanel]{display:none}`
    一并打死 —— 表现就是从别的步骤跳进来时解说卡一片空白。
    """
    html, _js = src
    for cid in ('narCard', 'movieCard'):
        m = re.search(r'<div[^>]*id="%s"[^>]*>' % cid, html)
        assert m, '找不到卡片 %s' % cid
        tag = m.group(0)
        assert 'data-narpanel' not in tag, (
            '%s 挂上了 data-narpanel，子标签与面板两套 display 逻辑会互相覆盖' % cid)
        assert 'narr-tab-content' in tag, '%s 应保留 narr-tab-content 供 switchNarrTab 管理' % cid


# ---------------------------------------------------------------------------
# 局部别名跨作用域误用（2026-11-05）
#
# 真实事故：`readNarSubtitle` 里写了 `esc(file.name)`，但 `esc` 只是
# `renderNarrGuide` 内部的局部别名（`const esc = escapeHtml;`）。
# 结果：字幕导入成功那一刻抛 ReferenceError，顶部红条弹出，
# 后面的「按钮文案改成『换一个字幕』」再也没执行 —— 用户以为导入失败。
#
# 这类 bug 特别难在代码评审里看出来：`esc` 这个名字在文件里确实存在
# （在别的函数里），grep 得到结果，肉眼看过去毫无问题。
# 而 app.js 里同时存在 esc / escapeHtml / _esc 三种转义名，更容易搞混。
#
# 静态检测规则：找出 `const X = Y;`（Y 是纯标识符）形式的别名，
# 若 X 在**定义它的函数之外**被调用，就是跨作用域误用。
# ---------------------------------------------------------------------------

def _func_ranges(js):
    """返回 [(name, start, end)]，用大括号配平算每个 function 的字面量区间。"""
    out = []
    for m in re.finditer(r'\bfunction\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{', js):
        name = m.group(1)
        i = js.index('{', m.start())
        depth = 0
        for j in range(i, len(js)):
            if js[j] == '{':
                depth += 1
            elif js[j] == '}':
                depth -= 1
                if depth == 0:
                    out.append((name, m.start(), j + 1))
                    break
    return out


def test_local_alias_not_used_outside_its_function():
    """局部别名不得在定义它的函数之外被调用（会抛 ReferenceError）。

    事故现场：`readNarSubtitle` 调用了 `renderNarrGuide` 内部的 `esc` 别名。
    报错发生在用户操作路径上（导入字幕 → 顶部红条 → 按钮文案不更新），
    静态阅读完全看不出来，因为 `esc` 这个名字在文件里确实存在。
    """
    with open(JS_PATH, encoding='utf-8') as f:
        js = f.read()

    funcs = _func_ranges(js)
    # 只有「别名指向一个已定义的函数」才纳入检查。这是关键收窄：
    # 初版把 `var s = ...` 这类普通变量也算进去，结果 `'s ('` 这种字符串片段
    # 被当成了函数调用，报了 3 条纯噪音 —— 门槛一旦有噪音就会被无视，等于没门禁。
    defined_funcs = {f[0] for f in funcs}
    # 别名声明：const/let/var X = Y;   （Y 是纯标识符，且 X != Y）
    aliases = []
    for m in re.finditer(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*;', js):
        name, target = m.group(1), m.group(2)
        if name == target or target not in defined_funcs:
            continue
        # 所属函数 = 最内层包住这个声明位置的 function
        owner = None
        for fname, s, e in funcs:
            if s <= m.start() < e and (owner is None or (e - s) < (owner[2] - owner[1])):
                owner = (fname, s, e)
        if owner is None:
            continue           # 顶层别名就是全局的，随便用
        aliases.append((name, owner, m.start()))

    bad = []
    for name, (fname, s, e), _decl in aliases:
        # 该别名在**定义函数之外**被调用的位置
        for m in re.finditer(r'(?<![.\w$])' + re.escape(name) + r'\s*\(', js):
            if not (s <= m.start() < e):
                line = js[:m.start()].count('\n') + 1
                bad.append('%s() 在 %s 之外被调用（%s 只在 %s 内定义为局部别名）第 %d 行'
                           % (name, fname, name, fname, line))

    assert not bad, (
        '发现局部别名跨作用域误用（运行时必抛 ReferenceError，且发生在用户操作路径上）：\n  '
        + '\n  '.join(sorted(set(bad))))

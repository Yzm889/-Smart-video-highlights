# -*- coding: utf-8 -*-
"""桌面版 DOM id 静态对账（已知坑 #3 的自动化守门）。

desktop.js 顶层有大量 `$('id').addEventListener(...)` 直接绑定：只要某个 id
在 index.html 里不存在，就会抛 TypeError，导致**该行之后全部顶层代码静默死亡**——
界面不报错、按钮全没反应，主人只看到「点了没反应」。历史上 v1.3.x 的
btnCompose、v1.4.3 的桥退化都是这个机制。node --check 查不出这种错误。

本用例把 JS 引用的 id 与 HTML 定义的 id 逐个对账；运行时动态生成的元素
（模板字符串里的 id）走白名单。以后谁删了元素或拼错 id，CI 直接红。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(ROOT, 'static', 'desktop')

# 这些 id 由 JS 在运行时用模板字符串动态生成，或已用 && 短路保护，
# 静态 HTML 里本就不该有；改动时请同步更新此处并说明原因。
DYNAMIC_IDS = {
    'inNvV',          # 检查器里动态生成的音量数值 span
    'inOvV',          # 同上
    'inQuoteToggle',  # 检查器里动态生成的「恢复为解说段」按钮
    'musicAudio',     # 曲库试听 audio，代码用 $('musicAudio') && ...pause() 短路保护
    'narr-',          # 前缀拼接的动态 id（$('narr-' + i)）
}


def _load(name):
    with open(os.path.join(DESKTOP, name), encoding='utf-8') as f:
        return f.read()


def _strip_js_comments(js):
    """注释里的示例（如 $('id')）不是真实绑定，先剥掉，避免误报。"""
    js = re.sub(r'/\*[\s\S]*?\*/', '', js)
    js = re.sub(r'(?m)^\s*//.*$', '', js)
    return js


def test_every_referenced_id_exists_or_is_dynamic():
    js = _strip_js_comments(_load('desktop.js'))
    html = _load('index.html')
    referenced = set(re.findall(r"\$\('([^']+)'\)", js))
    defined = set(re.findall(r'\bid="([^"]+)"', html))
    missing = sorted(i for i in referenced - defined if i not in DYNAMIC_IDS)
    assert not missing, (
        'desktop.js 引用了 HTML 中不存在的 id：%s\n'
        '顶层绑定会抛 TypeError 并让后续脚本静默死亡。'
        '若是运行时动态生成的元素，请加入 DYNAMIC_IDS 白名单并注明原因。' % missing)


def test_dynamic_whitelist_is_still_justified():
    """白名单不能变成垃圾桶：每个 id 必须在 JS 里确实有动态生成或短路保护的痕迹。"""
    js = _load('desktop.js')
    for i in ('inNvV', 'inOvV', 'inQuoteToggle'):
        assert ('id="%s"' % i) in js, '%s 已不再是动态生成，请从白名单移除' % i
    assert "$('musicAudio') &&" in js, 'musicAudio 的短路保护被删了，必须改成真实元素或恢复保护'


def test_html_holds_expected_minimum_elements():
    """index.html 被误删成大半个文件时，别等到真机才发现。"""
    html = _load('index.html')
    defined = set(re.findall(r'\bid="([^"]+)"', html))
    assert len(defined) >= 150, 'HTML 定义元素数骤降到 %d，可能被误删' % len(defined)

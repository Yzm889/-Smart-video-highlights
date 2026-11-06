# -*- coding: utf-8 -*-
"""回归：第一页只留新手引导（2026-11-07 主人指令）。

一键生成/指令成片与第二步重复、有迷惑性——第一页只承载「装机引导」，
用户下完模型直接进第二步（写剧情·选视频）。
"""
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
HTML_PATH = os.path.join(ROOT, 'static', 'index.html')
JS_PATH = os.path.join(ROOT, 'static', 'app.js')


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def test_start_page_has_only_guide():
    html = _read(HTML_PATH)
    # 两个入口 UI 整体移除
    for gone in ('id="smartCard"', 'id="instructCard"', 'id="smartDrop"', 'id="smartGo"',
                 'id="instructInput"', 'id="instructGo"', '⚡ 一键生成', '💬 指令成片'):
        assert gone not in html, '第一页残留已删入口：%s' % gone
    # 子标签只剩新手引导且为激活态
    import re
    m = re.search(r'<div id="startSubTabs".*?</div>', html, re.S)
    assert m and 'data-subtab="guide"' in m.group(0), '缺新手引导子标签'
    assert 'start-subtab active' in m.group(0), '新手引导子标签必须是激活态'
    # 引导卡默认可见（新用户无 JS 也能看到装机引导）
    g = re.search(r'<div[^>]*id="guideCard"[^>]*>', html)
    assert g and 'display:none' not in g.group(0), 'guideCard 不能内联隐藏——新用户会看到空白页'


def test_js_routes_start_to_guide():
    js = _read(JS_PATH)
    # STEP_CARDS / 回退值 / 流程映射全部指向 guideCard
    assert "start: ['guideCard']" in js, 'STEP_CARDS.start 未指向 guideCard'
    assert "switchStartTab(activeTab ? activeTab.dataset.subtab : 'guide')" in js, '子标签回退值仍是 smart'
    assert "card:'guideCard'" in js, 'FLOW_STEPS 第一步仍指向 smartCard'
    # 引导第 4 步指向第二步（老文案引用已删除的入口名）
    html = _read(HTML_PATH)
    assert '左栏选「🎬 解说出片」' in html and '2 写剧情·选视频' in html, '引导第 4 步未指向第二步'
    assert '短片解说 / 🌐 联网解说' not in html, '引导仍引用已废弃的入口名'
    # 死代码不再引用已删元素（无保护访问会被 test_ui_contract 抓，这里防回归）
    for fn in ('function smartGenerate', 'function runInstruct', 'function handleSmartVideo',
               'function pollInstruct', 'function pollSmart', 'function detectSmartConfig'):
        assert fn not in js, '死代码未清理：%s' % fn

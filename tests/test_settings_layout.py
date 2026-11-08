# -*- coding: utf-8 -*-
"""v1.4.15 设置页改版回归钉。

原状：620px 模态里 6 组卡片平铺、20+ 控件无主次，配音（最近的主线）排第 5，
没有任何引导文字，用户一打开就被一堆输入框淹没。
改版：顶部三步引导 + 三页分区（配音 / 模型 / 高级），默认落在配音页；
状态与安装动作前置，次要的音色/语速收进 <details>。

这里钉四件事：
  1. 原有 DOM id 一个不丢（desktop.js 顶层绑定 / 环境卡「去设置」跳转依赖）
  2. 三页齐全、默认只显示配音页
  3. 引导与折叠结构在位，CSS 有对应样式
  4. 环境卡跳转会顺手切到对应页（否则 scrollIntoView 会滚向隐藏元素）
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(ROOT, 'static', 'desktop')

CORE_IDS = (
    'settingsMask', 'settingsClose', 'settingsSave',
    'secModelCenter', 'secVoice', 'secVlm',
    'ollamaState', 'btnOllamaSetup', 'setLocalPick', 'btnPullLocal',
    'localPullWrap', 'localPullFill', 'localPullMeta',
    'setVlmPick', 'btnPullVlm', 'vlmPullWrap', 'vlmPullFill', 'vlmPullMeta',
    'mcHint', 'hwHint', 'btnHwApply',
    'setVisBase', 'setVisKey', 'setVisModel', 'setLocalBase', 'setVlmBase',
    'setTtsEngine', 'btnTtsTest', 'btnCloneVoice', 'btnTtsReset',
    'setTtsVoice', 'setCosyVoice', 'setTtsRate',
    'ttsTestHint', 'ttsStateHint', 'ttsInstalls',
    'setWhisperModel', 'btnWhisperDl',
    'whisperPullWrap', 'whisperPullFill', 'whisperPullMeta', 'whisperDlStatus',
)


def _load(name):
    with open(os.path.join(DESKTOP, name), encoding='utf-8') as f:
        return f.read()


def _pane_tag(html, name):
    i = html.index('data-set-pane="%s"' % name)
    return html[html.rfind('<div', 0, i):html.index('>', i)]


def test_all_original_ids_kept():
    ids = set(re.findall(r'\bid="([^"]+)"', _load('index.html')))
    missing = sorted(set(CORE_IDS) - ids)
    assert not missing, '重排丢了 DOM id（顶层绑定会静默死亡）：%s' % missing


def test_three_panes_and_voice_is_default():
    html = _load('index.html')
    for name in ('voice', 'models', 'advanced'):
        assert 'data-set-pane="%s"' % name in html, '缺分区 %s' % name
        assert 'data-set-tab="%s"' % name in html, '缺导航 %s' % name
    assert 'display:none' not in _pane_tag(html, 'voice'), '配音页必须默认可见'
    for name in ('models', 'advanced'):
        assert 'display:none' in _pane_tag(html, name), '%s 页初始必须隐藏' % name


def test_intro_and_numbered_steps():
    html = _load('index.html')
    assert 'class="set-intro"' in html and '配置只需一次' in html
    assert html.count('class="set-num"') == 3, '配音页应有 1/2/3 三步编号'
    assert 'class="set-tag"' in html


def test_secondary_controls_collapsed():
    html = _load('index.html')
    m = re.search(r'<details class="set-more">(.*?)</details>', html, re.S)
    assert m, '次要项应折叠进 details'
    for i in ('setTtsVoice', 'setCosyVoice', 'setTtsRate'):
        assert 'id="%s"' % i in m.group(1), '%s 应收进折叠区' % i


def test_env_card_jump_switches_pane():
    js = _load('desktop.js')
    assert 'function showSettingsPane(' in js, '缺分区切换函数'
    assert "sec === 'secVoice'" in js and "showSettingsPane('voice')" in js
    assert "sec === 'secModelCenter'" in js and "showSettingsPane('models')" in js
    assert 'initSettingsTabs' in js, 'tab 按钮必须真的绑定点击事件'


def test_css_rules_exist():
    css = _load('desktop.css')
    for token in ('.set-intro', '.set-tab.active', '.set-pane', '.set-num',
                  '.set-more', '.set-save-hint', '.set-actions'):
        assert token in css, 'CSS 缺 %s' % token
    assert '.modal.settings{width:720px' in css

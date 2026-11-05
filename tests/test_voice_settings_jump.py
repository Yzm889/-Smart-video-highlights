# -*- coding: utf-8 -*-
"""回归：配音设置的跳转必须直达「🔊 本地配音引擎」区。

历史 bug（2026-11-05 主人反馈「看不到下载/配置配音模型的入口」的根因）：
解说卡 narVoiceInfo 的链接传的是 'local'——只展开「本地离线模型」折叠区并停在其
顶部（本地大脑/Whisper/VLM 一大串配置之上），而 jumpToAISection 专门为 'tts' 实现的
「滚动到引擎选择器」分支从未被触发。配音区在长页面最底部，用户自然找不到。
"""
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
JS_PATH = os.path.join(ROOT, 'static', 'app.js')
HTML_PATH = os.path.join(ROOT, 'static', 'index.html')


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def test_narrate_voice_link_targets_tts_section():
    """解说卡「换配音引擎/音色」必须跳 'tts'（直达引擎选择器），不能只跳 'local'。"""
    src = _read(JS_PATH)
    anchor = "const nv = $('narVoiceInfo')"
    i = src.index(anchor)
    window = src[i:i + 1200].replace("\\'", "'")
    assert "jumpToAISection('tts')" in window, \
        '解说卡配音链接的跳转目标必须是 tts：直达引擎选择器/模型下载，而不是停在本地模型折叠区顶部'


def test_adjust_page_has_voice_settings_link():
    """⑥ 手动调整页（逐段试听配音处）必须有直达配音设置的入口。"""
    html = _read(HTML_PATH)
    assert "onclick=\"jumpToAISection('tts')\"" in html, \
        '手动调整页缺「配音设置」直达链接：主人在这页反复试听配音，改声音不该让他翻遍 AI 配置页'

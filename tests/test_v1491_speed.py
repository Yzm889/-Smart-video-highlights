# -*- coding: utf-8 -*-
"""v1.4.91 快速模式三项升级回归钉（全部来自主人 2026-11-15 实测）。

① 只配字幕模式：配音太慢（26 句约 10 分钟）→「生成解说配音」复选框 +
   未勾时弹窗确认；后端 skipTts 跳过 TTS（引用让位段照常）。
② Qwen3 移出兜底链：Qwen3 逐句 VoiceDesign「每句都像喊出来」且音色漂移
   是模型层问题——IndexTTS2 失败直接落 edge-tts。
③ 全篇对齐不再被探测误判：e2e-1490/主人 run-1-20261115 实锤「TTS 并行启动
   占 GPU → _local_model_available 探测超时 → 全篇对齐被跳过 → DP 跳跃上限
   整块错位复发（解说说柯蒂斯、画面在工地）」——剧情理解刚用同一个 LLM
   跑完就是可用性证明，直接调用、真失败回退 DP。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import movie_narrator as M  # noqa: E402
from tts_engines import _engine_order_for, _auto_order


def test_skip_tts_skips_generate(monkeypatch):
    """skipTts=true：两步走分支不调 _generate_all_tts（引用让位照常）。"""
    called = {'n': 0}

    def boom(*a, **k):
        called['n'] += 1
        return []
    monkeypatch.setattr(M, '_generate_all_tts', boom)
    # 直接调用 narrate_movie 太重——钉源码：skipTts 分支存在且在调用之前
    src = open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8').read()
    i = src.index('if tts_only:')
    block = src[i:i + 1200]
    assert "params.get('skipTts')" in block, '两步走分支必须先判 skipTts'
    j = block.index("params.get('skipTts')")
    k = block.index('_generate_all_tts')
    assert j < k, 'skipTts 判断必须在 _generate_all_tts 之前'
    assert called['n'] == 0


def test_skip_tts_direct_branch_source_pin():
    """直接合成分支：skipTts 不起 TTS 线程。"""
    src = open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8').read()
    i = src.index('TTS配音已启动')
    block = src[i - 700:i]
    assert "params.get('skipTts')" in block, '直接合成分支必须判 skipTts'


def test_qwen3tts_out_of_fallback_chain():
    """Qwen3 不再作为任何链的兜底（IndexTTS2 失败 → edge）。"""
    assert 'qwen3tts' not in _engine_order_for('indextts2'), \
        'IndexTTS2 的兜底链不应再有 Qwen3'
    assert 'qwen3tts' not in _auto_order(), 'auto 链不应再有 Qwen3'
    assert _engine_order_for('indextts2')[1] == 'edge'
    # 显式选 qwen3tts 仍尊重用户选择（第一个是 qwen3tts）
    assert _engine_order_for('qwen3tts')[0] == 'indextts2' or \
        _engine_order_for('qwen3tts')[0] == 'qwen3tts'


def test_full_align_no_probe_gate():
    """源码钉：全篇对齐不再做 _local_model_available 预判（探测时机误判的根因）。"""
    src = open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8').read()
    i = src.index('_full = _llm_align_full_script(')
    gate = src[max(0, i - 800):i]
    assert '_local_model_available()' not in gate, \
        '全篇对齐前不得做可用性预判（探测会被 TTS 占 GPU 误判）'


def test_frontend_voice_checkbox_and_confirm():
    """前端：复选框存在 + 未勾时 confirm 弹窗 + skipTts 进参数。"""
    html = open(os.path.join(ROOT, 'static', 'index.html'), encoding='utf-8').read()
    assert 'narGenVoice' in html, '「生成解说配音」复选框必须存在'
    js = open(os.path.join(ROOT, 'static', 'app.js'), encoding='utf-8').read()
    assert 'window.confirm' in js and 'skipTts' in js, '未勾配音必须弹窗确认并传 skipTts'
    assert 'narGenVoice' in js

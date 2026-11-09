# -*- coding: utf-8 -*-
"""v1.4.16 更新：auto 已改为只用新一代引擎（契约见 tests/test_tts2_engines.py）。

本文件保留旧引擎「显式指定时仍可用」的回归钉：主人拍板放弃旧的，
但代码留作回滚通道，engine=edge 等仍应能出声，不能因为改默认链就删坏。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401  （先导入宿主，避开循环导入）
import tts_engines as T  # noqa: E402

JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')
HTML = os.path.join(ROOT, 'static', 'desktop', 'index.html')


def test_legacy_edge_still_usable_when_explicit(monkeypatch, tmp_path):
    """旧引擎退出默认链，但显式指定 engine=edge 仍必须能出声（回滚通道）。"""
    monkeypatch.delattr(T._TLS, 'tts_engine', raising=False)
    monkeypatch.setattr(T, 'load_ai_config',
                        lambda: {'tts_local': {'engine': 'edge'}})
    monkeypatch.setattr(T, 'edge_tts_available', lambda: True)
    called = []

    def _edge(text, out, voice=None, rate=None):
        called.append(text)
        with open(out, 'wb') as f:
            f.write(b'ID3' + b'0' * 100)
        return True
    monkeypatch.setattr(T, 'edge_tts_speak', _edge)
    ok, eng, _p = T.local_tts_speak('旧引擎回滚测试。', str(tmp_path / 'a.mp3'))
    assert ok is True and eng == 'edge' and called, (ok, eng, called)


def test_first_available_engine_handles_legacy_names():
    order = ['chattts', 'sherpa', 'edge', 'sapi']
    assert order.index('sherpa') < order.index('edge')


def test_shared_order_source_is_used_everywhere():
    src = open(os.path.join(ROOT, 'tts_engines.py'), encoding='utf-8').read()
    assert 'order = _auto_order()' in src
    assert "_first = _first_available_engine(_auto_order())" in src
    assert 'import tts2_engines as T2' in src


def test_frontend_still_has_degrade_and_reset():
    js = open(JS, encoding='utf-8').read()
    html = open(HTML, encoding='utf-8').read()
    assert 'id="btnTtsReset"' in html and 'id="ttsStateHint"' in html
    assert '/api/tts_reset' in js and 'v.degrade_msg' in js

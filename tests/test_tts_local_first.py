# -*- coding: utf-8 -*-
"""v1.4.14「本地优先」配音链回归钉。

真痛点（成品 EXE 实测）：`models/` 里只有 whisper，本地引擎一个都没装；
`ai_config.json` 没有 tts_local → engine=auto → 旧顺序 `edge → chattts → sherpa → sapi`，
于是微软 edge 一崩，中间两个本地引擎因「没装」直接 continue，永远掉到 SAPI 机械音。
主人反馈的三件事（微软 TTS 常崩 / 本机引擎没效果 / CosyVoice 要手动复刻）
根子是同一个：默认主路径押在联网服务上，装了本地引擎也不会自动生效。

这里钉五件事：
  1. auto 顺序本地优先，edge 退到兜底
  2. CosyVoice 进兜底链必须由「常驻 worker 三件套」守卫（否则 100s/句慢坑）
  3. 第一个可用引擎的判定与标签一致，标签不许说谎
  4. 本地可用时不再白跑一次 edge 端到端自检；本地全无时仍保留原自检
  5. 前端把降级原因 / 熔断原因摆出来，并提供重试入口
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


def _no_local(monkeypatch):
    for fn in ('chattts_available', 'sherpa_tts_available', 'cosyvoice_available'):
        monkeypatch.setattr(T, fn, lambda: False)
    monkeypatch.setattr(T, 'edge_tts_available', lambda: True)


def test_auto_order_puts_local_first():
    order = T._auto_order()
    assert order[0] != 'edge', '联网 edge 不许再当默认主路径：%r' % order
    assert 'edge' in order, 'edge 仍要留作兜底增强'
    assert order[-1] == 'sapi', 'SAPI 只能排最后'
    assert order.index('sherpa') < order.index('edge'), '离线模型必须优先于联网服务'


def test_cosyvoice_guard_requires_resident_worker(monkeypatch, tmp_path):
    """常驻 worker 三件套不齐就绝不进自动兜底链（否则逐句重载 9GB 模型）。"""
    monkeypatch.setattr(T, 'HERE', str(tmp_path))
    monkeypatch.setattr(T, '_cosyvoice_venv_python', lambda: 'fake-python.exe')
    model = tmp_path / 'model'
    model.mkdir()
    monkeypatch.setattr(T, 'COSYVOICE_MODEL_DIR', str(model))
    assert T.cosyvoice_resident_ready() is False
    assert 'cosyvoice' not in T._auto_order()
    (tmp_path / 'cosyvoice_worker.py').write_text('# fake worker', encoding='utf-8')
    assert T.cosyvoice_resident_ready() is True
    assert T._auto_order()[0] == 'cosyvoice'


def test_first_available_engine_prefers_local(monkeypatch):
    monkeypatch.setattr(T, 'chattts_available', lambda: False)
    monkeypatch.setattr(T, 'sherpa_tts_available', lambda: True)
    monkeypatch.setattr(T, 'cosyvoice_available', lambda: False)
    monkeypatch.setattr(T, 'edge_tts_available', lambda: True)
    order = ['chattts', 'sherpa', 'edge', 'sapi']
    assert T._first_available_engine(order) == 'sherpa'
    monkeypatch.setattr(T, 'sherpa_tts_available', lambda: False)
    assert T._first_available_engine(order) == 'edge', '本地全没装才该回落到 edge'
    monkeypatch.setattr(T, 'edge_tts_available', lambda: False)
    assert T._first_available_engine(order) == 'sapi'


def test_label_matches_real_chain(monkeypatch):
    """auto 标签必须反映真实首选引擎，不能嘴上说 edge 实际用离线模型。"""
    monkeypatch.setattr(T, 'load_ai_config', lambda: {'tts_local': {'engine': 'auto'}})
    monkeypatch.setattr(T, '_cloud_tts_ready', lambda: False)
    monkeypatch.setattr(T, 'chattts_available', lambda: False)
    monkeypatch.setattr(T, 'cosyvoice_available', lambda: False)
    monkeypatch.setattr(T, 'sherpa_tts_available', lambda: True)
    monkeypatch.setattr(T, 'edge_tts_available', lambda: True)
    lab = T.local_tts_label()
    assert '离线模型' in lab and 'edge' not in lab, '标签说谎：%r' % lab


def test_ping_skipped_when_local_first(monkeypatch, tmp_path):
    monkeypatch.setattr(T, 'load_ai_config', lambda: {'tts_local': {'engine': 'auto'}})
    monkeypatch.setattr(T, 'chattts_available', lambda: True)
    _no_local(monkeypatch)
    monkeypatch.setattr(T, 'chattts_available', lambda: True)

    def _boom(*a, **k):
        raise AssertionError('本地引擎可用时不该再 ping 联网 edge')
    monkeypatch.setattr(T, 'edge_tts_speak', _boom)
    T._ping_preferred_engine(str(tmp_path))


def test_ping_still_runs_when_edge_is_only_option(monkeypatch, tmp_path):
    monkeypatch.setattr(T, 'load_ai_config', lambda: {'tts_local': {'engine': 'auto'}})
    _no_local(monkeypatch)
    called = []
    monkeypatch.setattr(T, 'edge_tts_speak',
                       lambda *a, **k: called.append(1) or True)
    T._ping_preferred_engine(str(tmp_path))
    assert called, '本地全没装时仍应保留 edge 首段试点'


def test_shared_order_used_by_speak_and_label():
    src = open(os.path.join(ROOT, 'tts_engines.py'), encoding='utf-8').read()
    assert 'order = _auto_order()' in src, 'local_tts_speak 的 auto 分支必须复用同一份顺序'
    assert "_first = _first_available_engine(_auto_order())" in src, '标签必须复用同一份顺序'
    assert "enable_split = (_first_eng == 'edge')" in src, \
        '本地优先后子句切分不能再按 order[0] 判断'
    assert 'preferred = _first_eng or (order[0] if order else None)' in src, \
        '首选引擎必须取第一个可用的，不能拿没装的本地引擎当 preferred'


def test_speak_prefers_local_and_never_touches_edge(monkeypatch, tmp_path):
    """端到端：装了 sherpa 后 auto 真的走本地，一次联网都不碰。"""
    monkeypatch.delattr(T._TLS, 'tts_engine', raising=False)
    monkeypatch.setattr(T, 'load_ai_config', lambda: {'tts_local': {'engine': 'auto'}})
    monkeypatch.setattr(T, 'chattts_available', lambda: False)
    monkeypatch.setattr(T, 'cosyvoice_available', lambda: False)
    monkeypatch.setattr(T, 'sherpa_tts_available', lambda: True)
    monkeypatch.setattr(T, 'edge_tts_available', lambda: True)

    def _boom(*a, **k):
        raise AssertionError('本地引擎可用时不该再调用联网 edge')
    monkeypatch.setattr(T, 'edge_tts_speak', _boom)
    called = []

    def _sherpa(text, out, speed=1.0):
        called.append(text)
        with open(out, 'wb') as f:
            f.write(b'RIFF' + b'0' * 100)
        return True
    monkeypatch.setattr(T, 'sherpa_tts_speak', _sherpa)
    ok, eng, path = T.local_tts_speak('这是一句测试解说词。', str(tmp_path / 'narr0.wav'))
    assert ok is True and eng == 'sherpa' and called, (ok, eng, called)
    assert os.path.exists(path)


def test_speak_falls_back_to_edge_when_no_local(monkeypatch, tmp_path):
    """没装任何本地引擎时行为与旧版一致：仍由 edge 出声，不破坏现网。"""
    monkeypatch.delattr(T._TLS, 'tts_engine', raising=False)
    monkeypatch.setattr(T, 'load_ai_config', lambda: {'tts_local': {'engine': 'auto'}})
    _no_local(monkeypatch)
    with T._TTS_DEGRADE_LOCK:
        T._TTS_DEGRADE.update({'from': None, 'to': None, 'msg': '', 'count': 0})
    called = []

    def _edge(text, out, voice=None, rate=None):
        called.append(text)
        with open(out, 'wb') as f:
            f.write(b'ID3' + b'0' * 100)
        return True
    monkeypatch.setattr(T, 'edge_tts_speak', _edge)
    ok, eng, path = T.local_tts_speak('这是一句测试解说词。', str(tmp_path / 'narr0.mp3'))
    assert ok is True and eng == 'edge' and called, (ok, eng, called)
    assert T._TTS_DEGRADE['count'] == 0, \
        '没装本地引擎时不该报「ChatTTS 不可用，已降级为 edge-tts」这种误导提示'


def test_frontend_exposes_degrade_and_reset():
    js = open(JS, encoding='utf-8').read()
    html = open(HTML, encoding='utf-8').read()
    for token in ('id="btnTtsReset"', 'id="ttsStateHint"'):
        assert token in html, '设置页缺 %s' % token
    for token in ('/api/tts_reset', 'v.degrade_msg', 'v.edge_dead'):
        assert token in js, '前端没接 %s' % token

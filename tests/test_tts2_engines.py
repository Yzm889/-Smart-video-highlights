# -*- coding: utf-8 -*-
"""v1.4.16 双新引擎契约：Qwen3-TTS VoiceDesign + IndexTTS2 接管默认链。

主人拍板「放弃以前的」：SAPI / edge / MeloTTS / ChatTTS / CosyVoice
全部退出 auto 与设置页下拉，代码保留作回滚。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import tts_engines as T  # noqa: E402
import tts2_engines as T2  # noqa: E402

JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')
SPEC = os.path.join(ROOT, 'FrameCut.spec')


def test_auto_order_is_only_new_engines():
    assert T._auto_order() == ['qwen3tts', 'indextts2']
    for legacy in ('edge', 'sapi', 'sherpa', 'chattts', 'cosyvoice'):
        assert legacy not in T._auto_order(), '旧引擎 %s 不该回默认链' % legacy


def test_prosody_marks_reach_new_engines():
    """旧引擎把 {情绪}/{停顿} 全剥掉；新引擎必须真的消费它们。"""
    hint, vec = T._tts2_style('注意看{情绪:紧张}，他突然停下{停顿:0.8}。')
    assert '紧张' in hint
    assert vec == T2.EMO_VECTORS['紧张'] and len(vec) == 8
    clean = T._tts2_clean('注意看{情绪:紧张}，他突然停下{停顿:0.8}。')
    assert '{' not in clean and '}' not in clean and '情绪' not in clean


def test_label_reports_new_engine(monkeypatch):
    # 两个引擎现在都真的装好了，必须同时钉住状态，否则断言依赖本机资产
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: False)
    monkeypatch.setattr(T, 'load_ai_config',
                        lambda: {'tts_local': {'engine': 'auto'}})
    monkeypatch.setattr(T, '_cloud_tts_ready', lambda: False)
    assert 'Qwen3-TTS' in T.local_tts_label()
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: False)
    assert '未安装' in T.local_tts_label()


def test_auto_dispatches_to_qwen3tts(monkeypatch, tmp_path):
    monkeypatch.delattr(T._TLS, 'tts_engine', raising=False)
    monkeypatch.setattr(T, 'load_ai_config',
                        lambda: {'tts_local': {'engine': 'auto'}})
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    called = []

    def _speak(text, out, instruct=None):
        called.append((text, instruct))
        with open(out, 'wb') as f:
            f.write(b'RIFF' + b'0' * 100)
        return True
    monkeypatch.setattr(T2, 'qwen3tts_speak', _speak)
    ok, eng, path = T.local_tts_speak(
        '注意看{情绪:紧张}，这个男人叫弗兰克。', str(tmp_path / 'n.wav'))
    assert ok is True and eng == 'qwen3tts' and called, (ok, eng, called)
    assert '紧张' in called[0][1], '情绪词必须进 VoiceDesign 的 instruct'
    assert os.path.isfile(path)


def test_no_engine_ready_fails_loudly(monkeypatch, tmp_path):
    """两个新引擎都没装时不许静默掉到机械音：明确返回失败。"""
    monkeypatch.delattr(T._TLS, 'tts_engine', raising=False)
    monkeypatch.setattr(T, 'load_ai_config',
                        lambda: {'tts_local': {'engine': 'auto'}})
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: False)
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: False)
    ok, eng, _p = T.local_tts_speak('测试', str(tmp_path / 'n.wav'))
    assert ok is False and eng is None


def test_workers_are_registered_in_spec():
    """worker 是独立 venv 调用的脚本，不登记进 datas 打包版里两个引擎全废。"""
    spec = open(SPEC, encoding='utf-8').read()
    assert "('qwen3tts_worker.py', '.')" in spec
    assert "('indextts2_worker.py', '.')" in spec
    for name in ('qwen3tts_worker.py', 'indextts2_worker.py'):
        assert os.path.isfile(os.path.join(ROOT, name))
        assert "'qwen_tts'" not in open(os.path.join(ROOT, name), encoding='utf-8').read() \
            or name == 'qwen3tts_worker.py'


def test_frontend_dropdown_has_no_legacy_engines():
    js = open(JS, encoding='utf-8').read()
    start = js.index('const TTS_ENGINES')
    block = js[start:start + 400]
    assert 'qwen3tts' in block and 'indextts2' in block
    for legacy in ("'cosyvoice'", "'sherpa'", "'sapi'", "'chattts'"):
        assert legacy not in block, '下拉里仍有旧引擎 %s' % legacy

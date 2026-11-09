# -*- coding: utf-8 -*-
"""v1.4.20 回归钉：整片必须用同一把嗓子。

事故（2026-11-09 实跑惩罚者前二十分钟）：用户听完 37 段配音说
「每句的音色、语气都很割裂，没有同一感」。

实测根因：auto 链首选 qwen3tts，而 VoiceDesign 是**按文字描述现场造音色**，
每条请求都重新采样一个发音人。同一段 instruct、三句不同文本：

    逐句采样（do_sample=True, T=0.9）   F0 105.7 / 96.8 / 89.4 Hz   离散 16.8%
    批量一次生成（text=[...]）           F0 100.4 / 91.6 /111.1 Hz   离散 19.3%
    贪心解码（do_sample=False）          F0 101.9 /100.8 / 85.1 Hz   离散 17.5%

也就是说：调采样参数、批量、贪心都救不了 —— 引擎选错了模式。
换成 IndexTTS2 拿固定参考音零样本克隆，同样三句 100.2 / 98.9 / 99.3 Hz，离散 1.4%。

本文件钉住三件事：
  1. auto 链首选克隆路线（indextts2）；
  2. 参考音只造一次、带 instruct 指纹缓存、造完释放 Qwen3 显存；
  3. 逐句合成必须把同一个 spk 传给引擎，不许让它退回自带默认音。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import tts_engines as T  # noqa: E402
import tts2_engines as T2  # noqa: E402


def _cfg(instruct=''):
    return {'engine': 'auto', 'voice': None, 'rate': '+0%', 'instruct': instruct}


def test_auto_prefers_clone_over_per_sentence_design():
    order = T._auto_order()
    assert order[0] == 'indextts2', '逐句 VoiceDesign 会每句换一个人，不能再排第一'
    assert 'qwen3tts' in order, 'Qwen3 仍要留着造音色/兜底'


def test_voice_ref_reuses_cached_sample(tmp_path, monkeypatch):
    """已经造过就不该再花 20 秒重造 —— 跨集复用同一把嗓子。"""
    monkeypatch.setattr(T2, 'BIN_DIR', str(tmp_path))
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, '_stop_engine', lambda srv: None)
    called = []

    def fake(text, out, instruct=None):
        called.append(1)
        open(out, 'wb').write(b'0' * 40000)
        return True

    monkeypatch.setattr(T2, 'qwen3tts_speak', fake)
    p1 = T2.voice_ref('沉稳男声')
    p2 = T2.voice_ref('沉稳男声')
    assert p1 and p1 == p2
    assert len(called) == 1, '第二次必须命中缓存，不能重造'


def test_voice_ref_mints_and_frees_qwen(tmp_path, monkeypatch):
    """造样本：调一次 qwen3、落盘，然后把 Qwen3 的显存让给 IndexTTS2。"""
    monkeypatch.setattr(T2, 'BIN_DIR', str(tmp_path))
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    stop = []
    monkeypatch.setattr(T2, '_stop_engine', lambda srv: stop.append(srv))

    def fake_speak(text, out, instruct=None):
        assert text == T2.VOICE_REF_TEXT
        open(out, 'wb').write(b'0' * 40000)
        return True

    monkeypatch.setattr(T2, 'qwen3tts_speak', fake_speak)
    p = T2.voice_ref('沉稳男声')
    assert p and os.path.isfile(p) and os.path.getsize(p) >= 20000
    assert stop, '造完音色必须释放 Qwen3，否则 IndexTTS2 的 6.6GB 装不下'


def test_voice_ref_key_follows_instruct(tmp_path, monkeypatch):
    """用户改音色描述 → 必须造一把新的，不能沿用旧嗓子。"""
    monkeypatch.setattr(T2, 'BIN_DIR', str(tmp_path))
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, 'qwen3tts_speak',
                        lambda text, out, instruct=None: bool(
                            open(out, 'wb').write(b'0' * 40000)) or True)
    monkeypatch.setattr(T2, '_stop_engine', lambda srv: None)
    a = T2.voice_ref('沉稳男声')
    b = T2.voice_ref('清亮女声')
    assert a != b


def test_voice_ref_none_when_qwen_missing(tmp_path, monkeypatch):
    """Qwen3 没装好 → 返回 None，由调用方退回引擎自带示例音（仍是一个固定的人）。"""
    monkeypatch.setattr(T2, 'BIN_DIR', str(tmp_path))
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: False)
    assert T2.voice_ref('沉稳男声') is None


def test_speak_passes_same_ref_every_sentence(tmp_path, monkeypatch):
    """逐句合成必须带上同一个参考音；否则 IndexTTS2 用自带默认音，换了音色。"""
    monkeypatch.setattr(T, 'tts_local_cfg', lambda: _cfg())
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'voice_ref', lambda instruct=None: r'C:\ref\voice.wav')
    seen = []

    def fake(text, out, **kw):
        seen.append(kw.get('spk'))
        open(out, 'wb').write(b'0' * 4096)
        return True

    monkeypatch.setattr(T2, 'indextts2_speak', fake)
    for i in range(3):
        ok, eng, _ = T.local_tts_speak('第%d句话测试' % i, str(tmp_path / ('n%d.wav' % i)))
        assert ok and eng == 'indextts2'
    assert seen == [r'C:\ref\voice.wav'] * 3


def test_preview_uses_same_voice_as_final(tmp_path, monkeypatch):
    """试听必须用成片那把嗓子（克隆），不能退回 Qwen3 逐句造音色。

    事故（2026-11-09 打包验证）：点试听返回「Qwen3-TTS · VoiceDesign」，
    8 秒才出 32 字，而成片走的是 IndexTTS2 克隆——两条路不一致，
    用户拿试听判断音色会被误导。
    """
    import webui_server as W
    monkeypatch.setattr(W, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(W, 'tts_local_cfg',
                        lambda: {'engine': 'auto', 'rate': '+0%', 'instruct': ''})
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, 'voice_ref', lambda instruct=None: r'C://ref.wav')
    monkeypatch.setattr(T2, 'rate_to_speed', lambda r: 1.12)
    seen = {}

    def fake_idx(text, out, spk=None, diag=None):
        seen['spk'] = spk
        open(out, 'wb').write(b'RIFF' + b'0' * 4000)
        return True

    def fake_qwen(*a, **k):
        seen['qwen'] = True
        return False

    monkeypatch.setattr(T2, 'indextts2_speak', fake_idx)
    monkeypatch.setattr(T2, 'qwen3tts_speak', fake_qwen)
    ok, msg, eng, rel = W.tts_test_speak('试听一句话')
    assert ok and eng == 'indextts2', (ok, msg, eng)
    assert seen.get('spk') == r'C://ref.wav'
    assert not seen.get('qwen'), '试听不该走 Qwen3'

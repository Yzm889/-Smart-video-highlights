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
    # v1.4.20：克隆优先。qwen3tts 逐句 VoiceDesign 会每句换一个发音人
    # （F0 离散 16.8%），只留作“造一次音色样本”和克隆故障时的兜底。
    # 【v1.4.91 变更】主人实测 Qwen3 兜底「每句都像喊出来」且音色漂移是模型层
    # 问题——Qwen3 移出 auto 链（IndexTTS2 失败直接落 edge），显式选 qwen3tts 仍可用。
    assert T._auto_order() == ['indextts2', 'edge', 'chattts', 'sherpa', 'sapi']
    assert 'qwen3tts' not in T._auto_order(), 'Qwen3 不再作为 auto 兜底（喊叫+音色漂移）'


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


def test_auto_dispatches_to_indextts2_with_fixed_ref(monkeypatch, tmp_path):
    """v1.4.20：auto 走克隆，且每句都带同一个参考音。

    以前这条断言的是「auto 交给 qwen3tts」——恰恰是音色割裂的根源：
    VoiceDesign 每条请求重新采样发音人。现在 Qwen3 只负责造参考音。
    """
    monkeypatch.delattr(T._TLS, 'tts_engine', raising=False)
    monkeypatch.setattr(T, 'load_ai_config',
                        lambda: {'tts_local': {'engine': 'auto'}})
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'voice_ref', lambda instruct=None: r'C:\ref\voice.wav')
    called = []

    def _speak(text, out, **kw):
        called.append((text, kw))
        with open(out, 'wb') as f:
            f.write(b'RIFF' + b'0' * 100)
        return True
    monkeypatch.setattr(T2, 'indextts2_speak', _speak)
    ok, eng, path = T.local_tts_speak(
        '注意看{情绪:紧张}，这个男人叫弗兰克。', str(tmp_path / 'n.wav'))
    assert ok is True and eng == 'indextts2', (ok, eng)
    assert called[0][1].get('spk') == r'C:\ref\voice.wav', '必须传固定参考音'
    assert called[0][1].get('emo_vector') == T2.EMO_VECTORS['紧张'], '情绪词必须进向量'
    assert '{' not in called[0][0] and '情绪' not in called[0][0]
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


def test_qwen3tts_label_matches_its_real_behaviour():
    """选了 Qwen3 之后实际也是克隆，标签不能再说「逐句文字造音色」。

    修 P1-3 之前 qwen3tts 排在链首、逐句 VoiceDesign 现造人，标签写
    「文字造音色」是对的；修完之后它退到第二位、由 IndexTTS2 克隆，
    标签还写旧话就是在骗用户（与 P1-1「谎报未装引擎」同一类缺陷）。
    """
    js = open(JS, encoding='utf-8').read()
    start = js.index('const TTS_ENGINES')
    block = js[start:start + 400]
    line = [l for l in block.splitlines() if "'qwen3tts'" in l][0]
    assert '克隆' in line, 'qwen3tts 的实际行为是「造参考音 + 克隆」，标签要写出来：%s' % line
    assert '文字造音色，无需录音' not in line, \
        '旧标签暗示逐句现造人，与 _engine_order_for(\'qwen3tts\') 已不符：%s' % line

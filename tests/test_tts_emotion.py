# -*- coding: utf-8 -*-
"""v1.4.76 情感表现接线回归钉。

根因背景：LLM 写稿允许「严肃/激动」、韵律层也产出「激动」，但 IndexTTS2 的
8 维向量表 EMO_VECTORS 没有这两词 → 静默传空，两类情绪等于没传；模型内置的
QwenEmotion 分类器（emo_text 路线）与句间气口 interval_silence 也从未被接线。
本文件钉住：词表统一、智能情感三档语义、参数透传、LLM prompt 词表一致。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tts_engines as T  # noqa: E402
import tts2_engines as T2  # noqa: E402


# ---------------------------------------------------------------------------
# 词表统一（唯一源 = EMO_VECTORS）
# ---------------------------------------------------------------------------

def test_emo_vectors_has_serious_and_excited():
    for w in ('严肃', '激动'):
        vec = T2.EMO_VECTORS.get(w)
        assert isinstance(vec, list) and len(vec) == 8, '新增情绪 %s 必须是 8 维' % w
        assert all(isinstance(x, (int, float)) for x in vec)
    # 原有 8 词一个都不能丢（试听按钮/旧稿子还在引用）
    for w in ('紧张', '惊险', '悲伤', '欢快', '愤怒', '温柔', '深情', '平静'):
        assert w in T2.EMO_VECTORS
    assert len(T2.EMO_VECTORS) >= 10


def test_prosody_emotion_covers_serious():
    # 韵律层产出的每个情绪词必须能被向量表接住——这是 v1.4.76 修的「词表错位」
    for emo in T._PROSODY_EMOTION:
        assert emo in T2.EMO_VECTORS or emo == '解说', \
            '韵律情绪 %s 在 EMO_VECTORS 里查不到 = 又要静默传空' % emo


def test_llm_prompt_emotion_words_match_emo_vectors():
    """源码钉：LLM 写稿 prompt 的情绪词表必须覆盖 EMO_VECTORS 全部键。"""
    with open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8') as f:
        src = f.read()
    start = src.index('【配音标记】')
    seg = src[start:src.index('【禁止】')]
    for word in T2.EMO_VECTORS:
        assert word in seg, 'LLM prompt 词表缺 %s（写了就会被静默丢掉）' % word


# ---------------------------------------------------------------------------
# 智能情感三档：_tts2_emotion_kwargs
# ---------------------------------------------------------------------------

def _cfg(**over):
    base = {'engine': 'indextts2', 'emotion': 'auto', 'emotion_alpha': 0.6,
            'interval_silence': 200}
    base.update(over)
    return base


def test_emotion_auto_unmarked_sentence_uses_emo_text():
    kw = T._tts2_emotion_kwargs('这个男人叫弗兰克，他没说话。', _cfg())
    assert kw.get('emo_text') == '这个男人叫弗兰克，他没说话。'
    assert kw.get('emo_alpha') == 0.6
    assert 'emo_vector' not in kw


def test_emotion_auto_marked_word_uses_vector():
    kw = T._tts2_emotion_kwargs('注意看{情绪:严肃}，他翻开案卷。', _cfg())
    assert kw.get('emo_vector') == T2.EMO_VECTORS['严肃']
    assert 'emo_text' not in kw, '词表命中时不许再走 emo_text（两条路线互斥）'


def test_emotion_auto_unknown_word_falls_back_to_emo_text():
    # 「忧郁」不在向量表：v1.4.76 之前这里静默传空（等于没传），现在必须降级到 emo_text
    kw = T._tts2_emotion_kwargs('注意看{情绪:忧郁}，这个男人叫弗兰克。', _cfg())
    assert 'emo_vector' not in kw
    assert kw.get('emo_text') == '注意看，这个男人叫弗兰克。'


def test_emotion_vector_mode_only_marked_words():
    assert T._tts2_emotion_kwargs('没有标记的句子。', _cfg(emotion='vector')) == {}
    kw = T._tts2_emotion_kwargs('注意看{情绪:紧张}，他突然停下。', _cfg(emotion='vector'))
    assert kw == {'emo_vector': T2.EMO_VECTORS['紧张']}


def test_emotion_off_sends_nothing():
    assert T._tts2_emotion_kwargs('注意看{情绪:紧张}，他突然停下。', _cfg(emotion='off')) == {}


def test_emotion_alpha_flows_from_cfg():
    kw = T._tts2_emotion_kwargs('平静的一句话。', _cfg(emotion_alpha=0.9))
    assert kw.get('emo_alpha') == 0.9


# ---------------------------------------------------------------------------
# tts_local_cfg 默认值与钳制
# ---------------------------------------------------------------------------

def test_tts_local_cfg_emotion_defaults(monkeypatch):
    monkeypatch.setattr(T, 'load_ai_config', lambda: {})
    c = T.tts_local_cfg()
    assert c['emotion'] == 'auto' and c['emotion_alpha'] == 0.6
    assert c['interval_silence'] == 200


def test_tts_local_cfg_emotion_clamps(monkeypatch):
    monkeypatch.setattr(T, 'load_ai_config', lambda: {
        'tts_local': {'emotion': 'bogus', 'emotion_alpha': 'abc',
                      'interval_silence': 99999}})
    c = T.tts_local_cfg()
    assert c['emotion'] == 'auto' and c['emotion_alpha'] == 0.6
    assert c['interval_silence'] == 1000
    monkeypatch.setattr(T, 'load_ai_config', lambda: {
        'tts_local': {'emotion': 'OFF', 'emotion_alpha': 0.05,
                      'interval_silence': -3}})
    c = T.tts_local_cfg()
    assert c['emotion'] == 'off' and c['emotion_alpha'] == 0.2
    assert c['interval_silence'] == 0


# ---------------------------------------------------------------------------
# 透传链：indextts2_speak → worker 请求
# ---------------------------------------------------------------------------

class _FakeSrv:
    def __init__(self, sink):
        self._sink = sink
        self.note = ''

    def ask(self, req):
        self._sink.update(req)
        with open(req['out'], 'wb') as f:
            f.write(b'RIFF' + b'0' * 2000)
        return True, ''


def test_indextts2_speak_passes_emo_alpha_and_gap(monkeypatch, tmp_path):
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'gpu_vram', lambda max_age=3.0: (0, 0))
    sink = {}
    monkeypatch.setattr(T2, '_get_idx', lambda: _FakeSrv(sink))
    out = str(tmp_path / 'x.wav')
    ok = T2.indextts2_speak('测试句子', out, emo_text='测试句子', emo_alpha=0.8,
                            interval_silence=350)
    assert ok and sink.get('emo_alpha') == 0.8
    assert sink.get('interval_silence') == 350
    assert sink.get('emo_text') == '测试句子'


def test_indextts2_speak_default_alpha(monkeypatch, tmp_path):
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'gpu_vram', lambda max_age=3.0: (0, 0))
    sink = {}
    monkeypatch.setattr(T2, '_get_idx', lambda: _FakeSrv(sink))
    out = str(tmp_path / 'x.wav')
    assert T2.indextts2_speak('测试', out, emo_text='测试')
    assert sink.get('emo_alpha') == 0.6, 'emo_text 不带 alpha 时必须落回默认 0.6'
    assert 'interval_silence' not in sink, '不传气口不许塞 0/None 进 worker'


def test_worker_passes_interval_silence_to_infer():
    """worker 必须把气口透传给 infer——源码钉（worker 跑在独立 venv，无法桩 infer）。"""
    with open(os.path.join(ROOT, 'indextts2_worker.py'), encoding='utf-8') as f:
        src = f.read()
    assert "kw['interval_silence'] = int(req['interval_silence'])" in src


# ---------------------------------------------------------------------------
# local_tts_speak 全链（auto 档端到端）
# ---------------------------------------------------------------------------

def test_auto_mode_unmarked_sentence_reaches_speak_with_emo_text(monkeypatch, tmp_path):
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
        '这个男人叫弗兰克，他没说话。', str(tmp_path / 'n.wav'))
    assert ok is True and eng == 'indextts2'
    kw = called[0][1]
    assert kw.get('spk') == r'C:\ref\voice.wav', '固定参考音不能丢'
    assert kw.get('emo_text') == '这个男人叫弗兰克，他没说话。'
    assert kw.get('emo_alpha') == 0.6 and kw.get('interval_silence') == 200


def test_off_mode_reaches_speak_without_emotion(monkeypatch, tmp_path):
    monkeypatch.delattr(T._TLS, 'tts_engine', raising=False)
    monkeypatch.setattr(T, 'load_ai_config',
                        lambda: {'tts_local': {'engine': 'indextts2',
                                               'emotion': 'off'}})
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'voice_ref', lambda instruct=None: r'C:\ref\voice.wav')
    called = []

    def _speak(text, out, **kw):
        called.append(kw)
        with open(out, 'wb') as f:
            f.write(b'RIFF' + b'0' * 100)
        return True
    monkeypatch.setattr(T2, 'indextts2_speak', _speak)
    ok, eng, _p = T.local_tts_speak('注意看{情绪:紧张}，他停下。', str(tmp_path / 'n.wav'))
    assert ok and eng == 'indextts2'
    kw = called[0]
    for key in ('emo_vector', 'emo_text', 'emo_alpha'):
        assert key not in kw, 'off 档不许传任何情感参数'
    assert kw.get('interval_silence') == 200, '气口是时长参数，off 档仍然生效'

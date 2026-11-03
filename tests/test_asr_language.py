# -*- coding: utf-8 -*-
"""ASR 语言处理与重复幻觉折叠的回归锁定。

真机证据（22 分钟英文片，9 分钟切片实测）：
  · 配置默认 'zh' → 英文对白被硬按中文解码，输出「我跟他们说」×12、
    「林顿省警察受伤」×4 这类翻译腔幻觉；
  · 这些脏行被下游剧情理解/语义对齐当成台词证据，解说质量从源头就歪。

锁定四件事：
  1. 默认语言是 auto（不再默认 zh）
  2. auto 时传给 whisper 的 language 必须是 None（真自动判别），且不塞中文 initial_prompt
  3. 显式配 zh 时仍保留中文 initial_prompt（不牺牲中文片精度）
  4. 连续重复行被折叠，时间轴连续
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_providers as A


# ---------- 1) 默认语言 ----------

def test_default_language_is_auto(monkeypatch):
    monkeypatch.setattr(A, 'load_ai_config', lambda: {'whisper': {}})
    assert A.whisper_language() == 'auto'


def test_explicit_language_respected(monkeypatch):
    monkeypatch.setattr(A, 'load_ai_config', lambda: {'whisper': {'language': 'zh'}})
    assert A.whisper_language() == 'zh'
    monkeypatch.setattr(A, 'load_ai_config', lambda: {'whisper': {'language': 'en'}})
    assert A.whisper_language() == 'en'


# ---------- 2/3) 传给 whisper 的参数 ----------

class _FakeSeg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _FakeInfo:
    def __init__(self, duration=10.0, language='en'):
        self.duration = duration
        self.language = language


class _FakeModel:
    """记录 transcribe 的入参，返回固定台词。"""

    def __init__(self, *a, **k):
        self.kwargs = None

    def transcribe(self, audio, **kwargs):
        self.kwargs = kwargs
        segs = [_FakeSeg(0.0, 1.0, 'Hello there'),
                _FakeSeg(1.0, 2.0, 'We need backup')]
        return iter(segs), _FakeInfo()


@pytest.fixture()
def whisper_env(monkeypatch, tmp_path):
    """把 faster_whisper / 抽音轨 / GPU 探测全部换成假体，只观察传参。"""
    import types

    holder = {}

    def _make_model(*a, **k):
        m = _FakeModel()
        holder['model'] = m
        return m

    fake_mod = types.SimpleNamespace(WhisperModel=_make_model)
    monkeypatch.setitem(sys.modules, 'faster_whisper', fake_mod)
    monkeypatch.setattr(A, 'ffmpeg_run', lambda args: (0, '', ''))
    monkeypatch.setattr(A, 'whisper_device', lambda: ('cpu', 'int8'))
    monkeypatch.setattr(A, '_whisper_env_setup', lambda: None)
    monkeypatch.setattr(A, '_whisper_load_path', lambda name: 'fake-model')
    monkeypatch.setattr(A, 'whisper_model_name', lambda: 'large-v3')
    monkeypatch.setattr(A, 'whisper_models_dir', lambda: str(tmp_path))
    monkeypatch.setattr(A, '_load_wav_as_float32', lambda p: None)
    monkeypatch.setattr(A, '_subtitle_segments', lambda p: None)
    monkeypatch.setattr(A, '_external_subtitle_segments', lambda p: None)
    monkeypatch.setattr(A, 'WORKDIR', str(tmp_path))
    monkeypatch.setattr(A, '_note_progress_warning', lambda msg: holder.setdefault('notes', []).append(msg))
    # os.path.exists 对假 wav 返回 True
    real_exists = os.path.exists
    monkeypatch.setattr(A.os.path, 'exists',
                        lambda p: True if str(p).endswith('.wav') else real_exists(p))
    return holder


def test_auto_language_passes_none_and_no_chinese_prompt(whisper_env, monkeypatch):
    monkeypatch.setattr(A, 'load_ai_config', lambda: {'whisper': {}})
    A.asr_segments('fake.mp4')
    kw = whisper_env['model'].kwargs
    assert kw.get('language') is None, 'auto 模式必须传 None（真自动判别），实际 %r' % kw.get('language')
    assert not kw.get('initial_prompt'), 'auto 模式不应塞中文 initial_prompt：%r' % kw.get('initial_prompt')


def test_explicit_zh_keeps_chinese_prompt(whisper_env, monkeypatch):
    monkeypatch.setattr(A, 'load_ai_config', lambda: {'whisper': {'language': 'zh'}})
    A.asr_segments('fake.mp4')
    kw = whisper_env['model'].kwargs
    assert kw.get('language') == 'zh'
    assert kw.get('initial_prompt'), '中文片丢了中文 initial_prompt，人名/术语准确率会下降'


def test_detected_language_is_surfaced(whisper_env, monkeypatch):
    monkeypatch.setattr(A, 'load_ai_config', lambda: {'whisper': {}})
    A.asr_segments('fake.mp4')
    notes = ' '.join(whisper_env.get('notes') or [])
    assert '识别语言' in notes, '没有把判别出的语言告知用户：%r' % notes


# ---------- 4) 重复折叠 ----------

def test_collapse_identical_run():
    segs = [{'start': 0, 'end': 1, 'text': '我跟他们说'},
            {'start': 1, 'end': 2, 'text': '我跟他们说'},
            {'start': 2, 'end': 3, 'text': '我跟他们说'},
            {'start': 3, 'end': 4, 'text': '他说完了'}]
    out = A._asr_collapse_repeats(segs)
    assert [s['text'] for s in out] == ['我跟他们说', '他说完了']
    assert out[0]['start'] == 0 and out[0]['end'] == 3, '折叠后时间轴没有覆盖整段：%r' % out[0]


def test_collapse_ignores_punctuation_and_space():
    segs = [{'start': 0, 'end': 1, 'text': '林顿省警察受伤'},
            {'start': 1, 'end': 2, 'text': '林顿省警察受伤。'},
            {'start': 2, 'end': 3, 'text': ' 林顿省警察受伤 '}]
    out = A._asr_collapse_repeats(segs)
    assert len(out) == 1, '标点/空白差异也应视为同一行重复：%r' % out


def test_single_line_not_collapsed():
    segs = [{'start': 0, 'end': 1, 'text': '只出现一次'}]
    assert len(A._asr_collapse_repeats(segs)) == 1


def test_different_lines_preserved():
    segs = [{'start': 0, 'end': 1, 'text': '第一句'},
            {'start': 1, 'end': 2, 'text': '第二句'},
            {'start': 2, 'end': 3, 'text': '第一句'}]
    out = A._asr_collapse_repeats(segs)
    assert [s['text'] for s in out] == ['第一句', '第二句', '第一句'], '不连续的同文不应被折叠'


def test_empty_input():
    assert A._asr_collapse_repeats([]) == []

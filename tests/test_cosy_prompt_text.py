# -*- coding: utf-8 -*-
"""克隆参考音文字稿转写回归钉（2026-11-06 主人上传录音实测）。

钉住：_cosyvoice_prompt_text 的转写模型优先级——
  本机有 large-v3（ASR 同款）→ 用 large-v3（tiny 实测把主人录音转成乱码级文字稿）；
  没有 large-v3 → 回退 tiny（老机器兼容）。
转写结果照旧缓存为 ref 同名 .txt 侧车。
"""
import os
import sys
import types

import pytest

import tts_engines as te


class _FakeSeg:
    text = '测试句'


class _FakeModel:
    calls = []

    def __init__(self, d, device=None, compute_type=None):
        _FakeModel.calls.append(d)

    def transcribe(self, audio, language=None):
        return iter([_FakeSeg()]), None


@pytest.fixture
def fake_whisper(monkeypatch):
    fake = types.ModuleType('faster_whisper')
    fake.WhisperModel = _FakeModel
    monkeypatch.setitem(sys.modules, 'faster_whisper', fake)
    _FakeModel.calls = []
    return _FakeModel


@pytest.fixture
def ref_wav(tmp_path):
    import webui_server as S
    p = str(tmp_path / 'ref.wav')
    rc, _o, e = S.ffmpeg_run(['-y', '-f', 'lavfi',
                              '-i', 'aevalsrc=0.9*sin(2*PI*440*t):d=1.5',
                              '-ar', '16000', '-ac', '1', p], timeout=60)
    assert rc == 0, e.decode('utf-8', 'ignore')[-200:]
    return p


def test_prefers_large_v3_when_present(ref_wav, fake_whisper, monkeypatch):
    te._COSY_ASR['model'] = None
    txt = te._cosyvoice_prompt_text(ref_wav)
    assert txt == '测试句'
    assert _FakeModel.calls and _FakeModel.calls[-1].endswith('large-v3'), \
        '本机有 large-v3 时必须优先用它（tiny 转写质量差）: %r' % _FakeModel.calls
    assert os.path.isfile(ref_wav[:-4] + '.txt'), '转写结果必须缓存为 .txt 侧车'


def test_falls_back_to_tiny_without_large_v3(ref_wav, fake_whisper, monkeypatch):
    te._COSY_ASR['model'] = None
    real_isdir = os.path.isdir

    def fake_isdir(p):
        return False if 'large-v3' in str(p) else real_isdir(p)
    monkeypatch.setattr(os.path, 'isdir', fake_isdir)
    txt = te._cosyvoice_prompt_text(ref_wav)
    assert txt == '测试句'
    assert _FakeModel.calls and _FakeModel.calls[-1].endswith('tiny'), \
        '没有 large-v3 时必须回退 tiny: %r' % _FakeModel.calls


def test_sidecar_short_circuits_transcription(ref_wav, fake_whisper, monkeypatch):
    te._COSY_ASR['model'] = None
    sidecar = ref_wav[:-4] + '.txt'
    with open(sidecar, 'w', encoding='utf-8') as f:
        f.write('已有文字稿')
    txt = te._cosyvoice_prompt_text(ref_wav)
    assert txt == '已有文字稿'
    assert not _FakeModel.calls, '有侧车文字稿时不应再调转写模型'


def teardown_function(_):
    te._COSY_ASR['model'] = None
    te._COSY_ASR.pop('dir', None)

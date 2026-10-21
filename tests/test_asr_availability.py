# -*- coding: utf-8 -*-
"""P0-1 回归：ASR 真实可用性（修复 av/PyAV 不兼容 + 静默失败）。

背景（真机全链路 P0-1 实测发现）：
  1. faster_whisper 1.2.1 内部 decode_audio 调 av.open(..., metadata_errors='ignore')，
     而 PyAV>=15 已移除该关键字（本机 av19.0.1），转写必抛
     `TypeError: open() got an unexpected keyword argument 'metadata_errors'`。
  2. asr_segments 用裸 `except Exception: return []` 把它吞掉，
     上层只看到「ASR 没结果」——用户完全无从判断是环境坏了还是片子没对白。
     31 段的分析结果草稿全空、default_keep 全 False，编辑台直接空转。

这里锁定三条：
  A. _load_wav_as_float32 能读标准 16bit/单声道 wav，且完全不碰 av；
  B. asr_segments 失败时把真实异常写进 PROGRESS['warnings']（不再静默）；
  C. _cuda_available 在「有驱动但无 CUDA 运行时库」时必须返回 False，
     避免 GPU 推理必然崩溃却被误判为可用。
"""
import os
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui_server as S  # noqa: E402  （必须先导入宿主，模块晚绑定其符号）
import ai_providers as P  # noqa: E402


def _make_wav(path, seconds=1.0, rate=16000):
    import struct
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        n = int(rate * seconds)
        # 简单斜坡，够解码用
        wf.writeframes(b''.join(struct.pack('<h', int(3000 * (i / n) - 1500))
                               for i in range(n)))
    return path


def test_load_wav_as_float32_does_not_need_av(tmp_path):
    """A. 读 wav 不依赖 PyAV——这正是原缺陷的根。"""
    p = _make_wav(str(tmp_path / 'a.wav'))
    audio = P._load_wav_as_float32(p)
    assert audio.dtype.name == 'float32'
    assert audio.shape == (16000,)
    # 幅度应在 [-1, 1]
    assert float(abs(audio).max()) <= 1.0


def test_load_wav_rejects_non_pcm16_mono(tmp_path):
    """A2. 非 16bit/单声道输入必须明确报错，而不是悄悄给出错数据。"""
    p = str(tmp_path / 'stereo.wav')
    with wave.open(p, 'wb') as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b'\x00\x01' * 2000)
    with pytest.raises(RuntimeError):
        P._load_wav_as_float32(p)


def test_asr_failure_is_reported_not_silent(monkeypatch, tmp_path):
    """B. 转写抛异常时，真实原因必须进当前任务的 warnings。"""
    from ffmpeg_utils import PROGRESS, _TLS
    rid = 'test-asr-warn'
    PROGRESS[rid] = {}
    monkeypatch.setattr(_TLS, 'runid', rid, raising=False)

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

    monkeypatch.setitem(sys.modules, 'faster_whisper',
                        type(sys)('faster_whisper'))
    sys.modules['faster_whisper'].WhisperModel = _Boom

    # 跳过 ffmpeg 解码这一步（用真wav 省时间），直接测异常路径
    def _fake_run(args, **kw):
        out = args[-1]
        _make_wav(out)
        return 0, '', ''
    monkeypatch.setattr(P, 'ffmpeg_run', _fake_run)
    monkeypatch.setattr(P, '_whisper_env_setup', lambda: None)
    monkeypatch.setattr(P, 'whisper_device', lambda: ('cpu', 'int8'))

    try:
        segs = P.asr_segments(str(tmp_path / 'x.mp4'), progress=PROGRESS[rid])
        warns = PROGRESS[rid].get('warnings') or []
        assert segs == []
        assert warns, 'ASR 失败必须留下 warning，不能静默返回空'
        assert any('metadata_errors' in w for w in warns), \
            'warning 里应能看到真实异常原因，实际: %s' % warns
    finally:
        PROGRESS.pop(rid, None)


def test_cuda_requires_runtime_not_just_driver(monkeypatch):
    """C. 有nvidia-smi 但无 CUDA 库时，必须判为不可用。"""
    monkeypatch.setitem(P._CUDA_CACHE, 'val', None)  # 清缓存
    monkeypatch.setattr(P, '_cuda_runtime_present', lambda: False)
    assert P._cuda_available() is False, \
        '缺 CUDA 运行时库时若仍判可用，GPU 推理必崩（cublas64_12.dll not found）'
    monkeypatch.setitem(P._CUDA_CACHE, 'val', None)


def test_cuda_runtime_present_is_pure_probe():
    """C2. 探测函数本身不抛异常（缺 ctranslate2 / 缺 dll 时返回 False）。"""
    try:
        v = P._cuda_runtime_present()
        assert isinstance(v, bool)
    except Exception as ex:  # pragma: no cover
        pytest.fail('CUDA 运行时探测不应抛异常: %s' % ex)
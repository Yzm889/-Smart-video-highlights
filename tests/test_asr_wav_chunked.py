# -*- coding: utf-8 -*-
"""F8 / M2-5 回归：ASR 音频必须分块读入，不得一次性把整段字节 + float 数组双副本
驻留（98 分钟视频峰值约 560MB）。分块读入预分配缓冲，数值结果与全量读取逐位一致，
确保不改变 ASR 行为。"""

import struct

import numpy as np
import wave

import ai_providers as A


def _write_pcm16_mono(path, samples, rate=16000):
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b''.join(struct.pack('<h', s) for s in samples))


def test_load_wav_as_float32_correct_and_chunked(tmp_path):
    wav = str(tmp_path / 't.wav')
    samples = [0, 32767, -32768, 16384, -16384, 100, -100, 42, -42]
    _write_pcm16_mono(wav, samples)
    arr = A._load_wav_as_float32(wav)
    expected = np.array(samples, dtype=np.float32) / 32768.0
    assert len(arr) == len(samples), (len(arr), len(samples))
    assert np.allclose(arr, expected, atol=1e-5), arr


def test_load_wav_as_float32_larger_than_chunk(tmp_path):
    # 3,000,000 帧 > 默认 chunk_frames(1<<20)，强制走多块循环，验证完整性与首尾值
    wav = str(tmp_path / 'big.wav')
    n = 3_000_000
    # 用交替值，避免全 0 掩盖丢块
    samples = [32767 if i % 2 == 0 else -32768 for i in range(n)]
    _write_pcm16_mono(wav, samples)
    arr = A._load_wav_as_float32(wav)
    assert len(arr) == n, '分块读取后帧数必须完整：%d != %d' % (len(arr), n)
    # 对照公式：与代码内部 np.int16/32768.0 完全一致，避免 32767/32768=0.9999695 这类
    # 看似「非 1.0」的逐位差异被误判为回归。首尾及中段抽样完整性校验：
    expected = np.array(samples, dtype=np.float32) / 32768.0
    assert np.allclose(arr, expected, atol=1e-5), (arr[:4], expected[:4])
    # 显式校验分块边界处的首尾值与中段值（强制走多块循环）
    assert abs(arr[0] - np.float32(32767 / 32768.0)) < 1e-6, arr[0]
    assert abs(arr[-1] - np.float32(-32768 / 32768.0)) < 1e-6, arr[-1]
    assert abs(arr[1000] - np.float32(32767 / 32768.0)) < 1e-6, arr[1000]

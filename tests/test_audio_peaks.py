# -*- coding: utf-8 -*-
"""配音波形（2026-11-06 计划#3）回归钉：峰值计算、缓存与边界。

钉住：
1. 正弦波有幅度、静音≈0、值域 [0,1]；
2. points 上下限钳制（8/1000）；
3. 缓存命中不再碰 ffmpeg；points 变化必须重算（缓存键含 points）；
4. 缓存盘文件按 mtime+points 落在 _peaks/ 下；
5. 文件不存在返回 (None, err)。
"""
import os

import pytest

import webui_server as S


def _mk_audio(path, kind='sine', dur=2.0):
    if kind == 'sine':
        # 注：lavfi 的 sine 源固定振幅 0.125（-18dB，ffmpeg 怪癖），要用 aevalsrc 才能出大振幅
        src = 'aevalsrc=0.9*sin(2*PI*440*t):d=%s' % dur
    elif kind == 'silence':
        src = 'anullsrc=duration=%s' % dur
    else:
        raise AssertionError('未知类型 ' + kind)
    rc, _o, e = S.ffmpeg_run(['-y', '-f', 'lavfi', '-i', src,
                              '-c:a', 'pcm_s16le', path], timeout=60)
    assert rc == 0, e.decode('utf-8', 'ignore')[-200:]


def test_peaks_sine_loud(tmp_path):
    p = str(tmp_path / 'tts_001.wav')
    _mk_audio(p, 'sine', 2.0)
    peaks, err = S._audio_peaks(p, points=40)
    assert peaks is not None, err
    assert len(peaks) == 40
    assert max(peaks) > 0.5, '正弦波必须有可感知幅度'
    assert all(0 <= v <= 1.0001 for v in peaks)


def test_peaks_silence_near_zero(tmp_path):
    p = str(tmp_path / 'tts_002.wav')
    _mk_audio(p, 'silence', 1.0)
    peaks, err = S._audio_peaks(p, points=16)
    assert peaks is not None, err
    assert max(peaks) <= 0.02, '静音的峰值必须≈0'


def test_peaks_points_clamped(tmp_path):
    p = str(tmp_path / 'tts_003.wav')
    _mk_audio(p, 'sine', 0.5)
    peaks, _err = S._audio_peaks(p, points=2)        # 下限钳到 8
    assert len(peaks) == 8
    peaks2, _err2 = S._audio_peaks(p, points=99999)  # 上限钳到 1000
    assert len(peaks2) == 1000


def test_peaks_cache_hit_and_recompute(tmp_path, monkeypatch):
    p = str(tmp_path / 'tts_004.wav')
    _mk_audio(p, 'sine', 1.0)
    first, err = S._audio_peaks(p, points=32)
    assert first is not None, err
    assert os.path.isfile(os.path.join(str(tmp_path), '_peaks', 'tts_004.wav.json'))

    def _boom(*a, **k):
        raise RuntimeError('不应再调 ffmpeg')
    monkeypatch.setattr(S, 'ffmpeg_run', _boom)
    second, err2 = S._audio_peaks(p, points=32)      # 命中缓存：不能碰 ffmpeg
    assert second == first
    with pytest.raises(RuntimeError):
        S._audio_peaks(p, points=33)                 # points 变了必须重算
    monkeypatch.undo()
    third, _e3 = S._audio_peaks(p, points=33)
    assert third is not None and len(third) == 33


def test_peaks_missing_file(tmp_path):
    peaks, err = S._audio_peaks(str(tmp_path / 'nope.wav'))
    assert peaks is None and err

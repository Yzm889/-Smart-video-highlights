# -*- coding: utf-8 -*-
"""分割片段（2026-11-06 计划#5）回归钉：音频切割。"""
import os

import webui_server as S


def _mk_sine(path, dur=4.0):
    rc, _o, e = S.ffmpeg_run(['-y', '-f', 'lavfi',
                              '-i', 'aevalsrc=0.9*sin(2*PI*440*t):d=%s' % dur,
                              '-c:a', 'pcm_s16le', path], timeout=60)
    assert rc == 0, e.decode('utf-8', 'ignore')[-200:]


def test_split_halves(tmp_path):
    p = str(tmp_path / 'narr0.wav')
    _mk_sine(p, 4.0)
    p1, p2, d1, d2, err = S._split_audio_file(p, 0.5)
    assert p1 and p2, err
    assert os.path.isfile(p1) and os.path.isfile(p2)
    assert abs(d1 - 2.0) < 0.2 and abs(d2 - 2.0) < 0.2, (d1, d2)
    assert abs((d1 + d2) - 4.0) < 0.3, '两段时长之和必须≈原时长'


def test_split_frac_clamped(tmp_path):
    p = str(tmp_path / 'narr1.wav')
    _mk_sine(p, 4.0)
    p1, p2, d1, d2, err = S._split_audio_file(p, 0.02)   # 钳到 0.1
    assert p1 and p2, err
    assert d1 < 0.6, 'frac=0.02 应钳到 0.1：d1=%s' % d1


def test_split_too_short(tmp_path):
    p = str(tmp_path / 'narr2.wav')
    _mk_sine(p, 0.5)
    p1, p2, d1, d2, err = S._split_audio_file(p, 0.5)
    assert p1 is None and '太短' in err


def test_split_missing_file(tmp_path):
    p1, p2, d1, d2, err = S._split_audio_file(str(tmp_path / 'nope.wav'), 0.5)
    assert p1 is None and err

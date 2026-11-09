# -*- coding: utf-8 -*-
"""v1.4.23 回归钉：解说语速。

用户 2026-11-09：「解说的语速好像不太正常，有点慢」。

实测：那批 37 段配音平均 **5.36 字/秒**（单句 5.97 秒念 32 字），
而影视解说普遍 6 字/秒以上。根因是参考音本身就慢——它是 VoiceDesign 按
「沉稳克制…句间有自然停顿」造出来的，6.4 秒只念 21 字（3.3 字/秒），
IndexTTS2 克隆时把这个节奏一起学走了。

修法：生成后按 rate 做保音高变速（ffmpeg atempo）。**不能用 duration_factor**
——本机这版 indextts 的 infer() 不吃这个参数，会被当成未知 model_kwargs
转给 HF generate 直接报错：
"The following model_kwargs are not used by the model: ['duration_factor']"。
"""
import os
import sys

import numpy as np
import pytest
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import tts_engines as T  # noqa: E402
import tts2_engines as T2  # noqa: E402


def test_rate_maps_to_speed():
    assert T2.rate_to_speed('+0%') == pytest.approx(1.12), '默认档要快 12%'
    assert T2.rate_to_speed('') == pytest.approx(1.12)
    assert T2.rate_to_speed(None) == pytest.approx(1.12)
    assert T2.rate_to_speed('+10%') == pytest.approx(1.10)
    assert T2.rate_to_speed('-10%') == pytest.approx(0.90)
    assert T2.rate_to_speed('乱码') == pytest.approx(1.12)
    # 越界夹紧，不许把口播拉到听不清
    assert T2.rate_to_speed('+300%') == 2.0
    assert T2.rate_to_speed('-90%') == 0.5


def _write_sine(path, sec=1.0, sr=24000):
    t = np.linspace(0, sec, int(sr * sec), endpoint=False)
    sf.write(path, (0.3 * np.sin(2 * np.pi * 220 * t)).astype('float32'), sr)


def test_apply_speed_changes_duration(tmp_path):
    p = str(tmp_path / 'a.wav')
    _write_sine(p, 1.0)
    T._apply_speed(p, 1.2)
    assert abs(sf.info(p).duration - 1 / 1.2) < 0.06


def test_apply_speed_noop_at_one(tmp_path):
    p = str(tmp_path / 'b.wav')
    _write_sine(p, 1.0)
    before = sf.info(p).duration
    T._apply_speed(p, 1.0)
    assert abs(sf.info(p).duration - before) < 1e-6


def test_apply_speed_survives_bad_file(tmp_path):
    """坏文件/不存在的文件绝不能把配音整条链路搞挂。"""
    p = str(tmp_path / 'bad.wav')
    open(p, 'wb').write(b'not a wav')
    assert T._apply_speed(p, 1.12) == p
    assert T._apply_speed(str(tmp_path / 'missing.wav'), 1.12).endswith('missing.wav')

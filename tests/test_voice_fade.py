# -*- coding: utf-8 -*-
"""每段配音淡入/淡出（2026-11-06 计划#6）回归钉。

钉住：
1. build_voice_track（解说 .wav）吃到 7 元组的 fade_in/fade_out——淡入段头部
   RMS 明显低于中段、淡出段尾部明显低于中段（用 astats 实测包络，不是只看参数透传）；
2. 旧 5 元组（无 fade）行为不变：头/中/尾 RMS 一致；
3. 淡入+淡出并存；过大的 fade 被钳到可用时长一半（不叠死）。
"""
import os
import re
import subprocess

import pytest

import webui_server  # noqa: F401  解除循环依赖
import video_render as vr
from ffmpeg_utils import ffmpeg_exe


def _rms_db(path, start, dur):
    """用 ffmpeg astats 量 [start, start+dur) 的 RMS (dB)。静音按 -120 计。"""
    exe = ffmpeg_exe()
    p = subprocess.run([exe, '-hide_banner', '-ss', str(start), '-t', str(dur),
                        '-i', path, '-af',
                        'astats=measure_perchannel=none:measure_overall=RMS_level',
                        '-f', 'null', '-'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    txt = p.stderr.decode('utf-8', 'ignore')
    m = re.findall(r'RMS level dB:\s*(-?[\d.]+|inf|-inf)', txt)
    assert m, 'astats 未输出 RMS：' + txt[-200:]
    v = m[-1]
    try:
        return float(v)
    except ValueError:
        return -120.0   # -inf = 数字静音


@pytest.fixture
def sine_wav(tmp_path):
    rc, _o, e = vr.ffmpeg_run(['-y', '-f', 'lavfi',
                               '-i', 'aevalsrc=0.9*sin(2*PI*440*t):d=3',
                               '-c:a', 'pcm_s16le', str(tmp_path / 'tts_1.wav')],
                              timeout=60)
    assert rc == 0, e.decode('utf-8', 'ignore')[-200:]
    return str(tmp_path / 'tts_1.wav')


def test_fade_in_and_out_envelope(tmp_path, sine_wav):
    out = vr.build_voice_track(
        [(sine_wav, 0.0, 3.0, None, None, 1.0, 1.0)],
        str(tmp_path), 3.0, out_name='fade_io.wav')
    assert out and os.path.isfile(out)
    head = _rms_db(out, 0.05, 0.15)      # 淡入起步（避开绝对 0 点的 -inf）
    mid = _rms_db(out, 1.2, 0.2)
    tail = _rms_db(out, 2.6, 0.15)       # 淡出收尾
    assert head < mid - 8, '淡入段头部必须明显更轻: head=%.1f mid=%.1f' % (head, mid)
    assert tail < mid - 8, '淡出段尾部必须明显更轻: tail=%.1f mid=%.1f' % (tail, mid)


def test_fade_in_only(tmp_path, sine_wav):
    out = vr.build_voice_track(
        [(sine_wav, 0.0, 3.0, None, None, 0.8, 0.0)],
        str(tmp_path), 3.0, out_name='fade_in.wav')
    assert out and os.path.isfile(out)
    head = _rms_db(out, 0.05, 0.15)
    mid = _rms_db(out, 1.2, 0.2)
    tail = _rms_db(out, 2.7, 0.2)
    assert head < mid - 8, '头部必须更轻: %.1f vs %.1f' % (head, mid)
    assert tail > mid - 3, '无淡出时尾部必须保持满幅: %.1f vs %.1f' % (tail, mid)


def test_legacy_tuple_no_fade(tmp_path, sine_wav):
    """旧 5 元组：不能被误加包络（头/中/尾一致）。"""
    out = vr.build_voice_track(
        [(sine_wav, 0.0, 3.0, None, None)],
        str(tmp_path), 3.0, out_name='no_fade.wav')
    assert out and os.path.isfile(out)
    head = _rms_db(out, 0.05, 0.15)
    mid = _rms_db(out, 1.2, 0.2)
    tail = _rms_db(out, 2.7, 0.2)
    assert head > mid - 3, '无淡入时头部必须满幅: %.1f vs %.1f' % (head, mid)
    assert tail > mid - 3, '无淡出时尾部必须满幅: %.1f vs %.1f' % (tail, mid)


def test_fade_clamped_to_half_duration(tmp_path, sine_wav):
    """fade=3s 但音频只有 3s：钳到 1.5s（中点开始有声，尾点接近无声但不越界）。"""
    out = vr.build_voice_track(
        [(sine_wav, 0.0, 3.0, None, None, 3.0, 3.0)],
        str(tmp_path), 3.0, out_name='fade_clamp.wav')
    assert out and os.path.isfile(out)
    mid = _rms_db(out, 1.5, 0.15)
    head = _rms_db(out, 0.15, 0.15)
    assert head < mid - 6, '淡入钳到 1.5s 后 0.15s 处仍应更轻: %.1f vs %.1f' % (head, mid)

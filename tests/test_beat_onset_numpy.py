# -*- coding: utf-8 -*-
"""v1.4.82 配音包内置 onset/节拍分析回归钉（ffmpeg+numpy 替代 librosa）。

背景：librosa 退出打包清单（其 numba 依赖破坏 pythonnet CLR 加载，v1.4.80-b
事故），而「智能强卡点」「detect_beats」「节拍同步」三处都靠它——打包版里
这些功能一直是静默降级的。本轮用 ffmpeg 解码 + numpy 包络/挑峰/自相关 BPM
补上，拍点位置用合成音轨的**已知地面真值**验证。
"""

import os
import sys
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402  必须先导宿主（beat_analysis 尾部回注会触发 _host）
import beat_analysis as B  # noqa: E402

SR = 22050


def _write_wav(path, y):
    """float [-1,1] → 16-bit mono wav（标准库 wave，无第三方写盘依赖）。"""
    pcm = np.clip(y, -1.0, 1.0)
    pcm = (pcm * 32767).astype('<i2')
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def _click_track(seconds=30.0, period=0.5, burst=0.04, freq=880.0, amp=0.8):
    """合成节拍音轨：每 period 秒一个 burst（拍点位置 = k*period，地面真值）。"""
    n = int(seconds * SR)
    y = np.zeros(n, dtype=np.float32)
    bn = int(burst * SR)
    t = np.arange(bn) / SR
    click = (amp * np.sin(2 * np.pi * freq * t) *
             np.exp(-t * 60.0)).astype(np.float32)   # 快速衰减，模拟打击瞬态
    k = 0
    while True:
        s = int(round(k * period * SR))
        if s + bn > n:
            break
        y[s:s + bn] += click
        k += 1
    return y, k


def test_onset_peaks_land_on_known_clicks(tmp_path):
    """峰值必须落在已知拍点附近（±0.1s），覆盖率 ≥80%，总时长正确。"""
    y, n_clicks = _click_track(30.0, period=0.5)
    p = tmp_path / 'clicks.wav'
    _write_wav(p, y)
    pts, vals, T = B._music_onset_peaks(str(p), delta=0.18, wait=0.25)
    assert T > 29.5, '总时长 %.2f 不对' % T
    assert vals and all(v > 0 for v in vals), '强度列表必须非空且为正'
    expected = [i * 0.5 for i in range(n_clicks)]
    hit = sum(1 for e in expected
              if any(abs(pt - e) <= 0.10 for pt in pts))
    assert hit >= int(0.8 * n_clicks), \
        '拍点命中 %d/%d（峰值数 %d）' % (hit, n_clicks, len(pts))
    for pt in pts:
        assert any(abs(pt - e) <= 0.10 for e in expected), \
            '峰值 %.2fs 不在任何拍点附近（误检）' % pt


def test_detect_strong_beats_estimates_bps(tmp_path):
    """detect_strong_beats 的每秒拍数估计 ≈ 2.0（0.5s 周期）。"""
    y, _ = _click_track(30.0, period=0.5)
    p = tmp_path / 'clicks.wav'
    _write_wav(p, y)
    pts, bps = B.detect_strong_beats(str(p), min_sep=0.25)
    assert pts, '强拍列表为空'
    assert bps is not None and abs(bps - 2.0) < 0.4, 'bps=%.3f 偏离 2.0' % bps


def test_analyze_beats_grid_bpm_and_phase(tmp_path):
    """自相关 BPM ≈ 120，节拍网格与真实拍点对齐（中位间距 ≈0.5s、起点贴拍）。"""
    y, n_clicks = _click_track(30.0, period=0.5)
    p = tmp_path / 'clicks.wav'
    _write_wav(p, y)
    r = B._analyze_beats_grid(str(p))
    assert r is not None, '节拍网格分析失败'
    bpm, beats = r
    assert abs(bpm - 120.0) < 10.0, 'BPM=%.1f 偏离 120' % bpm
    assert len(beats) >= 40, '节拍数过少: %d' % len(beats)
    gaps = np.diff(beats)
    assert abs(float(np.median(gaps)) - 0.5) < 0.06, \
        '网格间距中位数 %.3f 偏离 0.5' % np.median(gaps)
    # 对齐判据：网格点必须贴着某个拍点（相位可从周期中任意处起，
    # 但网格点要落在拍点上/紧邻拍点，绝不许落在拍点之间）。
    # 尾部例外：网格会延伸过最后一个拍点（音轨尾部留白处没有拍点可贴），
    # 对齐判据只对「最后一个拍点之前」的网格点生效。
    expected = [i * 0.5 for i in range(n_clicks)]
    last_click = expected[-1]
    d = [min(abs(b - e) for e in expected)
         for b in beats if b <= last_click + 0.1]
    assert d, '拍点区间内没有任何网格点'
    assert float(np.median(d)) <= 0.10, '网格点没贴上拍点: %.3f' % np.median(d)
    assert max(d) <= 0.15, '有网格点落在拍点之间: %.3f' % max(d)


def test_silence_degrades_gracefully(tmp_path):
    """静音：onset 无峰（T 仍是真实时长）；网格分析 → None（各自优雅降级）。"""
    p = tmp_path / 'silence.wav'
    _write_wav(p, np.zeros(int(SR * 12), dtype=np.float32))
    pts, vals, T = B._music_onset_peaks(str(p))
    assert pts == [] and vals == [] and T > 11, '静音应有时长无峰: %r' % ((pts, T),)
    assert B._analyze_beats_grid(str(p)) is None


def test_detect_beats_respects_sensitivity_scale(tmp_path):
    """detect_beats 的 wait 已统一为秒：低灵敏度（0.2）下拍间隔约束仍合理。

    旧实现 wait=15 是「帧」，新契约统一为秒——这里顺带钉住换算没修错。"""
    y, _ = _click_track(30.0, period=0.5)
    p = tmp_path / 'clicks.wav'
    _write_wav(p, y)
    beats = B.detect_beats(str(p), sensitivity=0.5)
    assert beats, 'detect_beats 空结果'
    assert len(beats) >= 30, '灵敏度 0.5 下检出过少: %d' % len(beats)


def test_webui_analyze_beats_integration(tmp_path):
    """宿主命名空间集成：workflows 经 _w.analyze_beats 拿到新实现，契约不变。"""
    y, _ = _click_track(30.0, period=0.5)
    p = tmp_path / 'clicks.wav'
    _write_wav(p, y)
    r = S.analyze_beats(str(p))
    assert r is not None and len(r) == 2
    bpm, beats = r
    assert abs(bpm - 120.0) < 10.0 and beats


def test_librosa_fully_out_of_packaged_path():
    """源码钉：卡点/节拍链路不得再 import librosa（它进不了打包清单）。"""
    with open(os.path.join(ROOT, 'beat_analysis.py'), encoding='utf-8') as f:
        ba = f.read()
    assert 'import librosa' not in ba, 'beat_analysis 仍依赖 librosa'
    with open(os.path.join(ROOT, 'webui_server.py'), encoding='utf-8') as f:
        ws = f.read()
    i = ws.index('def analyze_beats(')
    body = ws[i:ws.index('\ndef ', i + 10)]
    assert 'import librosa' not in body, 'analyze_beats 仍引用 librosa'
    assert 'beat_analysis._analyze_beats_grid(path)' in body

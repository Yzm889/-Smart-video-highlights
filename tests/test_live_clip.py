# -*- coding: utf-8 -*-
"""直播切片引擎单元回归（live_clip.py）。

锁定：相对化评分（分位次对绝对音量免疫）、选段策略（取峰-扩边-抑制-合并-取N）、
可读理由、无音轨/异常容错、导出命名与报告结构。
E2E（真实直播录像全管线跑通）由真机验收完成，不入库（禁止提交视频产物）。
"""
import json
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import live_clip as LC  # noqa: E402


# ---------------------------------------------------------------------------
# 相对化：分位次
# ---------------------------------------------------------------------------
def test_pct_rank_is_relative_not_absolute():
    """音量整体放大 10 倍，分位次曲线完全不变——对不同响度/编码的录像免疫。"""
    x = np.array([0.1, 0.5, 0.9, 0.3, 0.7, 0.2, 0.8, 0.4])
    r1 = LC.pct_rank(x)
    r2 = LC.pct_rank(x * 10.0)
    assert np.allclose(r1, r2)
    assert abs(r1.max() - 1.0) < 1e-9 and abs(r1.min()) < 1e-9


def test_pct_rank_constant_signal_is_neutral():
    """全场一样响（静音或恒定噪声）→ 全 0.5，不得把任何窗口判成峰值。"""
    r = LC.pct_rank(np.full(50, 0.3))
    assert np.allclose(r, 0.5)


def test_pct_rank_empty():
    assert LC.pct_rank(np.zeros(0)).size == 0


# ---------------------------------------------------------------------------
# 选段策略
# ---------------------------------------------------------------------------
def _mk_score(peaks, nwin=600, width=6, base=0.3):
    """构造带指定峰的能量曲线（每个峰 (中心窗, 高度)）。"""
    s = np.full(nwin, base)
    for c, h in peaks:
        lo, hi = max(0, c - width), min(nwin, c + width + 1)
        s[lo:hi] = np.maximum(s[lo:hi], h)
    return s


def test_select_clips_picks_top_peaks_and_respects_n():
    """两个高峰一个矮峰，要 2 条 → 拿到两条高峰，矮峰出局。"""
    s = _mk_score([(100, 0.95), (400, 0.90), (300, 0.55)])
    clips = LC.select_clips(s, window=2.0, vdur=1200.0, n_clips=2,
                            min_sec=20, max_sec=60, min_gap=45)
    assert len(clips) == 2
    centers = sorted((c['start'] + c['end']) / 2 for c in clips)
    assert any(abs(c - 202.0) < 40 for c in centers), '峰 100×2s 必须被覆盖'
    assert any(abs(c - 802.0) < 40 for c in centers), '峰 400×2s 必须被覆盖'


def test_select_clips_enforces_min_gap():
    """相邻峰间隔小于 min_gap → 不得切出两条贴脸切片。"""
    s = _mk_score([(100, 0.95), (115, 0.93)])   # 相距 30s < min_gap 45s
    clips = LC.select_clips(s, window=2.0, vdur=600.0, n_clips=5,
                            min_sec=20, max_sec=60, min_gap=45)
    assert len(clips) == 1, '贴脸峰必须合并/抑制成一条'
    for a, b in zip(clips, clips[1:]):
        assert b['start'] - a['end'] >= 0


def test_select_clips_clamps_length():
    """扩边有 [min_sec, max_sec] 钳制：窄峰不短于最短，长峰不长于最长。"""
    s = _mk_score([(300, 0.99)], nwin=1200)
    clips = LC.select_clips(s, window=2.0, vdur=2400.0, n_clips=1,
                            min_sec=20, max_sec=60, min_gap=45)
    assert clips, '高峰必须选出段'
    dur = clips[0]['end'] - clips[0]['start']
    assert 20.0 <= dur <= 60.0 + 2.0 * 2.0, '时长必须被钳制（±一个窗口容差）'


def test_select_clips_empty_and_flat_input():
    """全零曲线 → 一条都不出；空数组 → 不炸。"""
    assert LC.select_clips(np.zeros(100), 2.0, 200.0, n_clips=3,
                           min_sec=20, max_sec=60, min_gap=45) == []
    assert LC.select_clips(np.zeros(0), 2.0, 0.0) == []


# ---------------------------------------------------------------------------
# 理由
# ---------------------------------------------------------------------------
def test_reasons_are_readable_and_capped():
    """强信号才写理由（全片前 X%），最多三条；没有强信号给兜底话术。"""
    audio = np.full(100, 0.5)
    audio[95] = 1.0
    ranks = [LC.pct_rank(audio), None, None]
    rs = LC._reasons(95, ranks, None)
    assert rs and all(isinstance(x, str) for x in rs)
    assert any('音量' in x for x in rs)
    assert len(rs) <= 3
    weak = LC._reasons(5, ranks, None)      # 低位窗口：不该吹成峰值
    assert weak == ['综合能量处于全片前列']


def test_reasons_mentions_keywords():
    audio = np.full(100, 0.5)
    ranks = [LC.pct_rank(audio), None, None]
    kw = np.zeros(100)
    kw[50] = 2.0
    rs = LC._reasons(50, ranks, kw)
    assert any('高能词' in x for x in rs)


# ---------------------------------------------------------------------------
# 信号剖面
# ---------------------------------------------------------------------------
def test_rms_profile_windows_and_units():
    rate = 8000
    win = 2.0
    pcm = np.zeros(8000 * 10, dtype=np.int16)          # 10 秒静音
    pcm[16000:32000] = 16384                           # 第 2~4 秒满幅方波
    prof = LC._rms_profile(pcm, rate, win)
    assert prof.size == 5                              # 10s / 2s → 5 个完整窗口
    assert prof[1] > 0.4 and prof[0] < 0.01            # 第二窗响、第一窗静


def test_speech_profile_counts_coverage_and_keywords():
    asr = [{'start': 1.0, 'end': 5.0, 'text': '哈哈哈也太好笑了'},
           {'start': 8.0, 'end': 9.0, 'text': '卧槽666'}]
    density, kw = LC._speech_profile(asr, nwin=10, window=1.0)
    assert density[2] == 1.0 and density[4] == 1.0     # 1~5s 覆盖窗 1..4
    assert density[8] == 1.0
    assert kw[1] >= 1 and kw[8] >= 1                   # 高能词计数落在对应窗


def test_motion_profile_aggregates_frame_signals(monkeypatch):
    """帧级信号按窗口聚合：每窗取均值。"""
    import beat_analysis as BA
    fake = {'times': [0.25, 0.75, 2.25, 2.75],
            'motion': [1.0, 3.0, 0.0, 0.0],
            'frac': [0, 0, 0, 0], 'hist': [0, 0, 0, 0], 'bright': [0, 0, 0, 0]}
    monkeypatch.setattr(BA, '_analyze_video_frames', lambda *a, **k: fake)
    prof, vdur = LC._motion_profile('fake.mp4', 1.0)
    assert prof is not None and prof.size >= 3
    assert abs(prof[0] - 2.0) < 1e-6                   # 窗 0：两帧均值 (1+3)/2
    assert abs(prof[2] - 0.0) < 1e-6                   # 窗 2：静止


# ---------------------------------------------------------------------------
# 探测与容错
# ---------------------------------------------------------------------------
def test_probe_duration_parses_ffmpeg_stderr(monkeypatch):
    err = (b'Input #0, matroska, from x:\n'
           b'  Duration: 00:20:15.50, start: 0.000000, bitrate: 8000 kb/s\n'
           b'  Stream #0:0: Video: h264 ...\n'
           b'  Stream #0:1: Audio: aac, 44100 Hz\n').replace(b'\n', b'\r\n')
    monkeypatch.setattr(LC, 'ffmpeg_run', lambda *a, **k: (1, b'', err))
    vdur, has_audio = LC._probe_duration('x.mkv')
    assert abs(vdur - (20 * 60 + 15.5)) < 0.01
    assert has_audio


def test_analyze_rejects_no_audio_video(monkeypatch):
    monkeypatch.setattr(LC, '_probe_duration', lambda p: (1200.0, False))
    with pytest.raises(RuntimeError, match='没有音轨'):
        LC.analyze_highlights('x.mp4', run_dir='.', use_asr=False)


def test_analyze_rejects_zero_duration(monkeypatch):
    monkeypatch.setattr(LC, '_probe_duration', lambda p: (0.0, True))
    with pytest.raises(RuntimeError, match='时长'):
        LC.analyze_highlights('x.mp4', run_dir='.', use_asr=False)


def test_analyze_synthetic_pipeline_end_to_end(monkeypatch, tmp_path):
    """合成信号走通 analyze 全流程：音量峰 + 台词窗对齐 → 选段带理由。"""
    win = 2.0
    vdur = 300.0
    nwin = int(vdur / win)
    pcm_rate = 8000
    pcm = (np.random.default_rng(7).standard_normal(nwin * int(pcm_rate * win))
           * 800).astype(np.int16)
    # 在 100~110s 与 200~212s 造两个响度峰
    pcm[int(100 * pcm_rate):int(110 * pcm_rate)] = 20000
    pcm[int(200 * pcm_rate):int(212 * pcm_rate)] = 20000
    monkeypatch.setattr(LC, '_probe_duration', lambda p: (vdur, True))
    monkeypatch.setattr(LC, '_load_pcm', lambda *a, **k: (pcm, pcm_rate))
    monkeypatch.setattr(LC, '_motion_profile', lambda *a, **k: (None, 0.0))
    asr = [{'start': 101.0, 'end': 109.0, 'text': '卧槽这波操作太强了真的绝了'},
           {'start': 201.0, 'end': 211.0, 'text': '哈哈哈笑死我了名场面'}]
    monkeypatch.setattr(LC, 'asr_segments', lambda p: asr)
    res = LC.analyze_highlights('fake.mp4', run_dir=str(tmp_path),
                                n_clips=2, min_sec=20, max_sec=60, use_asr=True)
    assert res['asr_used'] is True and res['clips']
    centers = [c['peak_t'] for c in res['clips']]
    assert any(abs(c - 105.0) < 25 for c in centers), '峰 100~110s 必须命中'
    assert any(abs(c - 206.0) < 25 for c in centers), '峰 200~212s 必须命中'
    for c in res['clips']:
        assert c['reasons'] and c['dur'] >= 20.0 - 2.0 * win


# ---------------------------------------------------------------------------
# 导出命名与报告
# ---------------------------------------------------------------------------
def test_clip_name_is_ascii_and_encodes_start():
    n = LC._clip_name(3, 3723.5)
    assert n == 'clip03_01h02m03s.mp4'
    assert all(ord(ch) < 128 for ch in n), '/media/ 路由不做 URL 解码，文件名必须 ASCII'


def test_write_report_files_and_structure(tmp_path):
    meta = {'video': r'C:\rec\live.mkv', 'vdur': 1200.0, 'window': 2.0, 'asr_used': True}
    clips = [{'start': 100.0, 'end': 150.0, 'dur': 50.0, 'score': 0.91,
              'peak_t': 120.0, 'reasons': ['音量峰值（全片前5%）']},
             {'start': 600.0, 'end': 650.0, 'dur': 50.0, 'score': 0.85,
              'peak_t': 620.0, 'reasons': ['台词密度峰值（全片前8%）']}]
    files = [os.path.join(str(tmp_path), 'clip01_00h01m40s.mp4'),
             os.path.join(str(tmp_path), 'clip02_00h10m00s.mp4')]
    jp, tp = LC.write_report(meta, clips, files, str(tmp_path))
    assert jp == 'live_clip_report.json' and tp == 'live_clip_report.txt'
    data = json.loads(open(os.path.join(str(tmp_path), jp), encoding='utf-8').read())
    assert data['video'] == 'live.mkv' and len(data['clips']) == 2
    assert data['clips'][0]['file'] == 'clip01_00h01m40s.mp4'
    assert '音量峰值' in data['clips'][0]['reasons'][0]
    txt = open(os.path.join(str(tmp_path), tp), encoding='utf-8').read()
    assert '直播切片报告' in txt and '2 条切片' in txt and '第100秒' in txt


def test_enabled_switch():
    """LIVE_CLIP=0 → 总开关关闭（回滚开关必须真的能关）。"""
    os.environ['LIVE_CLIP'] = '0'
    try:
        assert LC.enabled() is False
    finally:
        os.environ.pop('LIVE_CLIP', None)
    assert LC.enabled() is True

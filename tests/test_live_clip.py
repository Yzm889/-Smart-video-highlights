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
    monkeypatch.setattr(LC, 'asr_segments', lambda p, language=None: asr)
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
    # 源片路径故意带 Windows 反斜杠：报告里的 video 字段必须只剩文件名
    # （write_report 会先归一化 \ 为 / 再 basename；CI 的 Linux 上曾因此红过）
    meta = {'video': 'C:\\rec\\live.mkv', 'vdur': 1200.0, 'window': 2.0, 'asr_used': True}
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


# ---------------------------------------------------------------------------
# 竖屏画幅 / 自动封面 / 语言固定（2026-11-11 主人拍板补齐，直发抖音）
# ---------------------------------------------------------------------------
def test_vf_chain_aspects():
    """三种画幅的滤镜链：orig 原样、crop 居中裁 9:16、blur 虚化背景板；
    HDR tonemap 链永远排在最前（先调色再构图形）。"""
    orig = LC._vf_chain('orig', '')
    assert orig == 'setpts=(PTS-STARTPTS)'
    assert 'crop=' not in orig and 'gblur' not in orig

    crop = LC._vf_chain('crop', '')
    assert "min(iw\\,ih*9/16)" in crop, '居中裁切必须按 9:16 计算裁宽'
    assert 'scale=720:1280' in crop, '竖屏输出统一 720x1280（短视频标准）'

    blur = LC._vf_chain('blur', '')
    assert 'gblur=sigma=24' in blur and 'overlay=(W-w)/2:(H-h)/2' in blur
    assert '[bg];' in blur and '[fg];' in blur, '全景+虚化 = 背景/前景两层叠加'
    assert blur.endswith('[vout]'), 'filtergraph 末尾必须标 [vout] 供 -map 取轨'

    crop_tm = LC._vf_chain('crop', 'zscale=...tonemap...')
    assert crop_tm.index('tonemap') < crop_tm.index('crop='), '调色必须在构图形之前'


def test_write_report_includes_covers_and_aspect(tmp_path):
    meta = {'video': 'live.mkv', 'vdur': 600.0, 'window': 2.0, 'asr_used': True}
    clips = [{'start': 100.0, 'end': 150.0, 'dur': 50.0, 'score': 0.9,
              'peak_t': 120.0, 'reasons': ['音量峰值（全片前3%）']}]
    files = [os.path.join(str(tmp_path), 'clip01_00h01m40s.mp4')]
    covers = [os.path.join(str(tmp_path), 'cover01_00h01m40s.jpg')]
    jp, tp = LC.write_report(meta, clips, files, str(tmp_path),
                             covers=covers, aspect='blur')
    data = json.loads(open(os.path.join(str(tmp_path), jp), encoding='utf-8').read())
    assert data['clips'][0]['cover'] == 'cover01_00h01m40s.jpg'
    assert '虚化' in data['aspect']
    txt = open(os.path.join(str(tmp_path), tp), encoding='utf-8').read()
    assert '封面' in txt


def test_make_covers_picks_and_saves(monkeypatch, tmp_path):
    """封面挑选：候选帧 → 打分 → 最佳帧落 ASCII 文件名；候选目录收尾清理。"""
    import types
    from PIL import Image
    import io as _io

    made = {}

    def fake_seq(video_path, cand_dir, ts_list, max_side=640, only=None):
        os.makedirs(cand_dir, exist_ok=True)
        rng = np.random.default_rng(k_seed)
        for k in range(len(ts_list)):
            # 带噪声的渐变帧（纯色 JPEG <1KB 会被防垃圾帧下限拦掉，不是真实帧形态）
            base = np.full((90, 160, 3), (20, 30, 40) if k != 1 else (200, 205, 215),
                           dtype=np.uint8)
            im = Image.fromarray(base + rng.integers(0, 60, (90, 160, 3), dtype=np.uint8))
            buf = _io.BytesIO()
            im.save(buf, 'JPEG')
            with open(os.path.join(cand_dir, 'cand_%02d.jpg' % k), 'wb') as f:
                f.write(buf.getvalue())
        made['ts'] = list(ts_list)

    fake_host = types.SimpleNamespace(
        _cover_candidates_sequential=fake_seq,
        _cover_score=lambda im: round(float(np.asarray(im.convert('L')).std()), 1))
    monkeypatch.setattr(LC, '_host', lambda: fake_host)
    k_seed = 11

    clips = [{'start': 100.0, 'end': 150.0, 'peak_t': 120.0, 'dur': 50.0}]
    covers = LC.make_covers('fake.mp4', clips, str(tmp_path), aspect='blur')
    assert len(covers) == 1 and covers[0], '至少产出一张封面'
    assert os.path.basename(covers[0]) == 'cover01_00h01m40s.jpg'
    assert all(ord(ch) < 128 for ch in os.path.basename(covers[0]))
    assert os.path.getsize(covers[0]) > 1024
    # 竖屏画幅时封面同步裁 9:16（源 160x90 → 宽 = 90*9/16 = 50）
    im = Image.open(covers[0])
    assert im.size == (50, 90), '竖屏封面必须居中裁成 9:16，实际 %s' % (im.size,)
    assert not os.path.exists(os.path.join(str(tmp_path), '_covers')), '候选目录必须清理'
    assert made['ts'][0] == 120.0, '峰值时刻必须进候选'


def test_analyze_passes_language_to_asr(monkeypatch, tmp_path):
    """语言固定：asr_language 逐次传给 asr_segments（不传/传 auto = 走全局配置）。"""
    seen = {}
    nwin = 150
    pcm = (np.random.default_rng(3).standard_normal(nwin * 16000) * 500).astype(np.int16)
    monkeypatch.setattr(LC, '_probe_duration', lambda p: (300.0, True))
    monkeypatch.setattr(LC, '_load_pcm', lambda *a, **k: (pcm, 8000))
    monkeypatch.setattr(LC, '_motion_profile', lambda *a, **k: (None, 0.0))

    def fake_asr(path, language=None):
        seen['language'] = language
        return [{'start': 40.0, 'end': 60.0, 'text': '哈哈哈'}]

    monkeypatch.setattr(LC, 'asr_segments', fake_asr)
    LC.analyze_highlights('x.mp4', run_dir=str(tmp_path), n_clips=1,
                          min_sec=20, max_sec=60, use_asr=True, asr_language='zh')
    assert seen['language'] == 'zh', '固定语言必须逐次穿透到 asr_segments'
    LC.analyze_highlights('x.mp4', run_dir=str(tmp_path), n_clips=1,
                          min_sec=20, max_sec=60, use_asr=True, asr_language=None)
    assert seen['language'] is None, '不指定 = 交给全局配置'


# ---------------------------------------------------------------------------
# 半手动路线（两阶段：①分析落草稿 → ②按用户调整后的清单导出）
# ---------------------------------------------------------------------------
def test_validate_segments_clamps_sorts_drops():
    """用户输入的段落数据不可信：钳制边界、丢短段、按时间排序、截超长段。"""
    segs = [
        {'start': 'abc', 'end': 50},          # 非数字 → 丢弃
        {'start': 700, 'end': 720},           # 超出时长 → 钳制后 <3s → 丢弃
        {'start': 200, 'end': 210.5},         # 合法
        {'start': 100, 'end': 105},           # 合法（乱序输入）
        {'start': -30, 'end': 50},            # 负起点 → 钳到 0
        {'start': 300, 'end': 2000},          # 超长 → 截到 600s
    ]
    out = LC.validate_segments(segs, vdur=600.0, min_sec=3.0, max_sec=600.0)
    assert [(c['start'], c['end']) for c in out] == [(0.0, 50.0), (100.0, 105.0), (200.0, 210.5), (300.0, 600.0)]
    assert all(c['dur'] >= 3.0 for c in out)
    assert out == sorted(out, key=lambda c: c['start'])
    assert LC.validate_segments([], 600.0) == []
    assert LC.validate_segments([{'start': 0, 'end': 1}], 600.0, min_sec=3.0) == []


def test_carry_reasons_inherits_adjusts_or_marks_manual():
    draft = [{'start': 100.0, 'end': 150.0, 'score': 0.9, 'reasons': ['音量峰值（全片前3%）']},
             {'start': 600.0, 'end': 650.0, 'score': 0.8, 'reasons': ['台词密度峰值（全片前8%）']}]
    # 原样保留 → 原样继承
    r = LC.carry_reasons([{'start': 100.0, 'end': 150.0, 'dur': 50.0}], draft)
    assert r[0]['score'] == 0.9 and r[0]['reasons'] == ['音量峰值（全片前3%）']
    # 改了边界 → 继承理由 + 标注 + 分数微降
    r = LC.carry_reasons([{'start': 105.0, 'end': 160.0, 'dur': 55.0}], draft)
    assert r[0]['score'] == round(0.9 * 0.9, 3)
    assert any('边界已手动调整' in x for x in r[0]['reasons'])
    # 完全对不上 → 手动指定
    r = LC.carry_reasons([{'start': 400.0, 'end': 430.0, 'dur': 30.0}], draft)
    assert r[0]['score'] == '' and r[0]['reasons'] == ['手动指定']


def test_dispatch_export_reads_draft_and_exports(monkeypatch, tmp_path):
    """阶段②全流程（引擎 mock）：读草稿 → 校验/继承理由 → 导出 → 封面 → 报告。"""
    import webui_server as S
    import workflows as W
    run_dir = os.path.join(str(S.OUTDIR), 'live-draft-test')
    os.makedirs(run_dir, exist_ok=True)
    fake_src = os.path.join(run_dir, 'src.mp4')
    with open(fake_src, 'wb') as f:
        f.write(b'0' * 1024)
    draft = {'video_src': fake_src, 'vdur': 600.0, 'window': 2.0, 'asr_used': True,
             'aspect_default': 'blur',
             'clips': [{'start': 100.0, 'end': 150.0, 'score': 0.9, 'peak_t': 120.0,
                        'reasons': ['音量峰值（全片前3%）']}]}
    import json as _j
    with open(os.path.join(run_dir, 'live_clip_draft.json'), 'w', encoding='utf-8') as f:
        _j.dump(draft, f, ensure_ascii=False)

    def fake_export(video, clips, rdir, progress=None, aspect='orig'):
        outs = []
        for i, c in enumerate(clips):
            fp = os.path.join(rdir, LC._clip_name(i + 1, c['start']))
            with open(fp, 'wb') as f2:
                f2.write(b'0' * 20480)
            outs.append(fp)
        return outs

    def fake_covers(video, clips, rdir, progress=None, aspect='orig'):
        outs = []
        for i, c in enumerate(clips):
            fp = os.path.join(rdir, LC._cover_name(i + 1, c['start']))
            with open(fp, 'wb') as f2:
                f2.write(b'0' * 4096)
            outs.append(fp)
        return outs

    monkeypatch.setattr(LC, 'export_clips', fake_export)
    monkeypatch.setattr(LC, 'make_covers', fake_covers)
    prog = {'phase': '', 'pct': 0, 'done': False, 'run_dir': run_dir}
    # 用户调整：候选①改了边界；另手动圈了一段
    req = {'run_dir': 'live-draft-test',
           'segments': [{'start': 105.0, 'end': 160.0}, {'start': 400.0, 'end': 430.0}],
           'aspect': 'blur'}
    W.dispatch_live_clip_export(req, prog)
    assert prog.get('done') is True, prog.get('error')
    assert len(prog['clips']) == 2
    assert prog['clips'][0]['rel'].startswith('live-draft-test/')
    assert any('边界已手动调整' in x for x in prog['clips'][0]['reasons'])
    assert prog['clips'][1]['reasons'] == ['手动指定']
    assert os.path.exists(os.path.join(run_dir, 'live_clip_report.json'))
    assert os.path.exists(os.path.join(run_dir, 'live_clip_report.txt'))


def test_dispatch_export_without_draft_fails_cleanly(monkeypatch, tmp_path):
    import workflows as W
    prog = {'phase': '', 'pct': 0, 'done': False, 'run_dir': ''}
    W.dispatch_live_clip_export({'run_dir': 'no-such-dir'}, prog)
    assert prog.get('done') is True and prog.get('error'), '缺草稿必须报错而不是静默成功'
    assert '草稿' in prog['error'] or '分析' in prog['error']


def test_rms_profile_chunked_matches_naive():
    """分块 RMS 必须与一次性算法逐窗一致——这是评测修复的正确性钉子。
    （内存收益不进单测：4 小时合成 PCM 在真机上实测 RSS 增长 0MB，
    旧一次性实现要瞬吃 ~700MB float32。）"""
    rng = np.random.default_rng(5)
    pcm = (rng.standard_normal(8000 * 600) * 3000).astype(np.int16)   # 10 分钟
    chunked = LC._rms_profile(pcm, 8000, 2.0)
    win = 16000
    x = pcm[:len(pcm) // win * win].reshape(-1, win).astype(np.float32) / 32768.0
    naive = np.sqrt((x ** 2).mean(axis=1))
    assert np.allclose(chunked, naive, atol=1e-6)


def test_select_clips_merges_never_exceed_max_sec():
    """贴脸高光合并后不得超出用户设定的单条最长时长。
    （90min 实测：两段贴脸峰合并出 124s > max_sec=60，违反「单条最长」承诺。）"""
    s = _mk_score([(100, 0.95), (118, 0.93), (131, 0.92)], nwin=1200)  # 三连贴脸峰
    clips = LC.select_clips(s, window=2.0, vdur=2400.0, n_clips=5,
                            min_sec=20, max_sec=60, min_gap=45)
    assert clips, '高光必须选出'
    for c in clips:
        assert c['end'] - c['start'] <= 60.0 + 2.0 * 2.0, (
            '合并段 %.1fs 超出 max_sec=60（±一窗容差）' % (c['end'] - c['start']))
        assert c['start'] >= 0 and c['end'] <= 2400.0

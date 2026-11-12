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

    def fake_export(video, clips, rdir, progress=None, aspect='orig', focus=None,
                    titles=None):
        outs = []
        for i, c in enumerate(clips):
            fp = os.path.join(rdir, LC._clip_name(i + 1, c['start']))
            with open(fp, 'wb') as f2:
                f2.write(b'0' * 20480)
            outs.append(fp)
        return outs

    def fake_covers(video, clips, rdir, progress=None, aspect='orig', focus=0.5,
                    titles=None):
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


# ---------------------------------------------------------------------------
# 笔记式时间轴字幕（2026-11-11 主人手稿正是这种格式，旧解析器整份拒收）
# ---------------------------------------------------------------------------
def test_parse_notes_timeline_basic():
    """`[MM:SS] 台词` 逐条解析：start=标注时间，end=下一条时间，最后一条 +15s。"""
    from ai_providers import _parse_notes_timeline
    lines = ['[00:15] -准备好了？ -好了', '', '[00:26] 爸', '', '[01:07] （阿拉巴马乡间）']
    segs = _parse_notes_timeline(lines)
    assert [(s['start'], s['end'], s['text']) for s in segs] == [
        (15.0, 26.0, '-准备好了？ -好了'),
        (26.0, 67.0, '爸'),
        (67.0, 82.0, '（阿拉巴马乡间）')]


def test_parse_notes_timeline_hhmmss_and_continuation():
    """`[HH:MM:SS]` 三段式 + 不带时间标注的行拼回上一条。"""
    from ai_providers import _parse_notes_timeline
    lines = ['[01:00:05] 第一条', '续行内容', '[01:02:10] 第二条']
    segs = _parse_notes_timeline(lines)
    assert segs[0]['start'] == 3605.0 and segs[0]['end'] == 3730.0
    assert '续行内容' in segs[0]['text']
    assert segs[1]['start'] == 3730.0


def test_parse_notes_timeline_rejects_single_cue():
    """只有一条带时间标注的行 → 不是笔记式时间轴，返回 []（防误判普通文本）。"""
    from ai_providers import _parse_notes_timeline
    assert _parse_notes_timeline(['[00:15] 只有这条']) == []
    assert _parse_notes_timeline(['普通文本行', '再来一行']) == []


def test_save_user_subtitle_accepts_notes_format(tmp_path):
    """端到端：_save_user_subtitle 不再拒收笔记式字幕（曾静默退回 ASR 白跑 3 分钟）。"""
    import webui_server as S
    vp = os.path.join(str(tmp_path), 'src.mp4')
    open(vp, 'wb').write(b'x' * 64)
    srt_text = '[00:15] -准备好了？ -好了\n\n[00:26] 爸\n\n[01:07] （阿拉巴马乡间）'
    saved = S._save_user_subtitle({'name': '前二十分钟字幕.srt', 'text': srt_text}, vp)
    assert saved and os.path.isfile(saved), '笔记式字幕必须能落盘'
    from ai_providers import parse_subtitle_file
    segs = parse_subtitle_file(saved)
    assert len(segs) == 3 and segs[0]['start'] == 15.0


def test_make_candidate_thumbs_writes_ascii_files(monkeypatch, tmp_path):
    """候选缩略图：每段一帧峰值画面，ASCII 文件名，落 run_dir 根目录（不能自毁）。"""
    from PIL import Image

    def fake_ffmpeg_run(cmd, **kw):
        out = cmd[-1]
        Image.new('RGB', (320, 180), (90, 140, 200)).save(out, 'JPEG', quality=85)
        return 0, b'', b''

    import ffmpeg_utils
    monkeypatch.setattr(ffmpeg_utils, 'ffmpeg_run', fake_ffmpeg_run)  # 函数内 import 从模块取
    clips = [{'start': 100.0, 'end': 150.0, 'peak_t': 120.0, 'dur': 50.0},
             {'start': 300.0, 'end': 350.0, 'peak_t': 320.0, 'dur': 50.0}]
    thumbs = LC.make_candidate_thumbs('fake.mp4', clips, str(tmp_path))
    assert len(thumbs) == 2 and all(thumbs), '两段都要出图'
    for t in thumbs:
        assert all(ord(ch) < 128 for ch in os.path.basename(t))
        assert os.path.getsize(t) > 512
        assert os.path.dirname(t) == str(tmp_path), '缩略图必须留在 run_dir 根（临时目录会被删）'


def test_live_analysis_cancel_button_wiring():
    """v1.4.55 回归钉：分析必须可取消（90 分钟片 235s，没有取消按钮像卡死）。

    后端 analyze_highlights 早已接 RUN_PROCS 协作取消；本钉保证前端入口
    不被误删，且取消走 /api/cancel（与解说链路同一机制）。
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "desktop", "desktop.js"), encoding="utf-8") as f:
        js = f.read()
    with open(os.path.join(root, "static", "desktop", "index.html"), encoding="utf-8") as f:
        h = f.read()
    assert 'id="lcCancel"' in h, "进度区必须有取消按钮"
    assert "_lcPollRunId" in js, "必须记录 runid 供取消用"
    assert "'/api/cancel'" in js and "runid: _lcPollRunId" in js
    assert "已取消分析" in js, "取消要轻提示，不报「分析失败」吓人"


def test_vf_chain_hd_16x9():
    """v1.4.56 横屏 16:9 标准化画幅：等比缩进 + 居中 pad 到 1280x720，
    简单线性链（走 -vf，与 crop 同款）；封面同步居中裁 16:9。"""
    hd = LC._vf_chain('hd', '')
    assert 'scale=1280:720:force_original_aspect_ratio=decrease' in hd
    assert 'pad=1280:720:(ow-iw)/2:(oh-ih)/2' in hd
    assert '[bg];' not in hd and '[vout]' not in hd, 'hd 是简单线性链，必须能走 -vf'
    hd_tm = LC._vf_chain('hd', 'zscale=...tonemap...')
    assert hd_tm.startswith('setpts=(PTS-STARTPTS),zscale=...tonemap...,scale=1280:720')
    # 封面 16:9 居中裁的数学：w=1920 h=1080 → ch=1080（无需裁）；w=1920 h=800 → ch=1080*? 
    # 直接验边界：竖长图 w=720 h=1280 → ch = 720*9/16 = 405
    w, h = 720, 1280
    ch = min(h, int(w * 9 / 16))
    assert ch == 405 and (h - ch) // 2 == 437, '竖长源裁 16:9 应保留中部横条'


def test_live_editor_params_memory_and_preview_ratio():
    """v1.4.57 编辑器小修包：切片参数记忆 + 结果预览框比例跟画幅。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "desktop", "desktop.js"), encoding="utf-8") as f:
        js = f.read()
    assert "framecut_desktop_lc_v1" in js, "参数记忆的 localStorage 键"
    assert "function lcSaveParams()" in js and "function lcRestoreParams()" in js
    assert "lcRestoreParams();   // v1.4.57" in js, "进入 live 页必须回填"
    assert "lcSaveParams();   // v1.4.57" in js, "提交分析时必须记忆"
    assert "_lcAspectUsed" in js and "'9 / 16' : '16 / 9'" in js, \
        "预览框比例必须按实际输出画幅切换（旧实现写死 16/9，竖屏大黑边）"


def test_live_candidate_preview_wiring():
    """v1.4.58 回归钉：候选试看——dispatch 回填源片名 + 前端 ▶ 按钮播到段尾自动停。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "workflows.py"), encoding="utf-8") as f:
        wf = f.read()
    assert "prog['video_file']" in wf, "dispatch_live_clip 必须回填源片文件名"
    assert "os.path.abspath(run_dir)" in wf, "直读形态（文件在项目外）必须留空不试看"
    with open(os.path.join(root, "static", "desktop", "desktop.js"), encoding="utf-8") as f:
        js = f.read()
    assert "function lcPreviewSeg(" in js
    assert "_lcPrevStop" in js and "currentTime >= _lcPrevStop" in js, "必须播到段尾自动停"
    assert "'▶ 试看'" in js and "row.append(n, lS, lE, dur, bPrev, bDel, why);" in js


def test_live_draft_restore_endpoint(tmp_path, monkeypatch):
    """v1.4.59 草稿恢复：/api/live_clip_draft 读草稿 → 历史记录可重进候选编辑器。"""
    import json
    import sys
    import webui_server as S
    import handler as H
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    run = tmp_path / "live-r1"
    run.mkdir()
    (run / "live_clip_draft.json").write_text(json.dumps({
        "video_src": str(run / "src.mp4"), "vdur": 600.0,
        "aspect_default": "blur",
        "clips": [{"start": 10.0, "end": 60.0, "dur": 50.0, "score": "", "reasons": ["手动指定"], "peak_t": 30.0}],
    }, ensure_ascii=False), encoding="utf-8")
    (run / "src.mp4").write_bytes(b"x")
    monkeypatch.setattr(S, "OUTDIR", str(tmp_path))
    captured = {}
    h = object.__new__(H.Handler)
    h.path = "/api/live_clip_draft?run_dir=live-r1"
    h._send = lambda code, body, ct: captured.update(
        {"code": code, "body": json.loads(body.decode("utf-8"))})
    h._get_live_clip_draft()
    body = captured["body"]
    assert body["ok"] is True
    assert body["clips"][0]["start"] == 10.0 and body["vdur"] == 600.0
    assert body["video_file"] == "src.mp4", "源片在 run_dir 里 → 可试看"
    # 缺草稿的目录 → 如实报「没有切片草稿」
    (tmp_path / "live-r2").mkdir()
    h2 = object.__new__(H.Handler)
    h2.path = "/api/live_clip_draft?run_dir=live-r2"
    cap2 = {}
    h2._send = lambda code, body, ct: cap2.update({"body": json.loads(body.decode("utf-8"))})
    h2._get_live_clip_draft()
    assert cap2["body"]["ok"] is False and "草稿" in cap2["body"]["error"]


def test_cover_crop_math_and_rebuild_contract():
    """v1.4.60 换封面：三种画幅的封面裁切数学 + rebuild_cover 契约。"""
    from PIL import Image
    # 9:16（crop/blur）：1920x1080 横源 → 裁宽 607（取偶 606）
    im = Image.new('RGB', (1920, 1080))
    c = LC._crop_cover_image(im, 'blur')
    assert c.size[0] % 2 == 0 and abs(c.size[0] - 1080 * 9 / 16) <= 2, c.size  # ±2：取偶修正
    assert c.size[1] == 1080
    # 16:9（hd）：720x1280 竖源 → 裁高 405，垂直居中
    im2 = Image.new('RGB', (720, 1280))
    c2 = LC._crop_cover_image(im2, 'hd')
    assert c2.size == (720, 404), c2.size  # 405 是奇数，取偶成 404（视频域惯例）
    # orig：原样
    assert LC._crop_cover_image(im2, 'orig').size == (720, 1280)
    # rebuild_cover 契约：抽帧失败（宿主帮手抛错）要如实上抛，不静默
    class _Boom:
        def _cover_candidates_sequential(self, *a, **k):
            raise RuntimeError('ffmpeg 失败')
    LC._host = lambda: _Boom()
    try:
        LC.rebuild_cover('v.mp4', 10.0, 'blur', os.path.join(tmpdir(), 'c.jpg'))
        assert False, '抽帧失败必须上抛'
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# v1.4.61：弹幕热度信号 + 候选台词明细
# ---------------------------------------------------------------------------
_XML_OK = ('<i><d p="12.5,1,25,16777215,0,0,0,0">哈哈</d>'
           '<d p="3.0,1,25,16777215,0,0,0,0">666</d>'
           '<d p="bad,1,25">坏时间戳</d>'
           '<d p="9.0,1,25,16777215,0,0,0,0"></d></i>')


def test_parse_danmaku_xml():
    """B 站录播姬 XML → [{t,text}] 按时间升序；坏/空输入一律 [] 不抛。"""
    ev = LC._parse_danmaku_xml(_XML_OK)
    assert [e['t'] for e in ev] == [3.0, 12.5], '坏时间戳与空文本要跳过，并按时间排序'
    assert [e['text'] for e in ev] == ['666', '哈哈']
    for bad in ('', '   ', 'not xml at all', '<i><d p="1,2">未闭合', None):
        assert LC._parse_danmaku_xml(bad) == [], '坏输入必须静默降级，不阻断分析'


def test_danmaku_profile_window_aggregation():
    """每窗弹幕条数曲线：跨窗按 int(t/window) 聚合，越界忽略。"""
    ev = [{'t': 0.5}, {'t': 2.0}, {'t': 3.9}, {'t': 999.0}, {'t': -3.0}, {}]
    c = LC._danmaku_profile(ev, 10, 2.0)
    # 缺 t 的坏事件按 0 秒兜底（与 _speech_profile 同款），越界的 999 / -3 忽略
    assert c.tolist() == [2.0, 2.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert LC._danmaku_profile([], 10, 2.0).sum() == 0


def _flat_pipeline(monkeypatch, tmp_path):
    """全片一样响 + 无画面 + 无台词：只有弹幕能打破平坦地形，便于验证排序。"""
    win, vdur, rate = 2.0, 300.0, 8000
    pcm = (np.random.default_rng(7).standard_normal(int(vdur / win) * int(rate * win))
           * 800).astype(np.int16)
    monkeypatch.setattr(LC, '_probe_duration', lambda p: (vdur, True))
    monkeypatch.setattr(LC, '_load_pcm', lambda *a, **k: (pcm, rate))
    monkeypatch.setattr(LC, '_motion_profile', lambda *a, **k: (None, 0.0))
    return win


def test_pct_rank_ties_keeps_equal_windows_equal():
    """稀疏弹幕曲线：并列的 0 窗必须同分位，不许越靠片尾分越高。

    stable 排序会让 98 个并列的零窗拿到 0~0.98 的递增分位次——片尾「一条
    弹幕都没有」的窗口反被判成全片峰值（实测把高光拖到 281 秒）。
    """
    x = np.zeros(100, dtype=np.float64)
    x[10:12] = 5.0
    r = LC.pct_rank(x, ties=True)
    zero_wins = np.concatenate([r[:10], r[12:]])
    assert np.allclose(zero_wins, zero_wins[0]), '并列零窗必须同分位'
    assert r[10] > 0.9 and r[10] > r[0] + 0.4
    assert LC.pct_rank(np.full(5, 0.3)).tolist() == [0.5] * 5, '默认行为不许变'


def test_analyze_danmaku_fusion_and_reasons(monkeypatch, tmp_path):
    """弹幕融进第 4 路：观众刷屏的窗口被选成高光，理由出现「弹幕热度」。"""
    _flat_pipeline(monkeypatch, tmp_path)
    # 200~220s 集中刷屏；其余时间零星几条
    dan = [{'t': 200.0 + (i % 20) * 1.0, 'text': '哈哈'} for i in range(200)]
    dan += [{'t': 20.0 + i * 5.0, 'text': 'x'} for i in range(10)]
    res = LC.analyze_highlights('f.mp4', run_dir=str(tmp_path), use_asr=False,
                                n_clips=1, min_sec=20, max_sec=60,
                                danmaku_events=dan)
    assert res['danmaku_used'] is True and res['asr'] == []
    assert res['clips'] and any(abs(c['peak_t'] - 210.0) < 30 for c in res['clips']), \
        '弹幕峰值窗必须主导高光排序：%r' % [c['peak_t'] for c in res['clips']]
    assert any('弹幕热度' in r for c in res['clips'] for r in c['reasons']), \
        '理由必须出现弹幕热度（弹幕置顶，不被 parts[:3] 挤掉）'
    assert 'lines' in res['clips'][0]


def test_analyze_without_danmaku_is_identical(monkeypatch, tmp_path):
    """无弹幕 = 旧行为逐位等价：None / 空 list / 全落在片长外的弹幕都一样。"""
    _flat_pipeline(monkeypatch, tmp_path)
    kw = dict(run_dir=str(tmp_path), use_asr=False, n_clips=1, min_sec=20, max_sec=60)
    base = LC.analyze_highlights('f.mp4', **kw)
    for absent in ([], [{'t': 99999.0, 'text': 'x'}]):
        got = LC.analyze_highlights('f.mp4', danmaku_events=absent, **kw)
        assert got['clips'] == base['clips'], '缺席弹幕不得改变候选/理由/分数'
        assert got['danmaku_used'] is False
    assert abs(LC.W_DANMAKU - 0.4) < 1e-9, '弹幕权重（观众直接投票，高于画面运动）'


def test_clip_lines_overlap_and_cap():
    """lines：与段有重叠的台词都可见（跨边界半句也要），最多 200 条，坏数据跳过。"""
    asr = [{'start': 5.0, 'end': 11.0, 'text': '跨左边界'},
           {'start': 12.0, 'end': 15.0, 'text': '段内'},
           {'start': 19.5, 'end': 25.0, 'text': '跨右边界'},
           {'start': 30.0, 'end': 31.0, 'text': '段外'},
           {'start': '坏', 'end': 13.0, 'text': '坏数据'},
           None]
    got = LC._lines_in_range(asr, 10.0, 20.0)
    assert [x['text'] for x in got] == ['跨左边界', '段内', '跨右边界'], got
    many = [{'start': float(i), 'end': float(i) + 0.5, 'text': str(i)} for i in range(260)]
    assert len(LC._lines_in_range(many, 0.0, 1000.0)) == 200
    assert LC._lines_in_range([], 0, 10) == []


def test_dispatch_live_clip_danmaku_wiring(monkeypatch, tmp_path):
    """入口链路：danmaku_text 解析成 list 直传分析器（不落盘）、草稿记 danmaku_used。"""
    import webui_server as S
    import workflows as W
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    vp = str(tmp_path / 'src.mp4')
    (tmp_path / 'src.mp4').write_bytes(b'x')
    monkeypatch.setattr(S, '_resolve_upload_video', lambda *a, **k: vp)
    seen = {}

    def fake_analyze(_vp, run_dir, **kw):
        seen.update(kw)
        return {'clips': [{'start': 1.0, 'end': 30.0, 'peak_t': 10.0, 'score': 0.8,
                           'reasons': ['弹幕热度峰值（全片前1%）'], 'lines': []}],
                'vdur': 300.0, 'window': 2.0, 'asr_used': True,
                'danmaku_used': True, 'asr': []}

    monkeypatch.setattr(LC, 'analyze_highlights', fake_analyze)
    monkeypatch.setattr(LC, 'make_candidate_thumbs', lambda *a, **k: [''])
    saved = {}
    monkeypatch.setattr(LC, 'save_draft', lambda rd, data: saved.update(data) or 'ok')
    prog = {'phase': '', 'pct': 0, 'done': False, 'run_dir': str(tmp_path / 'run1')}
    W.dispatch_live_clip({'video': {'name': 'src.mp4'}, 'danmaku_text': _XML_OK}, prog)
    assert prog.get('done') is True, prog.get('error')
    assert [e['t'] for e in seen['danmaku_events']] == [3.0, 12.5], 'XML 必须解析后直传 list'
    assert saved['danmaku_used'] is True, '草稿要记是否用了弹幕'
    assert saved['clips'][0]['lines'] == [] or isinstance(saved['clips'][0]['lines'], list)


# ---------------------------------------------------------------------------
# v1.4.62：crop 画幅水平焦点
# ---------------------------------------------------------------------------
def test_clamp_focus():
    """None/坏值/越界一律安全：坏值不能拼进 ffmpeg 表达式（会整条导出失败）。"""
    for bad in (None, '', 'abc', float('nan'), float('inf'), [], object()):
        assert LC._clamp_focus(bad) == 0.5
    assert LC._clamp_focus(-3) == 0.0 and LC._clamp_focus(9) == 1.0
    assert abs(LC._clamp_focus(0.25) - 0.25) < 1e-9


def test_vf_chain_crop_focus():
    """crop 的 x 表达式参数化；None 与显式 0.5 等价（旧版 /2 逐位兼容）。"""
    default = LC._vf_chain('crop', '')
    center = LC._vf_chain('crop', '', 0.5)
    assert default == center, '不传 focus 必须等于居中（旧行为）'
    assert '*0.5000' in center and 'min(iw\\,ih*9/16)' in center
    assert '*0.0000' in LC._vf_chain('crop', '', 0)
    assert '*1.0000' in LC._vf_chain('crop', '', 1)
    assert '*1.0000' in LC._vf_chain('crop', '', 42), '越界值必须钳到 1'
    # 其它画幅不吃焦点（blur 是全景虚化、hd 是垂直裁，没有水平焦点概念）
    assert LC._vf_chain('blur', '', 0.1) == LC._vf_chain('blur', '')
    assert LC._vf_chain('hd', '', 0.1) == LC._vf_chain('hd', '')


def test_crop_cover_focus_math():
    """封面横轴与视频链同一公式 x=(w-cw)*focus，且钳制不出画框。"""
    from PIL import Image
    im = Image.new('RGB', (1920, 1080))
    cw = LC._crop_cover_image(im, 'crop', 0.5).size[0]
    assert cw == 606, '9:16 裁宽取偶'
    left = LC._crop_cover_image(im, 'crop', 0.0)
    right = LC._crop_cover_image(im, 'crop', 1.0)
    assert left.size == right.size == (606, 1080)
    # 用像素色块验起点：左对齐应取到最左，右对齐应取到最右
    for x in range(1920):
        im.putpixel((x, 0), (255, 0, 0) if x < 960 else (0, 0, 255))
    assert LC._crop_cover_image(im, 'crop', 0.0).getpixel((0, 0)) == (255, 0, 0)
    assert LC._crop_cover_image(im, 'crop', 1.0).getpixel((605, 0)) == (0, 0, 255)
    # hd 是垂直裁中部横条，focus 不影响
    assert LC._crop_cover_image(im, 'hd', 0.0).size == LC._crop_cover_image(im, 'hd', 1.0).size


def test_dispatch_export_passes_focus(monkeypatch, tmp_path):
    """入口链路：focus 钳制后同时透传给视频导出与封面（换封面才不会错位）。"""
    import json
    import webui_server as S
    import workflows as W
    run = tmp_path / 'live-focus'
    run.mkdir()
    (run / 'live_clip_draft.json').write_text(json.dumps({
        'video_src': str(run / 'src.mp4'), 'vdur': 600.0, 'window': 2.0,
        'asr_used': False, 'aspect_default': 'crop',
        'clips': [{'start': 10.0, 'end': 60.0, 'dur': 50.0, 'score': 0.9,
                   'reasons': ['音量峰值'], 'peak_t': 30.0}]}), encoding='utf-8')
    (run / 'src.mp4').write_bytes(b'x')
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    seen = {}

    def fake_export(vp, clips, rdir, progress=None, aspect='orig', focus=None,
                    titles=None):
        seen['export'] = focus
        fp = os.path.join(rdir, 'c.mp4')
        with open(fp, 'wb') as f:
            f.write(b'0' * 20480)
        return [fp]

    def fake_covers(vp, clips, rdir, progress=None, aspect='orig', focus=0.5,
                    titles=None):
        seen['covers'] = focus
        return ['']

    monkeypatch.setattr(LC, 'export_clips', fake_export)
    monkeypatch.setattr(LC, 'make_covers', fake_covers)
    for raw, want in ((0.25, 0.25), (9, 1.0), ('坏值', 0.5)):
        prog = {'phase': '', 'pct': 0, 'done': False, 'run_dir': 'live-focus'}
        W.dispatch_live_clip_export({'run_dir': 'live-focus', 'aspect': 'crop',
                                     'focus': raw}, prog)
        assert prog.get('done') is True, prog.get('error')
        assert seen['export'] == want and seen['covers'] == want, (raw, seen)


def tmpdir():
    import tempfile
    return tempfile.mkdtemp()


def test_dispatch_export_accepts_hd_aspect(tmp_path, monkeypatch):
    """v1.4.74 修 P0：导出画幅白名单必须含 'hd'（v1.4.56 引入横屏 16:9 时漏了）。

    旧行为：前端能选 hd，这里却静默回退 blur——用户选 16:9 实际出竖屏虚化版。"""
    import json
    import live_clip as LC
    import workflows as WF

    run = tmp_path / "live-hd"
    run.mkdir()
    src = run / "src.mp4"
    src.write_bytes(b"x")
    draft = {"video_src": str(src), "vdur": 600.0, "aspect_default": "hd",
             "clips": [{"start": 10.0, "end": 60.0, "dur": 50.0, "score": "",
                        "reasons": ["手动指定"], "peak_t": 30.0}]}
    (run / "live_clip_draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    seen = {}

    def fake_export(vp, segs, rdir, progress=None, aspect="orig", focus=None, titles=None):
        seen["aspect"] = aspect
        return [str(run / "clip01.mp4")]

    # dispatch 内部「import live_clip as LC」在调用期解析到同一模块对象，
    # 所以 patch live_clip 的属性即可命中（不要 patch WF.LC——那是函数局部名）。
    monkeypatch.setattr(LC, "validate_segments", lambda segs, vdur: segs)
    monkeypatch.setattr(LC, "carry_reasons", lambda segs, clips: segs)
    monkeypatch.setattr(LC, "summarize_segment", lambda lines: None)
    monkeypatch.setattr(LC, "_clamp_focus", lambda v: 0.5)
    monkeypatch.setattr(LC, "export_clips", fake_export)
    monkeypatch.setattr(LC, "make_covers",
                        lambda vp, segs, rdir, progress=None, aspect="orig", focus=None, titles=None: ["cover01.jpg"])
    monkeypatch.setattr(LC, "write_report", lambda *a, **k: ("r.json", "r.txt"))
    monkeypatch.setattr(WF, "_w", type("_W", (), {  # dispatch 经 _w._record_history 写历史
        "_record_history": staticmethod(lambda *a, **k: None),
        "_safe_dir": staticmethod(lambda base, name: os.path.join(str(base), str(name))),
        "OUTDIR": str(tmp_path),
        "fail_task": staticmethod(lambda prog, e: prog.update(
            {"error": str(e), "done": True})),
    })())

    prog = {}
    WF.dispatch_live_clip_export({"run_dir": "live-hd", "segments": [],
                                  "aspect": "hd"}, prog)
    assert prog.get("error") is None, prog
    assert seen["aspect"] == "hd", "hd 必须原样传给导出（不许静默回退 blur）"

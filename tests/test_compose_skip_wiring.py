# -*- coding: utf-8 -*-
"""B 项锁定：剧情驱动剪辑的真实剪切接线。

用户流程（两步法）：
  ① /api/movie_tts 生成配音，保存 tts_state.json（segs 已按 cut_plan 剔除 keep=False 段）
  ② 编辑台勾选「跳过/剪除」某段 → 前端发 {run_dir, items, skip} →
     /api/movie_compose → dispatch_movie_compose → compose_movie_from_tts(skip=...)
     被 skip 的段从 segs 剔除 → _cut_video_by_spans 真实 ffmpeg 剪切

本测试锁定「skip 段在真正剪切前已从 segs 剔除」这一关键接线，
不依赖真实 ffmpeg / 视频 / 配音，只拦截交给 _cut_video_by_spans 的 segs。
"""
import json


class _CutCaptured(Exception):
    """用于在真正调用 ffmpeg 剪切前拦截，捕获当时要剪切的 segs。"""
    pass


def test_compose_skip_drops_segment_before_real_cut(tmp_path, monkeypatch):
    import movie_narrator as mn

    run = tmp_path / 'run1'
    run.mkdir()
    dummy_vid = run / 'src.mp4'
    dummy_vid.write_bytes(b'')  # 仅用于 exists 检查；probe 已 mock
    state = {
        'video_path': str(dummy_vid),
        'segs': [[0, 2], [2, 4], [4, 6]],
        'narr': ['a', 'b', 'c'],
        'narr_map': [0, 1, 2],
        'params': {'autoCut': True},
    }
    (run / 'tts_state.json').write_text(json.dumps(state), encoding='utf-8')

    captured = {}

    def fake_cut(video_path, segs, rd, progress):
        captured['segs'] = [tuple(s) for s in segs]
        raise _CutCaptured('captured')

    monkeypatch.setattr(mn, '_cut_video_by_spans', fake_cut)
    monkeypatch.setattr(mn, 'probe_audio_len', lambda p: 6.0)

    # skip 中间第 2 段（index 1）→ 应只剩 (0,2) 与 (4,6)
    try:
        mn.compose_movie_from_tts(str(run), progress={}, skip=[1], adjusted_items=None)
    except _CutCaptured:
        pass

    assert captured.get('segs') == [(0, 2), (4, 6)], \
        '被 skip 的段必须从交给真实剪切的 segs 中剔除，否则剪除无效：%r' % captured


def test_compose_no_skip_keeps_all_segments(tmp_path, monkeypatch):
    import movie_narrator as mn

    run = tmp_path / 'run1'
    run.mkdir()
    dummy_vid = run / 'src.mp4'
    dummy_vid.write_bytes(b'')
    state = {
        'video_path': str(dummy_vid),
        'segs': [[0, 2], [2, 4], [4, 6]],
        'narr': ['a', 'b', 'c'],
        'narr_map': [0, 1, 2],
        'params': {'autoCut': True},
    }
    (run / 'tts_state.json').write_text(json.dumps(state), encoding='utf-8')

    captured = {}

    def fake_cut(video_path, segs, rd, progress):
        captured['segs'] = [tuple(s) for s in segs]
        raise _CutCaptured('captured')

    monkeypatch.setattr(mn, '_cut_video_by_spans', fake_cut)
    monkeypatch.setattr(mn, 'probe_audio_len', lambda p: 6.0)

    try:
        mn.compose_movie_from_tts(str(run), progress={}, skip=[], adjusted_items=None)
    except _CutCaptured:
        pass

    assert captured.get('segs') == [(0, 2), (2, 4), (4, 6)], \
        '未 skip 时应保留全部段：%r' % captured

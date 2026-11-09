# -*- coding: utf-8 -*-
"""分割/插入段后的 compose 接线回归钉（2026-11-06 计划#5）。

用户流程：编辑台把一段拆成两段（配音切两个新 wav、文字断开、画面范围各半）→
/api/movie_compose → compose_movie_from_tts(adjusted_items=...) → 后端必须：
  ① 新增解说段的位置（≥ 原始 segs 数）也要拿到自己的画面范围——
     原实现「索引越界跳过」会让新段落到 (0,10) 兜底；
  ② B-roll/原声引用与解说混排时，解说段的文字/画面必须取条目本身——
     原实现按序取 narr[seg_idx]/segs[seg_idx]，后一段会被串成空文字+插入段的画面。

不依赖真实 ffmpeg/视频，拦截交给 _cut_video_by_spans 的 segs（与
test_compose_skip_wiring.py 同款手法）。
"""
import json


class _CutCaptured(Exception):
    pass


def _run_compose(tmp_path, monkeypatch, items, segs=None):
    import movie_narrator as mn

    run = tmp_path / 'run1'
    run.mkdir()
    dummy_vid = run / 'src.mp4'
    dummy_vid.write_bytes(b'')
    state = {
        'video_path': str(dummy_vid),
        'segs': segs or [[0, 4], [4, 8], [8, 12]],
        'narr': ['a', 'b', 'c'],
        'narr_map': [0, 1, 2],
        'params': {'autoCut': True},
    }
    (run / 'tts_state.json').write_text(json.dumps(state), encoding='utf-8')

    captured = {}

    def fake_cut(video_path, segs_, rd, progress, **kw):
        captured['segs'] = [tuple(s) for s in segs_]
        raise _CutCaptured('captured')

    monkeypatch.setattr(mn, '_cut_video_by_spans', fake_cut)
    # 本文件只验证 skip/分割的连线；「补画面让配音念完」见
    # tests/test_narration_no_truncation.py，这里置恒等以免改变断言基准
    monkeypatch.setattr(mn, '_fit_spans_to_voice',
                        lambda sp, d, src=None, tail=0.35, **kw: (list(sp), {'grew': 0, 'added': 0.0}))
    monkeypatch.setattr(mn, 'probe_audio_len', lambda p: 12.0)
    try:
        mn.compose_movie_from_tts(str(run), progress={}, skip=[], adjusted_items=items)
    except _CutCaptured:
        pass
    return captured


def test_split_grows_spans_for_new_position(tmp_path, monkeypatch):
    """一段拆两段后段数+1：新位置必须有画面，且四段画面各归其位。"""
    items = [
        {'index': 0, 'text': 'a1', 'audio': 'run1/narr0_s1.wav', 'video_start': 0, 'video_end': 2.5},
        {'index': 1, 'text': 'a2', 'audio': 'run1/narr0_s2.wav', 'video_start': 2.5, 'video_end': 4},
        {'index': 2, 'text': 'b', 'audio': 'run1/narr1.wav', 'video_start': 4, 'video_end': 8},
        {'index': 3, 'text': 'c', 'audio': 'run1/narr2.wav', 'video_start': 8, 'video_end': 12},
    ]
    captured = _run_compose(tmp_path, monkeypatch, items)
    assert captured.get('segs') == [(0, 2.5), (2.5, 4), (4, 8), (8, 12)], \
        '分割出的新段必须拿到自己的画面范围：%r' % captured.get('segs')


def test_broll_then_narration_keeps_own_text_and_span(tmp_path, monkeypatch):
    """[解说, B-roll, 解说] 混排：第 2 个解说的画面/文字不得被串成 B-roll 的。"""
    items = [
        {'index': 0, 'text': 'n0', 'audio': 'run1/narr0.wav', 'video_start': 0, 'video_end': 4},
        {'index': 1, 'text': '', 'video_start': 4, 'video_end': 6, 'isBroll': True},
        {'index': 2, 'text': 'n1', 'audio': 'run1/narr1.wav', 'video_start': 6, 'video_end': 12},
    ]
    captured = _run_compose(tmp_path, monkeypatch, items)
    assert captured.get('segs') == [(0, 4), (4, 6), (6, 12)], \
        'B-roll 之后的解说段必须用条目自己的画面范围：%r' % captured.get('segs')


def test_no_adjustment_keeps_original_segments(tmp_path, monkeypatch):
    """无用户调整：原始 segs 原样进剪切（回归保护）。"""
    captured = _run_compose(tmp_path, monkeypatch, None)
    assert captured.get('segs') == [(0, 4), (4, 8), (8, 12)]

# -*- coding: utf-8 -*-
"""废镜头筛除（shot_quality.filter_good_shots）契约测试。

v1.4.96 实测回归：event_pipeline 解包 2 元组而函数返回 3 元组 →
解包炸 → 兜底「全部保留」把筛除成果全丢（19 个废镜头白打分）。
这里钉死三件事：① 返回 3 元组；② 黑场/纯色被拒收；③ 抽帧失败保守保留。
"""
import os

import numpy as np
import pytest

from shot_quality import filter_good_shots, frame_quality

W, H = 320, 180


def _fake_ffmpeg_factory(images):
    """造一个假 ffmpeg_run_fn：按镜头序号把预生成图写出去。

    images: list of np.uint8 HxWx3（RGB）。第 k 次调用写 images[min(k, len-1)]。
    """
    calls = {'n': 0}

    def run(args):
        out = args[-1]
        idx = min(calls['n'], len(images) - 1)
        calls['n'] += 1
        try:
            from PIL import Image
            Image.fromarray(images[idx]).save(out, quality=92)
            return 0
        except Exception:
            return 1

    return run


def _noisy_frame():
    rng = np.random.default_rng(7)
    return rng.integers(0, 255, size=(H, W, 3), dtype=np.uint8)


def _black_frame():
    return np.zeros((H, W, 3), dtype=np.uint8)


def _flat_gray_frame():
    return np.full((H, W, 3), 128, dtype=np.uint8)


def test_frame_quality_scores():
    # 噪声帧（高清晰度/高对比/中亮度）应当高分；黑场低分
    sc_noise, _ = frame_quality(_noisy_frame().astype(np.float64))
    sc_black, det_black = frame_quality(_black_frame().astype(np.float64))
    assert sc_noise > 60.0
    assert sc_black < 25.0
    assert det_black['brightness'] < 0.06


def test_filter_returns_three_tuple(tmp_path):
    """契约钉死：返回 (good, rejected, stats)——调用方 event_pipeline 解包 3 元组。"""
    shots = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
    # 镜头 0 噪声（保留）、镜头 1 黑场（拒收）、镜头 2 灰板（拒收）
    images = [_noisy_frame(), _black_frame(), _flat_gray_frame()]
    good, rejected, stats = filter_good_shots(
        'fake.mp4', shots, _fake_ffmpeg_factory(images),
        quality_min=25.0, tmp_dir=str(tmp_path))
    assert isinstance(good, list) and all(isinstance(t, tuple) and len(t) == 2
                                          for t in good)
    assert isinstance(rejected, list)
    assert isinstance(stats, dict)
    assert (0.0, 2.0) in good
    assert len(rejected) == 2
    assert stats['checked'] == 3 and stats['rejected'] == 2


def test_filter_keeps_on_ffmpeg_failure(tmp_path):
    """抽帧失败（ffmpeg rc!=0/空文件）保守保留——宁滥勿缺。"""
    shots = [(0.0, 2.0), (2.0, 4.0)]

    def run(args):
        return 1        # 假 ffmpeg 全部失败

    good, rejected, stats = filter_good_shots(
        'fake.mp4', shots, run, quality_min=25.0, tmp_dir=str(tmp_path))
    assert len(good) == 2 and len(rejected) == 0
    assert stats['errors'] >= 0


def test_filter_keeps_on_exception(tmp_path):
    """run_fn 抛异常也保守保留（评分异常不上抛）。"""
    shots = [(0.0, 2.0)]

    def run(args):
        raise RuntimeError('boom')

    good, rejected, _stats = filter_good_shots(
        'fake.mp4', shots, run, quality_min=25.0, tmp_dir=str(tmp_path))
    assert len(good) == 1 and len(rejected) == 0


def test_event_pipeline_unpack_contract():
    """event_pipeline.build_pipeline 的解包面与函数签名一致（回归钉）。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'event_pipeline.py'), encoding='utf-8').read()
    assert 'shots, _rejected, _rej_stats = _SQ.filter_good_shots(' in src


@pytest.mark.parametrize('img,lo,hi', [
    (_black_frame(), 0.0, 25.0),
    (_flat_gray_frame(), 0.0, 25.0),
    (_noisy_frame(), 60.0, 101.0),
])
def test_score_ranges(img, lo, hi):
    sc, _ = frame_quality(img.astype(np.float64))
    assert lo <= sc < hi

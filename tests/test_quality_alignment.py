# -*- coding: utf-8 -*-
"""质量优先路线 · 对齐/润色回归。

锁定三件事：
  1. 算法语义对齐（不调模型）能按「画面内容」把解说句匹配到正确场景 —— 这是 LLM
     超时/失败时的保底，避免退化成「按字数盲切」导致声画错位。
  2. 分批 LLM 对齐失败时，必须保留算法对齐结果（不能变空）。
  3. 分批润色失败时，必须保留原稿（不能丢段、不能空段）。
"""
import webui_server as S  # noqa: F401  先导入入口，解循环依赖
import movie_narrator as M


def _scenes():
    return [
        {'start': 0, 'end': 5, 'location': '便利店',
         'characters': '主角', 'event': '主角走进便利店拿起一瓶水', 'summary': '雨夜便利店'},
        {'start': 5, 'end': 10, 'location': '街道',
         'characters': '主角', 'event': '主角在雨夜街道奔跑', 'summary': '雨夜街道'},
        {'start': 10, 'end': 15, 'location': '医院',
         'characters': '主角', 'event': '主角在医院病房醒来', 'summary': '医院病房'},
    ]


def _beats():
    return [
        '雨夜里，他走进一家便利店，货架上只剩一瓶水。',
        '走出门后，他在街道上狂奔，身后似乎有人在追。',
        '等他睁开眼，已经躺在医院的病床上。',
    ]


def test_algo_align_matches_by_content():
    """三句解说分别讲便利店/街道/医院 → 必须各自对齐到对应场景。"""
    al = M._algo_align_beats_to_scenes(_beats(), _scenes())
    assert set(al.keys()) == {0, 1, 2}
    assert al[0][0] == 0, al
    assert al[1][0] == 1, al
    assert al[2][0] == 2, al


def test_algo_align_is_monotonic():
    """对齐必须保持时间顺序（画面不能倒退）。"""
    al = M._algo_align_beats_to_scenes(_beats(), _scenes())
    starts = [min(v) for v in al.values()]
    assert starts == sorted(starts), starts


def test_algo_align_more_beats_than_scenes():
    """句子比场景多时不能崩、不能越界，且顺序不倒退。"""
    beats = ['第一句', '第二句', '第三句', '第四句', '第五句']
    al = M._algo_align_beats_to_scenes(beats, _scenes()[:2])
    assert set(al.keys()) == {0, 1, 2, 3, 4}
    idxs = [v[0] for v in al.values()]
    assert all(0 <= i < 2 for i in idxs), idxs
    assert idxs == sorted(idxs), idxs


def test_algo_align_empty_inputs():
    assert M._algo_align_beats_to_scenes([], _scenes()) == {}
    assert M._algo_align_beats_to_scenes(_beats(), []) == {}


def _patch_llm(monkeypatch, fn):
    monkeypatch.setattr(M._w, 'local_llm_enabled', lambda: True)
    monkeypatch.setattr(M._w, 'local_llm_ping', lambda: (True, 'ok'))
    monkeypatch.setattr(M._w, 'ai_enabled', lambda *a, **k: False)
    monkeypatch.setattr(M, '_aborted', lambda: False)
    monkeypatch.setattr(M, '_llm_light', fn)


def test_batched_align_keeps_base_on_failure(monkeypatch):
    """LLM 抛异常 → 必须原样返回算法对齐（绝不变成空）。"""
    base = {0: [0], 1: [1], 2: [2]}

    def boom(*a, **k):
        raise RuntimeError('timed out')

    _patch_llm(monkeypatch, boom)
    out = M._llm_align_batched(_beats(), _scenes(), base=base, batch=6)
    assert out == base, out


def test_batched_align_overrides_on_success(monkeypatch):
    """LLM 返回有效对齐 → 覆盖算法结果。"""
    base = {0: [0], 1: [1], 2: [2]}
    _patch_llm(monkeypatch, lambda *a, **k: '{"对齐":[{"解说词":1,"场景":[0]}]}')
    out = M._llm_align_batched(_beats(), _scenes(), base=base, batch=6)
    assert out[1] == [0], out
    # 未覆盖的句子保留算法结果
    assert out[0] == [0] and out[2] == [2], out


def test_refine_batched_keeps_original_on_failure(monkeypatch):
    """润色抛异常 → 必须逐字返回原稿（不丢段、不空段）。"""

    def boom(*a, **k):
        raise RuntimeError('boom')

    _patch_llm(monkeypatch, boom)
    out = M._refine_batched(_beats(), {0: [0], 1: [1], 2: [2]}, _scenes(), batch=4)
    assert out == _beats(), out


def test_refine_batched_applies_on_success(monkeypatch):
    """润色返回等长数组 → 按顺序替换。"""
    refined = ['改写一', '改写二', '改写三']
    _patch_llm(monkeypatch, lambda *a, **k: '{"润色":%s}' % __import__('json').dumps(refined, ensure_ascii=False))
    out = M._refine_batched(_beats(), {0: [0], 1: [1], 2: [2]}, _scenes(), batch=4)
    assert out == refined, out

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


# ===== 用户稿子路线 · DP 全局对齐（真机 TWD 29 句回归出的行为） =====

def _big_scenes():
    """模拟 110 个 12 秒场景窗：三段剧情（便利店 0-120 / 医院 300-420 / 码头 900+）。"""
    sc = []
    for i in range(100):
        t = i * 12.0
        if t < 120:
            d = {'location': '便利店', 'event': '主角走进便利店拿起一瓶水',
                 'summary': '雨夜便利店'}
        elif 300 <= t < 420:
            d = {'location': '医院', 'event': '主角在医院病房醒来看到枯萎的花',
                 'summary': '医院病房'}
        else:
            d = {'location': '空镜', 'event': '城市空镜转场', 'summary': '空镜'}
        sc.append({'start': t, 'end': t + 12, **d})
    return sc


def test_dp_align_monotonic_and_bounded():
    beats = ['雨夜里，他走进一家便利店，货架上只剩一瓶水。',
             '走出门后，他在街道上狂奔。',
             '等他睁开眼，已经躺在医院的病床上，床头有一束枯萎的花。']
    sc = _big_scenes()
    al = M._algo_align_beats_to_scenes(beats, sc)
    assert len(al) == 3
    firsts = [al[i][0] for i in range(3)]
    assert firsts == sorted(firsts), firsts          # 单调不减
    for g in al.values():
        assert 1 <= len(g) <= 3
        assert g == sorted(g)
    # 便利店句落在前 10 个场景（<120s），医院句落在 300-420s 区间
    assert sc[al[0][0]]['start'] < 120, al
    assert 300 <= sc[al[2][0]]['start'] < 420, al


def test_dp_align_connector_beat_does_not_drag():
    """零信号句（如「片头音乐炸开」）不能把时间轴甩到远处。

    真机 TWD 事故：这句没有任何实义词可与场景匹配，等比例先验把它单独锚到
    499 秒；DP 的单调约束又让后面 18 句全部只能排在它之后，中段整体错位
    250 秒。先验必须按语义峰值缩放，让这种句子跟着邻居走。
    """
    beats = ['雨夜里，他走进一家便利店，货架上只剩一瓶水。',
             '片头音乐炸开，破碎的世界。',                # 无信号连接句
             '他再次出现时，正走进另一家便利店找水。']     # 又一个强信号句
    sc = _big_scenes()
    al = M._algo_align_beats_to_scenes(beats, sc)
    # 连接句不能跑出便利店段（<120s），更不能把第三句顶到远处
    assert sc[al[1][0]]['start'] < 150, al
    assert sc[al[2][0]]['start'] < 150, al


def test_dp_align_far_jump_capped_by_window():
    """单句最多推进「窗口」个场景：不能一句台词跳过 300 秒。"""
    beats = ['雨夜里，他走进一家便利店。',
             '等他睁开眼，已经躺在医院的病床上。']        # 医院在 300s
    sc = _big_scenes()
    al = M._algo_align_beats_to_scenes(beats, sc)
    # 第一句不能因为「医院」在后就不在便利店段
    assert sc[al[0][0]]['start'] < 120, al
    assert sc[al[1][0]]['start'] >= 300, al


def test_isotonic_minimal_displacement():
    """保序回归：乱序投影成不减序列，且是位移平方和最小的那种（不是单向棘轮）。"""
    out = M._isotonic([300, 320, 280, 290])
    assert out == sorted(out), out
    # 棘轮式「往后抬」会得到 [300,320,320,320]；保序回归应该是均值池化
    assert abs(sum(out) - sum([300, 320, 280, 290])) < 1e-6


def test_sanitize_alignment_truncates_and_fixes_backstep():
    sc = [{'start': i * 10.0, 'end': i * 10.0 + 10} for i in range(60)]
    # 模型回了一整段 [21..39]：必须截断到 3 个场景
    merged = {0: list(range(21, 40)), 1: [5], 2: [10]}
    base = {0: [3, 4], 1: [6, 7], 2: [11, 12]}
    out = M._sanitize_alignment(merged, base, sc, 3)
    assert len(out[0]) <= 3
    # 1 落在 5（50s），比 0 的起点晚但比 0 的截断终点早 → 保序纠正
    firsts = [out[i][0] for i in range(3)]
    assert firsts == sorted(firsts), out


def test_parse_alignment_accepts_string_ids():
    """模型会把编号写成字符串（"18"），不能因此整批作废。"""
    obj = {'对齐': [{'解说词': '18', '场景': ['50']},
                    {'解说词': '19', '场景': [51, 52]}]}
    sc = [True] * 60
    al = M._parse_alignment_obj(obj, sc)
    assert al == {18: [50], 19: [51, 52]}, al


# ===== 用户稿子路线 · DP 全局对齐（真机 TWD 29 句回归出的行为） =====

def _big_scenes():
    """模拟 110 个 12 秒场景窗：三段剧情（便利店 0-120 / 医院 300-420 / 码头 900+）。"""
    sc = []
    for i in range(100):
        t = i * 12.0
        if t < 120:
            d = {'location': '便利店', 'event': '主角走进便利店拿起一瓶水',
                 'summary': '雨夜便利店'}
        elif 300 <= t < 420:
            d = {'location': '医院', 'event': '主角在医院病房醒来看到枯萎的花',
                 'summary': '医院病房'}
        else:
            d = {'location': '空镜', 'event': '城市空镜转场', 'summary': '空镜'}
        sc.append({'start': t, 'end': t + 12, **d})
    return sc


def test_dp_align_monotonic_and_bounded():
    beats = ['雨夜里，他走进一家便利店，货架上只剩一瓶水。',
             '走出门后，他在街道上狂奔。',
             '等他睁开眼，已经躺在医院的病床上，床头有一束枯萎的花。']
    sc = _big_scenes()
    al = M._algo_align_beats_to_scenes(beats, sc)
    assert len(al) == 3
    firsts = [al[i][0] for i in range(3)]
    assert firsts == sorted(firsts), firsts          # 单调不减
    for g in al.values():
        assert 1 <= len(g) <= 3
        assert g == sorted(g)
    # 便利店句落在前 10 个场景（<120s），医院句落在 300-420s 区间
    assert sc[al[0][0]]['start'] < 120, al
    assert 300 <= sc[al[2][0]]['start'] < 420, al


def test_dp_align_connector_beat_does_not_drag():
    """零信号句（如「片头音乐炸开」）不能把时间轴甩到远处。

    真机 TWD 事故：这句没有任何实义词可与场景匹配，等比例先验把它单独锚到
    499 秒；DP 的单调约束又让后面 18 句全部只能排在它之后，中段整体错位
    250 秒。先验必须按语义峰值缩放，让这种句子跟着邻居走。
    """
    beats = ['雨夜里，他走进一家便利店，货架上只剩一瓶水。',
             '片头音乐炸开，破碎的世界。',                # 无信号连接句
             '他再次出现时，正走进另一家便利店找水。']     # 又一个强信号句
    sc = _big_scenes()
    al = M._algo_align_beats_to_scenes(beats, sc)
    # 连接句不能跑出便利店段（<120s），更不能把第三句顶到远处
    assert sc[al[1][0]]['start'] < 150, al
    assert sc[al[2][0]]['start'] < 150, al


def test_dp_align_far_jump_capped_by_window():
    """单句最多推进「窗口」个场景：不能一句台词跳过 300 秒。"""
    beats = ['雨夜里，他走进一家便利店。',
             '等他睁开眼，已经躺在医院的病床上。']        # 医院在 300s
    sc = _big_scenes()
    al = M._algo_align_beats_to_scenes(beats, sc)
    # 第一句不能因为「医院」在后就不在便利店段
    assert sc[al[0][0]]['start'] < 120, al
    assert sc[al[1][0]]['start'] >= 300, al


def test_isotonic_minimal_displacement():
    """保序回归：乱序投影成不减序列，且是位移平方和最小的那种（不是单向棘轮）。"""
    out = M._isotonic([300, 320, 280, 290])
    assert out == sorted(out), out
    # 棘轮式「往后抬」会得到 [300,320,320,320]；保序回归应该是均值池化
    assert abs(sum(out) - sum([300, 320, 280, 290])) < 1e-6


def test_sanitize_alignment_truncates_and_fixes_backstep():
    sc = [{'start': i * 10.0, 'end': i * 10.0 + 10} for i in range(60)]
    # 模型回了一整段 [21..39]：必须截断到 3 个场景
    merged = {0: list(range(21, 40)), 1: [5], 2: [10]}
    base = {0: [3, 4], 1: [6, 7], 2: [11, 12]}
    out = M._sanitize_alignment(merged, base, sc, 3)
    assert len(out[0]) <= 3
    # 1 落在 5（50s），比 0 的起点晚但比 0 的截断终点早 → 保序纠正
    firsts = [out[i][0] for i in range(3)]
    assert firsts == sorted(firsts), out


def test_parse_alignment_accepts_string_ids():
    """模型会把编号写成字符串（"18"），不能因此整批作废。"""
    obj = {'对齐': [{'解说词': '18', '场景': ['50']},
                    {'解说词': '19', '场景': [51, 52]}]}
    sc = [True] * 60
    al = M._parse_alignment_obj(obj, sc)
    assert al == {18: [50], 19: [51, 52]}, al

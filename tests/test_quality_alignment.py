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


def _wild_jump_scenes():
    """复现 commit 3585294 记载的真机事故形状：

    44 个「巡逻车 N 号对讲机」场景铺满 0~852s → 5 个稀疏「医院走廊」场景
    852~1212s → 76 个密集「病房」场景 1212s 之后（只有第 1 个带
    「花枯了 / 走廊尽头」这个唯一实义信号）。

    事故原貌：第 12 句解说（讲「花枯了之后走廊尽头出现了她」）从 852 秒
    直接飞到 1212 秒，跳过了全部走廊戏。
    """
    sc = []
    for i in range(44):
        s = i * (852.0 / 44)
        sc.append({'start': s, 'end': s + 852.0 / 44, 'location': '街道',
                   'event': '巡逻车里啃汉堡，%d号对讲机响了' % i,
                   'summary': '巡逻车 %d号对讲机' % i})
    for j in range(5):
        s = 852.0 + j * 72.0
        sc.append({'start': s, 'end': s + 72.0, 'location': '医院走廊',
                   'event': '医院走廊里人来人往脚步声很轻', 'summary': '医院走廊'})
    for j in range(76):
        s = 1212.0 + j * (288.0 / 76)
        d = ({'location': '病房', 'event': '花枯了之后她在走廊尽头出现',
              'summary': '花枯了 走廊尽头 她'} if j == 0 else
             {'location': '病房', 'event': '病房里输液管滴答作响',
              'summary': '病房输液管'})
        sc.append({'start': s, 'end': s + 288.0 / 76, **d})
    return sc


def _wild_jump_beats():
    beats = []
    for i in range(52):
        if i < 12:
            beats.append('他抓起%d号对讲机，低声说了一句，然后推开车门下车。' % i)
        elif i < 16:
            beats.append('花枯了之后，走廊尽头出现了她，一切都变了。')
        else:
            beats.append('病房里输液管一滴一滴，时间就这么过去了。')
    return beats


def test_jump_cap_does_not_scale_away_wild_jump_protection():
    """【不开倒车】上限按 vdur/n 抬高后，852s→1212s 的狂跳必须依旧被挡住。

    这是 commit 3585294 加跳跃上限的原始诉求。该夹具 52 句 / 1500s →
    vdur/n = 28.8s，上限抬到 28.8s（仅比原 20s 高 8.8s），狂跳仍被拦在
    < 1000s（实测第 12 句落在 996s；无上限时会飞到 1212s、单句跃迁 379.4s）。
    """
    sc, beats = _wild_jump_scenes(), _wild_jump_beats()
    n = len(beats)
    al = M._algo_align_beats_to_scenes(beats, sc,
                                      max_jump_seconds=M.NAR_MAX_JUMP_SECONDS)
    assert len(al) == n, len(al)
    starts = [sc[al[i][0]]['start'] for i in range(n)]
    # ① 单调不倒流
    assert starts == sorted(starts), starts
    # ② 第 12 句（「花枯了」）不得飞到 1212s 的密集病房区
    assert starts[12] < 1000, ('狂跳重现：第12句落在 %.0fs' % starts[12])
    # ③ 全程单句跃迁受上限约束（生效上限 = max(20, 1500/52) = 28.8s）
    cap = M._effective_jump_cap(sc, n, M.NAR_MAX_JUMP_SECONDS)
    assert cap < 30.0, cap                     # 「只抬不降」：抬幅极小
    for i in range(n - 1):
        d = sc[al[i + 1][0]]['start'] - sc[al[i][-1]]['end']
        assert d <= cap + 1e-6, ('beat%d 跃迁 %.1fs 超过 cap %.1fs' % (i, d, cap))
        assert d >= -1e-6, ('beat%d 倒流 %.1fs' % (i, d))


def test_effective_jump_cap_scales_with_beats_and_never_shrinks():
    """上限 = max(显式值, vdur/n)：句数少时必须抬高（否则远处语义锚不可达），
    句数多时保持不变（不许降低，降低会改坏真机行为）。"""
    sc = _big_scenes()                          # 100 × 12s，vdur = 1200
    # 句数少 → 必须抬到 vdur/n：3 句 → 400s（实测 < 180s 就丢医院锚点）
    cap3 = M._effective_jump_cap(sc, 3, M.NAR_MAX_JUMP_SECONDS)
    assert abs(cap3 - 400.0) < 1e-6, cap3
    # 句数多 → 只抬不降：52 句 → max(20, 1200/52) = 23.08s
    cap52 = M._effective_jump_cap(sc, 52, M.NAR_MAX_JUMP_SECONDS)
    assert abs(cap52 - 1200.0 / 52.0) < 1e-6, cap52
    assert cap52 > M.NAR_MAX_JUMP_SECONDS
    # 显式上限更大时以显式值为准（只抬不降）
    assert M._effective_jump_cap(sc, 52, 90.0) == 90.0
    # 句数为 0 / 无场景 → 退回基数，不得抛异常
    assert M._effective_jump_cap(sc, 0, M.NAR_MAX_JUMP_SECONDS) > 0
    assert M._effective_jump_cap(None, 5, M.NAR_MAX_JUMP_SECONDS) > 0


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

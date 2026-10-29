"""剧情驱动·留/剪决策单测（纯函数，不触碰模型/ffmpeg）。

背景：此前「剧情驱动剪辑」的剧情重要度（key/advance/transition）没有真正进入
「保留/剪除」决策——outline 被写死成全保留，且「智能剪辑」门槛（铺满率>98%）
让目标时长几乎不触发，成片基本还是全片长度。

本文件锁定 _decide_keep_beats 的契约：
1. 目标时长≈全片（或无效）→ 全保留（不破坏旧默认行为）
2. 目标时长明显短于全片 → 进入精剪：key 必留、transition 最先剪、advance 凑预算
3. 保底：至少留少量关键节拍，避免目标过短把故事剪得支离破碎
4. 重要度排序 key(3) > advance(2) > transition(0)
"""

import webui_server as S  # noqa: F401  必须先导入入口以解开 video_render 循环依赖
import movie_narrator as mn


def _budget_case(beat_imp, beat_dur, target_sec, vdur):
    return mn._decide_keep_beats(beat_imp, beat_dur, target_sec, vdur)


def test_full_mode_keeps_all():
    # 自动（target=None）→ 全保留
    beat_imp = ['key', 'advance', 'transition', 'advance']
    beat_dur = {0: 30, 1: 30, 2: 30, 3: 30}
    assert _budget_case(beat_imp, beat_dur, None, 120) == {0, 1, 2, 3}
    # target==vdur → 不浓缩，全保留
    assert _budget_case(beat_imp, beat_dur, 120, 120) == {0, 1, 2, 3}
    # target 略低于 vdur（<0.98 边界内仍视作全片）→ 全保留
    assert _budget_case(beat_imp, beat_dur, 119, 120) == {0, 1, 2, 3}


def test_condense_keeps_key_drops_transition():
    beat_imp = ['key', 'advance', 'transition', 'advance']
    beat_dur = {0: 30, 1: 30, 2: 30, 3: 30}
    # 目标 60s（<0.98*120=117）→ 精剪
    kept = _budget_case(beat_imp, beat_dur, 60, 120)
    assert 0 in kept, 'key 节拍必须保留'
    assert 2 not in kept, 'transition（最低重要度）应最先被剪除'
    # 重要度排序正确
    assert mn._imp_rank('key') > mn._imp_rank('advance') > mn._imp_rank('transition')


def test_small_budget_respects_floor():
    beat_imp = ['key', 'advance', 'transition', 'advance', 'advance']
    beat_dur = {0: 30, 1: 30, 2: 30, 3: 30, 4: 30}
    # 目标 20s 远小于总时长，但保底至少留 2 个节拍（避免退化成空片）
    kept = _budget_case(beat_imp, beat_dur, 20, 150)
    assert len(kept) >= 2
    assert 0 in kept, 'key 必留'


def test_tiny_target_keeps_key_spine():
    # 8 个节拍，目标只给 10s：应保留关键节拍骨架，而不是一刀切
    beat_imp = ['key', 'advance', 'transition', 'advance',
                'key', 'transition', 'advance', 'advance']
    beat_dur = {i: 20 for i in range(8)}
    kept = _budget_case(beat_imp, beat_dur, 10, 160)
    assert 0 in kept and 4 in kept, '两个 key 关键节拍都应保留'
    # 两个 key 都被保留（rank=3 永不被预算截断）
    assert len(kept & {0, 4}) == 2


def test_advance_trimmed_before_key():
    # 全 advance 但目标很短：advance 会被剪，key 永不剪
    beat_imp = ['key', 'advance', 'advance', 'advance']
    beat_dur = {0: 40, 1: 40, 2: 40, 3: 40}
    kept = _budget_case(beat_imp, beat_dur, 45, 160)
    assert 0 in kept
    # key(40s) 已占满预算，后续 advance 不再加入（除非保底）
    assert len(kept) >= 2  # key + 保底至少再 1 个

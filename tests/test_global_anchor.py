# -*- coding: utf-8 -*-
"""v1.4.87 全局字幕锚定回归钉。

主人实测（2026-11-14）：「解说都已经到工地砸墙了，画面还在勒人，词已经到
肯尼迪机场，但是画面还在贩毒集团进家门」——除段内时刻无人管（v1.4.86 修），
更早的整块错位来自 DP 跳跃上限：稿子讲到 1154s 的互助会，DP 只能就近分到
784s 的工地（_effective_jump_cap≈vdur/n，远处场景不可达）。本轮让每句解说
带着文本到全片字幕 cue 里找证据锚点——字幕是地面真值。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401  先导宿主（narration_align 尾部回注依赖它）
import movie_narrator as M  # noqa: E402
from narration_align import (  # noqa: E402
    _event_anchored_spans, _global_subtitle_anchors, _bigrams_cn, _lcs_contig)


def _cue(start, text, dur=2.5):
    return {'start': float(start), 'end': float(start) + dur, 'text': text}


# 惩罚者前 20 分钟形态：主角名高频出现（df 高 → IDF 降权），场景词稀缺（df 低）
NOISY_NAME_CUES = [
    _cue(1221.0, '你呢？弗兰克'), _cue(1252.0, '弗兰克卡索死了'),
    _cue(730.0, '彼特，对吧？弗兰克'), _cue(899.7, '唐尼，谢谢你的三明治'),
    _cue(365.0, '对，唐尼夏维兹'), _cue(841.4, '我爸外派了三次，一次阿富汗'),
    _cue(205.0, '你赢了，你杀了每一个人，弗兰克'), _cue(1030.0, '弗兰克，你到底想干什么'),
    _cue(1120.0, '替我问弗兰克好'), _cue(475.0, '弗兰克这个人靠得住吗'),
    _cue(1188.0, '我觉得不是巧合'), _cue(107.0, '他来了！'),
]


# 惩罚者前 20 分钟拟真语料：主角名高频出现（df 高 → 主角名惩罚生效），场景词稀缺
NOISY_NAME_CUES = [
    _cue(1221.0, '你呢？弗兰克'), _cue(1252.0, '弗兰克卡索死了'),
    _cue(730.0, '彼特，对吧？弗兰克'), _cue(899.7, '唐尼，谢谢你的三明治'),
    _cue(365.0, '对，唐尼夏维兹'), _cue(841.4, '我爸外派了三次，一次阿富汗'),
    _cue(205.0, '你赢了，你杀了每一个人，弗兰克'), _cue(1030.0, '弗兰克，你到底想干什么'),
    _cue(1120.0, '替我问弗兰克好'), _cue(475.0, '弗兰克这个人靠得住吗'),
    _cue(1188.0, '我觉得不是巧合'), _cue(107.0, '他来了！'),
]

# 拟真解说稿：主角名（弗兰克/唐尼）在多数句里出现——句侧 df 高触发主角名惩罚
SIM_NARR = [
    '阿拉巴马乡间，弗兰克开皮卡把飞车党逼下摩托，碾过去。',          # 0 具象(弗兰克)
    '墨西哥华雷斯，贩毒集团最后一个成员刚进家门，狙击子弹替他收了尾。',  # 1 具象
    '弗兰克把那件印着骷髅标志的防弹背心烧了。',                      # 2 具象(弗兰克)
    '他来最早、走最晚，工友却恨他——加班费反而缩水。',               # 3 具象
    '砸墙不是劳动，是他给自己找的镇静剂。',                          # 4 议论
    '工地来了个新人唐尼，拿着三明治上天台找弗兰克搭话。',            # 5 具象(弗兰克+唐尼)
    '当晚，弗兰克去了退伍战友柯蒂斯的军人互助会，故意等散会才进去，只为还一本书。',  # 6 具象(弗兰克)
    '他怕的不是回忆，是遗忘。痛感是他最后一样还能证明「我还在」的东西。',  # 7 议论
]


def test_pain_case_370s_misplacement_fixed():
    """主人最痛的整块错位：互助会解说被 DP 分到 784s 工地段，锚点应修到 1154s。

    多句拟真语料：主角名「弗兰克」在 8 句里出现 5 次（句侧 df 高 → 惩罚生效），
    「柯蒂斯/互助会/还书」只在 1 句——专属场景词胜出人名噪音。"""
    narr = SIM_NARR
    voice = {i: 6.0 for i in range(len(SIM_NARR))}
    cues = NOISY_NAME_CUES + [_cue(1154.7, '聚会不错，柯蒂斯'), _cue(1166.4, '我看书很慢')]
    segs = [(38.7, 45.8), (84.5, 91.5), (224.4, 230.4), (352.0, 358.0),
            (500.0, 506.0), (633.7, 639.7), (784.4, 791.7), (1240.0, 1257.5)]
    got = _global_subtitle_anchors(segs, narr, cues, voice)
    assert 6 in got, '互助会句没锚定: %r' % got
    assert 1150 <= got[6] <= 1160, '互助会句应锚在聚会 cue（1154.7），落在 %s' % got
    # 端到端：窗口起点跟着锚走（句 6 的 DP 段错在 784，锚应把它修到 1154.7）
    segs[6] = (784.4, 791.7)
    spans = _event_anchored_spans(segs, narr, cues, voice)
    assert spans[6][0] >= 1150.0, '互助会句窗口应从互助会场景起播: %s' % (spans[6],)


def test_idf_suppresses_common_name_noise():
    """人名噪音 cue（主角名全片高频）被主角名惩罚降权，专属场景词胜出。"""
    narr = SIM_NARR
    voice = {i: 6.0 for i in range(len(SIM_NARR))}
    anchors = _global_subtitle_anchors([(38.7, 45.8), (84.5, 91.5), (224.4, 230.4), (352.0, 358.0),
                                        (500.0, 506.0), (633.7, 639.7), (784.4, 791.7), (1240.0, 1257.5)],
                                       narr,
                                       NOISY_NAME_CUES + [_cue(1154.7, '聚会不错，柯蒂斯')], voice)
    for i, t in enumerate(anchors.values()):
        assert t != 1221.0, '句 %d 锚到了「你呢？弗兰克」人名噪音: %s' % (i, anchors)


def test_window_agg_favors_first_mentioned():
    """一句解说提到两件事（进门→狙击）：聚合偏向先讲到的 cue（叙事顺序起播）。"""
    segs = [(84.5, 115.1)]
    narr = ['贩毒集团最后一个成员刚进家门，一颗狙击子弹替他收了尾。']
    cues = [_cue(85.0, '刚进家门，老婆孩子都在等他'),
            _cue(108.0, '一颗从埃尔帕索飞来的狙击子弹')]
    got = _global_subtitle_anchors(segs, narr, cues, {0: 7.9})
    assert 0 in got and got[0] <= 90.0, '应锚到先讲到的进门（85s），得到 %s' % got
    spans = _event_anchored_spans(segs, narr, cues, {0: 7.9})
    a, b = spans[0]
    assert a <= 85.5 and b >= 89.0, '窗口应盖住进门台词: %s' % (spans[0],)


def test_lis_keeps_output_order():
    """倒序候选被 LIS 丢弃：句 1 的锚比句 0 早时，保句序、句 1 不锚。"""
    segs = [(100.0, 110.0), (200.0, 210.0)]
    narr = ['工友拿他当笑话，把火全砌进了墙里。', '柯蒂斯的军人互助会里还一本书。']
    cues = [_cue(870.0, '你有家人吗？好好照顾自己'),
            _cue(1154.7, '聚会不错，柯蒂斯还书'),
            _cue(600.0, '笑话与墙里的火')]
    got = _global_subtitle_anchors(segs, narr, cues, {0: 6.0, 1: 5.0})
    if 0 in got and 1 in got:
        assert got[0] < got[1], '锚点必须随句序递增: %s' % got
    spans = _event_anchored_spans(segs, narr, cues, {0: 6.0, 1: 5.0})
    assert spans[0][0] <= spans[1][0] + 0.6, '窗口必须保序: %s' % (spans,)


def test_tail_space_downgrades_anchor():
    """锚点贴近片尾：窗内盖住锚、不越片尾、后句仍保住念完空间；
    实在装不下时降级不锚（不把后句挤进片尾黑场）。"""
    segs = [(0.0, 10.0), (1240.0, 1257.5)]   # 末句贴片尾（锚空间合法）
    narr = ['柯蒂斯的互助会里还一本书。', '他怕时间一长把那段经历忘了。']
    cues = [_cue(1250.0, '柯蒂斯互助会还书'),
            _cue(1255.0, '把那段经历忘了')]
    spans = _event_anchored_spans(segs, narr, cues, {0: 6.0, 1: 6.0})
    for a, b in spans:
        assert b <= 1257.5 + 1e-6, '不许越过片尾: %s' % (spans,)
    assert spans[1][1] >= 1257.5 - 1.0, '末句必须保住念完空间: %s' % (spans,)
    assert spans[0][1] <= spans[1][0] + 1e-6, '不许重叠: %s' % (spans,)


def test_quote_and_blank_untouched():
    """引用/留白窗原样（引用检测已对准台词时刻；输出按句序拼接，源轴倒跳=闪回）。"""
    segs = [(905.4, 907.2), (1154.7, 1161.1)]
    narr = ['但我不是在找同伴', '当晚，弗兰克去了退伍战友柯蒂斯的军人互助会。']
    cues = [_cue(905.4, '但我不是在找同伴'), _cue(1154.7, '聚会不错，柯蒂斯')]
    kinds = [M.KIND_QUOTE, M.KIND_CONCRETE]
    spans = _event_anchored_spans(segs, narr, cues, {1: 5.6}, beat_kinds=kinds)
    assert spans[0] == (905.4, 907.2), '引用窗不许被锚定改动: %s' % (spans[0],)
    assert spans[1][0] >= 1150.0, '具象句正常锚定: %s' % (spans[1],)


def test_no_cues_no_voice_fall_back():
    """无 cue / 无配音 → 原窗返回（与 v1.4.86 行为一致）。"""
    segs = [(84.5, 115.1)]
    narr = ['贩毒集团最后一个成员刚进家门。']
    assert _event_anchored_spans(segs, narr, [], {0: 7.9}) == [(84.5, 115.1)]
    assert _event_anchored_spans(segs, [''], [_cue(85.0, '台词')], {}) == [(84.5, 115.1)]


def test_real_run_anchor_table():
    """主人 run-065419 的关键句锚点表（地面真值来自主人提供的 SRT）：
    坑洞故事 919.5 / 泥士兵跳坑 979.8 / 互助会 1154.7 / 科提斯对话 1245.2。"""
    plan_snip = [
        (18, '唐尼临走前讲了个故事：一个士兵掉进坑里，军士丢铲子，军官丢水桶，心理医生给药让他忘掉坑洞，药吃完了坑还在。', 919.5),
        (19, '最后来了个满身泥的士兵，跟他一样上过战场，二话不说跳进坑里，说：“冷静，兄弟，我是过来人，我知道怎么出去。”', 979.8),
        (21, '当晚，弗兰克去了退伍战友柯蒂斯的军人互助会，故意等散会才进去，只为还一本书。', 1154.7),
        (22, '柯蒂斯劝他：“弗兰克·卡索死了，彼特·卡索还活着。”弗兰克回：“我是死了，科提斯，你没听说吗？”', 1245.2),
    ]
    cues = [_cue(919.5, '我听过一个故事'), _cue(954.1, '“这个可以帮助你忘掉坑洞”'),
            _cue(979.8, '他说：“冷静，兄弟，我是过来人，我知道怎么出去”'),
            _cue(1154.7, '聚会不错，柯蒂斯'), _cue(1166.4, '-有一阵子没见了 -我看书很慢'),
            _cue(1245.2, '-我是死了，科提斯，你没听说吗？ -'), _cue(1254.4, '彼特卡斯帝欧尼...还活着'),
            _cue(1221.0, '你呢？弗兰克'), _cue(1252.0, '弗兰克卡索死了')]
    # 拟真语境：SIM_NARR 提供「弗兰克」高频的句侧分布（主角名惩罚生效的前提）。
    # 句 19/22 含直接引语被句类判定为 quote → 走引用让位检测，不参与全局锚定。
    segs = [(735.2, 744.4), (747.2, 756.7), (760.2, 768.0), (771.2, 780.0)]   # DP 给的（错）位置
    segs[3] = (1240.0, 1257.5)   # 末句在片尾（锚空间合法）
    narr = SIM_NARR + [t for _, t, _ in plan_snip]
    voice = {i: 6.0 for i in range(len(narr))}
    got = _global_subtitle_anchors(segs, narr, cues, voice)
    for seq_in_tail, want in ((0, 919.5), (2, 1154.7)):
        seq = len(SIM_NARR) + seq_in_tail
        assert seq in got, '句没锚定: %r' % got
        assert abs(got[seq] - want) <= 2.0, \
            '应锚 %.1f，落在 %.1f' % (want, got[seq])


def test_helpers():
    """bigram/LCS 原子：标点剥离、专名整段命中。"""
    assert _bigrams_cn('聚会不错，柯蒂斯') == {'聚会', '会不', '不错', '柯蒂', '蒂斯'}
    assert _lcs_contig('彼特·卡索还活着', '彼特卡斯帝欧尼...还活着') >= 3
    assert _lcs_contig('完全不同', '毫无交集') == 0


def test_exported_from_host():
    """主模块可导（拆分族约定：公共符号经 movie_narrator re-export）。"""
    assert M._global_subtitle_anchors is _global_subtitle_anchors
    assert M._event_anchored_spans is _event_anchored_spans

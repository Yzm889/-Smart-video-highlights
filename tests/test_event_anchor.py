# -*- coding: utf-8 -*-
"""v1.4.86 句级事件锚点回归钉。

主人实测（2026-11-14）：「解说都已经到工地砸墙了，画面还在勒人，词已经到
肯尼迪机场，但是画面还在贩毒集团进家门」——画面窗口从整场戏开头起播，
解说词跑在画面前面。修法：每句解说的画面窗口对到「这句描述的事件时刻」
（段内与本句文本最像的字幕台词 cue，字幕是地面真值）。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401  先导宿主（narration_align 尾部回注依赖它）
from narration_align import _event_anchored_spans  # noqa: E402


# 惩罚者 S01E01 前二十分钟的华雷斯场景（主人真实数据的形态）
SPAN = (84.5, 115.1)
CUES = [
    {'start': 85.0, 'end': 89.0, 'text': '刚进家门，老婆孩子都在等他'},
    {'start': 108.0, 'end': 113.0, 'text': '一颗从埃尔帕索飞来的狙击子弹'},
]
NARR = '墨西哥华雷斯，贩毒集团最后一个成员刚进家门，一颗狙击子弹替他收了尾。'


def test_anchor_lands_on_matching_cue():
    """解说按叙事顺序提到 进家门→狙击收尾：窗口从**最先讲到的内容**（85s 进门台词）
    起播、按微变速窗延伸——解说念到哪句，画面就从那句的内容开始演。

    （9.1s 的窗口装不下 30s 的整场戏，狙击时刻在窗外——快进与截尾二选一，
      按主人红线选截尾；两全方案=E1 事件级锚点细化。）"""
    got = _event_anchored_spans([SPAN], [NARR], CUES, {0: 7.9})
    a, b = got[0]
    ln = b - a
    assert ln <= 7.9 * 1.15 + 1e-6, '窗口长 %.2f 超出微变速窗' % ln
    assert a <= 85.5, '窗口起点 %.1f 应贴住最先讲到的内容（85s 进门台词）' % a
    assert b >= 89.0, '窗口 %.1f-%.1f 没盖住进门台词' % (a, b)


def test_unrelated_cue_keeps_span_start():
    """段内台词与解说完全不像 → 保持原窗口起点（宁可不动，不许瞎跳）。"""
    cues = [{'start': 85.0, 'end': 89.0, 'text': '今天天气不错适合放风筝'},
            {'start': 108.0, 'end': 113.0, 'text': '明天可能有雨记得带伞'}]
    got = _event_anchored_spans([SPAN], [NARR], cues, {0: 7.9})
    assert got[0][0] == 84.5, '毫无匹配时不该挪窗口起点'


def test_windows_stay_ordered_and_disjoint():
    """多句锚定后仍保序、不重叠（重叠=两句解说叠声）。"""
    cues = [
        {'start': 40.0, 'end': 44.0, 'text': '皮卡把飞车党逼下摩托'},
        {'start': 110.0, 'end': 113.0, 'text': '狙击子弹替他收了尾'},
    ]
    spans = [(38.7, 45.8), (84.5, 115.1)]
    narr = ['弗兰克开皮卡把飞车党逼下摩托，碾了过去。', '贩毒集团最后一个成员刚进家门，狙击子弹收尾。']
    got = _event_anchored_spans(spans, narr, cues, {0: 5.0, 1: 7.9})
    for i in range(len(got) - 1):
        assert got[i][1] <= got[i + 1][0] + 1e-6, \
            '锚定后窗口重叠: %r' % (got,)
    # 句0 锚到 40s 的皮卡 cue（在自己的段内）
    assert 38.7 <= got[0][0] <= 45.8
    # 句1 的窗口必须盖住它的狙击 cue（108-113；段尾收后起点可略前移）
    assert got[1][0] <= 110.5 and got[1][1] >= 113.0 - 0.5, got[1]


def test_clamped_at_span_end_keeps_event():
    """锚点贴近段尾：窗口向前收（[段尾-窗长, 段尾]），事件时刻仍被盖住。"""
    cues = [{'start': 113.5, 'end': 115.0, 'text': '狙击子弹收尾'}]
    got = _event_anchored_spans([(84.5, 115.1)], ['贩毒集团的狙击收尾。'],
                                cues, {0: 7.9})
    a, b = got[0]
    assert b <= 115.1 + 1e-6
    assert a >= 115.1 - 7.9 * 1.15 - 0.1, '窗口长 %.2f 超出微变速窗' % (b - a)
    assert b >= 113.5, '段尾的事件时刻必须被窗口盖住'


def test_no_voice_and_no_cues_untouched():
    """引用/留白段（无配音）与无 cue 场景 → 原样保留。"""
    got = _event_anchored_spans([(84.5, 115.1)], [''], CUES, {})
    assert got[0] == (84.5, 115.1)
    got2 = _event_anchored_spans([(84.5, 115.1)], [NARR], [], {0: 7.9})
    assert got2[0] == (84.5, 115.1)


def test_wired_before_fit_in_narrate_movie():
    """源码钉：锚定必须在 fit 之前（fit 的补长/裁剪在锚定后的窗口上工作）。"""
    with open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8') as f:
        src = f.read()
    i = src.index('def narrate_movie(')
    body = src[i:]
    assert '_event_anchored_spans(segs, narr, asr, _vdur' in body
    assert body.index('_event_anchored_spans(') < body.index('_fit_spans_to_voice(segs'), \
        '锚定必须先于 fit'
    with open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8') as f:
        pass
    imp = src.index('from narration_align import')
    assert '_event_anchored_spans' in src[imp:imp + 400], '导入清单必须带上锚点函数'

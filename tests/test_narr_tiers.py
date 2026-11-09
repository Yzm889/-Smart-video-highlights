# -*- coding: utf-8 -*-
"""v1.4.27 回归钉：画面理解四档 + 「无台词高光镜头」不再被剪掉。

背景（用户 2026-11-09）：
「不放识别画面只看台词，最大的问题是稿子中需要播放打戏、对视感情戏这些
没有台词的精彩地方会被剪掉。画面模型做的就是让『他们深情地吻在了一起』
这种重点画面能保留下来并解说对应上。」

三个必须钉住的事实：
  1. 四档的差异是「覆盖 × 每帧字量」，不是简单的帧数加减；
  2. 极速档明确不覆盖静默段（否则用户以为它也能保住高光）；
  3. 中文 bigram 对同义动作是瞎的 ——「接吻」与「吻在一起」共享 0 个 bigram，
     必须靠动作词归一把它们连起来，否则带动作的镜头永远匹配不上。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401  （先导宿主，避免循环导入）
import movie_narrator as MN  # noqa: E402


def test_four_tiers_exist_and_ordered_by_cost():
    order = MN.NARR_TIER_ORDER
    assert order == ('speed', 'balanced', 'quality', 'ultra')
    ivals = [MN.NARR_TIERS[t]['interval'] for t in order]
    assert ivals == sorted(ivals, reverse=True), '档位间隔应逐级收紧'
    assert [MN.NARR_TIERS[t]['width'] for t in order] == [448, 512, 640, 720]


def test_speed_tier_does_not_cover_silent_shots():
    """极速档只看台词附近——必须与平衡档区分开，否则用户会被误导。"""
    assert MN.NARR_TIERS['speed']['dialogue_filter'] is True
    for t in ('balanced', 'quality', 'ultra'):
        assert MN.NARR_TIERS[t]['dialogue_filter'] is False, '%s 必须看全片' % t


def test_point_counts_scale_as_documented():
    """20 分钟素材：极速约 40 点、平衡约 100 点、质量 150 点、极致 240 点。"""
    vdur = 1200.0
    def pts(t):
        c = MN.NARR_TIERS[t]
        interval = max(float(c['interval']), vdur / float(c['max_points']))
        return int(vdur // interval)
    assert 30 <= pts('speed') <= 45
    assert 90 <= pts('balanced') <= 110
    assert 140 <= pts('quality') <= 160
    assert 220 <= pts('ultra') <= 260


def test_event_field_survives_every_tier():
    """任何档位都必须保留 event —— 它是唯一能匹配"吻在一起/挥拳"的动作信号。"""
    for fields in (1, 3, 5):
        spec = MN._tier_field_spec(fields)
        assert 'event' in spec, '档位 %d 字段丢了 event' % fields
    assert 'dialogue' not in MN._tier_field_spec(3), '3 字段档不该让模型复述台词'


def test_tier_resolution_priority(monkeypatch):
    monkeypatch.delenv('NARRATE_TIER', raising=False)
    assert MN.narr_tier({}) == 'balanced'
    assert MN.narr_tier({'narrTier': 'ultra'}) == 'ultra'
    assert MN.narr_tier({'narrTier': '乱填'}) == 'balanced'
    monkeypatch.setenv('NARRATE_TIER', 'quality')
    assert MN.narr_tier({}) == 'quality'
    assert MN.narr_tier({'narrTier': 'speed'}) == 'speed', 'params 优先级高于环境变量'


def test_action_tokens_unify_synonyms():
    """「接吻」与「吻在一起」字面 0 重合，但动作 token 必须相同。"""
    a = MN._action_tokens('两人相拥接吻')
    b = MN._action_tokens('他们深情地吻在了一起')
    assert 'ACT:吻' in a and 'ACT:吻' in b
    assert MN._action_tokens('挥拳打中对方') & MN._action_tokens('一拳把他打翻')
    assert not MN._action_tokens('两人在办公室谈话')


def test_kiss_scene_wins_over_chat_scene():
    """核心用例：解说词讲吻戏时，必须挑到接吻的镜头，而不是台词更多的闲聊镜头。"""
    scenes = [
        {'start': 0.0, 'end': 10.0, 'location': '咖啡馆', 'event': '两人坐着交谈'},
        {'start': 10.0, 'end': 20.0, 'location': '街道', 'event': '两名男子挥拳斗殴'},
        {'start': 20.0, 'end': 30.0, 'location': '卧室', 'event': '两人相拥接吻'},
    ]
    got = MN._algo_align_beats_to_scenes(['他们深情地吻在了一起'], scenes)
    assert got.get(0) == [2], '应命中接吻镜头（scene 2），实际 %r' % (got.get(0),)


def test_fight_scene_wins_over_neutral_scene():
    scenes = [
        {'start': 0.0, 'end': 10.0, 'location': '走廊', 'event': '人物走过'},
        {'start': 10.0, 'end': 20.0, 'location': '仓库', 'event': '一人挥拳打中另一人'},
    ]
    got = MN._algo_align_beats_to_scenes(['他一拳把对方打翻在地'], scenes)
    assert got.get(0) == [1]


def test_vlm_cache_key_carries_tier():
    """换档位必须换缓存键，否则会拿上一档的抽样结果糊弄。"""
    src = open(MN.__file__, encoding='utf-8').read()
    assert "'vlm_%s_%s_%s'" in src
    assert "'frames_{int(interval)}{_w}'" in open(
        os.path.join(ROOT, 'cache_utils.py'), encoding='utf-8').read()


def test_frontend_exposes_tier_selector():
    html = open(os.path.join(ROOT, 'static', 'desktop', 'index.html'),
                encoding='utf-8').read()
    js = open(os.path.join(ROOT, 'static', 'desktop', 'desktop.js'),
              encoding='utf-8').read()
    assert 'id="npTier"' in html
    for v in ('speed', 'balanced', 'quality', 'ultra'):
        assert 'value="%s"' % v in html
    assert 'narrTier:' in js, '前端必须把档位发给后端'

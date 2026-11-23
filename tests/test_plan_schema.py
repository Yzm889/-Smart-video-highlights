# -*- coding: utf-8 -*-
"""plan.json 一等公民层测试：遗留导入/双轴约定/校验/原子读写。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plan_schema as PS  # noqa: E402


def _tts_state_fixture():
    """两步路径 tts_state.json 的真实形状（惩罚者桌面轮实存字段）。"""
    return {
        'video_path': r'C:\v\test10b.mp4',
        'movie_name': '惩罚者',
        'segs': [[0.0, 5.9], [5.9, 15.6], [26.4, 33.8], [218.8, 222.6]],
        'narr': ['第一句', '第二句', '米奇求他：“杀了我。”', '（留白）'],
        'narr_map': [0, 1, 2, 3],
        'params': {'skipTts': True, 'burn': False},
        'tts_results': [[0, 'run/narr0.mp3'], [1, 'run/narr1.mp3'], [2, 'run/narr2.mp3']],
        'quote_slots': {'3': {'text': '杀了我。', 'start': 218.8, 'end': 222.6,
                              'how': 'literal', 'quote': '杀了我。'}},
        'tts_failures': [],
        'asr': [{'start': 0.0, 'end': 2.0, 'text': 'hi'}],
        'cut_plan': {'coverage': 14.0, 'segments': [
            {'start': 0.0, 'end': 33.8, 'keep': True},
            {'start': 33.8, 'end': 99.2, 'keep': False}]},
        'scene_story': {'1': {'story': 'x'}},
    }


def test_import_tts_state_two_step():
    plan = PS.plan_from_tts_state(_tts_state_fixture(), is_blank=lambda t: '留白' in t)
    assert plan['version'] == PS.PLAN_VERSION
    assert plan['meta']['mode'] == 'two-step'
    assert plan['meta']['movie'] == '惩罚者'
    segs = plan['segments']
    assert len(segs) == 4
    # 双轴：two-step 导入填 src、out=None
    assert segs[0]['src'] == [0.0, 5.9]
    assert segs[0]['out'] is None
    # 引用段：quote_slots 命中 → KIND_QUOTE + quote 详情
    q = segs[3]
    assert q['kind'] == PS.KIND_QUOTE
    assert q['quote']['src_start'] == 218.8
    assert q['quote']['how'] == 'literal'
    assert q['text'] == '（留白）'            # 原叙述文本（quote_slots 没写 narr）
    # 留白段：is_blank 命中 → KIND_BLANK
    assert segs[2]['kind'] == PS.KIND_NARRATE or segs[2]['kind'] == PS.KIND_BLANK
    # 音频：tts_results 有的是 narrate，quote 段没有 → audio None
    assert segs[0]['audio']['file'] == 'run/narr0.mp3'
    assert segs[3]['audio'] is None
    # 剪辑决策导入
    assert plan['cuts']['coverage'] == 14.0
    assert plan['cuts']['segments'][1]['keep'] is False
    # 阶段账本
    assert plan['stage_ledger']['scene_story']
    assert plan['stage_ledger']['asr']['count'] == 1


def test_import_final_state_one_shot():
    """一步直出状态 → out 轴（src 宁缺勿错）。"""
    state = {
        'src_video': r'C:\v\src.mp4',
        'segs': [[0.0, 5.9], [5.9, 12.0]],
        'narr': ['一句', '二句'],
        'tts_paths': [['run/a.mp3', 0.0, 5.9], ['run/b.mp3', 5.9, 12.0]],
        'voice_spans': {'0': [0.0, 5.4]},
        'music_path': 'run/bgm.mp3',
        'params': {},
        'final': r'C:\out\final.mp4',
    }
    plan = PS.plan_from_final_state(state)
    assert plan['meta']['mode'] == 'one-shot'
    segs = plan['segments']
    assert segs[0]['out'] == [0.0, 5.9]
    assert segs[0]['src'] is None              # 宁缺勿错
    assert segs[0]['voice_span'] == [0.0, 5.4]
    assert segs[1]['audio']['duration'] == 6.1
    assert plan['music'] == 'run/bgm.mp3'
    assert plan['stage_ledger']['render']['final'] == r'C:\out\final.mp4'


def test_save_load_roundtrip(tmp_path):
    plan = PS.plan_from_tts_state(_tts_state_fixture())
    fp = str(tmp_path / 'plan.json')
    assert PS.save_plan(fp, plan)
    loaded = PS.load_plan(fp)
    assert loaded['segments'] == plan['segments']
    assert loaded['meta'] == plan['meta']


def test_validate_invariants():
    plan = PS.new_plan()
    plan['segments'] = [
        PS.segment(0, src=[0, 5], out=[0, 5]),
        PS.segment(1, kind=PS.KIND_QUOTE, quote=None),          # quote 缺详情
        PS.segment(2, out=[3, 8]),                              # 与上一段重叠
    ]
    issues = PS.validate_plan(plan)
    assert any('quote' in x for x in issues)
    assert any('重叠' in x for x in issues)
    # 干净 plan 通过
    plan['segments'][1] = PS.segment(1, kind=PS.KIND_QUOTE,
                                     quote={'text': 'x', 'src_start': 1, 'src_end': 2})
    plan['segments'][2] = PS.segment(2, out=[5.1, 8])
    assert PS.validate_plan(plan) == []


def test_validate_rejects_garbage():
    assert PS.validate_plan({'version': 99})      # version 错 → 有 issue
    assert PS.validate_plan('nope') != []          # 非字典
    assert PS.load_plan('/nonexistent/plan.json') is None


def test_segment_kind_constant():
    assert PS.KIND_QUOTE != PS.KIND_NARRATE != PS.KIND_BLANK != PS.KIND_BROLL

# -*- coding: utf-8 -*-
"""v1.4.88 全篇一次 LLM 对齐回归钉。

主人拍板（2026-11-14）：「质量最好路线、不惜时间」——旧分批对齐批间打架被弃用
（错的不是 LLM 对齐，是分批无上下文）；DP 词对词吸附会重复错配（10 分钟一个大锤、
20 分钟又一个大锤）。全篇一次推理 = 模型带着完整叙事选段，理解归模型、秩序归代码。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import movie_narrator as M  # noqa: E402


SCENES = [{'start': 0.0 + i * 100.0, 'end': 60.0 + i * 100.0} for i in range(8)]
NARR = ['弗兰克在工地抡大锤砸墙。', '解说二关于互助会。', '解说三唐尼搭话。', '解说四尾声。']


def _patch_model(monkeypatch, resp, available=True):
    monkeypatch.setattr(M, '_local_model_available', lambda: available)
    monkeypatch.setattr(M, '_llm_light', lambda *a, **k: resp)


def test_happy_path(monkeypatch):
    resp = '[{"i": 0, "s": 2, "cue": "砸墙", "why": "抡大锤对砸墙场景"},' \
           '{"i": 1, "s": 5, "cue": "聚会不错", "why": "互助会"},' \
           '{"i": 2, "s": 6, "cue": "", "why": "搭话"},' \
           '{"i": 3, "s": 7, "cue": "", "why": "尾声"}]'
    _patch_model(monkeypatch, resp)
    got = M._llm_align_full_script(NARR, SCENES, {}, None, movie_name='测试')
    assert got == {0: [2], 1: [5], 2: [6], 3: [7]}, got


def test_quote_and_out_of_range_dropped(monkeypatch):
    resp = ('[{"i": 0, "s": 99, "cue": "", "why": ""},'    # 场景越界 → 丢
            '{"i": 1, "s": -1, "cue": "", "why": ""},'     # LLM 主动空缺 → 丢
            '{"i": 2, "s": 6, "cue": "", "why": ""},'
            '{"i": 9, "s": 7, "cue": "", "why": ""}]')      # 句号越界 → 丢
    _patch_model(monkeypatch, resp)
    got = M._llm_align_full_script(NARR, SCENES, {}, None)
    assert got == {2: [6]}, got


def test_lis_drops_backward_jump(monkeypatch):
    """LLM 局部倒序（句 2 指到比句 1 更早的段）：保最长递增链，倒序句丢弃回退 DP。"""
    resp = ('[{"i": 0, "s": 2, "cue": "", "why": ""},'
            '{"i": 1, "s": 6, "cue": "", "why": ""},'
            '{"i": 2, "s": 1, "cue": "", "why": ""},'   # 倒序
            '{"i": 3, "s": 7, "cue": "", "why": ""}]')
    _patch_model(monkeypatch, resp)
    got = M._llm_align_full_script(NARR, SCENES, {}, None)
    assert got == {0: [2], 1: [6], 3: [7]}, '倒序句 2 应被丢弃: %s' % got


def test_model_unavailable_returns_empty(monkeypatch):
    _patch_model(monkeypatch, '[]', available=False)
    assert M._llm_align_full_script(NARR, SCENES, {}, None) == {}


def test_bad_json_returns_empty(monkeypatch):
    _patch_model(monkeypatch, '模型胡言乱语没有 JSON')
    assert M._llm_align_full_script(NARR, SCENES, {}, None) == {}


def test_quote_sentences_excluded_even_if_llm_answers(monkeypatch):
    """引用/留白句由句类判定管（走引用让位），LLM 答了也丢弃。"""
    narr = ['弗兰克在工地抡大锤砸墙。', '他只回两个字：“不能。”']
    resp = '[{"i": 0, "s": 2, "cue": "", "why": ""},{"i": 1, "s": 3, "cue": "", "why": ""}]'
    _patch_model(monkeypatch, resp)
    got = M._llm_align_full_script(narr, SCENES, {}, None)
    assert got == {0: [2]}, got


def test_wired_in_user_script_branch():
    """源码钉：用户稿子路线先试全篇对齐、失败回退 DP（保底不变）。"""
    with open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8') as f:
        src = f.read()
    i = src.index('elif _has_user_script:')
    body = src[i:i + 2500]
    assert '_llm_align_full_script(' in body, '用户稿子分支必须先试全篇对齐'
    assert '_algo_align_beats_to_scenes' in src[i:i + 6000] or 'scene_alignment' in body, \
        '回退 DP 的保底不能丢'

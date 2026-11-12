# -*- coding: utf-8 -*-
"""v1.4.53 回归钉：译名自动归一——稿子人名必须对齐到台词字幕的叫法。

主人实测：解说稿讲「柯蒂斯和弗兰克的对话」，画面还是「弗兰克和唐尼」——
柯蒂斯就是字幕里的科特，译名不一致导致语义匹配抓不住。归一在句类标注/对齐
之前执行（对齐/引用让位/TTS 用的都是归一后的 texts）。

运行：pytest tests/test_name_unify.py -q
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server  # noqa: F401,E402  必须先导入宿主，规避循环导入
import movie_narrator as MN  # noqa: E402

_ASR = [{'start': 0.0, 'end': 3.0, 'text': '科特，你还好吗？'},
        {'start': 3.0, 'end': 6.0, 'text': '我很好，法兰克不在了。'}]


def test_unify_replaces_names(monkeypatch):
    """LLM 给出映射 → 稿子里的译名被替换成字幕叫法。"""
    calls = []

    def fake_llm(prompt, system='', timeout=0, **kw):
        calls.append(prompt)
        return '{"mapping": {"柯蒂斯": "科特", "弗兰克": "法兰克"}}'

    monkeypatch.setattr(MN, 'local_llm_chat', fake_llm)
    texts = ['柯蒂斯站在门口', '弗兰克已经死了']
    out, mapping = MN._unify_names_with_dialog(texts, _ASR)
    assert out == ['科特站在门口', '法兰克已经死了'], out
    assert mapping == {'柯蒂斯': '科特', '弗兰克': '法兰克'}
    assert texts == ['柯蒂斯站在门口', '弗兰克已经死了'], '原列表不许被原地改'
    assert '解说稿' in calls[0] and '台词字幕' in calls[0]
    assert '柯蒂斯' in calls[0] and '科特' in calls[0], '两边文本都要给 LLM'


def test_unify_no_mapping_returns_unchanged(monkeypatch):
    monkeypatch.setattr(MN, 'local_llm_chat', lambda *a, **k: '{"mapping": {}}')
    texts = ['柯蒂斯站在门口']
    out, mapping = MN._unify_names_with_dialog(texts, _ASR)
    assert out == texts and mapping == {}


def test_unify_llm_failure_never_breaks(monkeypatch):
    """LLM 挂了/超时 → 原样返回，绝不让归一阻断出片。"""
    def boom(*a, **k):
        raise RuntimeError('LLM 挂了')
    monkeypatch.setattr(MN, 'local_llm_chat', boom)
    texts = ['柯蒂斯站在门口']
    out, mapping = MN._unify_names_with_dialog(texts, _ASR)
    assert out == texts and mapping == {}


def test_unify_garbage_json_never_breaks(monkeypatch):
    monkeypatch.setattr(MN, 'local_llm_chat', lambda *a, **k: '我觉得没有译名差异')
    texts = ['柯蒂斯站在门口']
    out, mapping = MN._unify_names_with_dialog(texts, _ASR)
    assert out == texts and mapping == {}


def test_unify_disabled_by_env(monkeypatch):
    monkeypatch.setenv('NARRATE_NAME_UNIFY', '0')
    called = []
    monkeypatch.setattr(MN, 'local_llm_chat', lambda *a, **k: called.append(1) or '{}')
    texts = ['柯蒂斯站在门口']
    out, mapping = MN._unify_names_with_dialog(texts, _ASR)
    assert out == texts and mapping == {} and not called, '开关关闭时不得调用 LLM'


def test_unify_skips_empty_dialog():
    """没有台词轨（ASR 空）→ 无从对齐，直接跳过。"""
    called = []
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(MN, 'local_llm_chat', lambda *a, **k: called.append(1) or '{}')
        out, mapping = MN._unify_names_with_dialog(['柯蒂斯'], [])
        assert out == ['柯蒂斯'] and not called
    finally:
        monkeypatch.undo()


def test_unify_prompt_forbids_title_swaps_and_fabrication():
    """v1.4.62 prompt 守卫：称号/头衔不是译名，目标必须在字幕里真实出现。"""
    calls = []

    def fake_llm(prompt, **kw):
        calls.append(prompt)
        return '{"mapping": {}}'

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(MN, 'local_llm_chat', fake_llm)
        MN._unify_names_with_dialog(['秦王李世民出场'], _ASR)
        p = calls[0]
        assert '只替换纯译名差异' in p
        assert '称号/头衔/别名不是译名' in p and '禁止替换' in p
        assert '秦王' in p and '皇帝李世民' in p, '要给出「秦王李世民→皇帝李世民」反例'
        assert '必须在台词字幕文本中真实出现过' in p
    finally:
        monkeypatch.undo()


def test_unify_drops_fabricated_and_title_targets():
    """v1.4.62 结果级守卫：LLM 不守规则也得拦住。

    字幕是地面真值——目标译名在台词里没出现过，就一定是编造或称号误替换，
    整条映射丢弃；同批次的合法映射照常生效（不许一竿子打翻）。
    """
    def fake_llm(*a, **k):
        return ('{"mapping": {"秦王李世民": "皇帝李世民", '
                '"柯蒂斯": "凭空编造的名字", "弗兰克": "法兰克"}}')

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(MN, 'local_llm_chat', fake_llm)
        texts = ['秦王李世民和柯蒂斯、弗兰克在场']
        out, mapping = MN._unify_names_with_dialog(texts, _ASR)
        assert mapping == {'弗兰克': '法兰克'}, mapping
        assert out == ['秦王李世民和柯蒂斯、法兰克在场'], out
        assert texts == ['秦王李世民和柯蒂斯、弗兰克在场'], '原列表不许被改'
    finally:
        monkeypatch.undo()


def test_unify_runs_before_alignment_source_pin():
    """顺序钉：归一必须在句类标注/算法对齐之前（对齐吃的是归一后的 texts）。"""
    import inspect
    src = inspect.getsource(MN._narrate_by_plot)
    i_unify = src.index('_unify_names_with_dialog(texts')
    i_align = src.index('_algo_align_beats_to_scenes(texts')
    assert i_unify < i_align, '归一必须发生在算法对齐之前'

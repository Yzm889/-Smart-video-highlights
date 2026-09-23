# -*- coding: utf-8 -*-
"""S3 回归：合并 LLM 轮次（语义对齐 + 剧情润色一次调用）。

锁定：
  1. 完整输出（对齐+润色）正确解析
  2. 缺润色 → refined=None（调用方回退独立润色轮）
  3. 缺对齐 → alignment=None（调用方回退独立对齐轮）
  4. 输出非 JSON → None（整体回退旧分步路径）
  5. 本地 LLM 不可用 → None（云端行为不变，由旧路径云端兜底）
  6. 对齐为空数组 → alignment=None
"""
import webui_server as S  # noqa: F401  先建立 webui 入口，再导入 movie_narrator（避免循环依赖）
import movie_narrator as M

_FULL_JSON = ('{"对齐":[{"解说词":0,"场景":[0]},{"解说词":1,"场景":[1]}],'
              '"润色":["润色后的第一句解说词内容够长关于瑞克","润色后的第二句解说词关于肖恩"]}')
_ALIGN_ONLY = '{"对齐":[{"解说词":0,"场景":[0]},{"解说词":1,"场景":[1]}]}'
_REFINE_ONLY = '{"润色":["润色后的第一句解说词内容够长关于瑞克","润色后的第二句解说词关于肖恩"]}'

_BEATS = ['第一句解说词内容够长关于瑞克', '第二句解说词关于肖恩']
_SCENES = [
    {'start': 0.0, 'end': 5.0, 'event': '主角从昏迷中醒来', 'location': '医院病房',
     'characters': '瑞克', 'summary': '医院场景'},
    {'start': 5.0, 'end': 10.0, 'event': '两人在警车里交谈', 'location': '警车',
     'characters': '瑞克与肖恩', 'summary': '车内场景'},
]
_STORY = {
    0: {'story': '主角在医院醒来', 'who': '瑞克', 'act': '引入', 'keywords': ['医院', '醒来']},
    1: {'story': '警车内两人对话', 'who': '瑞克与肖恩', 'act': '发展', 'keywords': ['警车', '对话']},
}
_ASR = [{'start': 0.0, 'end': 2.0, 'text': '瑞克醒了'}]


def _install(monkeypatch, resp):
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: True)
    monkeypatch.setattr(S, 'local_llm_ping', lambda: (True, 'ok'))
    monkeypatch.setattr(M, 'local_llm_chat', lambda *a, **k: resp)


def test_merge_full_parses(monkeypatch):
    """完整输出：对齐 + 润色都解析成功。"""
    _install(monkeypatch, _FULL_JSON)
    out = M._llm_align_and_refine(_BEATS, _SCENES, _STORY, _ASR, movie_name='行尸走肉')
    assert out is not None and len(out) == 2
    alignment, refined = out
    assert alignment == {0: [0], 1: [1]}
    assert refined == ['润色后的第一句解说词内容够长关于瑞克', '润色后的第二句解说词关于肖恩']


def test_merge_missing_refine(monkeypatch):
    """缺润色字段：refined=None（调用方回退独立润色轮）。"""
    _install(monkeypatch, _ALIGN_ONLY)
    out = M._llm_align_and_refine(_BEATS, _SCENES, _STORY, _ASR)
    assert out is not None
    alignment, refined = out
    assert alignment == {0: [0], 1: [1]}
    assert refined is None


def test_merge_missing_alignment(monkeypatch):
    """缺对齐字段：alignment=None（调用方回退独立对齐轮）。"""
    _install(monkeypatch, _REFINE_ONLY)
    out = M._llm_align_and_refine(_BEATS, _SCENES, _STORY, _ASR)
    assert out is not None
    alignment, refined = out
    assert alignment is None
    assert refined == ['润色后的第一句解说词内容够长关于瑞克', '润色后的第二句解说词关于肖恩']


def test_merge_non_json(monkeypatch):
    """输出非 JSON：整体 None（回退旧分步路径）。"""
    _install(monkeypatch, '抱歉，我无法完成这个请求。')
    assert M._llm_align_and_refine(_BEATS, _SCENES, _STORY, _ASR) is None


def test_merge_local_unavailable(monkeypatch):
    """本地 LLM 不可用：None（云端行为不变，由旧路径云端兜底）。"""
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: False)
    monkeypatch.setattr(S, 'local_llm_ping', lambda: (False, 'no'))
    calls = []
    monkeypatch.setattr(M, 'local_llm_chat', lambda *a, **k: calls.append(a) or _FULL_JSON)
    assert M._llm_align_and_refine(_BEATS, _SCENES, _STORY, _ASR) is None
    assert calls == [], '本地不可用不得发起调用'


def test_merge_empty_alignment(monkeypatch):
    """对齐为空数组：alignment=None（不硬凑脏映射）。"""
    _install(monkeypatch, '{"对齐":[],"润色":["润色后的第一句解说词内容够长关于瑞克"]}')
    out = M._llm_align_and_refine(_BEATS, _SCENES, _STORY, _ASR)
    assert out is not None
    alignment, refined = out
    assert alignment is None
    # 润色行数不足 → refined 也 None（触发独立润色轮）
    assert refined is None

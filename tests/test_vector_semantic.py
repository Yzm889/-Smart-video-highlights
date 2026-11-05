# -*- coding: utf-8 -*-
"""向量语义精修回归钉（2026-11-06「解说↔画面对得上」向量方案）。

钉住：
1. 向量路径结构安全（不回跳/不越界）、确定性可复现，且真的会改选起点；
2. bigram 路径不受向量机制影响（无 scene_sem 时与旧行为逐字节一致）；
3. scene_sem 素材不齐/形状不对 → 自动退回 bigram，绝不抛异常；
4. _compose_scene_texts 组装与排序契约；
5. ollama_embed：失败返回 None 不抛；缓存命中不联网；enabled=False 直接 None；
   真机（有 Ollama）时向量必须 L2 归一化。
"""
import socket

import pytest

import webui_server  # noqa: F401  先导入，解除 webui_server↔video_render 循环依赖
import ai_providers as ap
import movie_narrator as mn

_SCENES = [{'start': 0, 'end': 30, 'summary': '医院走廊'},
           {'start': 30, 'end': 60, 'summary': '港口夜色'}]
# 故意 paraphrase：句子里不出现「港口/船」，bigram 对两个场景都是零重叠
_TEXTS = ['他悄悄登上了那艘白色的大家伙。']


def test_compose_scene_texts_contract():
    assert mn._compose_scene_texts([], {}) == []
    assert mn._compose_scene_texts(None, None) == []
    sc = [{'start': '10', 'end': '5', 'summary': '倒序必须被丢弃'},
          {'start': 0, 'end': 5, 'summary': '有效'}]
    out = mn._compose_scene_texts(sc, {'1': {'keywords': ['甲', '乙'], 'story': '含义'}})
    assert len(out) == 1, 'end<=start 的场景必须丢弃'
    assert out[0][0] == 0 and out[0][1] == 5
    assert out[0][2] == '有效甲乙含义', 'summary+keywords+story 必须按序拼接'


def test_vector_semantic_changes_selection_and_safe():
    st0 = mn._compose_scene_texts(_SCENES, {})
    assert len(st0) == 2
    tvecs = [(0.0, 1.0)]
    svecs = [(0.95, 0.05), (0.1, 0.9)]      # 句子与场景2（港口，30s 后）更贴
    spans_v, _ = mn._allocate_script_spans(_TEXTS, 60.0, scenes=_SCENES,
                                           sem_weight=1.2, scene_sem=(st0, svecs, tvecs))
    spans_b, _ = mn._allocate_script_spans(_TEXTS, 60.0, scenes=_SCENES, sem_weight=1.2)
    spans_off, _ = mn._allocate_script_spans(_TEXTS, 60.0, scenes=_SCENES, sem_weight=0.0)
    assert spans_b == spans_off, 'bigram 与这句零重叠：必须与关语义逐字节一致'
    for sp in spans_v:
        assert 0 <= sp[0] < sp[1] <= 60.5, '向量精修产生越界 span: %r' % (sp,)
    for a, b in zip(spans_v, spans_v[1:]):
        assert b[0] >= a[1] - 0.01, '向量精修产生回跳: %r → %r' % (a, b)
    assert spans_v[0][0] >= 20.0, '向量更贴场景2（30s 后），起点应后移；got %r' % (spans_v[0],)
    spans_v2, _ = mn._allocate_script_spans(_TEXTS, 60.0, scenes=_SCENES,
                                            sem_weight=1.2, scene_sem=(st0, svecs, tvecs))
    assert spans_v == spans_v2, '向量路径必须确定性可复现'


def test_scene_sem_mismatch_falls_back_to_bigram():
    st0 = mn._compose_scene_texts(_SCENES, {})
    spans_b, _ = mn._allocate_script_spans(_TEXTS, 60.0, scenes=_SCENES, sem_weight=1.2)
    bad_len = (st0, [(1.0, 0.0)], [(0.0, 1.0)])       # 向量数与场景数不齐
    bad_shape = ('x',)                                # 形状不对
    for sem in (bad_len, bad_shape, None):
        spans_g, _ = mn._allocate_script_spans(_TEXTS, 60.0, scenes=_SCENES,
                                               sem_weight=1.2, scene_sem=sem)
        assert spans_g == spans_b, 'scene_sem=%r 必须无害退回 bigram' % (sem,)


def _closed_port_cfg():
    return {'enabled': True, 'base_url': 'http://127.0.0.1:9', 'model': 'test-model'}


def test_ollama_embed_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(ap, 'embed_cfg', lambda: dict(_closed_port_cfg(), enabled=False))
    assert ap.ollama_embed(['abc']) is None
    assert ap.ollama_embed([]) is None


def test_ollama_embed_failure_returns_none(monkeypatch):
    ap._EMBED_CACHE.clear()
    monkeypatch.setattr(ap, 'embed_cfg', _closed_port_cfg)
    assert ap.ollama_embed(['没有缓存必然联网，而端口 9 必拒绝']) is None


def test_ollama_embed_cache_hit_no_network(monkeypatch):
    ap._EMBED_CACHE.clear()
    ap._EMBED_CACHE[('test-model', '', '已缓存文本')] = (0.6, 0.8)
    monkeypatch.setattr(ap, 'embed_cfg', _closed_port_cfg)
    assert ap.ollama_embed(['已缓存文本']) == [(0.6, 0.8)]


def test_ollama_embed_real_if_ollama_up():
    try:
        socket.create_connection(('127.0.0.1', 11434), timeout=1).close()
    except Exception:
        pytest.skip('本机无 Ollama，跳过真机向量测试')
    ap._EMBED_CACHE.clear()
    out = ap.ollama_embed(['港口夜色', '医院走廊'], prefix='search_document: ')
    assert out and len(out) == 2
    assert len(out[0]) == len(out[1]) >= 64
    n = sum(x * x for x in out[0]) ** 0.5
    assert abs(n - 1.0) < 0.02, '向量必须 L2 归一化（余弦=点积）：模长 %r' % n

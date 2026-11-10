# -*- coding: utf-8 -*-
"""「引用让位」语义档的入口判据与耗时预算回归钉（[2.6]）。

真实缺陷：`_pick_quote_span` 的语义分支入口写的是
    if not _local_model_available(): return None
而它下游 `_score_block` → `_llm_text`（movie_narrator.py:1490）**自己就有兜底**：
本地文字模型不可用时会回退**视觉模型的文字通道**（`_w.vlm_text`）。
于是「只装了视觉模型、没配本地文字模型」的机器（本机就是：
`local_llm_cfg()={'enabled':False,...}` 而 `vlm_enabled()=True`）被静默挡在门外——
真机产物 run-3 的 `tts_state.json` 里 `quote_slots` 恒为空，功能看着有、其实永不生效。

修法：判据对齐成 `if not (_local_model_available() or _w.vlm_enabled())`。
配套两道保险：
  1) 墙钟预算（`NARRATE_QUOTE_BUDGET`，默认 100s）：语义搜索是**由近及远**的，
     越靠后的块越不可能命中，而每块都会起一次 timeout=120s 的模型调用，
     不设闸时最坏 `max_blocks` 次会把合成流程拖到几十分钟。
  2) `_score_block` 把「后端没输出」返回成 `None`（而不是与「一条都不像」同形的 `[]`），
     调用方据此立即放弃这一句——因为 `vlm_enabled()` 只是**配置层**开关
     （webui_server.py:1216 只查 `enabled and base_url`，不探活），配了 VLM 但服务没起 /
     模型没拉的机器会被放行进语义分支，此时逐块问下去纯属白等。

⚠️ 本修复的**适用范围如实记录**：只覆盖「仅装了（配置了）视觉模型」这一类。
纯云端用户（配了 DeepSeek key、没配本地文字模型、也没开 VLM）仍然进不来——
`_llm_text`（movie_narrator.py:1490）的两条后端是 `local_llm_chat` 和 `_w.vlm_text`，
不含 `_cloud_chat_text`（:4119）。要让云端也支持是**另一个任务**（会动配置形状与调用链）。
"""
import time

import webui_server as S  # noqa: F401,E402  先导入宿主（符号晚绑定）
import movie_narrator as M  # noqa: E402


def _dense_asr(t0=0.0, t1=600.0, step=5.0):
    """密排台词，保证 anchor 周围每个 60s 块都非空（否则循环会 continue、不计数）。"""
    out = []
    t = t0
    i = 0
    while t < t1:
        out.append({'start': t, 'end': t + 3.0, 'text': '完全不相干的第%d句台词' % i})
        t += step
        i += 1
    return out


def _stub_backends(monkeypatch, local, vlm):
    monkeypatch.setattr(M, '_local_model_available', lambda: local)
    monkeypatch.setattr(M._w, 'vlm_enabled', lambda: vlm)


def test_semantic_reachable_when_only_vlm_available(monkeypatch):
    """仅视觉模型可用时，语义档必须**可达**（这是修复的核心）。"""
    calls = {'n': 0}

    def fake_score(qtext, block):
        calls['n'] += 1
        return [(block[0][0], block[0][1], block[0][2])]   # 第一块即命中

    _stub_backends(monkeypatch, local=False, vlm=True)
    monkeypatch.setattr(M, '_score_block', fake_score)

    hit = M._pick_quote_span('我不想交朋友', _dense_asr(), near=300.0)
    assert hit is not None, '仅 VLM 可用时语义档被拦掉了（修复前必红）'
    assert hit[3] == 'semantic', hit
    assert calls['n'] == 1, '命中第一块就该停，实际调用了 %d 次' % calls['n']


def test_no_backend_returns_none_without_calling_model(monkeypatch):
    """两个后端都没有时，必须快速返回 None，一次模型调用都不该发。"""
    calls = {'n': 0}

    def fake_score(qtext, block):
        calls['n'] += 1
        raise AssertionError('无后端时不该调用模型')

    _stub_backends(monkeypatch, local=False, vlm=False)
    monkeypatch.setattr(M, '_score_block', fake_score)

    assert M._pick_quote_span('我不想交朋友', _dense_asr(), near=300.0) is None
    assert calls['n'] == 0, '无后端时仍发了 %d 次模型调用（空转）' % calls['n']


def test_literal_still_wins_over_semantic(monkeypatch):
    """字面档必须仍然优先：字面能中就不该起模型调用。"""
    calls = {'n': 0}

    def fake_score(qtext, block):
        calls['n'] += 1
        return []

    _stub_backends(monkeypatch, local=True, vlm=True)
    monkeypatch.setattr(M, '_score_block', fake_score)

    asr = [{'start': 100.0, 'end': 103.0, 'text': '我是死了，科提斯，你没听说吗？'},
           {'start': 200.0, 'end': 203.0, 'text': '另一句不相干的话'}]
    hit = M._pick_quote_span('我死了，柯蒂斯，你没听说吗？', asr, near=100.0)
    assert hit is not None and hit[3] == 'literal', hit
    assert calls['n'] == 0, '字面已命中却仍起了 %d 次模型调用' % calls['n']


def test_budget_stops_runaway_semantic_search(monkeypatch):
    """一块不中会逐块搜下去，预算用尽必须停下（否则最坏 8×120s）。"""
    calls = {'n': 0}

    def slow_score(qtext, block):
        calls['n'] += 1
        time.sleep(0.15)
        return []          # 永不命中 → 必然把所有块走完

    _stub_backends(monkeypatch, local=False, vlm=True)
    monkeypatch.setattr(M, '_score_block', slow_score)
    monkeypatch.setenv('NARRATE_QUOTE_BUDGET', '0.2')

    t0 = time.time()
    assert M._pick_quote_span('我不想交朋友', _dense_asr(), near=300.0) is None
    elapsed = time.time() - t0

    assert calls['n'] < 8, '预算没生效：仍搜完了全部 8 块（调用 %d 次）' % calls['n']
    assert elapsed < 160.0, '未命中时应立即返回，实际耗时 %.2fs' % elapsed
    assert calls['n'] <= 3, '预算 0.2s / 每次 0.15s 应只允许 1~2 次调用，实际 %d 次' % calls['n']


def test_default_budget_does_not_shrink_normal_search(monkeypatch):
    """默认预算必须宽到「模型正常应答时仍搜满 8 块」，否则无辜缩水语义档。

    默认 100s 的取值理由：单次调用最长会烧掉 120s 的 timeout，所以
      · 模型活着、几秒就答 → 100s 足够搜满 max_blocks=8 块；
      · 模型挂住一直超时 → 第一次 120s 后就该放弃，不再罚 7 次。
    """
    calls = {'n': 0}

    def quick_score(qtext, block):
        calls['n'] += 1
        time.sleep(0.01)          # 模拟「模型正常、但确实要点时间」
        return []

    _stub_backends(monkeypatch, local=False, vlm=True)
    monkeypatch.setattr(M, '_score_block', quick_score)
    monkeypatch.delenv('NARRATE_QUOTE_BUDGET', raising=False)   # 用默认值

    assert M._pick_quote_span('我不想交朋友', _dense_asr(), near=300.0) is None
    assert calls['n'] == 8, '默认预算下正常应答应搜满 8 块，实际 %d 次' % calls['n']


def test_budget_zero_disables_the_gate(monkeypatch):
    """`NARRATE_QUOTE_BUDGET=0` 应关掉闸门，回到逐块搜到底的旧行为（可回滚）。"""
    calls = {'n': 0}

    def score(qtext, block):
        calls['n'] += 1
        return []

    _stub_backends(monkeypatch, local=True, vlm=True)
    monkeypatch.setattr(M, '_score_block', score)
    monkeypatch.setenv('NARRATE_QUOTE_BUDGET', '0')

    assert M._pick_quote_span('我不想交朋友', _dense_asr(), near=300.0) is None
    assert calls['n'] == 8, '关闭预算后应搜满 max_blocks=8 块，实际 %d 次' % calls['n']


def test_backend_configured_but_dead_stops_immediately(monkeypatch):
    """`vlm_enabled()=True` 但后端其实不可用（服务没起 / 模型没拉）时必须**立即**放弃。

    `vlm_enabled()` 只是配置层开关（webui_server.py:1216），不探活；判据放宽后这类
    机器会被放行进语义分支。`_score_block` 返回 None（后端无输出，区别于「一条都不像」
    的 []）时，调用方必须马上停，而不是把 8 块都白问一遍——这是 R3 的核心回归钉。
    """
    calls = {'n': 0}

    def dead_score(qtext, block):
        calls['n'] += 1
        return None              # _llm_text 两条后端都失败 → 无可用输出

    _stub_backends(monkeypatch, local=False, vlm=True)     # 配置层「已启用」
    monkeypatch.setattr(M, '_score_block', dead_score)
    monkeypatch.setenv('NARRATE_QUOTE_BUDGET', '0')        # 关掉墙钟闸，单看早停逻辑

    t0 = time.time()
    assert M._pick_quote_span('我不想交朋友', _dense_asr(), near=300.0) is None
    elapsed = time.time() - t0
    assert calls['n'] == 1, (
        '后端无输出时应问一次就放弃，实际问了 %d 次（白等到预算用尽）' % calls['n'])
    assert elapsed < 5.0, '应立即返回，实际 %.2fs' % elapsed


def test_empty_hits_still_keeps_searching(monkeypatch):
    """「问到了、但这条不像」（`[]`）必须继续问下一块，不能被误当成后端故障早停。"""
    calls = {'n': 0}

    def no_match_score(qtext, block):
        calls['n'] += 1
        return []                # 后端活着，只是这条块没有命中

    _stub_backends(monkeypatch, local=True, vlm=True)
    monkeypatch.setattr(M, '_score_block', no_match_score)
    monkeypatch.setenv('NARRATE_QUOTE_BUDGET', '0')

    assert M._pick_quote_span('我不想交朋友', _dense_asr(), near=300.0) is None
    assert calls['n'] == 8, '「没命中」不是「后端故障」，应搜满 8 块，实际 %d 次' % calls['n']


def test_score_block_distinguishes_dead_backend_from_no_match(monkeypatch):
    """`_score_block` 本身必须把两种空结果分开：后端无输出 → None；解析不到 → []。"""
    block = [(0.0, 3.0, '完全不相干的一句话')]

    monkeypatch.setattr(M, '_llm_text', lambda *a, **k: None)
    assert M._score_block('我不想交朋友', block) is None, (
        '后端无输出必须返回 None（否则调用方无法判断该不该早停）')

    monkeypatch.setattr(M, '_llm_text', lambda *a, **k: '模型今天不想说 JSON')
    assert M._score_block('我不想交朋友', block) == [], (
        '有输出但解析不出 JSON 应算「没命中」，返回空列表继续问下一块')

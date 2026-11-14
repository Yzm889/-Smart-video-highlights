# -*- coding: utf-8 -*-
"""v1.4.89 层 1 素材升级 + 验证闭环回归钉。

POC 实测（2026-11-14，主人真实视频）：同是楼顶，午餐段事件描述「拿着食物
准备吃」vs 互助会段「走向公告栏取物」——相似场景可区分；事件描述喂 LLM
做句-段匹配 5/5。刻度基线 run-065419：match 19%，升级目标 50%+。
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import movie_narrator as M  # noqa: E402
import vlm_index as VI  # noqa: E402
import narration_align as NA  # noqa: E402


SCENES = [{'start': 0.0 + i * 100.0, 'end': 50.0 + i * 100.0} for i in range(4)]


def test_event_windows_skips_short_and_capped(monkeypatch):
    """段过滤（≥2s）与封顶：VLM 只看有事件的段，超长片不拖死。"""
    monkeypatch.setattr(M, '_local_model_available', lambda: True)
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(S, 'vlm_ping', lambda: (True, 'ok'))
    calls = []

    def fake_multi(frames, text, **kw):
        calls.append([os.path.basename(f) for f in frames])
        return '一名男子抡起大锤砸向水泥墙，砖块飞溅。'
    monkeypatch.setattr(S, 'vlm_chat_multi', fake_multi)

    import movie_narrator as _MN2

    def fake_ff(args, *a, **k):
        out = args[-1]
        with open(out, 'wb') as f:
            f.write(b'x' * 2000)
        return 0, b'', b''
    monkeypatch.setattr(_MN2, 'ffmpeg_run', fake_ff)
    monkeypatch.setattr(_MN2, '_cache_load', lambda key: None)
    monkeypatch.setattr(_MN2, '_cache_save', lambda k, v: None)
    monkeypatch.setattr(_MN2, '_video_cache_key', lambda p, k: 'k')
    monkeypatch.setattr(_MN2, 'vlm_cfg', lambda: {'model': 'test'})
    tiny = [{'start': 0.0, 'end': 1.0}]   # <2s 应被过滤
    got = VI._vlm_event_windows('no.mp4', SCENES + tiny, None, '_t81/poc_tmp',
                                max_windows=3, per_window=3)
    assert 0 in got and len(got) <= 3, got
    assert len(calls[0]) == 3, '每段 3 连续帧'


def test_event_windows_cached(monkeypatch):
    """事件链结果缓存：重跑不重复花钱。"""
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(S, 'vlm_ping', lambda: (True, 'ok'))
    hit = {'called': 0}

    def fake_load(key):
        return {0: ' cached 事件'}
    monkeypatch.setattr(M, '_cache_load', fake_load)
    monkeypatch.setattr(M, '_video_cache_key', lambda p, k: 'k')
    monkeypatch.setattr(S, 'vlm_chat_multi', lambda *a, **k: '')
    got = VI._vlm_event_windows('no.mp4', SCENES, None, '_t81/poc_tmp')
    assert got == {0: ' cached 事件'}
    assert hit['called'] == 0


def test_full_align_prompt_carries_event_chain(monkeypatch):
    """全篇对齐器的场景表必须带上事件链（事件对事件匹配的原料）。"""
    monkeypatch.setattr(M, '_local_model_available', lambda: True)
    holder = {}

    def fake_llm(prompt, system='', **kw):
        holder['prompt'] = prompt
        return '[{"i": 0, "s": 1, "cue": "", "why": ""}]'
    monkeypatch.setattr(M, '_llm_light', fake_llm)
    story = {1: {'event_chain': '唐尼拿三明治上天台找弗兰克搭话'}}
    M._llm_align_full_script(['解说一。'], SCENES, story, None, movie_name='测试')
    assert '事件：唐尼拿三明治上天台找弗兰克搭话' in holder['prompt']


def test_dp_scene_text_carries_event_chain():
    """DP 兜底的场景文本也要吃事件链（narration_align 的 scene_txt 拼接）。"""
    src = open(os.path.join(ROOT, 'narration_align.py'), encoding='utf-8').read()
    i = src.index("if st.get('story'):")
    assert "if st.get('event_chain'):" in src[i:i + 400], 'DP 场景文本必须拼事件链'


def test_verify_final_judges(monkeypatch, tmp_path):
    """逐句验证：抽帧+VLM 描述+判断 → 汇总统计。"""
    fake_plan_segs = [{'index': 0, 'out_start': 0.0, 'out_end': 7.0,
                       'text': '弗兰克抡大锤砸墙。', 'duration': 5.0},
                      {'index': 1, 'out_start': 7.0, 'out_end': 13.0,
                       'text': '', 'duration': 0.0}]   # 留白不验证
    monkeypatch.setattr(S, 'vlm_chat', lambda *a, **k: '一名男子挥锤砸墙，砖块飞溅。')
    monkeypatch.setattr(S, 'vlm_text',
                        lambda *a, **k: '{"verdict": "match", "why": "同一动作"}')
    monkeypatch.setattr(M, '_w', S)

    def fake_ff2(args, *a, **k):
        out = args[-1]
        with open(out, 'wb') as f:
            f.write(b'x' * 2000)
        return 0, b'', b''
    monkeypatch.setattr(S, 'ffmpeg_run', fake_ff2)
    report, n = M._verify_final_alignment('no.mp4', fake_plan_segs)
    assert n['match'] == 1 and n['total'] == 1, n
    assert report[0]['verdict'] == 'match'


def test_auto_verify_writes_report(monkeypatch, tmp_path):
    """后台验证线程：align_report.json 落 run 目录 + progress['align_summary']。"""
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(M, '_verify_final_alignment',
                        lambda fp, segs, progress=None:
                        ([{'index': 0, 'verdict': 'match'}],
                         {'match': 1, 'related': 0, 'wrong': 0, 'total': 1}))
    with open(os.path.join(str(tmp_path), 'composed_plan.json'), 'w', encoding='utf-8') as f:
        json.dump({'segments': [{'index': 0}]}, f)
    saved = {}
    monkeypatch.setattr(M, '_atomic_json_dump',
                        lambda p, d: saved.__setitem__(os.path.basename(p), d))
    prog = {}
    M._auto_verify_alignment(str(tmp_path), 'no.mp4', progress=prog,
                             params={'autoVerify': True})
    # 线程异步——直接同步调用 _job 的效果：轮询等待
    import time
    for _ in range(50):
        if 'align_report.json' in saved:
            break
        time.sleep(0.05)
    assert 'align_report.json' in saved, '报告必须落盘'
    assert saved['align_report.json']['summary']['match'] == 1
    assert prog.get('align_summary', {}).get('rate') == 100.0, prog


def test_auto_verify_off_switch(monkeypatch, tmp_path):
    """params.autoVerify=false → 不启动验证。"""
    called = {'n': 0}
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)

    def boom(*a, **k):
        called['n'] += 1
        raise AssertionError('不应被调用')
    monkeypatch.setattr(M, '_verify_final_alignment', boom)
    M._auto_verify_alignment(str(tmp_path), 'no.mp4', params={'autoVerify': False})
    assert called['n'] == 0


def test_wired_in_compose_tail():
    """源码钉：compose 末尾必须起自动验证。"""
    src = open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8').read()
    i = src.index('成片时间轴已落盘')
    tail = src[i:i + 1200]
    assert '_auto_verify_alignment(run_dir, final' in tail
    i2 = src.index('def _auto_verify_alignment')
    assert 'autoVerify' in src[i2:i2 + 800]

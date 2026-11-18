# -*- coding: utf-8 -*-
"""v1.4.93 画面理解流水线回归钉。

主人定调：识别画面是一个引擎在做，出的效果不好——升级成多步流水线要质变。
四层：L0 ffmpeg 镜头切分（零 AI）→ L1 镜头卡（VLM 只描述动作）→
L2 场景聚合（文本 LLM 写叙述）→ L3 对齐（既有）。失败逐层退回。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import event_pipeline as EP  # noqa: E402


def test_detect_shots_parses_and_merges(monkeypatch):
    """L0：解析 showinfo 的 pts_time、碎镜头并入前镜头、末镜头到片尾。"""
    fake_err = (b'[Parsed_showinfo_0] n:0 pts_time:3.5\n'
                b'[Parsed_showinfo_0] n:1 pts_time:9.2\n'
                b'[Parsed_showinfo_0] n:2 pts_time:9.6\n')          # 9.6 是碎切（距 9.2 仅 0.4s）
    monkeypatch.setattr(S, 'ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(S, 'ffmpeg_run',
                        lambda args, timeout=None: (0, b'', fake_err))
    monkeypatch.setattr(S, 'probe_duration', lambda p: 20.0)
    monkeypatch.setattr(EP, '_w', S)
    shots = EP.detect_shots('no.mp4')
    assert shots[0][0] == 0.0
    # 9.2-9.6 长度 0.4s < min_len → 碎镜头并入前镜头 → (3.5, 9.6)；末镜头到片尾
    assert shots == [(0.0, 3.5), (3.5, 9.6), (9.6, 20.0)], '碎镜头应并入前镜头: %s' % shots


def test_detect_shots_capped_and_tail(monkeypatch):
    """超长片封顶：均匀保留 + 末镜头强制到片尾。"""
    fake_err = ''.join('[Parsed_showinfo_0] pts_time:%d.0\n' % t for t in range(1, 30)).encode()
    monkeypatch.setattr(S, 'ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(S, 'ffmpeg_run', lambda args, timeout=None: (0, b'', fake_err))
    monkeypatch.setattr(S, 'probe_duration', lambda p: 35.0)
    monkeypatch.setattr(EP, '_w', S)
    shots = EP.detect_shots('no.mp4', max_shots=10)
    assert len(shots) <= 10
    assert shots[-1][1] == 35.0, '末镜头必须到片尾'


def test_detect_shots_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(S, 'ffmpeg_exe', lambda: 'ffmpeg')
    monkeypatch.setattr(S, 'ffmpeg_run', lambda args, timeout=None: (1, b'', b'err'))
    monkeypatch.setattr(EP, '_w', S)
    assert EP.detect_shots('no.mp4') == []


def test_shot_cards_caches(monkeypatch, tmp_path):
    """L1：缓存命中不重复调 VLM。"""
    monkeypatch.setattr(S, 'vlm_cfg', lambda: {'base_url': 'http://x', 'model': 'm'})
    monkeypatch.setattr(S, 'vlm_ping', lambda: (True, 'ok'))
    monkeypatch.setattr(S, '_video_cache_key', lambda p, k: 'k1')
    monkeypatch.setattr(S, '_cache_load', lambda k: {0: ' cached 卡'})
    monkeypatch.setattr(EP, '_w', S)
    got = EP.shot_cards('no.mp4', [(0.0, 10.0)], str(tmp_path))
    assert got == {0: ' cached 卡'}


def test_scene_narratives_groups_and_tail(monkeypatch):
    """L2：镜头卡分窗聚合、产出按镜头组首尾、思考标记剥离。"""
    monkeypatch.setattr(EP, '_MN_ABORTED', lambda: False)
    monkeypatch.setattr(EP, '_asr_in', lambda asr, a, b: '')
    sent = {'n': 0}

    def fake_llm(prompt, system='', **kw):
        sent['n'] += 1
        return '<think>x</think>第%d段场景叙述：他进了工地拿起大锤砸墙，泄愤。' % sent['n']
    monkeypatch.setattr(EP, '_w', S)
    monkeypatch.setattr(S, 'local_llm_chat', fake_llm)
    monkeypatch.setattr(S, '_strip_think', lambda r: r.split('</think>')[-1])
    shots = [(float(i * 10), float(i * 10 + 10)) for i in range(6)]
    cards = {i: '镜头%d 卡' % i for i in range(6)}
    narrs = EP.scene_narratives(shots, cards, None, None, window=3)
    assert len(narrs) == 2, narrs
    assert narrs[0][0] == 0.0 and narrs[1][1] == 60.0
    assert 'think' not in narrs[0][2], '思考标记必须剥离'


def test_build_pipeline_fallback_chain(monkeypatch, tmp_path):
    """L2 失败 → 镜头卡直通为叙述；L0 失败 → 空（退回旧事件链通道）。"""
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(EP, '_w', S)
    shots = [(0.0, 10.0), (10.0, 20.0)]
    monkeypatch.setattr(EP, 'detect_shots', lambda p, **k: shots)
    monkeypatch.setattr(EP, 'shot_cards',
                        lambda p, sh, rd, progress=None, **k: {0: '卡A', 1: '卡B'})
    monkeypatch.setattr(EP, 'scene_narratives', lambda *a, **k: [])   # L2 失败
    narrs = EP.build_pipeline('no.mp4', None, str(tmp_path))
    assert [(a, b, t) for a, b, t in narrs] == [(0.0, 10.0, '卡A'), (10.0, 20.0, '卡B')], narrs
    monkeypatch.setattr(EP, 'detect_shots', lambda p, **k: [])
    assert EP.build_pipeline('no.mp4', None, str(tmp_path)) == []


def test_wired_into_narrate_movie_with_fallback():
    """源码钉：narrate_movie 先走流水线，失败退回旧事件链通道；叙述按重叠覆盖。"""
    src = open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8').read()
    i = src.index('_narrs = build_pipeline(video_path')
    block = src[i:i + 2200]
    assert '_vlm_event_windows' in block, '流水线失败必须退回旧事件链'
    assert 'event_chain' in block, '叙述必须并入 scene_story 的 event_chain'
    assert 'from event_pipeline import build_pipeline' in src

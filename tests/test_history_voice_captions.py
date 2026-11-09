# -*- coding: utf-8 -*-
"""v1.4.20 回归钉：记录页必须如实写配音与文案。

事故（2026-11-09 实跑「惩罚者前二十分钟」）：成片有 37 段 Qwen3 配音、字幕也
烧进画面了，history.json 里却是 {"voice": false, "captions": []}。
后果不止是显示难看——记录页靠 captions 判定：
  · 🗣️ 配音标记
  · 「文案: …」这一行
  · 历史搜索能否按解说词命中
  · 「✏️ 编辑解说词，只重生成修改的段落」按钮是否存在
写死空数组等于把这四个入口全部砍掉。

真因：_record_history 里直接写 {'voice': False, 'captions': []} 占位，
从未从任务里取过真实数据。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as W  # noqa: E402


def test_narration_from_tts_list(tmp_path, monkeypatch):
    """第一步（/api/movie_tts）进度里带 tts_list：直接取文案与条数。"""
    monkeypatch.setattr(W, 'OUTDIR', str(tmp_path))
    prog = {'tts_list': [{'index': 0, 'text': '第一句'}, {'index': 1, 'text': '第二句'}]}
    caps, has_voice = W._history_narration(prog)
    assert caps == ['第一句', '第二句']
    assert has_voice is True


def test_narration_from_diag(tmp_path, monkeypatch):
    """老进度没有 tts_list，但 diag.narration 有整份解说词。"""
    monkeypatch.setattr(W, 'OUTDIR', str(tmp_path))
    prog = {'diag': {'narration': ['甲', '乙'], 'voice_clips': 2}}
    caps, has_voice = W._history_narration(prog)
    assert caps == ['甲', '乙']
    assert has_voice is True


def test_narration_from_tts_state_on_disk(tmp_path, monkeypatch):
    """合成阶段（/api/movie_compose 的 run-2）只有 run_dir：
    解说词在第一步落盘的 tts_state.json 里，必须去磁盘取。"""
    monkeypatch.setattr(W, 'OUTDIR', str(tmp_path))
    rd = tmp_path / 'run-1-20261109-101448'
    rd.mkdir()
    (rd / 'tts_state.json').write_text(json.dumps({
        'narr': ['注意看，这个男人叫弗兰克。', '他正在做一件事，给仇人收尾。'],
        'tts_results': [[0, 'a.wav'], [1, 'b.wav']],
    }, ensure_ascii=False), encoding='utf-8')
    prog = {'run_dir': rd.name}
    caps, has_voice = W._history_narration(prog)
    assert caps == ['注意看，这个男人叫弗兰克。', '他正在做一件事，给仇人收尾。']
    assert has_voice is True


def test_no_narration_means_no_voice(tmp_path, monkeypatch):
    """真没配音的一键合成（无稿子、无 tts_state）仍应记成空，不能瞎标 🗣️。"""
    monkeypatch.setattr(W, 'OUTDIR', str(tmp_path))
    caps, has_voice = W._history_narration({})
    assert caps == []
    assert has_voice is False


def test_record_history_writes_real_fields(tmp_path, monkeypatch):
    """端到端：_record_history 落盘的 voice/captions 必须是真实值。"""
    monkeypatch.setattr(W, 'OUTDIR', str(tmp_path))
    out = tmp_path / 'run-x' / 'final.mp4'
    out.parent.mkdir(parents=True)
    out.write_bytes(b'0' * 32)
    saved = {}
    monkeypatch.setattr(W, 'add_history', lambda rec: saved.update(rec))
    monkeypatch.setattr(W, 'probe_audio_len', lambda p: 209.52)
    W._record_history({'params': {}}, {
        'file': 'run-x/final.mp4',
        'tts_list': [{'index': 0, 'text': '开场钩子'}, {'index': 1, 'text': '因果推进'}],
    }, 'movie')
    assert saved['voice'] is True
    assert saved['captions'] == ['开场钩子', '因果推进']
    assert saved['duration'] == 209.52

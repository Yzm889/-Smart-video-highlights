# -*- coding: utf-8 -*-
"""v1.4.38 回归钉：VLM 批循环必须常驻显存（keep_alive='30m'），函数退出必须卸载。

v1.4.34 真机取证 43 个 Ollama 事件的主要嫌疑：快速档 `_VLM_KEEP = '30m' if quality
else 0` 与 `_scan_full_visual` 批循环（不带 keep_alive）——每批都从磁盘重新加载
6GB 权重再卸掉。清理不靠 keep_alive，靠调用方/函数自身的 finally vlm_unload()。

运行：pytest tests/test_vlm_keep_resident.py -q
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server  # noqa: F401,E402  必须先导入宿主，规避 video_render<->webui_server 循环导入
import movie_narrator as M  # noqa: E402

SRC = os.path.join(ROOT, 'vlm_index.py')   # v1.4.41：画面索引族已拆至 vlm_index.py


def test_vlm_sample_timeline_keeps_resident_for_all_tiers():
    """快速档也必须常驻：'30m' if quality else 0 会让快速档每批重载 6GB 权重。"""
    with open(SRC, 'r', encoding='utf-8') as f:
        src = f.read()
    assert "_VLM_KEEP = '30m'" in src, '画面索引的 VLM 驻留被改掉/删掉'
    assert "'30m' if quality else 0" not in src, '快速档退回了逐批卸载重载'


def test_scan_full_visual_keeps_resident_and_unloads(monkeypatch):
    """_scan_full_visual：批间 keep_alive='30m'，函数退出 finally 兜底卸载。"""
    calls = []
    unloaded = []

    def _fake_vlm(frames, prompt, system=None, timeout=0, keep_alive=0, **kw):
        calls.append(dict(keep_alive=keep_alive, n=len(frames)))
        return '\n'.join('第%d段: 画面描述%d' % (i + 1, i) for i in range(len(frames)))

    monkeypatch.setattr(M, 'vlm_chat_multi', _fake_vlm)
    monkeypatch.setattr(M, 'vlm_unload', lambda: unloaded.append(1) or True)

    frames = {0: 'f0.jpg', 1: 'f1.jpg', 2: 'f2.jpg'}
    caps = M._scan_full_visual(frames, [(0, 5, ''), (5, 10, ''), (10, 15, '')], {})

    assert caps, '全扫应产出画面描述'
    assert calls and all(c['keep_alive'] == '30m' for c in calls), (
        '批循环必须常驻，否则每批冷加载 6GB 权重')
    assert unloaded, '函数退出必须兜底卸载 VLM（残留会挤爆写稿阶段的显存）'


def test_scan_full_visual_unloads_even_when_vlm_dead(monkeypatch):
    """第一批就失败（VLM 整体不可用）时也要卸载兜底，异常照常向上抛。"""
    monkeypatch.setattr(M, 'vlm_chat_multi',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('VLM 挂了')))
    unloaded = []
    monkeypatch.setattr(M, 'vlm_unload', lambda: unloaded.append(1) or True)

    with pytest.raises(RuntimeError):
        M._scan_full_visual({0: 'f0.jpg'}, [(0, 5, '')], {})
    assert unloaded, '异常路径不能漏掉卸载'

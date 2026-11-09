# -*- coding: utf-8 -*-
"""画面理解覆盖率回归锁定（质量路线 Phase 1）。

背景（真机证据）：22 分钟片子 60 个抽样点里，31 个因「±12 秒内无台词」在进 VLM
之前就被丢弃、16 个因 8 帧/批 + 30s 超时失败，最终只有 5 个场景有画面描述。
在这样残缺的原料上做剧情理解/语义对齐/润色，等于蒙眼解说。

本文件锁定修复后的三条硬约束：
  1. 质量模式：无台词点同样送 VLM（动作戏/空镜/特写才有机会被理解）
  2. 质量模式：调用必须带常驻 keep_alive 与足量超时（否则每批重载 6GB 权重）
  3. 快速模式：保持旧的「台词过滤」行为，不因改动而变慢
外加两条鲁棒性约束：
  4. 单批失败 → 逐帧重试救回
  5. 连续失败 → 熔断，不再逐帧重试（避免 8×超时×批数 把任务拖成几小时）

运行：pytest tests/test_vlm_coverage.py -q
"""
import json
import os
import sys

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as W  # noqa: E402
import movie_narrator as M  # noqa: E402

MAX_FRAMES = 40          # 覆盖所有用例的抽样点数上限


class Harness:
    """真调 _vlm_sample_timeline，只把外部依赖（抽帧/VLM/缓存）换成假体。"""

    def __init__(self, work):
        self.frame_dir = os.path.join(work, 'frames')
        self.run_dir = os.path.join(work, 'run-1')
        os.makedirs(self.frame_dir, exist_ok=True)
        os.makedirs(self.run_dir, exist_ok=True)
        # 相邻帧差异足够大 → 相似度低于 0.85 阈值，不会被差分复用合并
        for i in range(MAX_FRAMES):
            g = (i * 60) % 256
            Image.new('RGB', (64, 64), (g, g, g)).save(
                os.path.join(self.frame_dir, 'sample_%04d.jpg' % i), quality=80)
        self.calls = []          # [(frames_n, kwargs)]
        self.fail_mode = None    # None | 'once'（每批第一次调用失败）| 'always'
        self._first_of_batch = True

    # --- 假 VLM ---
    def _should_fail(self):
        fail = (self.fail_mode == 'always' or
                (self.fail_mode == 'once' and self._first_of_batch))
        self._first_of_batch = False
        return fail

    def chat_multi(self, frames, prompt, system=None, timeout=30, **kw):
        self.calls.append((len(frames), dict(kw, timeout=timeout)))
        if self._should_fail():
            raise RuntimeError('模拟 VLM 失败')
        parts = [json.dumps({'location': '地点', 'characters': '人物',
                             'event': '有人在做某件事', 'dialogue': '',
                             'summary': '画面概要'}, ensure_ascii=False)
                 for _ in frames]
        return '\n---\n'.join(parts)

    def chat_one(self, image_path, text, system=None, timeout=20, **kw):
        self.calls.append((1, dict(kw, timeout=timeout)))
        if self._should_fail():
            raise RuntimeError('模拟 VLM 单帧失败')
        return json.dumps({'location': '单帧地点', 'characters': '人物',
                           'event': '单帧事件', 'dialogue': '',
                           'summary': '单帧概要'}, ensure_ascii=False)

    def run(self, vdur, asr, quality):
        self._first_of_batch = True
        return M._vlm_sample_timeline('fake.mp4', vdur, asr, self.run_dir,
                                     progress={}, quality=quality)


@pytest.fixture()
def h(tmp_path, monkeypatch):
    e = Harness(str(tmp_path))
    monkeypatch.setattr(W, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(W, 'vlm_ping', lambda: (True, ''))
    monkeypatch.setattr(W, 'vlm_cfg', lambda: {'model': 'test-model'})
    monkeypatch.setattr(W, 'vlm_chat', e.chat_one)
    monkeypatch.setattr(M, 'vlm_cfg', lambda: {'model': 'test-model'})
    monkeypatch.setattr(M, '_video_cache_key', lambda vp, suffix='': 'FP')
    monkeypatch.setattr(M, '_cache_load', lambda key: None)
    monkeypatch.setattr(M, '_cache_save', lambda key, value: None)
    monkeypatch.setattr(M, '_sample_frame_cache_dir', lambda vp, itv, **kw: e.frame_dir)
    monkeypatch.setattr(M, '_sample_frame_cache_ready', lambda d, n: True)
    monkeypatch.setattr(M, '_sample_frame_cache_mark', lambda *a, **k: None)
    monkeypatch.setattr(M, '_sample_frame_cache_trim', lambda: None)
    monkeypatch.setattr(M, 'vlm_chat_multi', e.chat_multi)
    monkeypatch.setattr(M, '_aborted', lambda: False)
    return e


def test_quality_covers_scenes_without_dialogue(h):
    """质量模式：全片无台词也要逐点理解，不再产出『无台词区间』占位。"""
    out = h.run(120.0, [], quality=True)
    assert out, '质量模式没有产出任何场景'
    assert h.calls, '无台词时完全没调用 VLM —— 覆盖率修复失效'
    placeholders = [s for s in out if s.get('summary') == '无台词区间']
    assert not placeholders, '仍有 %d 个场景被『无台词区间』占位丢弃' % len(placeholders)
    described = [s for s in out if s.get('summary') in ('画面概要', '单帧概要')]
    assert len(described) == len(out), '有场景未被 VLM 描述：%d/%d' % (len(described), len(out))


def test_quality_passes_keep_alive_and_timeout(h):
    """质量模式必须让模型常驻显存：keep_alive 缺失/为 0 会让每批重载 6GB 权重。"""
    h.run(120.0, [], quality=True)
    assert h.calls, '没有 VLM 调用'
    for n, kw in h.calls:
        assert kw.get('keep_alive'), '第 %d 帧调用未带 keep_alive：%r' % (n, kw)
        assert kw.get('timeout', 0) >= 120, '超时过短（%ss），大批量必然超时' % kw.get('timeout')
        if n > 1:
            assert kw.get('max_tokens'), '批量调用未限制输出长度，解码会拖长每批耗时'


def test_fast_mode_keeps_dialogue_filter(h):
    """快速模式保持旧行为：无台词点跳过，不产生 VLM 调用。"""
    # 只有前 20 秒有台词：10 个抽样点里大部分落在台词窗口之外
    asr = [{'start': 0.0, 'end': 20.0, 'text': '前半段有台词'}]
    out = h.run(120.0, asr, quality=False)
    assert out, '快速模式没有产出场景'
    skipped = [s for s in out if s.get('summary') == '无台词区间']
    assert skipped, '快速模式不再跳过无台词点（行为被意外改了）'
    assert len(h.calls) < len(out), '快速模式对所有点都调用了 VLM'


def test_batch_failure_recovered_by_per_frame_retry(h):
    """单批失败 → 逐帧重试救回，结果仍是全量覆盖。"""
    h.fail_mode = 'once'
    out = h.run(60.0, [], quality=True)
    described = [s for s in out if s.get('summary') in ('画面概要', '单帧概要')]
    assert described, '逐帧重试没有救回任何场景'
    assert len(described) == len(out), '仍有场景缺失：%d/%d' % (len(described), len(out))
    assert len(h.calls) > 1, '没有观察到逐帧重试'


def test_circuit_breaker_stops_retry_storm(h):
    """VLM 整体不可用：连续失败后熔断，调用次数不随批数平方膨胀。"""
    h.fail_mode = 'always'
    out = h.run(600.0, [], quality=True)   # 600/12 = 50 个点 → 13 批（batch=4）
    assert out, '失败时也应返回占位场景而不是抛异常'
    total = len(h.calls)
    # 不熔断时：每批 1 次批调用 + batch 次单帧重试 → 13*(1+4)=65 次
    assert total <= 20, '熔断未生效，失败场景下发起了 %d 次调用（应远小于批数×(1+批大小)）' % total
    assert all(s.get('summary') in ('', 'VLM超时跳过') for s in out)


def test_short_video_still_sampled(h):
    """短片段保护：5 秒素材按 12s 间隔会抽不到帧，必须退化到有抽样点。"""
    out = h.run(5.0, [], quality=True)
    assert out, '5 秒素材抽样点数为 0，画面理解空手而归'

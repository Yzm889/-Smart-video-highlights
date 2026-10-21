# -*- coding: utf-8 -*-
"""F1 / M2-1 回归：长视频分段必须受段数硬顶约束（默认 60 段）。

旧实现按场景切点切分，98 分钟可切出 200+ 段，缩略图/草稿/配音逐段串行，
阶段①直接不可用。修复后 _segment_timeline 在段数超限时贪心合并最短相邻段，
把段数压到硬顶以内，且保持时间轴覆盖（首段起点、末段终点、单调递增）。
"""
# 必须在 movie_narrator 之前先导入 webui_server（生产入口），
# 否则 video_render <-> webui_server 的循环导入会因初始化顺序而失败。
import webui_server  # noqa: F401
import movie_narrator as M


def test_enforce_seg_cap_reduces_and_preserves_coverage():
    segs = [(i * 10.0, (i + 1) * 10.0) for i in range(10)]  # 10 段，0..100
    out = M._enforce_seg_cap(segs, 4)
    assert len(out) == 4, out
    assert out[0][0] == 0.0, '首段起点须保持'
    assert abs(out[-1][1] - 100.0) < 1e-6, '末段终点须保持'
    for k in range(len(out) - 1):
        assert abs(out[k][1] - out[k + 1][0]) < 1e-6, '相邻段须首尾相接：%s' % (out,)


def test_enforce_seg_cap_noop_when_under_cap():
    segs = [(i * 10.0, (i + 1) * 10.0) for i in range(3)]
    out = M._enforce_seg_cap(segs, 60)
    assert out == segs, '未超限时不应改动'


def test_segment_timeline_respects_cap(monkeypatch):
    # 模拟 98 分钟视频 + 每 20 秒一个场景切点（约 294 个切点 → 远超硬顶）
    vdur = 98 * 60.0
    cuts = [i * 20.0 for i in range(1, int(vdur / 20))]
    monkeypatch.setattr(M, '_cached_scene_cuts', lambda *a, **k: cuts)
    monkeypatch.setattr(M, 'probe_audio_len', lambda *a, **k: vdur)
    segs = M._segment_timeline('fake.mp4', max_seg=25.0, seg_cap=60)
    assert len(segs) <= 60, '段数必须 <= 硬顶：%d' % len(segs)
    assert segs[0][0] == 0.0, '首段须从 0 开始'
    assert abs(segs[-1][1] - vdur) < 1.0, '末段须覆盖到视频末尾：%s' % (segs[-1],)
    for k in range(len(segs) - 1):
        assert segs[k][1] <= segs[k + 1][0] + 1e-6, '时间轴须单调递增'


def test_segment_timeline_default_cap_is_60(monkeypatch):
    vdur = 120 * 60.0  # 2 小时
    cuts = [i * 15.0 for i in range(1, int(vdur / 15))]
    monkeypatch.setattr(M, '_cached_scene_cuts', lambda *a, **k: cuts)
    monkeypatch.setattr(M, 'probe_audio_len', lambda *a, **k: vdur)
    segs = M._segment_timeline('fake.mp4', max_seg=25.0)  # 用默认 seg_cap
    assert len(segs) <= 60, '默认硬顶应为 60：%d' % len(segs)

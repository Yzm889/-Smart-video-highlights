# -*- coding: utf-8 -*-
"""F2 回归：剪辑后时间轴与段落数必须严格一致，否则字幕/配音整体错位。

背景：render_pipeline 用 `enumerate(new_spans)` 同时索引 narr[] 与 kept_ids[]。
只要剪辑返回的段数比输入少 1，后面所有段的解说词就整体前移一位——
成片出现「第 3 段的解说词烧在第 4 段画面上」，而 align 表报给用户的段 ID 也是错的。

`_cut_video_by_spans` 的契约（其 docstring 自述）是「段数与输入严格一致」，
但入口过滤（过短段）与重叠修正后的过滤两条路径都破坏了这个契约。
"""
import json
import os

import pytest

import webui_server as S  # noqa: F401  先导入宿主（符号晚绑定）
import render_pipeline as RP
import video_render as VR


def _prepare(tmp_path, n_segs=3):
    """造一个最小可用的 analyze_result.json + 假源视频，返回 task_id。"""
    run = 'run-test'
    rd = str(tmp_path / run)
    os.makedirs(rd, exist_ok=True)
    src = str(tmp_path / 'src.mp4')
    with open(src, 'wb') as f:
        f.write(b'\x00' * 4096)
    segs = [{'segment_id': i, 'start': i * 10.0, 'end': i * 10.0 + 9.0,
             'asr_text': '台词%d' % i, 'default_keep': True,
             'thumbnail_url': '', 'ai_draft_narrations': []}
            for i in range(n_segs)]
    with open(os.path.join(rd, 'analyze_result.json'), 'w', encoding='utf-8') as f:
        json.dump({'task_id': run, 'video': {'duration': n_segs * 10.0},
                   'video_path': src, 'segments': segs,
                   'narrative_suggestion': {}, 'warnings': []}, f)
    return run


def _stub_downstream(monkeypatch, tmp_path):
    """把 TTS/混音合成等下游桩掉，让测试用例聚焦在段数校验这一步。
    假视频无法被真实 ffmpeg 处理，不桩会在混音阶段就失败。"""
    monkeypatch.setattr(RP, '_generate_all_tts', lambda *a, **k: [])

    def _compose(*a, **k):
        out = os.path.join(str(tmp_path), 'final.mp4')
        with open(out, 'wb') as f:
            f.write(b'\x00' * 8192)
        return out
    monkeypatch.setattr(RP, '_compose_narration_video', _compose)


def _req(task_id, n_segs=3, narrations=None):
    """narrations 默认留空：TTS 在剪辑之前执行，留空可跳过配音，
    让用例聚焦在「剪辑后段数校验」这一步（配音不是本文件的测试目标）。"""
    return {
        'task_id': task_id,
        'segments': [{'segment_id': i, 'keep': True,
                      'narration': (narrations[i] if narrations else ''),
                      'start': i * 10.0, 'end': i * 10.0 + 9.0}
                     for i in range(n_segs)],
        'params': {},
    }


def test_render_rejects_span_count_mismatch(monkeypatch, tmp_path):
    """剪辑返回段数少于输入时，必须报错而不是静默错位。"""
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(RP, '_w', S)
    run = _prepare(tmp_path, n_segs=3)
    # render_pipeline 从 ffmpeg_utils 直接导入了 probe_audio_len，要打在它的命名空间上
    monkeypatch.setattr(RP, 'probe_audio_len', lambda p: 30.0)
    _stub_downstream(monkeypatch, tmp_path)

    # 模拟剪辑少返回一段（历史上过短段被过滤就会这样）
    def _short(video_path, spans, run_dir, progress=None, **kw):
        return video_path, [(0.0, 9.0), (10.0, 19.0)], 1.0
    monkeypatch.setattr(RP, '_cut_video_by_spans', _short)

    prog = {}
    RP.dispatch_render(_req(run), prog)
    assert prog.get('error'), '段数不一致必须报错，而不是静默错位'
    assert '段数' in prog['error'] or '时间轴' in prog['error'], prog['error']


def test_render_ok_when_span_count_matches(monkeypatch, tmp_path):
    """段数一致时必须正常走完（防止校验矫枉过正）。"""
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(RP, '_w', S)
    run = _prepare(tmp_path, n_segs=3)
    # render_pipeline 从 ffmpeg_utils 直接导入了 probe_audio_len，要打在它的命名空间上
    monkeypatch.setattr(RP, 'probe_audio_len', lambda p: 30.0)
    _stub_downstream(monkeypatch, tmp_path)

    def _ok(video_path, spans, run_dir, progress=None, **kw):
        return video_path, [(0.0, 9.0), (10.0, 19.0), (20.0, 29.0)], 1.0
    monkeypatch.setattr(RP, '_cut_video_by_spans', _ok)

    prog = {}
    RP.dispatch_render(_req(run), prog)
    assert not prog.get('error'), '段数一致不应报错：%s' % prog.get('error')


def test_cut_preserves_segment_count_on_tiny_spans(monkeypatch, tmp_path):
    """过短段必须保留并给最小时长，不得静默丢弃。

    入口曾写 `raw = [(a, b) for a, b in raw if b - a > 0.02]` 直接丢弃过短段，
    与函数自身 docstring 的「段数与输入严格一致」契约冲突。
    """
    # 造一个真实可探测的假视频：直接测到入口规整为止，
    # 后面的 ffmpeg 拼接用 monkeypatch 顶掉，避免依赖真实编解码。
    src = str(tmp_path / 'v.mp4')
    monkeypatch.setattr(VR, 'probe_audio_len', lambda p: 100.0)

    def _fake_run(args, **kw):
        out = args[-1]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'wb') as f:
            f.write(b'\x00' * 2048)
        return 0, b'', b''
    monkeypatch.setattr(VR, 'ffmpeg_run', _fake_run)

    # 5 段输入，其中 2 段长度几乎为 0（历史实现会直接丢掉这 2 段）
    spans = [(0.0, 10.0), (10.0, 10.001), (11.0, 20.0), (20.0, 20.0), (21.0, 30.0)]
    try:
        _path, new_spans, _cut = VR._cut_video_by_spans(
            src, spans, str(tmp_path / 'cuts'), progress={}, skip_no_cut=True)
    except Exception as e:
        pytest.fail('过短段不应导致剪辑整体失败：%s' % e)
    assert len(new_spans) == len(spans), \
        '段数必须与输入一致（输入 %d，实际 %d）：少一段就会让解说词整体错位' % (
            len(spans), len(new_spans))
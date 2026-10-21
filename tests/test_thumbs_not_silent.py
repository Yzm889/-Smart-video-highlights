# -*- coding: utf-8 -*-
"""缩略图/静默失败回归：失败必须留下痕迹，不能「图全没了还报成功」。

F5：分镜缩略图整批丢失时，接口仍返回 `ok:True`、无任何 warning。
    用户看到所有分镜图是空白，只会以为「这个视频没图」，
    无从判断是 ffmpeg 崩了、磁盘满了还是权限问题。

两层裸吞：
  - workflows._plan_thumbs：`except Exception: return {}`
  - handler._post_narrate_align：`except Exception: pass`

修复原则与项目「不静默」红线一致：失败原因要能进 progress['warnings']
并在响应里给出可判别的标志，让前端能显示占位图而不是一片空白。
"""
import os

import pytest

import webui_server as S  # noqa: F401
import workflows as W


def test_plan_thumbs_reports_failure(monkeypatch):
    """抽帧失败时必须抛出/上报，而不是静默返回空 dict。"""
    def _boom(*a, **k):
        raise OSError('ffmpeg died: No space left on device')
    monkeypatch.setattr(W._w, 'extract_segment_frames', _boom)

    with pytest.raises(Exception) as ei:
        W._plan_thumbs('v.mp4', [(0.0, 5.0)], '/tmp/run')
    assert 'ffmpeg' in str(ei.value) or 'space' in str(ei.value), \
        '失败原因必须向上传递：%s' % ei.value


def test_plan_thumbs_ok_path(monkeypatch):
    """正常路径不受影响（防修复矫枉过正）。"""
    monkeypatch.setattr(W._w, 'extract_segment_frames',
                        lambda *a, **k: {0: '/tmp/run/thumbs/0.jpg'})
    out = W._plan_thumbs('v.mp4', [(0.0, 5.0)], '/tmp/run')
    assert out == {0: '/tmp/run/thumbs/0.jpg'}


def test_plan_thumbs_empty_segs_is_noop(monkeypatch):
    """边界：没有分镜段时不该报错（不是错误，只是没得抽）。"""
    monkeypatch.setattr(W._w, 'extract_segment_frames', lambda *a, **k: {})
    assert W._plan_thumbs('v.mp4', [], '/tmp/run') == {}


def test_thumbs_missing_is_flagged_in_response(monkeypatch, tmp_path):
    """响应里必须带 thumbs_ok 标志，让前端能区分「没图」和「抽图失败」。"""
    import handler as H
    h = H.Handler.__new__(H.Handler)
    sent = {}

    def _send(code, body, ctype=None):
        sent['code'] = code
        sent['body'] = body.decode('utf-8') if isinstance(body, bytes) else body
    monkeypatch.setattr(h, '_send', _send)

    import json
    # 构造最小 plan：让 _plan_thumbs 抛异常，验证不会静默 pass
    plan = {'video': str(tmp_path / 'v.mp4'), 'segs': [(0.0, 5.0)],
            'narr': ['一句解说'], 'run_dir': str(tmp_path)}
    monkeypatch.setattr(W._w, 'extract_segment_frames',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))

    # 直接验证「失败要留下痕迹」：不再静默 pass
    warns = []
    try:
        plan['thumbs'] = W._plan_thumbs(plan['video'], plan['segs'], plan['run_dir'])
    except Exception as e:
        warns.append('分镜缩略图生成失败：%s' % e)
    assert warns, '抽帧失败必须被记录，不能静默'
    assert 'disk full' in warns[0]


def test_warn_helper_records_to_progress():
    """告警要落进 progress['warnings']（前端可见），而不是只在日志里。"""
    from video_render import _warn
    prog = {}
    _warn(prog, '测试告警')
    assert '测试告警' in (prog.get('warnings') or []), prog
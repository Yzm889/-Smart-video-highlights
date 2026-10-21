# -*- coding: utf-8 -*-
"""F4 回归：/api/video_frame 的 run_dir 必须被限制在 OUTDIR 内。

旧实现 `os.path.join(OUTDIR, x) if not os.path.isabs(x) else x` 直接信任
入参——当服务以 HOST=0.0.0.0 暴露时，攻击者可传绝对路径或 `..` 穿越，
读取任意目录的 tts_state.json 并抽取任意视频帧。

修复后统一走 `_w._safe_dir`，非法目录名一律 400 拒绝。
"""
import os

import pytest

import webui_server as S
import handler as H


def _make_handler(monkeypatch):
    h = H.Handler.__new__(H.Handler)
    sent = {}

    def _send(code, body, ctype=None):
        sent['code'] = code
        sent['body'] = body.decode('utf-8') if isinstance(body, bytes) else body
    monkeypatch.setattr(h, '_send', _send)
    return h, sent


def test_missing_run_dir_is_400(monkeypatch):
    h, sent = _make_handler(monkeypatch)
    h.path = '/api/video_frame?time=1.0'
    h._get_video_frame()
    assert sent['code'] == 400, sent


def test_absolute_run_dir_is_rejected(monkeypatch):
    """绝对路径（盘符）必须被 _safe_dir 拒绝，返回 400。"""
    h, sent = _make_handler(monkeypatch)
    # C:\Windows 经 URL 编码传入
    h.path = '/api/video_frame?run_dir=C%3A%5CWindows&time=1.0'
    h._get_video_frame()
    assert sent['code'] == 400, ('绝对路径不应被接受: %s' % sent)
    assert '非法' in sent['body'], sent


def test_dotdot_run_dir_is_rejected(monkeypatch):
    """`..` 穿越必须被拒绝。"""
    h, sent = _make_handler(monkeypatch)
    h.path = '/api/video_frame?run_dir=..%2F..%2F..%2Fetc&time=1.0'
    h._get_video_frame()
    assert sent['code'] == 400, ('穿越路径不应被接受: %s' % sent)
    assert '非法' in sent['body'], sent


def test_safe_dir_confines_normal_name():
    """正常目录名必须落在 OUTDIR 内（不抛错、路径合法）。"""
    out = S._safe_dir(S.OUTDIR, 'run-1-20261012-200235')
    assert os.path.commonpath([os.path.abspath(S.OUTDIR), out]) == os.path.abspath(S.OUTDIR)

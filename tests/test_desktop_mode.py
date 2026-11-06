# -*- coding: utf-8 -*-
"""桌面版本地路径直连（2026-11-07）：安全红线回归钉。

路径形态 {name, path, desktop_token} 只对「持有启动器会话令牌」的请求生效——
否则任意网页都能借 127.0.0.1 服务读取本地任意文件（安全红线）。
"""
import os
import sys

import webui_server as S  # noqa: F401  解除循环依赖


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sample_path(tmp_path):
    src = os.path.join(ROOT, 'spring10s.mp4')
    assert os.path.exists(src), '本地无样片，此测试需要 spring10s.mp4'
    return src


def test_path_form_requires_matching_token(tmp_path):
    S.set_desktop_token('T-123')
    try:
        src = _sample_path(tmp_path)
        ok = S._resolve_upload_video(
            {'name': 'spring10s.mp4', 'path': src, 'desktop_token': 'T-123'},
            str(tmp_path), 'src_video')
        assert ok and os.path.isfile(ok), '正确令牌应拷贝落盘'
        assert S._resolve_upload_video(
            {'name': 'spring10s.mp4', 'path': src, 'desktop_token': 'WRONG'},
            str(tmp_path), 'src_video') is None, '错误令牌必须拒绝'
        assert S._resolve_upload_video(
            {'name': 'spring10s.mp4', 'path': src},
            str(tmp_path), 'src_video') is None, '无令牌必须拒绝（网页模式红线）'
        assert S._resolve_upload_video(
            {'name': 'spring10s.mp4', 'path': 'relative/x.mp4', 'desktop_token': 'T-123'},
            str(tmp_path), 'src_video') is None, '相对路径必须拒绝'
    finally:
        S.set_desktop_token('')


def test_frontend_desktop_wiring():
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'static', 'app.js'), encoding='utf-8').read()
    for anchor in ('function initDesktopMode', 'function pickLocalVideo', 'function pickLocalMusic',
                   'function setNarVideoPath', 'file.isLocalPath', 'desktop_token: _desktopToken'):
        assert anchor in js, '桌面模式前端缺：%s' % anchor
    html = open(os.path.join(ROOT, 'static', 'index.html'), encoding='utf-8').read()
    assert 'id="narPickLocalBtn"' in html, '缺「从电脑选择视频」桌面按钮'

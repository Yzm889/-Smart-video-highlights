# -*- coding: utf-8 -*-
"""task #21 回归：_send 必须统一下发安全响应头（CSP + nosniff）。

背景：前端 7040 行 vanilla JS 大量 innerHTML 手拼，XSS 面靠 escapeHtml 手工自觉，
风险集中。CSP 是单点兜底：真正收益在 connect-src 'self'（app.js 全部请求同源，
堵住注入脚本向外网外传数据）+ object-src/frame-src/base-uri 收紧。

同时锁定「不能误伤」：Google Fonts 必须放行（否则字体崩），img/media 的 data:/blob:
必须放行（否则封面预览与本地文件预览崩），且不得出现通配 *。
"""
import handler as H


class _WFile:
    def __init__(self):
        self.buf = b''

    def write(self, b):
        self.buf += b


class _FakeHandler:
    """最小 HTTP 响应桩：捕获状态码与响应头。"""

    def __init__(self):
        self.hdrs = {}
        self.wfile = _WFile()
        self.code = None

    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.hdrs[k] = v

    def end_headers(self):
        pass


def _send_once(ctype='text/html; charset=utf-8', content=b'<html>'):
    h = _FakeHandler()
    H.Handler._send(h, 200, content, ctype)
    return h


def test_send_emits_csp_and_nosniff():
    h = _send_once()
    assert h.code == 200
    assert h.hdrs.get('X-Content-Type-Options') == 'nosniff'
    csp = h.hdrs.get('Content-Security-Policy') or ''
    assert csp, '必须下发 Content-Security-Policy'


def test_csp_blocks_exfiltration_and_plugins():
    csp = _send_once().hdrs['Content-Security-Policy']
    for must in ("default-src 'self'", "connect-src 'self'", "object-src 'none'",
                 "base-uri 'self'", "frame-ancestors 'self'"):
        assert must in csp, 'CSP 缺少关键指令：%s' % must


def test_csp_still_allows_google_fonts_and_media():
    """不能误伤：Google Fonts + 媒体 data:/blob: 必须放行。"""
    csp = _send_once().hdrs['Content-Security-Policy']
    assert 'fonts.googleapis.com' in csp, '未放行 Google Fonts 样式表 → 字体崩'
    assert 'fonts.gstatic.com' in csp, '未放行 Google Fonts 字体文件 → 字体崩'
    assert 'blob:' in csp and 'data:' in csp, '未放行 data:/blob: → 封面/预览崩'


def test_csp_no_wildcard():
    assert '*' not in H.CSP_POLICY, 'CSP 不应出现通配 *（等同不设防）'


def test_send_body_intact_and_extra_headers_kept():
    """CSP 注入不得破坏响应体与调用方自定义头。"""
    h = _FakeHandler()
    H.Handler._send(h, 200, b'{"ok":true}', 'application/json', extra={'X-Test': '1'})
    assert h.wfile.buf == b'{"ok":true}'
    assert h.hdrs.get('X-Test') == '1'
    assert h.hdrs.get('Content-Type') == 'application/json'

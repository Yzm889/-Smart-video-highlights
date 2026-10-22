# -*- coding: utf-8 -*-
"""真机 HTTP 冒烟：真起一个服务，用真 HTTP 请求验首页文档与关键端点（task #23）。

为什么需要它：单测用桩替换 _send/_read_json，**验不到真实响应头**。
初版 CSP 只加在 _send 上、单测全绿，但真机请求首页发现**文档响应没有 CSP**
（首页走 _send_file 而非 _send；浏览器只认文档响应的 CSP → 策略完全失效）。
本文件用真实 socket + 真实 Handler，杜绝「单测绿、真机废」。

只依赖标准库 + 随机空闲端口，无需浏览器、无需 GPU/模型。
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import webui_server as S


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope='module')
def live_server():
    """在随机空闲端口上真起一个服务（daemon 线程，进程退出即回收）。"""
    port = _free_port()
    err = []

    def _run():
        try:
            S.start_server(port, open_browser=False)
        except Exception as e:      # 端口被占等 → 记下来由断言报错，而不是静默跳过
            err.append(e)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        if err:
            pytest.skip('本机无法启动测试服务：%s' % err[0])
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.15)
    else:
        pytest.skip('测试服务 20s 内未就绪')
    yield 'http://127.0.0.1:%d' % port


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_index_document_has_csp(live_server):
    """真机：首页文档必须带 CSP（初版就是漏在这里，单测查不出来）。"""
    st, hd, body = _get(live_server, '/')
    assert st == 200, st
    csp = hd.get('Content-Security-Policy') or hd.get('content-security-policy') or ''
    assert csp, '首页文档响应缺少 Content-Security-Policy → 策略不生效'
    assert "connect-src 'self'" in csp, csp
    assert "object-src 'none'" in csp, csp


def test_index_html_has_key_dom_anchors(live_server):
    """真机：首页含前端关键锚点，避免「页面能开但元素没了」。"""
    st, _hd, body = _get(live_server, '/')
    html = body.decode('utf-8', 'ignore')
    for anchor in ('id="narPlot"', 'id="narDsBtn"', 'id="movieDsBtn"', 'id="ttsSherpaModel"'):
        assert anchor in html, '首页缺少 DOM 锚点：%s' % anchor


def test_static_assets_served(live_server):
    st, _hd, b = _get(live_server, '/static/app.js')
    assert st == 200 and len(b) > 100000, (st, len(b))
    st, _hd, b = _get(live_server, '/static/style.css')
    assert st == 200 and len(b) > 1000, (st, len(b))


def test_api_endpoints_return_valid_json(live_server):
    for path, key in (('/api/ai_status', 'tts_local'), ('/api/tts/voices', 'degrade_msg')):
        st, _hd, b = _get(live_server, path)
        assert st == 200, (path, st)
        j = json.loads(b.decode('utf-8'))
        assert key in j, '%s 缺少字段 %s' % (path, key)


def test_deepseek_plot_error_is_visible_not_500(live_server):
    """新端点：未配置 Key 时必须返回可判别的 200+ok:False（不静默、不 500）。"""
    data = json.dumps({'title': '盗梦空间', 'which': 'nar'}).encode('utf-8')
    req = urllib.request.Request(live_server + '/api/deepseek_plot', data=data, method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            st, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        st, body = e.code, e.read()
    assert st == 200, st
    j = json.loads(body.decode('utf-8'))
    assert 'ok' in j, j
    if j['ok'] is False:
        assert j.get('error'), '失败必须带 error 文案（不静默）'


def test_missing_media_returns_404_not_500(live_server):
    st, _hd, _b = _get(live_server, '/media/__definitely_not_exist__.mp4')
    assert st == 404, st

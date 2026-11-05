# -*- coding: utf-8 -*-
"""回归：/api/upload/init 的文件大小上限判定（2026-11-06 主人实测 5GB 被误拒）。

历史 bug：init 把 need(=文件大小×2.5 的磁盘占用预估) 拿去对比 UPLOAD_TOTAL_MAX，
实际上限被缩到 8/2.5=3.2GB —— 5GB 电影报「文件超过 8GB 上限」。
正确语义：文件大小按文件本身判；×2.5 的预估只用于「磁盘剩余空间」护栏。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
GB = 1024 ** 3


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope='module')
def server():
    import urllib.request  # noqa: F401 探测在 _wait 里用
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, '-c',
         'import webui_server as S; S.start_server(%d, open_browser=False)' % port],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen('http://127.0.0.1:%d/api/hardware' % port, timeout=8)
                ok = True
                break
            except Exception:
                time.sleep(0.3)
        if not ok:
            pytest.skip('服务未能在 12s 内就绪，跳过 HTTP 层断言')
        yield port
    finally:
        proc.terminate()


def _init(port, size):
    req = urllib.request.Request(
        'http://127.0.0.1:%d/api/upload/init' % port,
        data=json.dumps({'name': 't.mp4', 'size': size}).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def test_init_accepts_5gb_movie(server):
    """5GB（主人真实场景）必须放行：ok 或「磁盘空间不足」，绝不能误报「超过 8GB」。"""
    out = _init(server, 5 * GB)
    err = str(out.get('error') or '')
    assert not out.get('ok') or 'up-' in str(out.get('upload_id') or ''), out
    assert '文件超过' not in err, '5GB 文件被误报超限（need×2.5 泄漏进大小判定的回归）：%s' % err


def test_init_rejects_real_oversize(server):
    """真正超过 8GB 的文件必须被拒，且错误文案准确。"""
    out = _init(server, 9 * GB)
    assert out.get('ok') is False
    assert '文件超过' in str(out.get('error') or ''), out

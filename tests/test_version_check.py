# -*- coding: utf-8 -*-
"""新版本自检回归钉（候选 B）。

钉死三条约束：
  1. 版本号按整数元组比较（v1.4.10 必须新于 v1.4.9，字符串比较会判错）
  2. 断网 / 超时 / 被墙一律静默返回 ok:false，绝不抛异常、绝不阻塞启动
  3. 结果进程内缓存，每次启动只查一次
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402
from app_version import APP_VERSION  # noqa: E402


class _Resp:
    def __init__(self, url):
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reset():
    S._VERSION_CHECK_CACHE['done'] = False
    S._VERSION_CHECK_CACHE['result'] = None


def _mock_urlopen(monkeypatch, url):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda req, timeout=None: _Resp(url))


def test_newer_patch_version_detected(monkeypatch):
    _reset()
    major, minor, patch = (int(x) for x in APP_VERSION.split('.'))
    _mock_urlopen(monkeypatch,
                  'https://github.com/Yzm889/-Smart-video-highlights/releases/tag/v%d.%d.%d'
                  % (major, minor, patch + 1))
    out = S.check_latest_version(force=True)
    assert out['ok'] and out['has_update']
    assert out['latest'] == 'v%d.%d.%d' % (major, minor, patch + 1)
    assert out['current'] == 'v' + APP_VERSION


def test_minor_version_two_digits_compared_as_integers(monkeypatch):
    """v1.4.10 必须判定为新于 v1.4.9（字符串比较会误判）。"""
    _reset()
    _mock_urlopen(monkeypatch,
                  'https://github.com/Yzm889/-Smart-video-highlights/releases/tag/v9.10.0')
    out = S.check_latest_version(force=True)
    assert out['has_update'] is True, '版本号比较退化了：%r' % out


def test_same_version_no_update(monkeypatch):
    _reset()
    _mock_urlopen(monkeypatch,
                  'https://github.com/Yzm889/-Smart-video-highlights/releases/tag/v' + APP_VERSION)
    out = S.check_latest_version(force=True)
    assert out['ok'] and not out['has_update']


def test_network_failure_is_silent(monkeypatch):
    """断网不得抛异常、不得阻塞启动。"""
    _reset()
    import urllib.request

    def boom(*a, **k):
        raise OSError('network unreachable')

    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    out = S.check_latest_version(force=True)
    assert out['ok'] is False
    assert out['has_update'] is False
    assert out['current'] == 'v' + APP_VERSION


def test_timeout_is_passed_and_result_cached(monkeypatch):
    """必须带 8s 超时（3s 在代理环境下必超时）；且第二次调用不再打网络。"""
    _reset()
    import urllib.request
    calls = []

    def fake(req, timeout=None):
        calls.append(timeout)
        return _Resp('https://github.com/x/releases/tag/v' + APP_VERSION)

    monkeypatch.setattr(urllib.request, 'urlopen', fake)
    S.check_latest_version()
    S.check_latest_version()
    assert calls == [8], '超时参数或缓存不符合预期：%r' % calls


class _JsonResp(_Resp):
    """模拟 api.github.com 的 JSON 响应。"""

    def __init__(self, body):
        _Resp.__init__(self, '')
        self._body = body

    def read(self):
        return self._body


def test_api_channel_used_when_redirect_blocked(monkeypatch):
    """通道 1（github.com 重定向）被代理掐掉时，必须走通道 2（api.github.com）。

    v1.4.6 修的真实场景：本地代理对 github.com 首次 TLS 建连超时，
    只有 api.github.com 能通；单通道版本自检等于永久失效。
    """
    _reset()
    import urllib.request

    def fake(req, timeout=None):
        url = getattr(req, 'full_url', str(req))
        if 'api.github.com' in url:
            return _JsonResp(b'{"tag_name": "v9.9.9"}')
        raise OSError('proxy CONNECT tunnel failed, response 502')

    monkeypatch.setattr(urllib.request, 'urlopen', fake)
    out = S.check_latest_version(force=True)
    assert out['ok'] is True, 'API 兜底通道没生效：%r' % out
    assert out['latest'] == 'v9.9.9'
    assert out['has_update'] is True
    assert 'redirect' in out['error'], '失败原因要留痕便于排查：%r' % out


def test_all_channels_fail_keeps_reason_but_stays_silent(monkeypatch):
    """两条通道都失败：仍返回 ok:false、不抛异常、error 里有两条原因。"""
    _reset()
    import urllib.request

    def boom(*a, **k):
        raise OSError('network unreachable')

    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    out = S.check_latest_version(force=True)
    assert out['ok'] is False and out['has_update'] is False
    assert 'redirect' in out['error'] and 'api' in out['error']

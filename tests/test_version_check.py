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
    """必须带 3s 超时；且第二次调用不再打网络。"""
    _reset()
    import urllib.request
    calls = []

    def fake(req, timeout=None):
        calls.append(timeout)
        return _Resp('https://github.com/x/releases/tag/v' + APP_VERSION)

    monkeypatch.setattr(urllib.request, 'urlopen', fake)
    S.check_latest_version()
    S.check_latest_version()
    assert calls == [3], '超时参数或缓存不符合预期：%r' % calls

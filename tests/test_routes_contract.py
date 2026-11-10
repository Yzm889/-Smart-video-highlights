# -*- coding: utf-8 -*-
"""HTTP 路由表契约（评测 2026-11-11 新增门禁）。

handler.py 的路由表是「字符串名字 → 方法名」的映射，方法名拼错一个字母
Python 不会报错，直到用户点下那个按钮才静默 404——这类错误没有任何既有
门禁能拦（module_split 只查宿主符号，不查路由表）。本文件把三类映射
（POST_EXACT / GET_EXACT / GET_PREFIX）逐条对账到 Handler 的真实方法。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: F401,E402  先建立 webui 入口（避免循环依赖）
import handler  # noqa: E402


def _all_routes():
    H = handler.Handler
    routes = {}
    for path, name in H.POST_EXACT.items():
        routes[('POST', path)] = name
    for path, name in H.GET_EXACT.items():
        routes[('GET', path)] = name
    for prefix, name in H.GET_PREFIX:
        routes[('GET', prefix + '*')] = name
    return routes


def test_every_route_resolves_to_a_real_handler_method():
    """路由表里的每个方法名都必须真实存在于 Handler 上——拼错=用户点了没反应。"""
    routes = _all_routes()
    assert len(routes) >= 40, '路由表异常缩水（现在 %d 条），像是被误改' % len(routes)
    missing = [(m, p, n) for (m, p), n in routes.items()
               if not callable(getattr(handler.Handler, n, None))]
    assert not missing, '路由指向不存在的方法（会静默 404）：%s' % missing[:8]


def test_live_clip_routes_are_registered():
    """直播切片两条路由必须都在：阶段①分析 + 阶段②按调整清单导出。"""
    routes = _all_routes()
    assert routes.get(('POST', '/api/live_clip')) == '_post_live_clip'
    assert routes.get(('POST', '/api/live_clip/export')) == '_post_live_clip_export'


def test_api_paths_are_namespaced():
    """路径卫生：/api/ 前缀必须真的是 /api/ 开头（防手滑把页面路径塞进 API 表）。"""
    for (method, path) in _all_routes():
        if path.startswith('/api'):
            assert path.startswith('/api/') or path == '/api', path

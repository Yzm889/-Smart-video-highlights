# -*- coding: utf-8 -*-
"""v1.4.43 回归钉：桌面版窗口必须自适应主屏分辨率并在工作区居中。

主人实报：「每次打开桌面版，窗口不在正中间，不自适应本机分辨率和比例」。
旧代码 create_window 写死 1440×900 且不给 x/y——Windows 上按系统默认级联位摆
放（不居中），尺寸也不随屏幕缩放。

运行：pytest tests/test_desktop_window_geometry.py -q
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import desktop  # noqa: E402


def test_geometry_1080p_centered():
    """1080p：85% 比例（1632×918），x 水平居中，y 在工作区（屏高−80）内居中。"""
    w, h, x, y = desktop._calc_window_geometry(1920, 1080)
    assert w == int(1920 * 0.85) and h == int(1080 * 0.85)
    assert x == (1920 - w) // 2, '必须水平居中'
    assert y == (1000 - h) // 2, '必须在工作区（屏高−80 任务栏）内垂直居中'
    assert h <= 1000, '高度不许顶出任务栏'


def test_geometry_2k_scales_up():
    """2K 屏：窗口等比变大，而不是写死的 1440×900。"""
    w, h, x, y = desktop._calc_window_geometry(2560, 1440)
    assert w == int(2560 * 0.85) and h == int(1440 * 0.85)
    assert w > 1440 and h > 900, '2K 屏上窗口必须比旧的写死尺寸更大'


def test_geometry_laptop_768_fits_screen():
    """1366×768 笔记本：min 下限不许把窗口顶出任务栏（可用高只有 688）。"""
    w, h, x, y = desktop._calc_window_geometry(1366, 768)
    assert w <= 1366 - 40 and h <= 768 - 80
    assert w >= 1100 and x == (1366 - w) // 2


def test_geometry_tiny_screen_clamps():
    """极端小屏（800×600）：贴屏上限，绝不超屏。"""
    w, h, x, y = desktop._calc_window_geometry(800, 600)
    assert w <= 800 - 40 and h <= 600 - 80
    assert x >= 0 and y >= 0


def test_geometry_no_screen_returns_none():
    """拿不到屏幕信息 → None（调用方退回系统默认摆放，不许崩）。"""
    assert desktop._calc_window_geometry(0, 0) is None
    assert desktop._calc_window_geometry(None, None) is None


def test_run_desktop_window_passes_centered_geometry(monkeypatch):
    """create_window 必须带上居中的 x/y 与自适应尺寸（拿假 webview 模块验证）。"""
    calls = {}

    class _Screen:
        x, y, width, height = 0, 0, 1920, 1080

    fake = types.SimpleNamespace()
    fake.screens = [_Screen()]

    def _create_window(title, url, **kw):
        calls.update(kw)
        calls['title'] = title
        return object()

    fake.create_window = _create_window
    fake.start = lambda **kw: None
    monkeypatch.setitem(sys.modules, 'webview', fake)

    assert desktop._run_desktop_window('http://127.0.0.1:8765/static/desktop/index.html') is True
    assert calls['title'] == '一帧成片'
    assert calls['x'] == (1920 - calls['width']) // 2, 'create_window 必须带居中 x'
    assert calls['y'] == (1000 - calls['height']) // 2, 'create_window 必须带工作区居中 y'
    assert calls['width'] == int(1920 * 0.85) and calls['height'] == int(1080 * 0.85)
    assert calls.get('js_api') is not None, 'js_api 挂载不许丢（历史事故）'


def test_run_desktop_window_falls_back_when_no_screens(monkeypatch):
    """screens 拿不到（为空）→ 退回旧尺寸且 x/y=None（系统默认摆放），不许崩。"""
    calls = {}

    fake = types.SimpleNamespace()
    fake.screens = []                    # screens 为空 = 拿不到屏幕信息

    def _create_window(title, url, **kw):
        calls.update(kw)
        return object()

    fake.create_window = _create_window
    fake.start = lambda **kw: None
    monkeypatch.setitem(sys.modules, 'webview', fake)

    assert desktop._run_desktop_window('http://127.0.0.1:8765/x') is True
    assert calls['width'] == 1440 and calls['height'] == 900, '回退旧尺寸'
    assert calls['x'] is None and calls['y'] is None, '不瞎给坐标，交给系统默认'

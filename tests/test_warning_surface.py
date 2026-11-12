# -*- coding: utf-8 -*-
"""降级提示必须「看得见」（v1.4.67）。

真机背景：桌面前端的 warnings 从来没被渲染过——后端认真写进
PROGRESS[run].warnings 的降级（画面分析失败 / 弹幕没用上 / 封面没出 /
台词转写失败）在桌面版全落进日志黑洞。网页版 static/app.js 的 renderWarnings
一直是有的，桌面版漏了，于是用户看到的是「分数照出、结果照有，只是悄悄变差」。

锁定三件事：
  1. 桌面版有降级提示坞，且所有进度轮询都走同一个入口 uiProgressHints
  2. 弹幕解析失败会写 warning（以前只 log）
  3. 复用台词缓存/缓存失配这类「不跑转写」的路径也不会把提示吞掉
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding='utf-8') as f:
        return f.read()


# ---------- 1) 前端：提示坞存在 + 轮询统一入口 ----------

def test_desktop_has_warning_dock():
    html = _read('static', 'desktop', 'index.html')
    assert 'id="warnDock"' in html, '桌面版必须有降级提示坞'
    assert 'id="warnDockList"' in html


def test_all_progress_polls_surface_warnings():
    js = _read('static', 'desktop', 'desktop.js')
    assert 'function uiWarnings(' in js and 'function uiProgressHints(' in js
    # 任何 /api/progress 轮询都必须经过统一入口，否则那条流水线的降级又静默了
    n_poll = len(re.findall(r"/api/progress\?run=", js))
    n_hint = js.count('uiProgressHints(p)')
    assert n_poll >= 5, '/api/progress 轮询点异常：%d' % n_poll
    assert n_hint >= n_poll - 1, (
        '有 %d 处进度轮询、只有 %d 处接了 uiProgressHints：漏接的那条流水线'
        '降级了用户看不到' % (n_poll, n_hint))


# ---------- 2) 后端：弹幕解析失败要留痕 ----------

def test_danmaku_parse_failure_warns(monkeypatch):
    import live_clip as LC
    msgs = []
    monkeypatch.setattr(LC, '_note_progress_warning', lambda m: msgs.append(m))
    assert LC._parse_danmaku_xml('<not-a-valid-xml') == []
    assert msgs, '弹幕解析失败必须写 warning（以前只写日志，桌面端等于没提示）'
    assert '弹幕' in msgs[0]


def test_danmaku_empty_input_does_not_warn(monkeypatch):
    """没给弹幕是正常情况，不该报警（只有「给了但用不上」才提示）。"""
    import live_clip as LC
    msgs = []
    monkeypatch.setattr(LC, '_note_progress_warning', lambda m: msgs.append(m))
    assert LC._parse_danmaku_xml('') == []
    assert not msgs


# ---------- 3) 画面运动分析失败 ----------

def test_motion_failure_is_flagged_in_source():
    """_compute_signals 里运动信号缺席必须写 warning。

    这条只能做源码钉：要真跑 _compute_signals 得准备完整视频+音轨，
    而这里要锁的是「有没有那个分支」，不是数值。"""
    src = _read('live_clip.py')
    i = src.index('motion, _mdur = _motion_profile(')
    tail = src[i:i + 700]
    assert '_note_progress_warning' in tail, '运动分析失败必须提示（否则少一路信号没人知道）'

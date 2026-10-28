# -*- coding: utf-8 -*-
"""锁定「唯一流程：先写剧情 → AI 按剧情剪」这一产品约束。

背景（2026-10-29）：用户要求删掉「直接进行 AI 分析」的流程，只留先填剧情再剪辑。
改动前存在三条可触发的路径，其中两条绕过了剧情：

  ① 协同编辑台「▶ 开始 AI 分析」→ /api/analyze          —— 完全不经过剧情
  ② /api/plan 但 plot 留空 → 退化成「AI 识别画面」模式   —— 静默绕过剧情
  ③ /api/plan + plot → _narrate_by_plot                 —— 唯一保留的剧情驱动路径

①②正是「AI 不知道片子讲什么、于是乱剪」的来源。这些测试确保它们不会悄悄回来：
前端入口被移除 + 剧情必填校验存在 + 后端 HTTP 层真的会拒绝无剧情请求。
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


@pytest.fixture(scope='module')
def front():
    return _read('static/index.html'), _read('static/app.js')


# ---------------------------------------------------------------------------
# ① 前端不再有「不填剧情就分析」的入口
# ---------------------------------------------------------------------------

def test_no_direct_ai_analyze_button(front):
    """协同编辑台不得再有「开始 AI 分析」按钮（那是唯一一条完全绕过剧情的入口）。"""
    html, _js = front
    # 只看「真的能点」的地方：按钮/链接的 onclick 绑定。
    # （注释里提到这几个字是允许的，那只是说明，不构成入口）
    for m in re.finditer(r'<button[^>]*onclick="([^"]+)"', html):
        assert 'collabStartAnalyze' not in m.group(1), (
            '又出现了调用 collabStartAnalyze 的按钮：它不填剧情就丢给 AI 盲猜画面，'
            '会导致解说与画面对不上（已按产品要求移除）')
    assert 'id="collabGoBtn"' not in html, '找到了已删除的分析启动按钮容器'


def test_collab_analyze_function_no_longer_runs_analysis(front):
    """collabStartAnalyze 不得再真的发起分析——只能指引用户去写剧情。"""
    _html, js = front
    i = js.find('async function collabStartAnalyze')
    assert i >= 0, '找不到 collabStartAnalyze（若已整体删除，请同步更新本断言）'
    end = js.find('\n}', i)          # 截到本函数闭合，别把下一个函数也算进来
    body = js[i:end if end > 0 else i + 800]
    assert '/api/analyze' not in body, (
        'collabStartAnalyze 仍在调用 /api/analyze —— 这是无需剧情的直接分析，违反流程约束')


def test_build_narrate_requires_plot(front):
    """解说主流程必须在没有剧情时直接拒绝，而不是退化成「AI 识别画面」模式。"""
    _html, js = front
    i = js.find('async function buildNarrate')
    assert i >= 0
    body = js[i:i + 1600]
    # 必须有明确的拦截并 return
    assert '请先写剧情' in body, (
        'buildNarrate 缺少剧情必填提示：不拦截就会走到无剧情分支')
    # 不得再出现「有剧情走A、没剧情走B」的双分支
    assert "plot ? '剧情驱动剪辑' :" not in body, (
        'buildNarrate 仍保留「无剧情 → AI 识别画面」的退化分支')


# ---------------------------------------------------------------------------
# ② 后端 HTTP 层真的会拒绝无剧情请求
# ---------------------------------------------------------------------------

def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope='module')
def server():
    """真起一个服务实例，用于验证 HTTP 层的拦截行为（静态源码断言不够）。

    用子进程而非线程：webui_server.start_server 里有 Ctrl+C 的 signal 注册，
    在 pytest 的非主线程里调用会失败（单独脚本却正常），故沿用 tools/selfcheck.py
    已验证可行的子进程方式。
    """
    import subprocess
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, '-c',
         'import webui_server as S; S.start_server(%d, open_browser=False)' % port],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen('http://127.0.0.1:%d/api/hardware' % port, timeout=2)
                ok = True
                break
            except Exception:
                time.sleep(0.3)
        if not ok:
            pytest.skip('服务未能在 %.0fs 内就绪（端口 %d），跳过 HTTP 层断言' % (40 * 0.3, port))
        yield port
    finally:
        proc.terminate()


def test_plan_rejects_narrate_without_plot(server):
    """POST /api/plan 的解说任务若无剧情必须被拒绝，且给出可行动的错误文案。

    仅做静态检查不够：这条约束的关键是「HTTP 层真的拦住了」，所以要真发一次请求。
    """
    payload = json.dumps({
        'type': 'narrate',
        'params': {},
        'video': {'data': 'ZmFrZQ==', 'name': 'f.mp4'},  # 极小假数据，拦截发生在解析之后
    }).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:%d/api/plan' % server, data=payload,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode('utf-8'))
    assert out.get('ok') is False, '无剧情的解说请求应被拒绝，实际却被接受：%s' % out
    err = out.get('error') or ''
    assert '剧情' in err, '错误文案必须告诉用户要做什么（提到剧情），实际：%s' % err


def test_plan_allows_beatcut_without_plot(server):
    """卡点（beatcut）是音乐节奏驱动，本来就不依赖剧情 —— 不能被误伤。"""
    payload = json.dumps({'type': 'beatcut', 'params': {}, 'video': {'data': 'ZmFrZQ==', 'name': 'f.mp4'}}).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:%d/api/plan' % server, data=payload,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        pytest.fail('卡点请求被意外拦截：%s' % e)
    # 允许因为假素材而业务失败，但不得以「请先提供剧情」为由拒绝
    assert '请先提供剧情' not in (out.get('error') or ''), (
        '卡点任务不应受剧情约束影响，却被拦了：%s' % out.get('error'))

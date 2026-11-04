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


def test_collab_editor_fully_removed(front):
    """协同编辑台应彻底移除——它不是删个按钮就够的。

    原本它是「不填剧情 → 直接 AI 分析 → 人工校订」的旧路径。只删「开始 AI 分析」
    按钮后，整张卡其实还留着：校订区依赖被删掉的轮询，永远拿不到数据；
    而剪成片路径自带完整校订能力（openPlanEditor 支持勾选保留 / 真的剪掉），
    所以留着它只会让人以为存在第二个工作台，还多一处传视频入口（重复且误导）。
    """
    html, js = front
    assert 'collabCard' not in html, '协同编辑台卡片应已移除'
    n = js.count('collab')
    assert n == 0, 'app.js 仍有 %d 处 collab 残留 —— 会访问已删除的 DOM 而抛 TypeError' % n


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
# ③ 上手引导：第一次用的人必须知道「写什么、写多少、写没写够」
# ---------------------------------------------------------------------------

def test_plot_box_open_by_default(front):
    """剧情框必须默认展开——藏在折叠面板里的话，新用户根本不会发现这步是必填的。"""
    html, _js = front
    m = re.search(r'<details[^>]*id="narPlotBox"[^>]*>', html)
    assert m, '找不到剧情框 narPlotBox'
    assert ' open' in m.group(0), '剧情框不应默认折叠：这是流程第一步，必须一眼看到'


def test_plot_guide_tells_what_and_how_much(front):
    """必须明确告诉用户「写什么」和「写多少」——非专业用户最大的卡点就在这里。"""
    html, _js = front
    i = html.find('id="narPlot"')
    assert i > 0
    near = html[max(0, i - 1200):i + 200]      # 剧情框附近
    assert '写什么' in near, '缺少「写什么」的指引'
    assert '写多少' in near, '缺少「写多少」的指引（不给量化标准，用户只能猜）'
    assert '100 字' in near, '「写多少」必须给具体字数门槛'


def test_plot_length_feedback_exists(front):
    """边写边反馈「写够了吗」，比事后报错友好得多。"""
    html, js = front
    assert 'id="plotLenHint"' in html, '缺少字数反馈提示位'
    assert 'function updatePlotHint' in js, '缺少 updatePlotHint 实现'
    assert 'oninput="updatePlotHint()"' in html, 'textarea 未绑定输入事件'
    assert '.plot-len' in _read('static/style.css'), '字数提示缺少样式定义'


def test_no_stale_ai_recognize_flow_wording(front):
    """不得再出现描述「AI 自动识别画面」这类被删除流程的文案。

    流程收敛后仍在 narCard 顶部留着「自动分段 → 识别台词」等描述，
    对第一次用的人是最直接的误导——他会以为 AI 会自己看懂。
    """
    html, _js = front
    for stale in ('自动<b>分段</b>', '看画面 + 写稿'):
        assert stale not in html, (
            '发现过时的流程描述「%s」：它描述的是已被删除的「AI 识别画面」流程。'
            '现在是剧情驱动，文案必须同步。' % stale)


def test_narrate_has_own_movie_name_input(front):
    """解说页必须有**独立的片名输入框**，「一键查剧情」必须读它。

    原实现复用 narTheme（主题/梗概框）当片名：那个框填的是「春天樱花树下散步」
    这类描述，拿它当片名去搜索必然搜不到 —— 用户只会觉得「一键查剧情是坏的」。
    """
    html, js = front
    assert 'id="narMovieName"' in html, '解说页缺少独立的片名输入框'
    i = js.find('async function fetchDeepSeekPlot')
    body = js[i:i + 900]
    assert "$('narMovieName')" in body, (
        '一键查剧情必须读独立的片名框（narMovieName），不能拿主题/梗概框当片名')


def test_two_tabs_state_their_difference(front):
    """两个标签页必须讲清差别——否则新用户不知道该进哪个。

    真实差异（已核对后端）：剪成片必须有视频；只要解说稿可以只给片名/剧情。
    两者引擎相同（都走 _narrate_by_plot），所以区别真的只有「要不要传视频」。
    """
    html, _js = front
    for tab, must in (('nar', '要传视频'), ('movie', '可不传视频')):
        m = re.search(r'<button[^>]*data-subtab="%s"[^>]*>(.*?)</button>' % tab, html, re.S)
        assert m, '找不到 %s 标签页' % tab
        assert must in m.group(1), (
            '标签页 %s 未说明「%s」—— 用户无法判断该进哪个' % (tab, must))


# ---------------------------------------------------------------------------
# ④ 参数区「常用 / 高级」分组：首屏不要被七八个名词砸懵
# ---------------------------------------------------------------------------

# 常用项：对成片影响明显，必须一眼看到（不能藏进折叠区）
_COMMON_FIELDS = ('narrStyle', 'narReq')
# 高级项：不常用，必须收进高级设置
_ADVANCED_FIELDS = ('narGenre', 'narMaxSeg', 'detailLevel', 'narTheme',
                    'subFontSize', 'exportResolution')


def _advanced_span(html):
    """返回高级设置 details 的 (起, 止) 字符区间。内部已无嵌套 details，
    所以从 <details class="nar-advanced"> 到下一个 </details> 即为该区间。"""
    m = re.search(r'<details class="nar-advanced"', html)
    assert m, '找不到「高级设置」折叠区'
    end = html.find('</details>', m.start())
    assert end > 0, '「高级设置」折叠区未闭合'
    return m.start(), end


def test_common_params_are_visible_without_expanding(front):
    """常用参数必须直接可见——不能藏在折叠面板里。"""
    html, _js = front
    a, b = _advanced_span(html)
    for fid in _COMMON_FIELDS:
        pos = html.find('id="%s"' % fid)
        assert pos > 0, '缺少常用参数 %s' % fid
        assert not (a < pos < b), (
            '常用参数 %s 被塞进了高级设置里 —— 常用的东西不该要人展开' % fid)


def test_advanced_params_are_tucked_away(front):
    """高级参数必须收起来，否则首屏信息过载。"""
    html, _js = front
    a, b = _advanced_span(html)
    for fid in _ADVANCED_FIELDS:
        pos = html.find('id="%s"' % fid)
        assert pos > 0, '缺少高级参数 %s' % fid
        assert a < pos < b, (
            '高级参数 %s 没在高级设置里 —— 首屏会被这些细节撑爆' % fid)


def test_plot_box_comes_before_advanced(front):
    """流程顺序：先写剧情（第 1 步），再谈高级设置。顺序反了会显得剧情不重要。"""
    html, _js = front
    a, _b = _advanced_span(html)
    pos = html.find('id="narPlotBox"')
    assert pos > 0
    assert pos < a, '剧情框必须排在高级设置之前——它是第一步，不能落到后面去'


def test_movie_page_also_groups_params(front):
    """「只要解说稿」页同样要分组：加配乐常用可见，分段/剪辑占比/预设收进高级。"""
    html, _js = front
    m = re.search(r'<details class="movie-advanced"', html)
    assert m, '「只要解说稿」页缺少高级设置折叠区'
    end = html.find('</details>', m.start())
    for fid in ('movieMaxSeg', 'movieClipRatio', 'presetSelect'):
        pos = html.find('id="%s"' % fid)
        assert pos > 0, '缺少控件 %s' % fid
        assert m.start() < pos < end, '控件 %s 应收进高级设置，不该平铺' % fid
    # 加配乐是常用项，留在外面
    pos = html.find('id="movieBgm"')
    assert pos > 0
    assert not (m.start() < pos < end), '加配乐是常用项，不该藏进高级设置'


def test_movie_page_shows_numbered_steps(front):
    """「只要解说稿」页必须给出①②③步骤标记——否则用户不知从哪下手。"""
    html, _js = front
    i = html.find('id="movieCard"')
    assert i > 0
    seg = html[i:i + 3000]
    for tag in ('①', '②', '③'):
        assert tag in seg, '缺少步骤标记 %s' % tag
    # 顺序要对：片名 → 剧情 → 视频
    assert seg.find('id="movieName"') < seg.find('id="moviePlot"') < seg.find('id="movieDrop"'), (
        '步骤顺序应为 片名 → 剧情 → 视频')

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

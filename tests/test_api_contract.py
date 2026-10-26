# -*- coding: utf-8 -*-
"""前后端契约门禁（自动发现"接缝漏��"，替代手工点页面测）。

背景：2026-10-26 一天抓到 3 个真实 bug，共同点都是**两侧各写一份、靠人记**：
  · CSP 加在 `_send` 上，但首页走 `_send_file` → 策略完全失效（单测测的是桩，不是真实路径）；
  · 「题材类型」的前端 option value 与后端 GENRE_TEMPLATES 的 key 各写一份，漏传参数 → 控件完全失效；
  · 模型「就绪」只探 /models（存在性）而非真推理（可用性）→ 骗人。

所以本文件把「容易漏、靠人记」的三类接缝变成自动门禁：
  A. **枚举双向对账**：后端枚举 key ↔ 前端 option value（题材/风格/详略/离线模型）；
  B. **API 路径双向对账**：前端 fetch 的每个 /api/... ↔ 后端路由表（抓接口改名/漏接/404）；
  C. **关键响应字段**：前端会读的关键字段，后端必须真的返回（抓"读到 undefined 静默失效"）。
"""
import os
import re

import pytest

import webui_server as S
import handler as H
import movie_narrator as MN
import analyze_pipeline as AN

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, 'static', 'index.html')
JS = os.path.join(ROOT, 'static', 'app.js')


@pytest.fixture(scope='module')
def front():
    with open(HTML, encoding='utf-8') as f:
        html = f.read()
    with open(JS, encoding='utf-8') as f:
        js = f.read()
    return html, js


def _select_options(html, select_id):
    """取某个 <select id="xxx"> 里的所有 option value。"""
    m = re.search(r'<select[^>]*id="%s"[^>]*>(.*?)</select>' % re.escape(select_id), html, re.S)
    if not m:
        return None
    return re.findall(r'<option[^>]*value="([^"]*)"', m.group(1))


# ===========================================================================
# A. 枚举双向对账
# ===========================================================================
def test_genre_options_match_backend_templates(front):
    """题材类型：前端 option value 必须与后端 GENRE_TEMPLATES 的 key 对得上
    （漏一个/拼错一个 → 那个题材就静默走不到对应模板）。"""
    html, _ = front
    opts = set(_select_options(html, 'narGenre') or [])
    assert opts, '页面应存在 narGenre 下拉'
    assert 'auto' in opts, 'narGenre 应保留 auto（自动判型）选项'
    backend = set(MN.GENRE_TEMPLATES.keys())
    missing = backend - opts          # 后端支持、前端没有 → 用户选不到
    extra = opts - backend - {'auto'}  # 前端有、后端不认 → 选了没用
    assert not missing, '后端支持但前端缺失的题材：%s' % sorted(missing)
    assert not extra, '前端有但后端不认的题材（选了会静默失效）：%s' % sorted(extra)


def test_narr_style_options_match_backend(front):
    """解说风格：前端 option value ↔ 后端 NARR_STYLES。"""
    html, _ = front
    opts = set(_select_options(html, 'narrStyle') or [])
    backend = set(MN.NARR_STYLES.keys())
    assert not (backend - opts), '后端支持但前端缺失：%s' % sorted(backend - opts)
    assert not (opts - backend), '前端有但后端不认：%s' % sorted(opts - backend)


def test_detail_level_options_match_backend(front):
    """详略程度：前端 option value ↔ 后端 DETAIL_LEVELS。"""
    html, _ = front
    opts = set(_select_options(html, 'detailLevel') or [])
    backend = set(MN.DETAIL_LEVELS.keys())
    assert opts, '页面应存在 detailLevel 下拉'
    assert not (backend - opts), '后端支持但前端缺失：%s' % sorted(backend - opts)
    assert not (opts - backend), '前端有但后端不认：%s' % sorted(opts - backend)


def test_sherpa_models_come_from_backend(front):
    """离线模型清单必须由后端下发（前端硬编码一份必然对不上）。"""
    html, js = front
    # 前端下拉由 /api/tts/voices 的 sherpa_models 渲染 → 不得硬编码具体模型 key
    hard = [k for k in S.SHERPA_TTS_MODELS.keys() if ("value='%s'" % k) in html or ('"%s"' % k) in html]
    assert not hard, '前端硬编码了离线模型 key %s（应由后端下发）' % hard
    assert 'sherpa_models' in js, '前端应使用后端下发的 sherpa_models'


# ===========================================================================
# B. API 路径双向对账
# ===========================================================================
def _all_routes():
    """后端可路由的路径集合（精确 + 前缀）。"""
    exact = set(H.Handler.GET_EXACT.keys()) | set(H.Handler.POST_EXACT.keys())
    prefixes = [p for p, _ in (getattr(H.Handler, 'GET_PREFIX', None) or [])]
    prefixes += [p for p, _ in (getattr(H.Handler, 'POST_PREFIX', None) or [])]
    return exact, prefixes


def _frontend_api_paths(js):
    """前端 fetch 的 /api/... 路径（去掉 query string 与动态尾巴）。"""
    out = set()
    for m in re.finditer(r"""fetch\(\s*['"`]([^'"`]*?/api/[^'"`]*)['"`]""", js):
        p = m.group(1)
        # 去掉 query string：'/api/progress?run=xxx' → '/api/progress'
        p = p.split('?', 1)[0]
        if p:
            out.add(p)
    norm = set()
    for p in out:
        norm.add(p)
        if p.endswith('/'):
            norm.add(p.rstrip('/'))
    return norm


def test_frontend_api_paths_all_exist_in_backend(front):
    """前端 fetch 的每个 API 路径，后端必须有对应路由（否则 404，功能静默失效）。"""
    _html, js = front
    exact, prefixes = _all_routes()
    missing = []
    for p in sorted(_frontend_api_paths(js)):
        if p in exact:
            continue
        if any(p.startswith(pre) or pre.rstrip('/') == p.rstrip('/') for pre in prefixes):
            continue
        # 动态拼接：'/api/xxx/' + id → 前缀命中即可
        if any(p.rstrip('/') + '/' == pre.rstrip('/') for pre in prefixes):
            continue
        missing.append(p)
    assert not missing, ('前端调用了后端不存在的接口（会 404）：%s\n'
                          '提示：后端改接口名/删端点后，前端必须同步。' % missing)


def test_backend_api_paths_used_by_frontend_or_explicit(front):
    """反向：后端有、前端没用的端点——只提示不失败（可能是给外部/脚本用的）。"""
    _html, js = front
    exact, _pre = _all_routes()
    used = _frontend_api_paths(js)
    unused = sorted(p for p in exact if p.startswith('/api/') and p not in used)
    if unused:
        print('[提示] 后端有但前端未调用的端点（确认是否为遗留）：%s' % unused)


# ===========================================================================
# C. 关键响应字段契约
# ===========================================================================
def _probe_get(path):
    """真起服务 GET 一个端点，返回解析后的 JSON（不依赖浏览器）。"""
    import json
    import socket
    import threading
    import time
    import urllib.request

    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    t = threading.Thread(target=lambda: S.start_server(port, open_browser=False), daemon=True)
    t.start()
    for _ in range(100):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.skip('测试服务未就绪')
    with urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


@pytest.mark.parametrize('path,keys', [
    ('/api/ai_status', ['tts_local', 'voice', 'narr_guide']),
    ('/api/tts/voices', ['degrade_msg', 'sherpa_models', 'label', 'setup']),
    ('/api/local/status', ['infer_ok', 'infer_msg', 'model', 'installed']),
    ('/api/hardware', ['gpu_vram_gb', 'tier', 'recommendations', 'upgrades']),
])
def test_endpoint_provides_required_fields(path, keys):
    """前端会读的关键字段，后端必须真的返回（读到 undefined = 静默失效）。"""
    data = _probe_get(path)
    for k in keys:
        assert k in data, '/api%s 缺少前端依赖的字段 %s（实际返回：%s）' % (path, k, list(data.keys())[:12])

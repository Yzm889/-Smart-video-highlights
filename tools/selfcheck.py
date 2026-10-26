#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键巡检：把「手工点页面测」变成一条命令。

用法：
    python tools/selfcheck.py            # 全量（静态门禁 + 真起服务扫端点 + 单元测试）
    python tools/selfcheck.py --fast     # 快速（跳过单元测试，只跑静态门禁 + 端点扫）
    python tools/selfcheck.py --port 8899

为什么需要它（2026-10-26 的教训）：
    今天抓到的 3 个真实 bug（CSP 加错响应路径 / 题材类型失效 / 模型"就绪"是假的），
    共同点都是**没被任何测试覆盖的接缝**——单测全绿，真机照样坏。
    本工具不追求覆盖一切，只守住最容易漏、最该自动发现的那几类：
      ① 前后端枚举/接口/字段对账（静态，秒级）
      ② 真起服务把每个 GET 端点扫一遍，抓 404/500/非法 JSON
      ③ 关键 POST 端点做「不 500」冒烟
      ④ 单元测试全量

输出即「已知问题清单」：不用再靠人肉点页面发现问题。
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

# 只扫这些 POST（语义上无副作用：查询/测试类）。
# 明确排除会**改状态或触发下载**的端点：/api/ai/config、/api/cancel、/api/history/*、
# /api/material/*、/api/local/pull、/api/vlm/pull、/api/ollama/install、/api/tts/model/*、
# /api/whisper/download、/api/plan、/api/render、/api/narrate*、/api/build 等。
SAFE_POSTS = [
    ('/api/tts/test', {'text': '巡检试听'}),                 # 合成一句试听（写临时文件，可忽略）
    ('/api/deepseek_plot', {'title': '巡检', 'which': 'nar'}),  # LLM 调用，无副作用
    ('/api/tts/cosyvoice/voices', {}),                        # 只读：音色列表
    ('/api/cover', {}),                                       # 缺 file → 400，说明处理器正常执行
]

OK, BAD = '✅', '❌'
problems = []
notes = []


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def step(n, total, title):
    print('\n%s [%d/%d] %s' % ('=' * 4, n, total, title), flush=True)


def static_gate():
    """① 静态门禁：枚举 / API 路径 / 关键字段（复用 tests/test_api_contract 的逻辑）。"""
    step(1, 4, '静态契约门禁（枚举对账 · API 路径对账）')
    import test_api_contract as T
    import movie_narrator as MN
    html = open(os.path.join(ROOT, 'static', 'index.html'), encoding='utf-8').read()
    js = open(os.path.join(ROOT, 'static', 'app.js'), encoding='utf-8').read()

    # 枚举对账
    for sel, backend, name in (('narGenre', set(MN.GENRE_TEMPLATES), '题材'),
                               ('narrStyle', set(MN.NARR_STYLES), '风格'),
                               ('detailLevel', set(MN.DETAIL_LEVELS), '详略')):
        opts = set(T._select_options(html, sel) or [])
        miss = backend - opts
        extra = opts - backend - ({'auto'} if sel == 'narGenre' else set())
        if miss or extra:
            problems.append('枚举漂移[%s]：后端有前端缺=%s，前端有后端不认=%s' % (name, sorted(miss), sorted(extra)))
            print('  %s 枚举对账[%s]：后端有前端缺=%s / 前端有后端不认=%s' % (BAD, name, sorted(miss), sorted(extra)))
        else:
            print('  %s 枚举对账[%s]：%d 项一致' % (OK, name, len(opts)))

    # API 路径对账
    exact, prefixes = T._all_routes()
    missing = []
    for p in sorted(T._frontend_api_paths(js)):
        if p in exact:
            continue
        if any(p.startswith(pre) or pre.rstrip('/') == p.rstrip('/') for pre in prefixes):
            continue
        missing.append(p)
    if missing:
        problems.append('接口漂移：前端调用了后端不存在的接口 %s' % missing)
        print('  %s API 路径对账：前端调了不存在的接口 %s' % (BAD, missing))
    else:
        n = len(T._frontend_api_paths(js))
        print('  %s API 路径对账：前端 %d 个接口全部存在' % (OK, n))

    # 前端 inline handler 引用的函数是否存在（另一个静态门禁）
    import test_ui_contract as U
    defined = U._defined_functions(js)
    miss_fn = sorted({nm for attr, body in U._EVENT_ATTR.findall(html)
                      for nm in U._CALL.findall(body)
                      if nm not in U.BUILTINS and nm not in defined})
    if miss_fn:
        problems.append('UI 断裂：页面事件引用了不存在的函数 %s' % miss_fn)
        print('  %s UI 事件对账：引用了不存在的函数 %s' % (BAD, miss_fn))
    else:
        print('  %s UI 事件对账：inline 事件引用的函数全部存在' % OK)


def live_sweep(port):
    """②③ 真起服务，扫全部 GET 端点 + 关键 POST 冒烟。"""
    import webui_server as S
    import handler as H
    step(2, 4, '真起服务，扫描全部 GET 端点')
    t = threading.Thread(target=lambda: S.start_server(port, open_browser=False), daemon=True)
    t.start()
    for _ in range(120):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.1)
    else:
        problems.append('服务未能在 12s 内启动')
        print('  %s 服务未启动' % BAD)
        return

    def get(path):
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=30) as r:
                return r.status, r.headers.get('Content-Type', ''), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get('Content-Type', ''), e.read()
        except Exception as e:
            return 0, '', str(e).encode()

    gets = sorted(H.Handler.GET_EXACT.keys())
    bad = 0
    for p in gets:
        st, ct, body = get(p)
        txt = body[:400].decode('utf-8', 'ignore').strip()
        is_json = False
        try:
            json.loads(body.decode('utf-8'))
            is_json = True
        except Exception:
            pass
        verdict = 'OK'
        if st == 0:
            verdict = '无响应'
        elif st >= 500:
            verdict = '服务端异常(5xx)'
        elif st == 404 and not is_json:
            # 路由未命中回的是纯文本 b'not found'；handler 自己的 404 回 JSON → 那不算故障
            verdict = '未命中路由'
        elif 'json' in ct and st == 200 and not is_json:
            verdict = '返回非法 JSON'
        if verdict != 'OK':
            bad += 1
            problems.append('GET %s → %s (%s) %s' % (p, verdict, st, txt[:80]))
            print('  %s %-28s %s %s  %s' % (BAD, p, st, verdict, txt[:60]))
    print('  %s GET 端点 %d 个：%d 正常 / %d 异常' % (OK if not bad else BAD, len(gets), len(gets) - bad, bad))

    step(3, 4, '关键 POST 端点冒烟（只查「不 500 / 不未命中路由」）')
    posts = {p: b for p, b in SAFE_POSTS if p in H.Handler.POST_EXACT}
    skipped = [p for p, _ in SAFE_POSTS if p not in H.Handler.POST_EXACT]
    if skipped:
        notes.append('以下候选不是 POST 端点，已跳过：%s' % skipped)
    for p, body in posts.items():
        data = None if body is None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, p), data=data,
                                     method='POST', headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                st, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            st, raw = e.code, e.read()
        except Exception as e:
            st, raw = 0, str(e).encode()
        txt = raw[:200].decode('utf-8', 'ignore')
        if st == 0 or st >= 500 or (st == 404 and 'not found' in txt):
            problems.append('POST %s → %s %s' % (p, st, txt[:60]))
            print('  %s %-28s %s %s' % (BAD, p, st, txt[:60]))
        else:
            try:
                j = json.loads(raw.decode('utf-8'))
                note = '' if j.get('ok', True) else '（业务未就绪：%s）' % str(j.get('error', ''))[:60]
            except Exception:
                note = ''
            if note:
                notes.append('POST %s %s' % (p, note))
            print('  %s %-28s %s%s' % (OK, p, st, note))


def unit_tests(fast):
    step(4, 4, '单元测试全量')
    if fast:
        print('  （--fast 已跳过）')
        return
    cmd = [sys.executable, '-m', 'pytest', 'tests', '-q', '--tb=line', '-p', 'no:cacheprovider']
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    tail = [l for l in (r.stdout or '').strip().splitlines() if l.strip()][-1:]
    print('  %s %s' % (OK if r.returncode == 0 else BAD, tail[0] if tail else '(无输出)'))
    if r.returncode != 0:
        fails = [l for l in (r.stdout or '').splitlines() if l.startswith('FAILED')]
        problems.append('单元测试有 %d 条失败' % len(fails))
        for f in fails[:10]:
            print('     %s' % f)


def main():
    ap = argparse.ArgumentParser(description='一键巡检（替代手工点页面测）')
    ap.add_argument('--port', type=int, default=0, help='巡检用端口（0=随机空闲）')
    ap.add_argument('--fast', action='store_true', help='跳过单元测试')
    a = ap.parse_args()
    port = a.port or _free_port()

    print('=' * 62)
    print(' FrameCut 一键巡检 —— 静态门禁 / 真实端点扫 / 单元测试')
    print(' 项目：%s' % ROOT)
    print('=' * 62)

    static_gate()
    live_sweep(port)
    unit_tests(a.fast)

    print('\n%s 巡检结论 %s' % ('=' * 22, '=' * 22))
    if not problems:
        print('  %s 没有发现已知问题（静态门禁 + 端点扫 + 单元测试全过）' % OK)
    else:
        print('  %s 发现 %d 个问题：' % (BAD, len(problems)))
        for i, p in enumerate(problems, 1):
            print('    %2d. %s' % (i, p))
    if notes:
        print('\n  附注（不算故障）：')
        for n in notes:
            print('    - %s' % n)
    print('\n  提示：端点 500/404 属真问题；「业务未就绪」多半是没配模型/Key 或 Ollama 未启动。')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())

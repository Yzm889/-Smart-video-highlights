# -*- coding: utf-8 -*-
"""第4批模块拆分的结构契约（batch4-3.3/3.4）。

workflows / handler 对宿主符号的引用是 `_w.<符号>` 动态属性访问——这是为了让
测试对 webui_server.<符号> 的 monkeypatch、conftest 的 OUTDIR/HISTORY_PATH
隔离改写继续生效。代价是宿主侧改名/删除符号只会在运行时 AttributeError。

本文件把全部 `_w.` 引用静态锁定：宿主符号改名而忘了同步拆分模块时，
这里立刻红，而不是等到某个端点 500。
"""
import os
import re

import webui_server as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_W_REF = re.compile(r'(?<![A-Za-z0-9_.])_w\.([A-Za-z_][A-Za-z0-9_]*)')


def _host_refs(path):
    with open(path, encoding='utf-8') as f:
        src = f.read()
    return set(_W_REF.findall(src))


def test_workflows_host_refs_resolve():
    refs = _host_refs(os.path.join(ROOT, 'workflows.py'))
    assert refs, 'workflows.py 应当包含 _w. 宿主引用'
    missing = [n for n in sorted(refs) if not hasattr(S, n)]
    assert not missing, 'workflows 引用了宿主不存在的符号: %s' % missing


def test_handler_host_refs_resolve():
    refs = _host_refs(os.path.join(ROOT, 'handler.py'))
    assert refs, 'handler.py 应当包含 _w. 宿主引用'
    missing = [n for n in sorted(refs) if not hasattr(S, n)]
    assert not missing, 'handler 引用了宿主不存在的符号: %s' % missing


def test_split_modules_inject_back():
    """公共符号注入回宿主：webui_server.dispatch_* / Handler 等旧入口完整。"""
    import handler
    import workflows
    for n in ('dispatch_build', 'dispatch_beatcut', 'dispatch_narrate', 'dispatch_movie',
              'dispatch_movie_tts', 'dispatch_movie_compose', 'dispatch_tts_single',
              'dispatch_tts_regen_all', 'dispatch_instruct', 'collect_partial', 'assemble',
              'finalize', '_start_next_queued', 'parse_instruction', '_resolve_music',
              'fail_task', '_plan_thumbs', '_plan_to_ui'):
        fn = getattr(S, n, None)
        assert fn is not None and fn.__module__ == 'workflows', n
    assert S.Handler is handler.Handler
    assert S.start_server is handler.start_server
    assert S.MIME is not None


def test_conftest_isolation_surface_intact():
    """conftest 靠改写这些宿主属性做测试隔离；被挪走/改名 = 测试写真实目录。"""
    for n in ('OUTDIR', 'HISTORY_PATH', 'AI_CONFIG_PATH', 'UPLOAD_DIR', 'MATERIAL_DIR'):
        assert hasattr(S, n), n
    import ai_providers
    assert hasattr(ai_providers, 'AI_CONFIG_PATH')


def test_route_tables_dispatch_to_real_methods():
    """路由表用字符串方法名派发（getattr(self, h)）：拼错方法名 = 该端点必 500。"""
    H = S.Handler
    for path, meth in H.GET_EXACT.items():
        assert hasattr(H, meth), 'GET %s -> %s 方法不存在' % (path, meth)
    for path, meth in H.POST_EXACT.items():
        assert hasattr(H, meth), 'POST %s -> %s 方法不存在' % (path, meth)
    for path, meth in H.GET_PREFIX:
        assert hasattr(H, meth), 'GET 前缀 %s -> %s 方法不存在' % (path, meth)

# -*- coding: utf-8 -*-
"""桌面版 localStorage 跨重启回归钉（2026-11-10）。

真实缺陷：`desktop.py` 一直调用裸的 `webview.start()`，而 pywebview 的
`private_mode` 参数**默认为 True**（webview/__init__.py:173，docstring 明写
"In private mode, cookies and local storage are not preserved"）。

`webview/platforms/winforms.py:740 init_storage()` 的分支是：
    if not _state['private_mode'] or _state['storage_path']:
        cache_dir = _state['storage_path'] or os.path.join(%APPDATA%, 'pywebview')
    else:
        cache_dir = tempfile.TemporaryDirectory().name
默认（private_mode=True 且无 storage_path）走 else —— **每次启动一个全新随机临时
目录**，并且 `winforms.py:372-378 on_close` 会调 `edgechromium.py:122 clear_user_data()`
把它 `shutil.rmtree` 掉。

后果：桌面版里**所有** localStorage 都不跨重启 ——
  · `framecut_desktop_draft_v1`（剧情稿草稿，v1.4.28 新增）
  · `framecut_desktop_adv_v1`（高级参数记忆）
  · `framecut_param_presets`（参数预设，v1.4.4 起就存在）
  · `framecut_mig_dismissed` / `framecut_set_tab`
—— 用户关窗重开一切归零，而状态栏还写着「已恢复上次未提交的剧情稿」（谎报）。

磁盘旁证（本轮实测）：`%APPDATA%\\pywebview` 与 `%LOCALAPPDATA%\\pywebview` 都不存在，
而 `%LOCALAPPDATA%\\Temp` 下有 6 个 `tmp*/EBWebView` 临时 profile，其 BrowserMetrics
里含字面量 `FrameCut.exe`；每个 `Local Storage/leveldb/LOG` 都是
"Creating DB ... since it was missing."，`000003.log` 只有 leveldb 自带的 VERSION 记录
—— 从未以保留存储模式跑过。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _desktop_py():
    with open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8') as f:
        return f.read()


def test_webview_start_disables_private_mode():
    """`webview.start()` 必须显式传 `private_mode=False`，否则 localStorage 不持久。"""
    src = _desktop_py()
    calls = re.findall(r'webview\.start\(([^)]*)\)', src, re.S)
    assert calls, '找不到 webview.start(...) 调用'
    assert any('private_mode=False' in c for c in calls), (
        'webview.start() 必须是 private_mode=False —— 默认 True 会让每次启动拿到一个'
        '全新临时 UserDataFolder（winforms.py:757）并在关窗时 rmtree，'
        'localStorage 全部不跨重启。当前调用：%r' % (calls,))


def test_private_mode_fix_keeps_a_reason_in_comment():
    """这行不是随手改的，必须留下原因注释，否则后人会「顺手清理」掉它。"""
    src = _desktop_py()
    assert 'private_mode' in src
    idx = src.index('private_mode=False')
    around = src[max(0, idx - 1200):idx + 200]
    assert 'localStorage' in around, '缺少「localStorage 不持久」的原因说明'
    assert 'pywebview' in around, '缺少 pywebview 私有模式机制的说明'


def test_no_bare_webview_start_remains():
    """全仓不得再有裸的 webview.start()（无参或只带无关参数）。"""
    me = os.path.abspath(__file__)
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (
            '.git', '.venv', '.venv_build', 'node_modules', '__pycache__',
            '.dsh_tools', '.pytest_scratch', 'webui_output', 'webui_workspace')]
        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            p = os.path.join(dirpath, fn)
            if os.path.abspath(p) == me:
                continue          # 本文件正文里就写着这个调用形态，扫自己只会自我命中
            try:
                with open(p, encoding='utf-8', errors='ignore') as f:
                    t = f.read()
            except OSError:
                continue
            for m in re.finditer(r'webview\.start\(([^)]*)\)', t, re.S):
                if 'private_mode' not in m.group(1):
                    hits.append('%s:%d' % (
                        os.path.relpath(p, ROOT),
                        t[:m.start()].count('\n') + 1))
    assert not hits, '这些 webview.start() 没关 private_mode（localStorage 会丢）：%s' % hits


def test_desktop_js_still_declares_persisted_keys():
    """持久化前提是这些 key 真的会写 localStorage —— 与上面的修复配套，防止两头都空。"""
    with open(os.path.join(ROOT, 'static', 'desktop', 'desktop.js'), encoding='utf-8') as f:
        js = f.read()
    for key in ('framecut_desktop_draft_v1', 'framecut_desktop_adv_v1',
                'framecut_param_presets'):
        assert key in js, 'desktop.js 里找不到持久化 key：%s' % key
    assert 'localStorage.setItem' in js

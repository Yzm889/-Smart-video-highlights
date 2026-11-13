# -*- coding: utf-8 -*-
"""v1.4.81 WebView2 运行时目录自愈回归钉。

【真机事故（2026-11-13）】注册表 `EdgeUpdate\\Clients\\{F3017226…}` 的 pv 写
156.0.4314.50，但 Application 目录下只有 157.0.4341.42（156 被清理软件删了）。
WebView2 加载器按注册表找 156 → "Couldn't find a compatible Webview2 Runtime"
→ 窗口起不来。**更坑的是**：服务线程照常跑、8765 照样监听，所以从这个信号
完全看不出窗口其实没起来（我一度据此判定"启动成功"，误删了 43 个 Release）。

修法：开窗前扫描 Application 下真实存在的最高版本目录，设置 pywebview 认的
`WEBVIEW2_BROWSER_EXECUTABLE_FOLDER`——不改注册表、不要管理员权限。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_desktop_module():
    """desktop.py 在 import 期会读版本号等，但不会启动窗口——可直接 exec。"""
    import importlib.util as u
    spec = u.spec_from_file_location('_dk_under_test',
                                     os.path.join(ROOT, 'desktop.py'))
    m = u.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _make_runtime(root, versions, with_exe=True):
    """造 WEBVIEW2 Application 目录：版本号目录 + msedgewebview2.exe。"""
    for v in versions:
        d = root / v
        d.mkdir(parents=True, exist_ok=True)
        if with_exe:
            (d / 'msedgewebview2.exe').write_bytes(b'x')
    return root


def _point_to(monkeypatch, tmp_path, versions, with_exe=True):
    """把探测的搜索根指到临时目录（篡改两个 Program Files 环境变量）。"""
    root = tmp_path / 'EdgeWebView' / 'Application'
    _make_runtime(root, versions, with_exe)
    # 让 os.path.join(ProgramFiles(x86), 'Microsoft','EdgeWebView','Application')
    # 正好落到 root：给一个假 ProgramFiles(x86)，其下补全路径
    fake_pf = tmp_path / 'pf'
    (fake_pf / 'Microsoft' / 'EdgeWebView' / 'Application').parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(root, fake_pf / 'Microsoft' / 'EdgeWebView' / 'Application',
                   target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        # 无符号链接权限：直接建真实目录
        target = fake_pf / 'Microsoft' / 'EdgeWebView' / 'Application'
        target.mkdir(parents=True, exist_ok=True)
        _make_runtime(target, versions, with_exe)
    monkeypatch.setenv('ProgramFiles(x86)', str(fake_pf))
    monkeypatch.setenv('ProgramFiles', str(tmp_path / 'pf2'))
    monkeypatch.delenv('WEBVIEW2_BROWSER_EXECUTABLE_FOLDER', raising=False)
    return fake_pf / 'Microsoft' / 'EdgeWebView' / 'Application'


def test_picks_highest_existing_version(monkeypatch, tmp_path):
    """多版本共存时选最高版本（且必须真有 msedgewebview2.exe）。"""
    m = _load_desktop_module()
    _point_to(monkeypatch, tmp_path,
              ['150.1.1.1', '157.0.4341.42', '156.0.4314.50'])
    got = m._fix_webview2_runtime()
    assert got, '应探测到运行时目录'
    assert os.path.basename(got) == '157.0.4341.42', got
    assert os.environ.get('WEBVIEW2_BROWSER_EXECUTABLE_FOLDER') == got


def test_skips_dirs_without_exe(monkeypatch, tmp_path):
    """版本目录存在但没有 msedgewebview2.exe（残包）→ 跳过，选次高的完整版。"""
    m = _load_desktop_module()
    app = _point_to(monkeypatch, tmp_path, ['150.1.1.1'])
    # 再单独造一个"只有目录没有 exe"的更高版本
    (app / '199.0.0.1').mkdir(parents=True, exist_ok=True)
    got = m._fix_webview2_runtime()
    assert os.path.basename(got) == '150.1.1.1', got


def test_respects_existing_env(monkeypatch, tmp_path):
    """用户/上层已指定 WEBVIEW2_BROWSER_EXECUTABLE_FOLDER → 绝不复写。"""
    m = _load_desktop_module()
    _point_to(monkeypatch, tmp_path, ['157.0.4341.42'])
    mine = r'D:\my\custom\webview2'
    monkeypatch.setenv('WEBVIEW2_BROWSER_EXECUTABLE_FOLDER', mine)
    got = m._fix_webview2_runtime()
    assert got == '', '已指定时应直接返回，不探测'
    assert os.environ['WEBVIEW2_BROWSER_EXECUTABLE_FOLDER'] == mine


def test_no_runtime_returns_empty(monkeypatch, tmp_path):
    """WebView2 真没装 → 返回空、不设环境变量，让 pywebview 自己报错。"""
    m = _load_desktop_module()
    empty = tmp_path / 'pf_empty'
    monkeypatch.setenv('ProgramFiles(x86)', str(empty))
    monkeypatch.setenv('ProgramFiles', str(tmp_path / 'pf_empty2'))
    monkeypatch.delenv('WEBVIEW2_BROWSER_EXECUTABLE_FOLDER', raising=False)
    got = m._fix_webview2_runtime()
    assert got == ''
    assert not os.environ.get('WEBVIEW2_BROWSER_EXECUTABLE_FOLDER')


def test_ignores_non_version_dirs(monkeypatch, tmp_path):
    """Application 下的非版本目录（Installer/SetupMetrics 等）不能被误选。"""
    m = _load_desktop_module()
    app = _point_to(monkeypatch, tmp_path, ['157.0.4341.42'])
    for junk in ('SetupMetrics', 'PlatformExperiencesHelper', 'Installer'):
        (app / junk).mkdir(parents=True, exist_ok=True)
        (app / junk / 'msedgewebview2.exe').write_bytes(b'x')   # 有 exe 但名字不是版本
    got = m._fix_webview2_runtime()
    assert os.path.basename(got) == '157.0.4341.42', got


def test_wired_into_main_before_heavy_imports():
    """源码钉：探测统一在 main() 入口、且早于 `import webui_server`。

    目的是覆盖所有启动路径并尽早设置，不依赖外部预先 export（os.environ 在进程内
    对 ucrtbase 的 getenv 立即生效）。注意：pythonnet 的 CLR 加载与本变量无关，
    不要在注释里建立因果——真根因见 AI_CHANGELOG v1.4.80-b。"""
    src = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
    body = src[src.index('def main():'):]
    assert '_fix_webview2_runtime()' in body, 'main() 必须调用探测'
    probe = body.index('_fix_webview2_runtime()')
    heavy = body.index('import webui_server')
    assert probe < heavy, '探测必须早于 import webui_server（入口统一设置）'

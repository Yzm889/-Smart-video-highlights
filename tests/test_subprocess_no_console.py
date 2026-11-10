# -*- coding: utf-8 -*-
"""Windows GUI 进程不得弹控制台黑框：所有出片/探测类子进程必须带 CREATE_NO_WINDOW。

背景（2026-11-10 主人实测）：「启动生成的时候会一直弹命令行窗口，一定时间后又没有了」。
430c0cd 只修了 ffmpeg_utils.ffmpeg_run 主路径，tts_engines / webui_server / ai_providers
里仍有一批裸 subprocess.run/Popen；Windows 下 GUI 父进程起控制台子进程会新建黑框，
一次出片几十次调用就连成一片闪跳。

本测试**有意排除** desktop.py 的三处 GUI 程序（explorer 文件管理器、Edge --app 窗口）：
它们本来就该显示窗口，加 CREATE_NO_WINDOW 反而破坏「打开文件夹」与独立窗口回退。

非空转证明：修复前 tts_engines.py / webui_server.py / ai_providers.py 有大量不带
creationflags 的调用，本文件必然变红；可用 git show 430c0cd:<file> 复核当时的源码。
"""
import io
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 允许不带 creationflags 的白名单：仅 GUI 程序 / 已有专门标志的写法
GUI_ONLY = {
    'desktop.py',          # explorer / Edge --app，见模块说明
}
# 临时诊断脚本不入库、不参与门禁
SKIP_PREFIX = ('_diag', 'run_test', 'poll_run', 'check_monotonic')


def _product_py_files():
    for fn in sorted(os.listdir(ROOT)):
        if not fn.endswith('.py') or fn in GUI_ONLY:
            continue
        if fn.startswith(SKIP_PREFIX):
            continue
        yield fn


def _calls_without_flag(path):
    src = io.open(path, encoding='utf-8', errors='replace').read().splitlines()
    bad = []
    for i, line in enumerate(src, 1):
        # 【2026-11-10 补漏】原来只匹配 run|Popen|call|check_output，**漏了 check_call**
        # —— ensure_deps() 的 pip install 正好是 check_call，于是长期躲过门禁。
        # 词表要与「Windows 下会新建控制台」的 API 全集对齐。
        if not re.search(r'subprocess\.(run|Popen|call|check_call|check_output)', line):
            continue
        # 参数可能跨多行；向后看 9 行足够覆盖本项目的调用形态
        chunk = ' '.join(src[i - 1:i + 9])
        if 'creationflags' not in chunk:
            bad.append((i, line.strip()[:100]))
    return bad


@pytest.mark.parametrize('fn', sorted(_product_py_files()))
def test_no_console_flash_in_product_modules(fn):
    bad = _calls_without_flag(os.path.join(ROOT, fn))
    assert not bad, ('%s 有未隐藏窗口的子进程调用（会弹黑框）：%s'
                     % (fn, bad[:6]))


def test_white_list_is_documented_and_still_gui_only():
    """白名单不能被悄悄扩大：desktop.py 里只允许 explorer / Edge 这类 GUI 程序。"""
    src = io.open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
    for m in re.finditer(r'subprocess\.Popen\(\[([^\]]+)', src, re.S):
        head = m.group(1).strip()
        assert head.startswith(("'explorer'", "exe")) or "'explorer'" in head, (
            'desktop.py 出现了新的非 GUI 子进程，需要补 creationflags 或更新白名单：%s'
            % head[:100])


def test_no_win_constant_is_shared():
    """三个模块共用 ffmpeg_utils 的 _NO_WIN，避免各写一份、以后又漏。"""
    import ffmpeg_utils
    assert hasattr(ffmpeg_utils, '_NO_WIN')
    assert ffmpeg_utils._NO_WIN in (0, 0x08000000)  # 0=非Windows / CREATE_NO_WINDOW
    for mod in ('tts_engines', 'webui_server', 'ai_providers'):
        src = io.open(os.path.join(ROOT, mod + '.py'), encoding='utf-8').read()
        assert '_NO_WIN' in src, '%s 未引用共享常量' % mod

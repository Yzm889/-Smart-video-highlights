# -*- coding: utf-8 -*-
"""桥调用失败必须可见（v1.4.7 教训固化）。

真事故：`DesktopApi.open_folder` 因模块漏导入必然抛 NameError，而前端写的是
`window.pywebview.api.open_folder(...).catch(() => {})` —— 异常被吞掉，主人点
「打开文件夹」看到的就是「点了没反应」，坏了很久没人发现。

规则：主人主动点击触发的桥调用，失败必须有 toast；只有后台轮询 / 自动播放
（autoplay 被浏览器策略拦）/ 版本自检这类「不该打扰」的才能静默。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')


def _js():
    with open(JS, encoding='utf-8') as f:
        src = f.read()
    src = re.sub(r'/\*[\s\S]*?\*/', '', src)
    src = re.sub(r'(?m)^\s*//.*$', '', src)
    return src


def test_open_folder_only_reached_through_helper():
    """全文件只允许 openFolder() 里出现一次 api.open_folder。"""
    js = _js()
    assert js.count('api.open_folder') == 1, \
        '有绕过 openFolder 直接调桥的地方：%d 处' % js.count('api.open_folder')
    assert 'async function openFolder(' in js, '缺 openFolder 统一封装'
    assert 'function bridgeFail(' in js, '缺 bridgeFail 统一错误出口'


def test_open_folder_failure_is_visible():
    """openFolder 的 catch 必须调用 bridgeFail（→ toast），不能空吞。"""
    js = _js()
    start = js.index('async function openFolder(')
    end = js.index('\n}', start)
    body = js[start:end]
    assert 'catch(e)' in body, 'openFolder 没有 catch，失败会变未捕获异常'
    assert 'bridgeFail(' in body, 'openFolder 失败被静默吞掉了'
    assert 'catch(() => {})' not in body, 'openFolder 里仍有空 catch'


def test_bridge_fail_reports_to_user():
    js = _js()
    start = js.index('function bridgeFail(')
    body = js[start:js.index('\n}', start)]
    assert 'toast(' in body, 'bridgeFail 必须弹 toast 让主人看见'


def test_no_silent_catch_around_pywebview_calls():
    """pywebview.api 的调用后面直接跟空 catch 的一律视为回归。"""
    js = _js()
    bad = re.findall(r'pywebview\.api\.[A-Za-z_]+\([^)]*\)\s*\.catch\(\s*\(\s*\)\s*=>\s*\{\s*\}',
                     js)
    assert not bad, '这些桥调用失败被静默吞掉：%r' % bad

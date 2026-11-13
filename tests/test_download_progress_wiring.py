# -*- coding: utf-8 -*-
"""源码钉：模型下载进度条必须读后端真实的扁平状态字段。

v1.4.81 实测 bug：/api/whisper/status 返回扁平字段（downloading / download_pct /
download_ok ...），前端却读 s2.download || {} → 恒为空对象，于是进度条被藏、
状态文案被清空、isDone 首帧即真、onDone 也不刷新下拉。两处下载都要保证
「点击即显示」和「至少轮询 2 次才允许结束」，挡住首帧 idle 竞态。
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')
SERVER = os.path.join(ROOT, 'webui_server.py')


def _text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_whisper_progress_reads_flat_status_fields():
    """不得再读不存在的 s2.download 子对象。"""
    s = _text(JS)
    assert 's2.download || {}' not in s, 'whisper/status 是扁平结构，读子对象会恒为空'
    assert 's2.downloading' in s
    assert 's2.download_pct' in s
    assert 's2.download_ok === true' in s
    assert '++ticks >= 2 && !s2.downloading' in s, '必须挡住首帧 idle 竞态'
    assert "_pullBar('whisperPullWrap', 'whisperPullFill', 'whisperPullMeta', true, 0)" in s


def test_whisper_download_marks_running_before_thread():
    """后端必须在启动线程前同步置 running，否则 POST 后立刻轮询会读到 idle。"""
    s = _text(SERVER)
    body = s[s.index('def whisper_download_async'):]
    body = body[:body.index('def _whisper_download_thread')]
    assert "WHISPER_DL['running'] = True" in body


def test_tts2_progress_shown_immediately_and_guarded():
    s = _text(JS)
    assert '++ticks >= 2 && !p.running' in s, '配音下载同样要挡首帧 idle'
    assert "bw.style.display = ''" in s, '点击后必须立刻亮进度条'
    assert "$('tts2PullMeta').textContent = r.message || '正在启动…'" in s

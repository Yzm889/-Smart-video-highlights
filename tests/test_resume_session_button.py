# -*- coding: utf-8 -*-
"""回归：「🔄 恢复上次进度」入口（2026-11-06 主人需求）。

背景：两步走流程的第一步（配音）完成后，用户刷新/重开页面——浏览器必丢本地
视频文件，而「⑥ 手动调整」在流程第 3 步，用户在第 2 步找不到入口，被要求
重新上传 5GB 视频。实际配音与剪辑方案都在服务器（tts_state.json），只缺一个
显眼的恢复入口。

钉住：生成按钮旁必须有「恢复上次进度」按钮；点击 = 跳手动调整页 + 恢复最近
配音任务（loadRecentTts），全程不要求重传视频。
"""
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
JS_PATH = os.path.join(ROOT, 'static', 'app.js')
HTML_PATH = os.path.join(ROOT, 'static', 'index.html')


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def test_resume_button_next_to_generate():
    html = _read(HTML_PATH)
    i_go = html.index('id="narGo"')
    i_resume = html.index('resumeLastSession()')
    assert abs(i_resume - i_go) < 800, '恢复按钮必须紧挨生成按钮（同一个操作行内）'
    assert '恢复上次进度' in html


def test_resume_function_jumps_and_restores():
    js = _read(JS_PATH)
    i = js.index('async function resumeLastSession')
    body = js[i:js.index('\nfunction ', i + 10) if '\nfunction ' in js[i:] else i + 900]
    assert "showStep('adjust')" in body, '必须先跳到手动调整页（流程第 3 步）'
    assert 'loadRecentTts' in body, '必须调用 loadRecentTts 恢复服务器上的配音'
    # loadRecentTts 本身走 /api/tts_recent + restoreTtsState，不要求本地视频文件
    lr = js[js.index('async function loadRecentTts'):js.index('async function restoreTtsState')]
    assert '/api/tts_recent' in lr and 'restoreTtsState' in lr
    assert 'videoToBody' not in lr, '恢复流程不得要求重新上传视频'

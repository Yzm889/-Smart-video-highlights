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


def test_preview_uses_real_source_filename():
    """片段预览/时间轴不得写死 src.mp4——4K MKV 任务实存 src.mkv，写死即全 404
    （2026-11-06 主人实测《惩罚者》预览黑屏的根因）。必须走 _adjVideoUrl 统一取址。"""
    js = _read(JS_PATH)
    assert 'function _adjVideoUrl' in js, '缺统一取址 helper'
    # 三处预览（时间轴播放/ensureLoaded/片段预览）都必须经 helper，且带 mediaUrl 去重载标记
    assert js.count('_adjVideoUrl()') >= 3, '预览取址未统一走 _adjVideoUrl'
    assert js.count('dataset.mediaUrl') >= 3, '缺少 mediaUrl 去重载标记（换任务后可能播旧源）'
    # helper 必须兼容两种形态：带目录的 video_file 与纯文件名 video_path
    i = js.index('function _adjVideoUrl')
    body = js[i:js.index('\n}', i)]
    assert "indexOf('/') < 0" in body, '纯文件名形态未补 runDir 目录段'
    # 恢复与两个完成分支都要把文件名传进 renderAdjustPanel
    assert 'renderAdjustPanel(out.tts_list, runDir, \'movie\', [], out.video_path)' in js
    assert js.count('p.video_file') >= 2, 'nar/movie 完成分支未传 video_file'

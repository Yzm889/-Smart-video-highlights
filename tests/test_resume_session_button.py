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
    """恢复入口两处常驻：左上角流程导航（主人 2026-11-06 指定）+ 生成按钮旁。"""
    html = _read(HTML_PATH)
    i_nav = html.index('id="flowNav"')
    i_top = html.index('id="resumeTopBtn"')
    i_steps = html.index('class="flow-steps"')
    assert i_nav < i_top < i_steps, '恢复按钮必须位于左上角流程导航最前（主人指定显眼位置）'
    assert '恢复上次进度' in html
    # 生成按钮旁保留第二个入口（操作行内就近可用）
    i_go = html.index('id="narGo"')
    i_row = html.index('resumeLastSession()', i_go - 800) if 'resumeLastSession()' in html[:i_go] else -1
    assert i_row > 0 or html.count('resumeLastSession()') >= 2, '生成按钮旁的恢复入口丢失'


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


def test_export_uses_latest_adjustments():
    """导出必须等于最后调整过的成品（2026-11-06 主人要求）。

    两个缺口钉死：
    ① 改词后未手动重生成 → 合成必须自动重配改词段（新字幕配旧声=事故）；
    ② 合成请求必须带上当前交付设置（字幕烧不烧/成片音轨），合成前改卡片要生效。
    """
    js = _read(JS_PATH)
    i = js.index('async function confirmAdjustAndCompose')
    body = js[i:i + 4200]
    # ① 改词检测 + 自动重配（文字基线 audio_text vs 当前文字）
    assert 'audio_text' in body, '合成前未做改词检测'
    assert 'regenSingleTts' in body, '检测到改词后未自动重配'
    # 改词基线在渲染时建立、regen 成功后同步
    assert js.count('audio_text') >= 3, 'audio_text 基线未建立/未同步'
    # ② 两个合成入口都带当前交付设置
    assert 'audio: getAudioDelivery()' in body, 'confirmAdjustAndCompose 未带音轨交付设置'
    j = js.index('async function confirmTtsAndCompose')
    body2 = js[j:j + 1600]
    assert 'audio: getAudioDelivery()' in body2, 'confirmTtsAndCompose 未带音轨交付设置'
    assert 'subtitle: getSubtitleStyle()' in body2, 'confirmTtsAndCompose 未带字幕交付设置'


def test_tts_recent_filters_before_sort():
    """恢复列表必须「先筛任务目录、再按时间取前 20」——曾按目录 mtime 排序后
    才筛：webui_output 里 progress_state.json（每次轮询都刷新 mtime）、上传暂存、
    试听缓存等杂物把任务目录挤出前 20，恢复列表永远为空（2026-11-06 主人实测）。"""
    src = _read(os.path.join(ROOT, 'handler.py'))
    i = src.index('def _get_tts_recent')
    body = src[i:src.index('def _get_tts_state')]
    assert 'task_dirs' in body and "tts_state.json" in body.split('task_dirs.sort')[0], \
        '未先筛任务目录就排序取前 20（杂物会挤掉真任务）'
    assert body.index('tts_state.json') < body.index('sort('), '筛选必须发生在排序之前'


def test_side_by_side_layout():
    """片段列表必须并入预览页右侧竖列（PR 字幕面板式），「片段列表」独立标签页取消
    （2026-11-06 主人需求：对照着时间轴改词，不要来回切换）。"""
    html = _read(HTML_PATH)
    assert 'id="adjustSide"' in html, '缺右侧竖列容器'
    assert 'id="tabSegments"' not in html, '「片段列表」独立标签页应已移除'
    # adjustList 必须位于 adjustSide 内、且都在 tabPreview 内
    i_tp = html.index('id="tabPreview"')
    i_side = html.index('id="adjustSide"')
    i_list = html.index('id="adjustList"')
    i_adv = html.index('id="tabAdvanced"')
    assert i_tp < i_side < i_list < i_adv, 'adjustList 未落在 tabPreview 的 adjustSide 内'
    js = _read(JS_PATH)
    assert 'function _syncSideList' in js, '缺时间轴↔列表联动函数'
    assert js.count('_syncSideList(') >= 3, '联动未接入（时间轴点击/片段预览）'
    # 滚动容器必须量 #adjustList（60vh 的真滚动容器），不能量外层侧栏
    i_sync = js.index('function _syncSideList')
    assert "getElementById('adjustList')" in js[i_sync:i_sync + 700], '滚动容器量错对象'


def test_caption_editor_panel():
    """右侧竖列必须是「纯字幕编辑面板」（PR 式，2026-11-06 主人拍板）：
    紧凑文字行 + 点行跳转时间点 + ✕ 删除 + ➕ 在指针处加字幕；
    行内不再放视频/音频播放器和一堆控件（预览在左边全局播放器）。"""
    js = _read(JS_PATH)
    html = _read(HTML_PATH)
    # 紧凑行模板：cap-row + 时间徽标 + ✕，且不再有行内 <video>/<audio controls>
    i_fn = js.index('function renderAdjustPanel')
    i = js.index('ttsList.forEach(function(item, idx){', i_fn)
    row_html = js[i:js.index('list.innerHTML = html', i)]
    assert "cap-row" in row_html and '✕' in row_html and 'timeLabel' in row_html
    assert '<video' not in row_html and 'audio controls' not in row_html, \
        '字幕行里不应再有视频/音频播放器（预览在左边）'
    # 行点击 → 全局播放器 seek 到该字幕时间点
    assert 'seekAdjVideo(it.video_start' in row_html or 'seekAdjVideo' in js[i:i+3000]
    # ➕ 在指针处加字幕：入口 + 实现（按时间序插入、唯一 index、确认时自动 TTS）
    assert 'addCaptionAtPlayhead' in js and 'addCaptionAtPlayhead()' in html
    i_add = js.index('function addCaptionAtPlayhead')
    add_body = js[i_add:js.index('\nfunction ', i_add + 10) if '\nfunction ' in js[i_add:] else i_add + 1200]
    assert 'video.currentTime' in add_body, '新字幕必须插在播放指针当前位置'
    assert "audio_text: ''" in add_body, '新段无音频基线 → 确认合成时自动 TTS'
    # 单段重配用 item.index（增删后数组位置会漂，用位置会覆盖别人的配音文件）
    assert 'serverIdx' in js, 'regenSingleTts 未改用 item.index 稳定命名'
    # 撤销/重做/B-roll 的重绘统一委托紧凑渲染
    assert 'renderAdjustPanel(_adjustState.items' in js[js.index('function _rerenderAdjustList'):], '_rerenderAdjustList 未统一委托'

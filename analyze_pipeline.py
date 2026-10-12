# -*- coding: utf-8 -*-
"""人机协同两段式 · 阶段①：自动分析管线。

输入一个视频，产出结构化的「段落编辑台」数据：
  - 镜头切分（复用 movie_narrator._segment_timeline）
  - 每段缩略图（ffmpeg 抽帧）
  - 每段 ASR 台词归窗（复用 ai_providers.asr_segments）
  - 每段多版解说词草稿（简洁/悬念/口语，LLM 生成，可失败降级）
  - 整体叙事线建议（主线段 / 建议剪除段 / 理由）

设计红线（与 HANDOFF 一致）：
  - 本模块只产出「草稿与建议」，绝不替用户做留/剪决定；
  - LLM/ASR 不可用或失败时降级并写 warnings，不静默、不崩；
  - 结果写 run_dir/analyze_result.json，供编辑台加载。
"""
import os
import json
import logging

from ffmpeg_utils import ffmpeg_run, probe_audio_len
from ai_providers import asr_segments
from movie_narrator import (
    _segment_timeline,
    _asr_text_in,
    _llm_light,
    _try_parse_json,
    _atomic_json_dump,
)

_log = logging.getLogger('framecut.analyze')

DRAFT_STYLES = ['简洁', '悬念', '口语']


# ---------------------------------------------------------------------------
# 缩略图
# ---------------------------------------------------------------------------
def _make_thumbnail(video_path, mid_sec, out_jpg):
    """取 mid_sec 处一帧缩略图。失败返回 False（不影响主流程）。"""
    try:
        rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.3f' % max(0.0, mid_sec),
                                 '-i', video_path, '-frames:v', '1',
                                 '-q:v', '3', out_jpg])
        return rc == 0 and os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 多版草稿
# ---------------------------------------------------------------------------
def _drafts_for_segment(asr_text):
    """调一次轻量 LLM，要 3 版风格草稿。
    返回 (drafts, err)：成功 drafts=[{style,text}...]；失败 err 非空、drafts=[]。
    """
    if not asr_text.strip():
        return [], 'no_asr'  # 无台词段不生成草稿（由调用方决定 default_keep）
    prompt = (
        '你是影视解说撰稿人。下面是一段视频台词，写 3 个不同风格的解说词草稿。\n'
        '台词：%s\n\n'
        '要求：\n'
        '- 每版 1~2 句，口语化，不超过 40 字\n'
        '- 【简洁】直给说清画面\n'
        '- 【悬念】留钩子勾人看下一段\n'
        '- 【口语】像跟朋友聊电影\n'
        '严格只输出 JSON：{"简洁":"...","悬念":"...","口语":"..."}'
    ) % asr_text[:300]
    try:
        raw = _llm_light(prompt, system='你是资深影视解说撰稿人，只输出 JSON。', timeout=120)
    except Exception as ex:
        return [], 'llm_error: %s' % ex
    if not raw or not raw.strip():
        return [], 'llm_empty'
    obj = _try_parse_json(raw)
    if not isinstance(obj, dict):
        return [], 'llm_parse_fail'
    out = []
    for st in DRAFT_STYLES:
        t = str(obj.get(st) or '').strip()
        if t:
            out.append({'style': st, 'text': t})
    if not out:
        return [], 'llm_empty'
    return out, None


# ---------------------------------------------------------------------------
# 叙事线建议
# ---------------------------------------------------------------------------
def _narrative_suggestion(segments, warnings):
    """基于 segments 给出主线/建议剪除段。LLM 失败时用默认建议（有台词=主线）。"""
    main_line = [s['segment_id'] for s in segments if s['default_keep']]
    suggested_cuts = [s['segment_id'] for s in segments if not s['default_keep']]
    reason = ''
    if not segments:
        return {'main_line': [], 'suggested_cuts': [], 'reason': '无分段'}

    brief = '\n'.join(
        '段%d %.1f-%.1fs %s' % (s['segment_id'], s['start'], s['end'],
                                (s['asr_text'] or '').strip()[:40] or '(无台词)')
        for s in segments
    )
    prompt = (
        '下面是一部视频的分段（段id 时间区间 台词）。给出叙事线建议：\n'
        '哪些段是主线（构成完整故事），哪些段是可剪除的过渡/水词。\n'
        '严格只输出 JSON：{"main_line":[id数组],"suggested_cuts":[id数组],"reason":"一句话理由"}\n\n'
        '%s' % brief
    )
    try:
        raw = _llm_light(prompt, system='你是影视剪辑顾问，只输出 JSON。', timeout=90)
        obj = _try_parse_json(raw or '')
        if isinstance(obj, dict) and 'main_line' in obj:
            ids = set(range(len(segments)))
            ml = [int(x) for x in obj['main_line'] if str(x).lstrip('-').isdigit() and int(x) in ids]
            sc = [int(x) for x in obj.get('suggested_cuts', []) if str(x).lstrip('-').isdigit() and int(x) in ids]
            if ml:  # 至少要有主线才采纳
                main_line, suggested_cuts = ml, sc
                reason = str(obj.get('reason') or '')[:200]
    except Exception as ex:
        warnings.append('叙事建议生成失败（%s），已用默认建议' % ex)
    return {'main_line': main_line, 'suggested_cuts': suggested_cuts, 'reason': reason}


# ---------------------------------------------------------------------------
# 主入口（被 workflows/_spawn 调用，签名与 dispatch_narrate 对齐）
# ---------------------------------------------------------------------------
def dispatch_analyze(req, prog):
    run_dir = prog['run_dir']
    """阶段①自动分析。req: {video_path, options:{max_seg, draft}}。
    结果写 run_dir/analyze_result.json，并挂到 prog['analyze_result']。"""
    video_path = (req.get('video_path') or '').strip()
    options = req.get('options') or {}
    warnings = []
    prog['phase'] = '准备分析'
    prog['pct'] = 2

    if not video_path or not os.path.exists(video_path):
        prog['done'] = True
        prog['error'] = '视频文件不存在: %s' % video_path
        return

    # 1. 镜头切分
    prog['phase'] = '镜头切分中'
    prog['pct'] = 8
    segs = _segment_timeline(video_path, max_seg=float(options.get('max_seg') or 25.0))
    vdur = probe_audio_len(video_path) or 0.0
    if not segs or vdur <= 0:
        prog['done'] = True
        prog['error'] = '镜头切分失败或视频时长为 0'
        return

    # 2. ASR（faster-whisper 未装/失败 → 返回 []，降级不崩）
    prog['phase'] = '识别台词中'
    prog['pct'] = 15
    asr = asr_segments(video_path, progress=prog, pct_range=(15, 45))
    if not asr:
        warnings.append('ASR 不可用或该视频无台词：解说词草稿将受限，你可在编辑台直接手写')

    # 3. 每段缩略图 + ASR 归窗
    thumb_dir = os.path.join(run_dir, 'thumbnails')
    os.makedirs(thumb_dir, exist_ok=True)
    run_base = os.path.basename(run_dir)
    prog['phase'] = '抽取缩略图'
    prog['pct'] = 48
    segments = []
    for i, (s, e) in enumerate(segs):
        mid = (s + e) / 2.0
        jpg = os.path.join(thumb_dir, '%d.jpg' % i)
        has_thumb = _make_thumbnail(video_path, mid, jpg)
        asr_text = _asr_text_in(asr, s, e)
        has_speech = bool((asr_text or '').strip())
        segments.append({
            'segment_id': i,
            'start': round(s, 2),
            'end': round(e, 2),
            'thumbnail_url': ('/media/%s/thumbnails/%d.jpg' % (run_base, i)) if has_thumb else '',
            'asr_text': asr_text or '',
            'semantic_tag': '',
            'emotion': '',
            'key_info': '',
            'ai_importance_hint': 'keep' if has_speech else 'cut_candidate',
            'importance_reason': '' if has_speech else '无台词过渡段',
            'default_keep': has_speech,
            'ai_draft_narrations': [],
        })

    # 4. 每段多版草稿（LLM 失败降级）
    if options.get('draft', True):
        n = max(1, len(segments))
        for i, seg in enumerate(segments):
            prog['pct'] = 55 + int(30 * i / n)
            prog['phase'] = '生成解说词草稿 %d/%d' % (i + 1, n)
            drafts, err = _drafts_for_segment(seg['asr_text'])
            seg['ai_draft_narrations'] = drafts
            if err and err != 'no_asr':
                warnings.append('段%d 草稿生成失败（%s）：可在编辑台手写' % (i, err))
    else:
        warnings.append('草稿生成已按选项关闭（draft=false）')

    # 5. 叙事线建议
    prog['phase'] = '生成叙事线建议'
    prog['pct'] = 88
    narrative = _narrative_suggestion(segments, warnings)

    # 6. 落盘 + 挂到 prog
    result = {
        'task_id': run_base,
        'status': 'analyzed',
        'video': {'duration': round(vdur, 2)},
        'narrative_suggestion': narrative,
        'segments': segments,
        'warnings': warnings,
    }
    _atomic_json_dump(os.path.join(run_dir, 'analyze_result.json'), result)
    prog['analyze_result'] = result
    prog['phase'] = '分析完成，等待你在编辑台确认'
    prog['pct'] = 100
    prog['done'] = True

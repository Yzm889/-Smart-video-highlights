# -*- coding: utf-8 -*-
"""人机协同两段式 · 阶段②：按用户定稿合成。

严格执行用户在编辑台的决策，不替用户做任何内容决定：
  - 保留/剪除：只合成 keep=true 的段；
  - 入点/出点：每段用用户在编辑台确认的 start/end（回退分析默认值）；
  - 解说词：逐字送 TTS，绝不改写、不增删；
  - 顺序：按 segment_id 升序（前端排序在后续版本扩展）。
技术细节（裁剪、画面微变速、混音、字幕窗口、编码）由现有管线智能处理。

输入 req:
  {
    "task_id": "<阶段① run_dir 名>",
    "segments": [
      {"segment_id": 0, "keep": true, "narration": "定稿解说词", "start": 12.3, "end": 20.8},
      ...
    ],
    "params": {可选：分辨率/字幕样式等}
  }
产物：<analyze_dir>/render/final.mp4，prog['file'] 为相对 OUTDIR 的路径。
"""
import os
import json
import shutil
import logging

from ffmpeg_utils import probe_audio_len
from video_render import _cut_video_by_spans, _compose_narration_video
from movie_narrator import _generate_all_tts, _clean_caption, _w

_log = logging.getLogger('framecut.render')


def _to_float(v, default):
    """宽松地把入点/出点转 float；空值/非法值回退 default。"""
    try:
        if v is None or v == '':
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def dispatch_render(req, prog):
    try:
        task_id = (req.get('task_id') or '').strip()
        if not task_id:
            raise RuntimeError('缺少 task_id（请重新进入阶段①分析）')
        analyze_dir = os.path.join(_w.OUTDIR, task_id) if not os.path.isabs(task_id) else task_id
        result_path = os.path.join(analyze_dir, 'analyze_result.json')
        if not os.path.exists(result_path):
            raise RuntimeError('找不到阶段①分析结果（任务可能已过期），请重新分析视频')
        with open(result_path, encoding='utf-8') as f:
            result = json.load(f)

        src_video = result.get('video_path') or (result.get('video') or {}).get('path')
        if not src_video or not os.path.exists(src_video):
            raise RuntimeError('源视频不存在（可能已被清理），请重新分析视频')
        vdur = probe_audio_len(src_video) or 0.0
        if vdur <= 0:
            raise RuntimeError('无法读取源视频时长，源文件可能已损坏')

        # 原始分析段：提供默认时间边界
        orig = {int(s['segment_id']): s for s in (result.get('segments') or [])
                if str(s.get('segment_id')).lstrip('-').isdigit()}

        submitted = req.get('segments') or []
        kept = [s for s in submitted if s.get('keep')]
        kept.sort(key=lambda s: int(s.get('segment_id', 0)))
        if not kept:
            raise RuntimeError('你没有勾选任何保留段落，无法合成（至少保留一段）')

        segs = []
        narr = []
        kept_ids = []
        skipped_invalid = 0
        for s in kept:
            sid = int(s.get('segment_id', -1))
            o = orig.get(sid, {})
            d0 = float(o.get('start', 0.0))
            d1 = float(o.get('end', 0.0))
            st = _to_float(s.get('start'), d0)
            en = _to_float(s.get('end'), d1)
            # 夹到 [0, 视频时长]，并保证出点 > 入点
            st = max(0.0, min(vdur, st))
            en = max(0.0, min(vdur, en))
            if en <= st + 0.02:
                skipped_invalid += 1
                continue
            text = str(s.get('narration') or '').strip()
            segs.append((round(st, 3), round(en, 3)))
            narr.append(text)
            kept_ids.append(sid)

        if not segs:
            raise RuntimeError('没有有效的时间段（入点/出点设置无效，需出点晚于入点）')

        # 草稿与正式片用独立目录，互不覆盖（草稿反复看，正式片定稿后才出）
        is_draft = bool(req.get('draft') or (req.get('params') or {}).get('draft'))
        out_dir = os.path.join(analyze_dir, 'render_draft' if is_draft else 'render')
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)

        params = dict(req.get('params') or {})
        params.setdefault('autoCut', True)
        # 前端传 w/h 时统一转成缩放滤镜识别的分辨率串（否则 build_video_filter 读不到、不缩放）
        if not params.get('resolution') and params.get('w') and params.get('h'):
            try:
                params['resolution'] = '%dx%d' % (int(params['w']), int(params['h']))
            except (TypeError, ValueError):
                pass
        if is_draft:
            # 快速草稿：480p + 最快档，几十秒出片；保留 TTS/变速/字幕，才能真实反映对齐
            params['resolution'] = '854x480'
            params['qualityTier'] = 'speed'
            params['draft'] = True

        # 1) TTS：严格按用户定稿逐字合成，不改文本
        prog['phase'] = '草稿：生成解说配音' if is_draft else '按定稿生成解说配音'
        prog['pct'] = 10
        tts_results = _generate_all_tts(narr, out_dir, prog)

        # 2) 真裁剪：按用户入点/出点，强制剪（skip_no_cut 杜绝「勾选了却不剪」）
        prog['phase'] = '草稿：按入点/出点剪辑画面' if is_draft else '按入点/出点剪辑画面'
        prog['pct'] = 55
        cut_video, new_spans, cut_sec = _cut_video_by_spans(
            src_video, segs, out_dir, prog, skip_no_cut=True)

        # 3) 配音/字幕对齐到裁剪后的新时间轴
        tts_paths = []
        voice_spans = {}
        tts_by_idx = {}
        for i, clip in tts_results:
            tts_by_idx[i] = clip
            if i >= len(new_spans):
                continue
            sp = new_spans[i]
            tts_paths.append((clip, sp[0], sp[1]))
            v_len = probe_audio_len(clip) or max(0.5, sp[1] - sp[0])
            voice_spans[i] = (sp[0], min(sp[1], sp[0] + v_len + 0.35))

        expected_tts = len([t for t in narr if t.strip()])
        if expected_tts > 0 and not tts_paths:
            raise RuntimeError('全部配音生成失败：请在设置页检查配音引擎（edge-tts 需联网 / 本地 TTS 已下载），再重新合成')

        # 3.5) 真实对齐表（用真实 TTS 时长，而非前端字数估算）：配音超出画面会被截断，必须让用户看到
        align = []
        for idx, sp in enumerate(new_spans):
            pic = max(0.0, sp[1] - sp[0])
            text = narr[idx] if idx < len(narr) else ''
            has_text = bool(text.strip())
            clip = tts_by_idx.get(idx)
            voice = probe_audio_len(clip) if clip else 0.0
            align.append({
                'segment_id': kept_ids[idx] if idx < len(kept_ids) else idx,
                'pic_sec': round(pic, 2),
                'voice_sec': round(voice, 2) if has_text else 0.0,
                'has_voice': bool(clip and voice > 0.05),
                'delta': round(voice - pic, 2) if has_text else None,
                'missing_voice': bool(has_text and not (clip and voice > 0.05)),
            })

        # 4) 混音 + 烧字幕 + 导出
        prog['phase'] = '草稿：混音、烧字幕、导出' if is_draft else '混音、烧字幕、导出成片'
        prog['pct'] = 85
        narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)') else _clean_caption(t)
                    for t in narr]
        final = _compose_narration_video(
            cut_video, new_spans, narr_srt, tts_paths, out_dir, params,
            music_path=None, voice_spans=voice_spans)

        if not final or not os.path.exists(final) or os.path.getsize(final) < 1024:
            raise RuntimeError('合成未产出有效视频（ffmpeg 或配音可能失败），请查看日志')

        prog['done'] = True
        prog['pct'] = 100
        prog['phase'] = '草稿完成' if is_draft else '合成完成'
        prog['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
        prog['draft'] = is_draft
        prog['align'] = align
        prog['cut_sec'] = round(float(cut_sec or 0), 1)
        prog['kept_segments'] = len(segs)
        if skipped_invalid:
            prog['warn'] = '%d 个段因入点/出点无效被跳过' % skipped_invalid
        if expected_tts > len(tts_paths):
            prog['warn'] = (prog.get('warn') + '；' if prog.get('warn') else '') + \
                '%d 段解说词未成功配音（已保留画面与字幕）' % (expected_tts - len(tts_paths))
    except Exception as e:
        _w.fail_task(prog, e)

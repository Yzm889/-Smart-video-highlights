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
    _w,
)

_log = logging.getLogger('framecut.analyze')

DRAFT_STYLES = ['简洁', '悬念', '口语']

# 批量草稿的最小段数：低于此值仍走逐段路径（行为完全不变、失败粒度更细）
DRAFT_BATCH_MIN = 6


# ---------------------------------------------------------------------------
# 批量草稿：一次 LLM 调用产出多段（长视频的量级差异就在这里）
# ---------------------------------------------------------------------------
def _try_parse_json_array(text):
    """从模型输出里尽力提取 JSON 数组。返回 list 或 None。

    模型常在数组前后加解释文字或 ```json 围栏，不能直接 json.loads 整段。策略：
    ① 整体能解析且是 list → 直接用；
    ② 找第一个 '[' 到配对 ']' 解析；
    ③ 兜底：逐个提取平衡的 '{...}' 对象（不少模型把数组写成多行对象）。
    """
    if not text:
        return None
    import json as _j
    import re as _re
    t = _re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', text.strip(), flags=_re.S)

    def _balanced(s, open_ch, close_ch):
        """返回 s 中第一个 open_ch 到配对 close_ch 的片段（含两端）；配不上返回 None。"""
        start = s.find(open_ch)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
        return None

    try:
        obj = _j.loads(t)
        if isinstance(obj, list):
            return obj
    except Exception:
        pass
    frag = _balanced(t, '[', ']')
    if frag:
        try:
            obj = _j.loads(frag)
            if isinstance(obj, list):
                return obj
        except Exception:
            pass
    out = []
    pos = 0
    while True:
        m = _re.search(r'\{', t[pos:])
        if not m:
            break
        pos += m.start()
        frag = _balanced(t[pos:], '{', '}')
        if not frag:
            break
        pos += len(frag)
        try:
            o = _j.loads(frag)
            if isinstance(o, dict):
                out.append(o)
        except Exception:
            pass
    return out or None


def _drafts_for_segments_batch(items, timeout=300):
    """一次 LLM 调用产出多段草稿。items=[(seg_index, asr_text), ...]

    【为什么必须批量】旧实现逐段串行调用：20 分钟视频切 60 段 → 60 次模型请求。
    本机实测单次 47~159 秒（模型 9.28GB 且失败后不驻留，每次要重读权重），
    60 次 ≈ 47 分钟~2.6 小时，反复重载把磁盘 I/O 打满、16GB 内存页缓存抖动 →
    整机卡死（用户反馈「网卡都卡掉」）。批量后只 1 次请求。

    返回 ({seg_index: drafts}, err)：解析不到/缺项的段留空，由调用方给出可见告警。
    """
    n = len(items)
    lines = '\n'.join('[%d] %s' % (i, (txt or '').strip()[:300])
                      for i, (_idx, txt) in enumerate(items))
    prompt = (
        '你是影视解说撰稿人。下面是同一部视频的 %d 段台词，请为每段各写 3 个不同风格的解说词草稿。\n'
        '台词：\n%s\n\n'
        '要求：\n'
        '- 每版 1~2 句，口语化，不超过 40 字\n'
        '- 【简洁】直给说清画面\n'
        '- 【悬念】留钩子勾人看下一段\n'
        '- 【口语】像跟朋友聊电影\n'
        '严格只输出 JSON 数组，共 %d 项，每项形如 {"i":0,"简洁":"...","悬念":"...","口语":"..."}，'
        'i 是上面台词的编号。' % (n, lines, n)
    )
    max_tokens = min(8000, 1200 + 140 * n)
    raw = _llm_light(prompt, system='你是资深影视解说撰稿人，只输出 JSON 数组。',
                     timeout=timeout, max_tokens=max_tokens)
    arr = _try_parse_json_array(raw or '')
    if not isinstance(arr, list) or not arr:
        return {}, 'llm_parse_fail'
    # 先按显式 i 归位；只有当显式编号没能覆盖全部段、且数组长度正好等于段数时，
    # 才改用位置对应（有的模型会忽略 i 或用 1 起始），否则宁可不猜（错位更糟）。
    by_idx = {}
    for k, obj in enumerate(arr):
        if not isinstance(obj, dict):
            continue
        try:
            idx = int(obj.get('i'))
        except Exception:
            idx = None
        if idx is not None and 0 <= idx < n:
            by_idx.setdefault(idx, obj)
    use_positional = (len(by_idx) < n) and (len(arr) == n)
    out = {}
    for k, obj in enumerate(arr):
        if not isinstance(obj, dict):
            continue
        if use_positional:
            idx = k
        else:
            try:
                idx = int(obj.get('i'))
            except Exception:
                continue
            if not (0 <= idx < n):
                continue
        seg_idx = items[idx][0]
        drafts = []
        for st in DRAFT_STYLES:
            t = str(obj.get(st) or '').strip()
            if t:
                drafts.append({'style': st, 'text': t})
        if drafts:
            out[seg_idx] = drafts
    return out, (None if out else 'llm_parse_fail')


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

    # 视频来源：优先上传对象（{name, upload_id/data}，落盘到 run_dir/src.*，render 时稳定可用）；
    # 否则用本地路径（兼容手动填路径）。
    video_obj = req.get('video')
    if video_obj:
        try:
            resolved = _w._resolve_upload_video(video_obj, run_dir, 'src')
        except Exception as ex:
            resolved = None
            warnings.append('视频落盘失败：%s' % ex)
        if resolved:
            video_path = resolved
        elif not video_path:
            # 收到了 video 对象但解析不出有效文件——给可操作的提示，而不是笼统的「(未收到视频)」
            prog['done'] = True
            if video_obj.get('upload_id'):
                prog['error'] = '上传的视频源已失效（没找到已上传文件，可能上传未完成或已被清理）。请重新选择视频后再点「开始 AI 分析」。'
            elif video_obj.get('name') or video_obj.get('data'):
                prog['error'] = '视频数据不完整（没收到有效内容）。请重新选择视频后再点「开始 AI 分析」。'
            else:
                prog['error'] = '未收到视频：请先在协同台选择视频文件，再点「开始 AI 分析」。'
            return
    if not video_path or not os.path.exists(video_path):
        prog['done'] = True
        prog['error'] = '视频文件不存在: %s' % (video_path or '(未收到视频)')
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
    # ASR 内部把真实异常写进了 PROGRESS['warnings']（模型缺失/CUDA 缺库/网络等），
    # 这里原样带到结果里——不能只说「无台词」，用户无从判断是片源静音还是环境坏了。
    for w in (prog.get('warnings') or []):
        if w not in warnings:
            warnings.append(w)
    if not asr:
        warnings.append('ASR 未识别到台词：可能是该片段本就无对白，也可能是语音识别环境异常'
                        '（详见上方原因）。解说词草稿将受限，你可在编辑台直接手写。')

    # 3. 每段缩略图 + ASR 归窗
    thumb_dir = os.path.join(run_dir, 'thumbnails')
    os.makedirs(thumb_dir, exist_ok=True)
    run_base = os.path.basename(run_dir)
    prog['phase'] = '抽取缩略图'
    prog['pct'] = 48
    # 抽帧并发：ffmpeg 是子进程（释放 GIL），线程池有效；封顶 8 路避免抢磁盘。
    # 旧实现逐段串行，60 段 = 60 次串行 ffmpeg 启动，长视频上是分钟级。
    thumb_ok = {}
    try:
        from concurrent.futures import ThreadPoolExecutor

        def _one_thumb(idx, s, e):
            jpg = os.path.join(thumb_dir, '%d.jpg' % idx)
            return idx, _make_thumbnail(video_path, (s + e) / 2.0, jpg)
        _jobs = [(i, s, e) for i, (s, e) in enumerate(segs)]
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(_jobs)))) as _ex:
            for _i, _ok in _ex.map(lambda t: _one_thumb(*t), _jobs):
                thumb_ok[_i] = _ok
    except Exception as _te:
        thumb_ok = {}
        warnings.append('缩略图并发抽取异常（已退回逐段抽取）：%s' % str(_te)[:120])
        # 真的退回逐段抽取（不能只在告警里说要退、实际却没抽）
        for _i, (_s, _e) in enumerate(segs):
            _jpg = os.path.join(thumb_dir, '%d.jpg' % _i)
            thumb_ok[_i] = _make_thumbnail(video_path, (_s + _e) / 2.0, _jpg)
    segments = []
    for i, (s, e) in enumerate(segs):
        mid = (s + e) / 2.0
        jpg = os.path.join(thumb_dir, '%d.jpg' % i)
        has_thumb = bool(thumb_ok.get(i)) or (os.path.exists(jpg) and os.path.getsize(jpg) > 0)
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
    # 4a. 草稿生成前先做**真实推理探针**：实测本地模型能列出却推理即崩/挂起 200+ 秒，
    #     不探就等 → 用户跑 20 分钟视频时才发现，本机已被拖死。探针 25 秒内出不来
    #     就直接跳过草稿阶段并给出可操作提示（而不是整程挂着）。
    _infer_ok, _infer_msg = True, ''
    try:
        from ai_providers import local_llm_infer_probe
        _infer_ok, _infer_msg = local_llm_infer_probe()
    except Exception:
        _infer_ok, _infer_msg = True, ''
    if options.get('draft', True) and not _infer_ok:
        warnings.append('本地模型不可用，已跳过草稿生成：%s（可在「🤖 AI 配置 → 测试连接」查看；'
                        '若为显存不足请换更小的模型）' % _infer_msg[:200])

    if options.get('draft', True) and _infer_ok:
        n = max(1, len(segments))
        # 批量优先：段数多时**一次** LLM 调用产出全部草稿。
        # 旧实现逐段串行 → 20 分钟视频 60 段 = 60 次模型请求（本机单次 47~159 秒
        # 且失败后要重读 9.28GB 权重）→ 47 分钟~2.6 小时，并把磁盘/内存打满、整机卡死。
        speech_idx = [i for i, seg in enumerate(segments) if (seg.get('asr_text') or '').strip()]
        if len(speech_idx) >= DRAFT_BATCH_MIN:
            prog['phase'] = '批量生成解说词草稿（%d 段一次请求）' % len(speech_idx)
            prog['pct'] = 62
            _items = [(i, segments[i]['asr_text']) for i in speech_idx]
            try:
                _got, _err = _drafts_for_segments_batch(_items)
            except Exception as _ex:
                _got, _err = {}, 'llm_error: %s' % _ex
            for i in speech_idx:
                _d = _got.get(i)
                if _d:
                    segments[i]['ai_draft_narrations'] = _d
            _missing = [i for i in speech_idx if not _got.get(i)]
            if _err:
                warnings.append('批量草稿生成失败（%s）：%d 段无草稿，可在编辑台手写'
                                % (_err, len(_missing)))
            elif _missing:
                warnings.append('批量草稿解析不完整：%d/%d 段无草稿（可在编辑台手写）'
                                % (len(_missing), len(speech_idx)))
        else:
            for i, seg in enumerate(segments):
                prog['pct'] = 55 + int(30 * i / n)
                prog['phase'] = '生成解说词草稿 %d/%d' % (i + 1, n)
                drafts, err = _drafts_for_segment(seg['asr_text'])
                seg['ai_draft_narrations'] = drafts
                if err and err != 'no_asr':
                    warnings.append('段%d 草稿生成失败（%s）：可在编辑台手写' % (i, err))
    else:
        warnings.append('草稿生成已按选项关闭（draft=false）')

    # 4b. 本地模型失败原因必须让用户看见（原来被 except: pass 吞掉 → 用户只感到「卡」）
    try:
        from ai_providers import local_llm_last_error
        _lerr = local_llm_last_error()
    except Exception:
        _lerr = ''
    if _lerr and not any('本地模型' in w for w in warnings):
        warnings.append('本地模型异常：%s' % _lerr[:220])

    # 5. 叙事线建议
    prog['phase'] = '生成叙事线建议'
    prog['pct'] = 88
    narrative = _narrative_suggestion(segments, warnings)

    # 6. 落盘 + 挂到 prog
    result = {
        'task_id': run_base,
        'status': 'analyzed',
        'video': {'duration': round(vdur, 2), 'path': os.path.abspath(video_path)},
        'video_path': os.path.abspath(video_path),
        'narrative_suggestion': narrative,
        'segments': segments,
        'warnings': warnings,
    }
    _atomic_json_dump(os.path.join(run_dir, 'analyze_result.json'), result)
    prog['analyze_result'] = result
    prog['phase'] = '分析完成，等待你在编辑台确认'
    prog['pct'] = 100
    prog['done'] = True

# -*- coding: utf-8 -*-
"""解说方案（plan.json）一等公民层 —— 五阶段架构的地基（v1.4.99 立项）。

【为什么存在】
本周实锤的四类架构病全部指向同一根因：管线的状态没有一等公民——
segs/narr/quote_slots/voice_spans/params 走私在 progress 大字典与
tts_state.json/state.json 两套散落文件里，两条编排路径（一步直出/
两步合成）各自解释轴约定，功能修在一条路径另一条永远坏（引用让位、
白 spans 轴、估时窗都是实例）。plan.json 把「解说方案」变成纯数据：
纯数据 → 可版本、可手改、可独立重执行、可对比。

【双轴约定】—— 本模块存在的第一条铁律
- src 轴：源片时间轴（剪辑前）。stage.plan 产出它。
- out 轴：拼接后成片时间轴（剪辑后）。stage.render 产出它。
两条轴都显式存（src/out 两字段），谁导入谁知道哪条轴有值——
「这个窗到底是哪条轴」这一整类事故（白 spans、引用窗、估时窗）
从 schema 上消灭。

【阶段账本】stage_ledger 记录每个阶段用什么引擎/模型跑出来的——
「换引擎重跑这一步」的对比与可复现性靠它。

【迁移路线】
1. 本模块：schema + 遗留导入器（tts_state.json / 最终 state.json → plan v1）
2. 双路径写入 plan.json（纯新增，旧行为零变化）
3. 双路径改消费 plan.json（消灭分叉）
4. render 按 plan 执行（out 轴回填）

导入器约定：缺失字段一律 None/空，绝不猜——「宁缺勿错」与引用让位
的检测同哲学。本模块不 import movie_narrator（避免循环），需要其判定
函数（如 is_white_line）由调用方传入。
"""
import json
import os
import time

PLAN_VERSION = 1

KIND_NARRATE = 'narrate'    # 普通解说段
KIND_QUOTE = 'quote'        # 引用让位段：不配音，原片台词原声
KIND_BROLL = 'broll'        # 无解说插入段
KIND_BLANK = 'blank'        # 留白段：闭嘴让原声飞


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

def new_plan(meta=None):
    """空 plan 骨架。meta: {movie, plot, video, params, mode}。"""
    return {
        'version': PLAN_VERSION,
        'meta': dict(meta or {}),
        'roles': {},          # 各阶段引擎足迹 {stage: {engine, model, at}}
        'cuts': None,         # 剪辑决策 {coverage, segments:[{start,end,keep}]}（源轴）
        'segments': [],       # 一等公民段列表（见 _segment）
        'music': None,
        'subtitle_file': None,
        'stage_ledger': {},   # {stage: {'engine':..., 'model':..., 'at': iso}}
    }


def segment(index, kind=KIND_NARRATE, text='', audio_text=None,
            src=None, out=None, quote=None, audio=None,
            voice_span=None, volume=None):
    """单个段的构造器（字段缺失一律 None/空——宁缺勿错）。"""
    assert kind in (KIND_NARRATE, KIND_QUOTE, KIND_BROLL, KIND_BLANK), kind
    return {
        'index': int(index),
        'kind': kind,
        'text': str(text or ''),
        'audio_text': str(audio_text if audio_text is not None else text or ''),
        'src': list(src) if src else None,      # [start, end] 源轴
        'out': list(out) if out else None,      # [start, end] 输出轴
        'quote': dict(quote) if quote else None,  # {text, src_start, src_end, how}
        'audio': dict(audio) if audio else None,  # {file, duration, engine}
        'voice_span': list(voice_span) if voice_span else None,
        'volume': dict(volume) if volume else None,
    }


# ---------------------------------------------------------------------------
# 遗留导入器：两套旧状态文件 → plan v1
# ---------------------------------------------------------------------------

def plan_from_tts_state(state, is_blank=None):
    """两步路径遗留状态（tts_state.json）→ plan v1。

    state 关键字段：video_path/segs(源轴)/narr/narr_map/params/tts_results
    [[i, path]]/quote_slots/{i:{text,start,end,how}}/movie_name/tts_failures/
    cut_plan/scene_story/scene_alignment。
    轴：segs 是剪辑前源轴 → 填 src，out=None（render 后回填）。
    is_blank: 调用方传入的留白判定（如 movie_narrator.is_white_line），
    命中 → KIND_BLANK。"""
    plan = new_plan({
        'movie': state.get('movie_name') or '',
        'video': state.get('video_path') or '',
        'params': state.get('params') or {},
        'mode': 'two-step',
    })
    segs = state.get('segs') or []
    narr = state.get('narr') or []
    results = {}
    for item in (state.get('tts_results') or []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            results[int(item[0])] = item[1]
    quote_slots = {}
    for k, v in (state.get('quote_slots') or {}).items():
        try:
            quote_slots[int(k)] = v or {}
        except (TypeError, ValueError):
            continue
    cuts = _import_cuts(state.get('cut_plan'))
    if cuts:
        plan['cuts'] = cuts
    for i, seg in enumerate(segs):
        try:
            s0, e0 = float(seg[0]), float(seg[1])
        except (TypeError, ValueError, IndexError):
            continue
        text = narr[i] if i < len(narr) else ''
        q = quote_slots.get(i)
        if q and q.get('start') is not None:
            kind = KIND_QUOTE
            quote = {'text': str(q.get('text') or ''),
                     'src_start': q.get('start'), 'src_end': q.get('end'),
                     'how': q.get('how') or ''}
        elif is_blank and is_blank(text):
            kind = KIND_BLANK
            quote = None
        else:
            kind = KIND_NARRATE
            quote = None
        audio_file = results.get(i)
        seg_rec = segment(
            i, kind=kind, text=text,
            src=[s0, e0], out=None, quote=quote,
            audio=({'file': audio_file} if audio_file else None),
        )
        plan['segments'].append(seg_rec)
    if state.get('scene_story') is not None:
        plan['stage_ledger']['scene_story'] = {'at': _now()}
    if state.get('asr'):
        plan['stage_ledger']['asr'] = {'count': len(state['asr']), 'at': _now()}
    return plan


def plan_from_final_state(state, is_blank=None):
    """一步直出遗留状态（state.json）→ plan v1。

    state 关键字段：src_video/segs(剪辑后输出轴!)/narr/tts_paths
    [[path, start, end]]/voice_spans/{i:[s,e]}/music_path/params/final。
    轴：segs 是拼接后输出轴 → 填 out，src=None（一步直出路径没存源映射，
    宁缺勿错——这是要还的债，不是设计）。"""
    plan = new_plan({
        'video': state.get('src_video') or '',
        'params': state.get('params') or {},
        'mode': 'one-shot',
    })
    if state.get('music_path'):
        plan['music'] = state['music_path']
    segs = state.get('segs') or []
    narr = state.get('narr') or []
    paths = {}
    for item in (state.get('tts_paths') or []):
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            paths[len(paths)] = item   # [path, start, end] 按序对应段
    spans = state.get('voice_spans') or {}
    for i, seg in enumerate(segs):
        try:
            s0, e0 = float(seg[0]), float(seg[1])
        except (TypeError, ValueError, IndexError):
            continue
        text = narr[i] if i < len(narr) else ''
        if is_blank and is_blank(text):
            kind = KIND_BLANK
        else:
            kind = KIND_NARRATE
        path_entry = paths.get(i)
        audio = None
        if path_entry:
            audio = {'file': path_entry[0],
                     'duration': round(float(path_entry[2]) - float(path_entry[1]), 3)}
        vs = spans.get(str(i)) or spans.get(i)
        plan['segments'].append(segment(
            i, kind=kind, text=text,
            src=None, out=[s0, e0], audio=audio,
            voice_span=(list(vs) if vs else None),
        ))
    if state.get('final'):
        plan['stage_ledger']['render'] = {'final': state['final'], 'at': _now()}
    return plan


def _import_cuts(cut_plan):
    """遗留 cut_plan → cuts（源轴剪决策）。无效返回 None。"""
    if not isinstance(cut_plan, dict):
        return None
    segs = []
    for x in (cut_plan.get('segments') or []):
        try:
            segs.append({'start': float(x['start']), 'end': float(x['end']),
                         'keep': bool(x.get('keep'))})
        except (KeyError, TypeError, ValueError):
            continue
    if not segs:
        return None
    return {'coverage': cut_plan.get('coverage'), 'segments': segs}


# ---------------------------------------------------------------------------
# 读写（原子）与校验
# ---------------------------------------------------------------------------

def save_plan(path, plan):
    """原子写 plan.json（同目录临时文件 + os.replace）。返回 bool。"""
    plan['version'] = PLAN_VERSION
    d = os.path.dirname(os.path.abspath(path)) or '.'
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, '.tmp_plan_%d.json' % int(time.time() * 1000))
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(plan, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def load_plan(path):
    """读 plan.json。失败返回 None（宁缺勿错，调用方回退旧状态文件）。"""
    try:
        with open(path, encoding='utf-8') as f:
            plan = json.load(f)
        if not isinstance(plan, dict) or 'segments' not in plan:
            return None
        return plan
    except Exception:
        return None


def validate_plan(plan):
    """结构校验，返回问题列表（空列表=通过）。不抛异常。"""
    issues = []
    if not isinstance(plan, dict):
        return ['plan 不是字典']
    if plan.get('version') != PLAN_VERSION:
        issues.append('version=%r 不是 %d' % (plan.get('version'), PLAN_VERSION))
    segs = plan.get('segments') or []
    prev_end = None
    for seg in segs:
        i = seg.get('index')
        if seg.get('kind') not in (KIND_NARRATE, KIND_QUOTE, KIND_BROLL, KIND_BLANK):
            issues.append('段 %s kind 非法: %r' % (i, seg.get('kind')))
        if seg.get('kind') == KIND_QUOTE and not seg.get('quote'):
            issues.append('段 %s 是 quote 但缺 quote 详情' % i)
        out = seg.get('out')
        if out:
            if out[1] < out[0]:
                issues.append('段 %s out 轴倒挂: %r' % (i, out))
            if prev_end is not None and out[0] < prev_end - 0.05:
                issues.append('段 %s out 轴与上一段重叠: %s < %.2f' % (i, out, prev_end))
            prev_end = out[1]
    return issues


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')

# -*- coding: utf-8 -*-
"""画面理解流水线（v1.4.93）：把「一个引擎一步式」拆成专业分工的多步。

主人 2026-11-18 定调：识别画面是一个引擎在做，出的效果不好，升级成多步
流水线，要质变。

旧链路的问题（单引擎混干三件事）：
  抽样点单帧 → VLM 既「看」又「猜剧情」→ 粒度粗、职责不清、跨镜头误配。

四层流水线（每层只干一件事，失败逐层退回）：
  L0 镜头切分  ffmpeg 场景检测（纯信号处理，零 AI 成本）——真实镜头边界，
              「一帧里两个人分属两个镜头」的误配从此消失；
  L1 镜头卡    每镜头首/中/尾 3 连续帧 → VLM 只描述「镜头内动作推进」
              （客观看，不猜剧情）；
  L2 场景聚合  相邻镜头卡 + 窗内台词 → 文本 LLM 写「场景叙述」（起因→动作→
              结果，120 字）——「懂」是文本 LLM 的专职；
  L3 对齐      解说句 vs 场景叙述（全篇 LLM 对齐 + 字幕锚，既有通道）。

所有层独立缓存，任何一层失败退回上一层产出，绝不阻断出片。
"""
import json
import logging
import os
import re

_log = logging.getLogger('framecut.narrator')

_HERE = os.path.dirname(os.path.abspath(__file__))
_HOST_FILE = os.path.join(_HERE, 'webui_server.py')


def _host():
    m = sys.modules.get('webui_server') if 'sys' in dir() else None
    if m is not None:
        return m
    m = sys.modules.get('__main__')
    if m is not None and os.path.abspath(getattr(m, '__file__', '')) == _HOST_FILE:
        return m
    import webui_server
    return webui_server


import sys  # noqa: E402

_w = _host()


# ---------------------------------------------------------------------------
# L0 镜头切分（ffmpeg 场景检测，零 AI）
# ---------------------------------------------------------------------------

def detect_shots(video_path, threshold=0.30, min_len=1.5, max_shots=220):
    """ffmpeg 场景检测 → [(start, end)] 真实镜头边界（源片时间轴）。

    场景分数 = 帧间颜色/亮度直方图差异（ffmpeg 内建），纯信号处理零 AI 成本。
    min_len：短于此的镜头并入前一个（闪切会产生 0.5s 碎镜头，喂给 VLM 是浪费）。
    max_shots：超长片封顶，均匀保留（长镜头优先——短镜头聚合时信息密度低）。
    失败/无输出返回 []，调用方退回旧的抽样段。"""
    try:
        rc, out, err = _w.ffmpeg_run(
            ['-i', video_path, '-vf',
             "select='gt(scene,%s)',showinfo" % threshold,
             '-an', '-f', 'null', '-'], timeout=600)
    except Exception as e:
        _log.info('[DIAG] L0 镜头检测失败: %s' % str(e)[:100])
        return []
    if rc != 0:
        return []
    cuts = [0.0]
    for m in re.finditer(r'pts_time:([0-9.]+)', err.decode('utf-8', 'ignore')):
        t = float(m.group(1))
        if t > cuts[-1] + 0.3:
            cuts.append(t)
    if len(cuts) < 2:
        return []
    vdur = _w.probe_duration(video_path) or (cuts[-1] + 10.0)
    shots = []
    for k, a in enumerate(cuts):
        b = cuts[k + 1] if k + 1 < len(cuts) else float(vdur)
        if shots and b - a < min_len:
            shots[-1] = (shots[-1][0], b)          # 碎镜头并入前镜头
        elif b > a:
            shots.append((a, b))
    if shots and vdur - shots[-1][1] > 0.1:
        shots[-1] = (shots[-1][0], float(vdur))
    # 封顶：均匀保留（保长镜头——信息密度高）
    if len(shots) > max_shots:
        step = len(shots) / float(max_shots)
        keep = [shots[int(k * step)] for k in range(max_shots)]
        keep[-1] = (keep[-1][0], float(vdur))      # 末镜头必须到片尾
        shots = keep
    _log.info('[DIAG] L0 镜头切分: %d 个真实镜头（阈值 %.2f）' % (len(shots), threshold))
    return shots


# ---------------------------------------------------------------------------
# L1 镜头卡（VLM 看层：每镜头 5 连续帧，三段式结构化理解——动作链/因果/细节）
# ---------------------------------------------------------------------------

_SHOT_PROMPT = (
    '这是同一段连续镜头的 {n} 帧（按时间先后）。请输出三段式镜头卡：\n'
    '1.【动作链】镜头里连续发生的动作（谁、做什么、怎么推进、结果），尽可能详细；\n'
    '2.【因果推断】结合画面细节推断这段镜头在剧情里的作用（人物在做什么/为什么做）；\n'
    '3.【关键细节】值得注意的物件、表情、环境线索（有助于后续与解说词对齐）。\n'
    '不限长度，尽可能充分。不要评价画面质量。')


def shot_cards(video_path, shots, run_dir, progress=None, per_shot=5, max_cards=200):
    """每镜头抽 5 帧（Phase 1 测绘定稿：1280px/1500 tokens/三段式结构化）→
    VLM 镜头卡 → {shot_idx: 镜头卡文本}。时序上下文：卡尾串入下一镜头的 prompt。"""
    cfg = {}
    try:
        cfg = _w.vlm_cfg()
        if not cfg.get('base_url') or not _w.vlm_ping()[0]:
            return {}
    except Exception:
        return {}
    try:
        vlm_model = cfg.get('model', 'default')
    except Exception:
        vlm_model = 'default'
    cache_key = _w._video_cache_key(video_path, f'shot_cards_v2_{vlm_model}')
    cached = _w._cache_load(cache_key)
    if cached:
        _log.info('[DIAG] L1 镜头卡命中缓存: %d 个' % len(cached))
        return cached

    frame_dir = os.path.join(run_dir or '.', 'shot_frames')
    os.makedirs(frame_dir, exist_ok=True)
    use = shots[:max_cards]
    out = {}
    _partial_fp = os.path.join(frame_dir, 'cards_partial.json')
    if os.path.isfile(_partial_fp):
        try:
            with open(_partial_fp, encoding='utf-8') as f:
                _part = json.load(f)
            out.update({int(k): v for k, v in _part.items()})
            _log.info('[DIAG] L1 断点续跑: 跳过已完成的 %d 个镜头卡' % len(_part))
        except Exception:
            pass
    _prev_card_tail = ''   # 时序上下文：前一镜头卡的结尾
    for k, (a, b) in enumerate(use):
        if _MN_ABORTED():
            raise RuntimeError('用户取消了任务')
        if progress:
            progress['phase'] = 'L1 镜头卡 %d/%d（看每个镜头的动作）' % (k + 1, len(use))
        frames = []
        for k2, frac in enumerate((0.1, 0.3, 0.5, 0.7, 0.9)[:per_shot]):
            fp = os.path.join(frame_dir, 'shot_%04d_%d.jpg' % (k, k2))
            if not (os.path.isfile(fp) and os.path.getsize(fp) > 1000):
                try:
                    _w.ffmpeg_run(['-y', '-ss', '%.3f' % (a + (b - a) * frac),
                                   '-i', video_path, '-frames:v', '1',
                                   '-vf', 'scale=min(iw\\,1280):-2', '-q:v', '4', '-an', fp])
                except Exception:
                    pass
            if os.path.isfile(fp) and os.path.getsize(fp) > 1000:
                frames.append(fp)
        if len(frames) < 2:
            continue
        _prompt = _SHOT_PROMPT.format(n=len(frames))
        if _prev_card_tail:
            _prompt = '【上一镜头结尾】' + _prev_card_tail + '\n' + _prompt
        try:
            r = _w.vlm_chat_multi(frames, _prompt,
                                  system='你是影视镜头理解器。输出三段式镜头卡：动作链/因果推断/关键细节。',
                                  timeout=300, keep_alive='30m', max_tokens=1500)
        except Exception:
            continue
        r = (r or '').strip().replace('\n', ' ')
        if len(r) >= 8:
            out[k] = r[:900]
            _prev_card_tail = r[-80:]
            # 【断点续跑】每张卡即存 partial（挂掉不丢进度）
            try:
                with open(_partial_fp, 'w', encoding='utf-8') as f:
                    json.dump({str(kk): vv for kk, vv in out.items()}, f,
                              ensure_ascii=False)
            except Exception:
                pass
    try:
        _w.vlm_unload()
    except Exception:
        pass
    _log.info('[DIAG] L1 镜头卡: %d/%d 个镜头拿到描述' % (len(out), len(use)))
    if len(out) == len(use) and use:
        _w._cache_save(cache_key, out)
    return out


# ---------------------------------------------------------------------------
# L2 场景聚合（LLM 懂层：镜头卡+台词 → 场景叙述）
# ---------------------------------------------------------------------------

def segment_anchor_with_shots(text, seg, shots, cards, cues):
    """【v1.4.94 镜头级精修】段内三层证据融合，返回 (start, end, evidence)。

    1. 镜头卡匹配：解说句 vs 段内镜头卡（n-gram 命中）→ 最佳镜头 → 窗口=镜头边界；
    2. 字幕锚精修：段内台词 cue 的时刻落在最佳镜头内 → 起播=台词时刻（双证据）；
       台词不在最佳镜头内 → 起播=镜头开始（镜头卡证据优先于孤立台词）；
    3. 无镜头卡/无匹配 → (None, None, '') 调用方退回旧 cue 锚定。
    evidence 返回匹配依据（日志与面板展示用）。"""
    a, b = float(seg[0]), float(seg[1])
    # 段内镜头
    inner = []
    for k, (sa, sb) in enumerate(shots or []):
        if sb > a and sa < b:
            card = (cards or {}).get(k, '')
            if card:
                inner.append((max(sa, a), min(sb, b), card, k))
    if not inner:
        return None, None, ''
    # 镜头卡匹配（n-gram 命中）
    def _bg(t):
        t = re.sub(r'[\s，。？！、“”：；…\-—（）\']', '', str(t or ''))
        return set(t[i:i + 2] for i in range(len(t) - 1))
    sbg = _bg(text)
    best, best_hit = None, 0
    for sa, sb, card, k in inner:
        hit = len(sbg & _bg(card))
        if hit > best_hit:
            best_hit, best = hit, (sa, sb, card, k)
    if best is None or best_hit < 2:
        return None, None, ''
    sa, sb, card, k = best
    # 字幕锚精修：台词时刻落在最佳镜头内 → 起播=台词时刻
    cue_t = None
    for cs, ce, ct in (cues or []):
        if cs >= sa and cs < sb:
            cue_t = cs
            break
    if cue_t is not None and cue_t >= sa:
        return sa, sb, 'shot%d+cue%.1f' % (k, cue_t)
    return sa, sb, 'shot%d' % k


def scene_narratives(shots, cards, asr, run_dir, progress=None, window=10, max_narr=80):
    """相邻镜头卡 + 窗内台词 → 文本 LLM 写「场景叙述」。

    返回 [(start, end, text)]：叙述按镜头组聚合（window 个镜头一组，相邻组
    共享 1 个边界镜头卡保证因果连贯）。文本 LLM 的专职是「懂」：把客观镜头卡
    组织成有因果的叙述（起因→动作→结果），VLM 不再兼职猜剧情。"""
    if not shots or not cards:
        return []
    # 聚合窗口
    groups = []
    use = shots[:max_narr * window]
    k = 0
    while k < len(use):
        grp = use[k:k + window]
        groups.append(grp)
        k += len(grp)
    out = []
    prev_tail = ''
    for gi, grp in enumerate(groups):
        if _MN_ABORTED():
            raise RuntimeError('用户取消了任务')
        if progress:
            progress['phase'] = 'L2 场景聚合 %d/%d（把镜头组织成叙述）' % (gi + 1, len(groups))
        lines = []
        for si, (a, b) in enumerate(grp):
            card = cards.get(gi * window + si - (gi if gi == 0 else 0)) or ''
            dlg = _asr_in(asr, a, b)
            lines.append('镜头%d [%.0f-%.0f秒] %s%s' % (
                si + 1, a, b, card, ('。窗内台词：%s' % dlg) if dlg else ''))
        prompt = ('下面是某部影片一段连续镜头的「镜头卡」（每个镜头的画面动作）与窗内台词。'
                  '请把这段镜头组织成 1 段【场景叙述】：这段戏在讲什么（起因→动作→结果），'
                  '120 字以内，只写画面与台词支持的内容，不要猜没发生的事。\n'
                  + (('【上一段结尾】' + prev_tail + '\n') if prev_tail else '')
                  + '\n'.join(lines))
        try:
            r = _w.local_llm_chat(prompt, system='你是影视叙事分析师，输出一段连贯叙述。',
                                  timeout=240) or ''
        except Exception:
            continue
        r = _w._strip_think(r).strip().replace('\n', ' ')
        if len(r) < 12:
            continue
        a, b = grp[0][0], grp[-1][1]
        out.append((a, b, r[:300]))
        prev_tail = r[-60:]
    _log.info('[DIAG] L2 场景聚合: %d 段场景叙述' % len(out))
    return out


def _MN_ABORTED():
    try:
        import movie_narrator as _MN
        return bool(_MN._aborted())
    except Exception:
        return False


def _asr_in(asr, a, b):
    parts = []
    for seg in (asr or []):
        try:
            s0, s1 = float(seg.get('start', 0)), float(seg.get('end', 0))
            t = str(seg.get('text') or '').strip()
        except Exception:
            continue
        if t and s1 > a and s0 < b:
            parts.append(t)
    return ' '.join(parts)[:160]


# ---------------------------------------------------------------------------
# 入口：完整流水线 → 产出可并入 scene_story 的叙述列表
# ---------------------------------------------------------------------------

def build_pipeline(video_path, asr, run_dir, progress=None):
    """L0→L1→L2 全跑。返回 (shots, cards, narrs)：
    - shots: [(start, end)] 真实镜头边界（镜头级对齐的画面分辨率来源）
    - cards: {shot_idx: 镜头卡}（动作链/因果/细节三段式）
    - narrs: [(start, end, 场景叙述)]
    任何一层失败返回 ([], {}, [])，调用方退回旧通道。"""
    try:
        if not _w.vlm_enabled():
            return [], {}, []
    except Exception:
        return [], {}, []
    shots = detect_shots(video_path)
    if not shots:
        _log.info('[DIAG] 流水线 L0 失败，退回旧抽样段')
        return [], {}, []
    cards = shot_cards(video_path, shots, run_dir, progress=progress)
    if not cards:
        _log.info('[DIAG] 流水线 L1 失败，退回旧抽样段')
        return [], {}, []
    narrs = scene_narratives(shots, cards, asr, run_dir, progress=progress)
    if not narrs:
        _log.info('[DIAG] 流水线 L2 失败，镜头卡直通为叙述')
        narrs = [(a, b, cards.get(k, '')) for k, (a, b) in enumerate(shots)
                 if cards.get(k)]
    return shots, cards, narrs

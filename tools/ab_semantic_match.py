# -*- coding: utf-8 -*-
"""画面分配 A/B：旧打分（台词密度+台词重叠） vs 加语义精修（场景内容匹配）。

用法（在项目根目录）： python tools/ab_semantic_match.py <run_dir>
纯离线：只读 run 目录的 tts_state.json / scene_descs.json，不碰模型、不起服务。

指标（语义匹配必须赢才值得上默认）：
  ① 语义重合度：每句解说与其选中画面所覆盖场景文本（summary/event/keywords/story）
     的 bigram 重合度均值 —— 「解说说到什么、画面演什么」的直接代理
  ② 台词密度保留率：选中画面的台词字符密度均值相对旧版的比值（不能为语义牺牲
     台词主信号太多）
  ③ 结构：span 数、单调性（不回跳）、越界（不超过片长）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import webui_server  # noqa: F401  解除循环依赖
import movie_narrator as mn


def _bigrams(text):
    text = str(text or '')
    return set(text[i:i + 2] for i in range(len(text) - 1) if text[i:i + 2].strip())


def _scene_text_at(scenes, story, t):
    best = (0.0, '')
    qbg = None
    return best  # 占位（真实计算在下方 metrics 里逐句做）


def main(run_dir):
    state = json.load(open(os.path.join(run_dir, 'tts_state.json'), encoding='utf-8'))
    texts = state.get('narr') or []
    segs_state = [tuple(s) for s in state.get('segs', [])]
    vdur = max((e for _s, e in segs_state), default=0.0)
    asr = state.get('asr')
    sa = {int(k): v for k, v in (state.get('scene_alignment') or {}).items()}
    story = state.get('scene_story') or {}
    scenes = json.load(open(os.path.join(run_dir, 'scene_descs.json'), encoding='utf-8'))
    if isinstance(scenes, dict):
        scenes = scenes.get('scenes') or scenes.get('items') or []

    # 场景文本（与 _allocate_script_spans 内部同构）
    scene_texts = []
    for si, sc in enumerate(scenes):
        try:
            st, en = float(sc.get('start', 0) or 0), float(sc.get('end', 0) or 0)
        except Exception:
            continue
        if en <= st:
            continue
        sto = story.get(si) or story.get(str(si)) or {}
        parts = [str(sc.get('summary') or ''), str(sc.get('event') or '')]
        kws = sto.get('keywords') or []
        if kws:
            parts.append(''.join(str(k) for k in kws))
        if sto.get('story'):
            parts.append(str(sto['story']))
        scene_texts.append((st, en, ''.join(parts)))
    scene_texts.sort(key=lambda x: x[0])

    def sem_overlap(span):
        """span 中点覆盖场景与所属解说句的 bigram 重合度。"""
        i = nm_at(span)
        if i is None or i >= len(texts):
            return 0.0
        mid = (span[0] + span[1]) / 2.0
        qbg = _bigrams(texts[i])
        if not qbg:
            return 0.0
        best = 0.0
        for st, en, stx in scene_texts:
            if en <= mid:
                continue
            if st >= span[1]:
                break
            sbg = _bigrams(stx)
            if sbg and (qbg & sbg):
                best = max(best, len(qbg & sbg) / max(1, len(qbg)))
        return best

    nm_map = {}
    def nm_at(span):
        return nm_map.get(span)

    def density_of(span):
        tot = 0.0
        for x in (asr or []):
            try:
                s0, s1 = float(x['start']), float(x['end'])
            except Exception:
                continue
            if s1 <= span[0] or s0 >= span[1]:
                continue
            tot += len(str(x.get('text') or '')) * (min(s1, span[1]) - max(s0, span[0])) / max(1e-6, s1 - s0)
        return tot / max(0.1, span[1] - span[0])

    variants = {
        '旧版(无语义)': dict(scene_alignment=sa, scenes=scenes),
        '语义精修': dict(scene_alignment=sa, scenes=scenes, scene_story=story),
    }
    results = {}
    for tag, kw in variants.items():
        spans, nm = mn._allocate_script_spans(texts, vdur, asr=asr, **kw)
        for k, sp in enumerate(spans):
            nm_map[sp] = nm[k] if k < len(nm) else None
        sems = [sem_overlap(sp) for sp in spans]
        dens = [density_of(sp) for sp in spans]
        mono_viol = sum(1 for a, b in zip(spans, spans[1:]) if b[0] < a[1] - 0.01)
        oob = sum(1 for s0, s1 in spans if s0 < -0.01 or s1 > vdur + 0.5)
        results[tag] = {
            'spans': spans, 'nm': nm,
            'sem_mean': sum(sems) / max(1, len(sems)),
            'sem_good': sum(1 for v in sems if v >= 0.15),
            'dens_mean': sum(dens) / max(1, len(dens)),
            'spans': len(spans), 'mono_viol': mono_viol, 'oob': oob,
        }
        nm_map.clear()

    base, new = results['旧版(无语义)'], results['语义精修']
    print('=' * 64)
    print('A/B 画面分配（%d 句解说 / %.0fs 片长 / %d 场景）' % (len(texts), vdur, len(scene_texts)))
    print('-' * 64)
    print('%-14s %10s %10s' % ('指标', '旧版', '语义精修'))
    print('%-14s %10.3f %10.3f   ← 核心指标（越高=画面越贴解说）' % ('语义重合度', base['sem_mean'], new['sem_mean']))
    print('%-14s %10d %10d   （重合度≥0.15 的句数）' % ('贴合句数', base['sem_good'], new['sem_good']))
    print('%-14s %10.1f %10.1f   （台词字符密度，保留率 %.0f%%）' % ('台词密度', base['dens_mean'], new['dens_mean'],
          new['dens_mean'] / max(0.1, base['dens_mean']) * 100))
    print('%-14s %10d %10d' % ('span数', base['spans'], new['spans']))
    print('%-14s %10d %10d   （必须为 0）' % ('单调回跳', base['mono_viol'], new['mono_viol']))
    print('%-14s %10d %10d   （必须为 0）' % ('越界', base['oob'], new['oob']))
    print('=' * 64)
    improve = new['sem_mean'] - base['sem_mean']
    safe = new['mono_viol'] == 0 and new['oob'] == 0 and new['dens_mean'] >= base['dens_mean'] * 0.8
    print('结论：语义重合度 %+.1f%%，结构%s，台词密度保留 %.0f%% → %s' % (
        improve / max(0.001, base['sem_mean']) * 100,
        '安全' if safe else '有风险',
        new['dens_mean'] / max(0.1, base['dens_mean']) * 100,
        '✅ 建议启用' if (improve > 0 and safe) else '⚠️ 不建议默认启用'))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: python tools/ab_semantic_match.py <run_dir>')
        sys.exit(1)
    main(sys.argv[1])

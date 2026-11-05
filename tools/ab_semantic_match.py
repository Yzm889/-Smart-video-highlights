# -*- coding: utf-8 -*-
"""画面分配 A/B：基线(无语义) vs bigram 精修 vs 向量精修（余弦相似度）。

用法（在项目根目录）：
  python tools/ab_semantic_match.py <run_dir>              # 纯离线：基线 + bigram 两档
  python tools/ab_semantic_match.py <run_dir> --vector     # 追加向量档（需本机 Ollama + nomic-embed-text）
  可选 --weight 1.2                                        # 语义项权重（默认 1.2，等价 sem_weight）

只读 run 目录的 tts_state.json / scene_descs.json，不碰剪辑链、不起服务；
--vector 只连本机 Ollama 的 /api/embed（模型 274MB，机器上已拉好）。

指标（语义匹配必须赢才值得上默认）：
  ① 语义重合度——两把尺对所有档位统一测量，跨档位同尺对比才公平：
     · bigram 覆盖度：解说句与选中画面所覆盖场景文本的字符 bigram 覆盖率
     · 余弦相似度：解说句向量 vs 覆盖场景向量的最大余弦（解说句=search_query、
       场景=search_document，nomic-embed 官方检索前缀）
  ② 台词密度保留率：选中画面的台词字符密度均值 / 基线（≥80% 才算安全）
  ③ 结构：span 数、单调性（不回跳）、越界（不超过片长）——必须为 0
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import webui_server  # noqa: F401  解除循环依赖
import movie_narrator as mn


def _bigrams(text):
    text = str(text or '')
    return set(text[i:i + 2] for i in range(len(text) - 1) if text[i:i + 2].strip())


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def main(run_dir):
    weight = 1.2
    if '--weight' in sys.argv:
        weight = float(sys.argv[sys.argv.index('--weight') + 1])
    use_vec = '--vector' in sys.argv

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

    # 场景文本与句/场景 bigram（所有档位共用的「尺」，与 _allocate_script_spans 内部同构）
    scene_texts = mn._compose_scene_texts(scenes, story)
    qbg_list = [_bigrams(t) for t in texts]
    sbg_list = [_bigrams(t) for _a, _b, t in scene_texts]

    variants = {
        '基线(无语义)': dict(scene_alignment=sa, scenes=scenes, sem_weight=0.0),
        'bigram精修': dict(scene_alignment=sa, scenes=scenes, scene_story=story, sem_weight=weight),
    }

    # 向量素材（解说句=查询侧，场景=文档侧）
    vec_pack = None
    if use_vec:
        import ai_providers as ap
        _t0 = time.time()
        sv = ap.ollama_embed([t for _a, _b, t in scene_texts], prefix='search_document: ')
        tv = ap.ollama_embed(texts, prefix='search_query: ')
        _dt = time.time() - _t0
        if sv and tv and len(sv) == len(scene_texts) and len(tv) == len(texts):
            vec_pack = (scene_texts, sv, tv)
            variants['向量精修'] = dict(scene_alignment=sa, scenes=scenes, scene_story=story,
                                      sem_weight=weight, scene_sem=vec_pack)
            print('[向量] %d 场景 + %d 句 → %.1fs（%s）' % (len(sv), len(tv), _dt, ap.embed_cfg()['model']))
        else:
            print('[向量] 不可用（Ollama 不在/超时/模型缺）→ 只测基线与 bigram')
    else:
        print('[向量] 未加 --vector，跳过向量档')

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

    results = {}
    for tag, kw in variants.items():
        spans, nm = mn._allocate_script_spans(texts, vdur, asr=asr, **kw)
        bg_vals, cos_vals, dens_vals = [], [], []
        for k, sp in enumerate(spans):
            i = nm[k] if k < len(nm) else None
            dens_vals.append(density_of(sp))
            if i is None or i >= len(qbg_list):
                bg_vals.append(0.0)
                cos_vals.append(0.0)
                continue
            bg_best = 0.0
            for j, (st, en, _tx) in enumerate(scene_texts):
                if en <= sp[0]:
                    continue
                if st >= sp[1]:
                    break
                ov = len(qbg_list[i] & sbg_list[j])
                if ov:
                    bg_best = max(bg_best, ov / max(1, len(qbg_list[i])))
            bg_vals.append(bg_best)
            c_best = 0.0
            if vec_pack:
                qv = vec_pack[2][i]
                for j, (st, en, _tx) in enumerate(scene_texts):
                    if en <= sp[0]:
                        continue
                    if st >= sp[1]:
                        break
                    c = _cos(qv, vec_pack[1][j])
                    if c > c_best:
                        c_best = c
            cos_vals.append(c_best)
        mono_viol = sum(1 for a, b in zip(spans, spans[1:]) if b[0] < a[1] - 0.01)
        oob = sum(1 for s0, s1 in spans if s0 < -0.01 or s1 > vdur + 0.5)
        results[tag] = {
            'bg_mean': sum(bg_vals) / max(1, len(bg_vals)),
            'bg_good': sum(1 for v in bg_vals if v >= 0.15),
            'cos_mean': (sum(cos_vals) / max(1, len(cos_vals))) if vec_pack else None,
            'dens_mean': sum(dens_vals) / max(1, len(dens_vals)),
            'n': len(spans), 'mono_viol': mono_viol, 'oob': oob,
        }

    tags = list(results)
    base = results['基线(无语义)']
    line = '=' * (16 + 12 * len(tags))
    print(line)
    print('A/B 画面分配（%d 句解说 / %.0fs 片长 / %d 场景 / 权重 %.1f）'
          % (len(texts), vdur, len(scene_texts), weight))
    print('-' * (16 + 12 * len(tags)))
    print(' '.join(['%-14s' % '指标'] + ['%10s' % t for t in tags]))

    def row(label, key, fmt, none='—'):
        cells = ['%-14s' % label]
        for t in tags:
            v = results[t][key]
            cells.append('%10s' % (none if v is None else fmt % v))
        print(' '.join(cells))

    row('bigram覆盖度', 'bg_mean', '%.3f')
    row('贴合句数', 'bg_good', '%d')
    row('余弦相似度', 'cos_mean', '%.3f')
    row('台词密度', 'dens_mean', '%.1f')
    row('span数', 'n', '%d')
    row('单调回跳', 'mono_viol', '%d')
    row('越界', 'oob', '%d')
    print(line)

    def dens_ok(tag):
        return results[tag]['dens_mean'] >= base['dens_mean'] * 0.8

    def struct_ok(tag):
        return results[tag]['mono_viol'] == 0 and results[tag]['oob'] == 0

    if '向量精修' in results:
        vec, big = results['向量精修'], results['bigram精修']
        ref_cos = max(x for x in (base['cos_mean'], big['cos_mean']) if x is not None)
        gain = (vec['cos_mean'] - ref_cos) / max(0.01, ref_cos) * 100
        bg_keep = vec['bg_mean'] / max(0.001, base['bg_mean']) * 100
        win = (gain >= 10.0 and struct_ok('向量精修') and dens_ok('向量精修')
               and vec['bg_mean'] >= base['bg_mean'] * 0.95)
        print('结论：向量余弦比最强参照 %+.1f%%，bigram 尺保留 %.0f%%，结构%s，台词密度保留 %.0f%% → %s' % (
            gain, bg_keep, '安全' if struct_ok('向量精修') else '有风险',
            vec['dens_mean'] / max(0.1, base['dens_mean']) * 100,
            '✅ 建议启用（把 _SEM_WEIGHT 调开）' if win else '⚠️ 不建议默认启用'))
    else:
        big = results['bigram精修']
        improve = big['bg_mean'] - base['bg_mean']
        print('结论：bigram 语义重合度 %+.1f%%，结构%s，台词密度保留 %.0f%% → %s' % (
            improve / max(0.001, base['bg_mean']) * 100,
            '安全' if struct_ok('bigram精修') else '有风险',
            big['dens_mean'] / max(0.1, base['dens_mean']) * 100,
            '✅ 建议启用' if (improve > 0 and struct_ok('bigram精修') and dens_ok('bigram精修'))
            else '⚠️ 不建议默认启用'))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: python tools/ab_semantic_match.py <run_dir> [--vector] [--weight 1.2]')
        sys.exit(1)
    main(sys.argv[1])

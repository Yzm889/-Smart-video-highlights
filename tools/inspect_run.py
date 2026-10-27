#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""体检最近一次分析结果：把"台词准不准 / 剧情懂没懂 / 剪得对不对"一次说清。

用法：
    python tools/inspect_run.py            # 最近一次 run
    python tools/inspect_run.py run-1-xxx  # 指定 run
    python tools/inspect_run.py --list     # 列出所有 run

为什么需要它：跑完一次真实视频后，最该看的不是"成片好不好看"，而是三件事：
  ① 台词从哪来、有没有识别上（字幕轨 / 外挂字幕 / 语音识别）、覆盖率多少；
  ② AI 理解的剧情对不对（不对 → 后面全是乱剪，必须先解决这个）；
  ③ 草稿覆盖率与主线段选择是否合理。
本脚本直接读 analyze_result.json，把这三件事量化打印出来，不用截图、不用肉眼比对。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(ROOT, 'webui_output')


def _runs():
    if not os.path.isdir(OUTDIR):
        return []
    out = []
    for name in sorted(os.listdir(OUTDIR)):
        p = os.path.join(OUTDIR, name, 'analyze_result.json')
        if os.path.isfile(p):
            out.append((name, p, os.path.getmtime(p)))
    out.sort(key=lambda x: x[2])
    return out


def _fmt_hms(sec):
    try:
        sec = float(sec or 0)
    except Exception:
        return '?'
    return '%d:%02d' % (int(sec // 60), int(sec % 60))


def inspect(path, name):
    with open(path, encoding='utf-8') as f:
        r = json.load(f)
    segs = r.get('segments') or []
    warns = r.get('warnings') or []
    src = '未知'
    for w in warns:
        if '台词来源' in w:
            src = w.replace('台词来源：', '').strip()
    print('=' * 66)
    print(' 体检报告：%s' % name)
    print('=' * 66)
    v = (r.get('video') or {})
    print('\n【视频】%s | 时长 %s' % (v.get('path') or '(未记录)', _fmt_hms(v.get('duration'))))
    print('【分段】%d 段' % len(segs))

    # ① 台词
    with_txt = [s for s in segs if (s.get('asr_text') or '').strip()]
    print('\n① 台词识别')
    print('   来源：%s' % src)
    print('   有台词的段：%d/%d (%.0f%%)' % (len(with_txt), len(segs),
                                        (100.0 * len(with_txt) / len(segs)) if segs else 0))
    for s in with_txt[:3]:
        print('     段%-3d %6.1f-%6.1fs  %s' % (s.get('segment_id', 0), s.get('start', 0),
                                                s.get('end', 0), (s.get('asr_text') or '')[:40]))
    if not with_txt:
        print('   ⚠️ 一段台词都没有 → 剧情理解与写稿必然失准。')
        print('      排查：① 视频有没有字幕轨/同目录外挂字幕；② whisper.language 是否设对；')
        print('            ③ 若是硬字幕，等「画面字幕」读取生效（需 VLM 可用）')

    # ② 剧情理解
    ns = r.get('narrative_suggestion') or {}
    main = ns.get('main_line') or []
    cuts = ns.get('suggested_cuts') or []
    print('\n② 剧情理解 / 主线判断')
    print('   主线段 %d 个 | 建议剪除 %d 个 | 保留率 %.0f%%'
          % (len(main), len(cuts), (100.0 * len(main) / len(segs)) if segs else 0))
    if ns.get('reason'):
        print('   理由：%s' % str(ns['reason'])[:120])
    if not main and not cuts:
        print('   ⚠️ 没有主线建议 → 剪辑可能退化为「均匀分段」，即用户说的"乱剪"')

    # ③ 草稿
    with_d = [s for s in segs if s.get('ai_draft_narrations')]
    print('\n③ 解说词草稿')
    print('   有草稿的段：%d/%d (%.0f%%)' % (len(with_d), len(segs),
                                        (100.0 * len(with_d) / len(segs)) if segs else 0))
    if with_d:
        s = with_d[0]
        print('   样例（段%d）：' % s.get('segment_id', 0))
        for d in (s.get('ai_draft_narrations') or [])[:3]:
            print('     【%s】%s' % (d.get('style'), (d.get('text') or '')[:44]))
    else:
        print('   ⚠️ 没有草稿 → 看下面告警里的模型/解析原因')

    # 告警
    if warns:
        print('\n④ 告警（这些就是问题本身）')
        for w in warns:
            print('   · %s' % str(w)[:130])
    print('\n' + '=' * 66)


def main():
    ap = argparse.ArgumentParser(description='体检最近一次分析结果')
    ap.add_argument('run', nargs='?', help='run 目录名（默认最近一次）')
    ap.add_argument('--list', action='store_true', help='列出所有 run')
    a = ap.parse_args()
    runs = _runs()
    if a.list:
        for name, _p, t in runs:
            print('  %-34s %s' % (name, __import__('time').strftime('%m-%d %H:%M', __import__('time').localtime(t))))
        if not runs:
            print('  （没有分析结果）')
        return 0
    if not runs:
        print('还没有分析结果。先在网页上跑一次「AI 分析」。')
        return 1
    target = None
    if a.run:
        for name, p, _t in runs:
            if name == a.run or name.startswith(a.run):
                target = (name, p)
                break
        if not target:
            print('找不到 run：%s' % a.run)
            return 1
    else:
        target = (runs[-1][0], runs[-1][1])
    inspect(target[1], target[0])
    return 0


if __name__ == '__main__':
    sys.exit(main())

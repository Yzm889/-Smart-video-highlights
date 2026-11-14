# -*- coding: utf-8 -*-
"""逐句对比两份 align_report.json —— BASELINE.md 第 6 节的验收工具。

只读、零第三方依赖、不改任何产品代码。

用法：
    python tools/compare_align.py 旧报告.json 新报告.json

为什么要按「解说文本」对齐而不是按序号：
    基线是 27 句、v1.4.9x 报告是 31 句，按 i 对齐会整体串位。
为什么兼容两种格式：
    旧版落盘是纯数组；新版是 {'summary': {...}, 'report': [...]}。
"""
import argparse
import difflib
import json
import re
import sys

LEVEL = {'match': 2, 'related': 1, 'wrong': 0}
LABEL = {'match': 'match  ', 'related': 'related', 'wrong': 'wrong  '}


def load_report(path):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        return (data.get('report') or []), data.get('summary')
    raise ValueError('认不出的报告格式：%s' % path)


def norm(text):
    """归一化解说文本用于配对：只保留汉字、字母、数字，丢掉全部标点与空白。"""
    return re.sub(r'[^\w\u4e00-\u9fff]', '', (text or '')).lower()
def tally(rows):
    out = {'match': 0, 'related': 0, 'wrong': 0, 'other': 0}
    for r in rows:
        v = (r.get('verdict') or '').lower()
        out[v if v in out else 'other'] += 1
    return out


def brief(text, n=34):
    s = re.sub(r'\s+', ' ', (text or '')).strip()
    return s[:n] + ('…' if len(s) > n else '')


def compare(old_path, new_path, show_all=False):
    old, old_sum = load_report(old_path)
    new, new_sum = load_report(new_path)
    to, tn = tally(old), tally(new)
    no, nn = len(old), len(new)

    print('=' * 74)
    print('基准  %s' % old_path)
    print('        %d 句  match %d / related %d / wrong %d  →  match 率 %.1f%%'
          % (no, to['match'], to['related'], to['wrong'],
             100.0 * to['match'] / no if no else 0))
    if old_sum:
        print('        (报告自带 summary: %s)' % old_sum)
    print('本轮  %s' % new_path)
    print('        %d 句  match %d / related %d / wrong %d  →  match 率 %.1f%%'
          % (nn, tn['match'], tn['related'], tn['wrong'],
             100.0 * tn['match'] / nn if nn else 0))
    if new_sum:
        print('        (报告自带 summary: %s)' % new_sum)
    if no != nn:
        print('⚠ 分母不同（%d vs %d）：百分比不可直接相减，以下按解说文本逐句配对。' % (no, nn))
    print('=' * 74)

    a = [norm(r.get('narr')) for r in old]
    b = [norm(r.get('narr')) for r in new]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    pairs = []
    for i, j, size in sm.get_matching_blocks():
        for k in range(size):
            pairs.append((i + k, j + k))
    matched_old = {i for i, _ in pairs}
    matched_new = {j for _, j in pairs}

    better, worse, same = [], [], []
    for i, j in pairs:
        vo = (old[i].get('verdict') or '').lower()
        vn = (new[j].get('verdict') or '').lower()
        d = LEVEL.get(vn, 1) - LEVEL.get(vo, 1)
        rec = (i, j, vo, vn, brief(old[i].get('narr')), brief(new[j].get('why')))
        (better if d > 0 else worse if d < 0 else same).append(rec)

    def dump(title, rows, limit=None):
        print('\n%s  %d 句' % (title, len(rows)))
        if limit == 0:
            print('  （%d 句，加 --all 展开）' % len(rows))
            return
        show = rows if limit is None else rows[:limit]
        for i, j, vo, vn, narr, why in show:
            print('  #%02d→#%02d  %s → %s  %s' % (i, j, LABEL.get(vo, vo.ljust(7)),
                                                   LABEL.get(vn, vn.ljust(7)), narr))
            if why:
                print('        理由：%s' % why)
        if limit and len(rows) > limit:
            print('  … 另有 %d 句（加 --all 看全量）' % (len(rows) - limit))

    dump('✅ 变好', better)
    dump('❌ 变坏', worse)
    dump('➖ 不变', same, limit=None if show_all else 0)
    only_old = [i for i in range(no) if i not in matched_old]
    only_new = [j for j in range(nn) if j not in matched_new]
    if only_old:
        print('\n仅基准有（%d 句）：%s' % (len(only_old),
              '；'.join(brief(old[i].get('narr'), 20) for i in only_old[:6])))
    if only_new:
        print('仅本轮有（%d 句）：%s' % (len(only_new),
              '；'.join(brief(new[j].get('narr'), 20) for j in only_new[:6])))

    print('\n汇总：变好 %d / 变坏 %d / 不变 %d ｜ 仅基准 %d / 仅本轮 %d'
          % (len(better), len(worse), len(same), len(only_old), len(only_new)))
    print('注意：本工具只比 verdict，不判断判官是否被改过——'
          '若本轮动过判官，按 BASELINE.md 第 4 节，本轮验收无效。')
    return 0


def main():
    ap = argparse.ArgumentParser(description='逐句对比两份 align_report.json')
    ap.add_argument('old', help='基准报告路径')
    ap.add_argument('new', help='本轮报告路径')
    ap.add_argument('--all', action='store_true', help='连「不变」的句子也全部打印')
    a = ap.parse_args()
    try:
        return compare(a.old, a.new, show_all=a.all)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print('读取失败：%s' % e, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

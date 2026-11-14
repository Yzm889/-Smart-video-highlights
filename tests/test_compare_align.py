# -*- coding: utf-8 -*-
"""源码钉：BASELINE.md 的逐句对比工具必须能处理新旧两种报告格式与不同分母。

防的是「验收依据本身悄悄坏掉」——基线是纯数组 27 句，新版是
{'summary','report'} 对象且句数不同；按序号硬对齐会整体串位。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, 'tools', 'compare_align.py')
BASE = os.path.join(ROOT, 'webui_output', 'run-1-20261114-065419', 'align_report.json')


def _run(old, new, *extra):
    r = subprocess.run([sys.executable, TOOL, str(old), str(new), *extra],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_self_compare_has_no_fake_changes():
    """同一份报告自比：27 句全不变，不许凭空报「变好」。"""
    if not os.path.exists(BASE):
        return  # 基线成片被清理时跳过，不阻塞 CI
    out = _run(BASE, BASE)
    assert '变好 0 / 变坏 0 / 不变 27' in out


def test_new_object_format_and_different_denominator(tmp_path):
    """新版对象格式 + 分母不同 + 标点扰动：仍能按解说文本正确配对出升降。"""
    rows = [
        {'i': 0, 'narr': '第一句，解说。', 'verdict': 'wrong', 'why': ''},
        {'i': 1, 'narr': '第二句解说', 'verdict': 'related', 'why': ''},
        {'i': 2, 'narr': '第三句解说', 'verdict': 'match', 'why': ''},
    ]
    old = tmp_path / 'old.json'
    old.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    new_rows = [
        {'i': 0, 'narr': '第一句,解说.', 'verdict': 'match', 'why': '画面确实在演'},  # 变好
        {'i': 1, 'narr': '第二句解说', 'verdict': 'wrong', 'why': ''},                # 变坏
        {'i': 2, 'narr': '第三句解说', 'verdict': 'match', 'why': ''},                # 不变
        {'i': 3, 'narr': '新增的一句', 'verdict': 'related', 'why': ''},
    ]
    new = tmp_path / 'new.json'
    new.write_text(json.dumps({'summary': {'match': 2, 'related': 2, 'wrong': 0},
                               'report': new_rows}, ensure_ascii=False), encoding='utf-8')
    out = _run(old, new)
    assert '分母不同（3 vs 4）' in out, '分母不同必须告警，不许默默比百分比'
    assert '✅ 变好  1 句' in out and '❌ 变坏  1 句' in out
    assert '变好 1 / 变坏 1 / 不变 1' in out
    assert '仅本轮 1' in out
    assert '第一句' in out, '标点扰动后仍要按文本配对上，不能掉进「仅基准有」'

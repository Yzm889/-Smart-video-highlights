"""端到端冒烟：人机协同解说「取消勾选 = 真剪掉画面」。

历史 bug：解说链路只烧字幕 + 混音，成片时长恒等于原片，
用户在预览里取消勾选的段落画面照样留在成片里。

本用例走完整链路：/api/plan 分析 → /api/confirm 提交「只保留第一段」的编辑 →
断言成片时长明显短于原片、diag.cut 报告了被剪掉的秒数。
需要本机服务已启动（默认 http://127.0.0.1:8765），未启动则 skip。
"""

import base64
import json
import os
import time
import urllib.request

import pytest

BASE = os.environ.get('SMOKE_BASE', 'http://127.0.0.1:8765')
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
# 用 10 秒样片并把 maxSeg 调小，才能切出多段（素材库里的 5 秒小样只够 1 段，验证不了剪切）
VIDEO = os.path.join(ROOT, 'spring10s.mp4')
MAX_SEG = 3


def _post(path, payload, timeout=120):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _get(path, timeout=15):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


@pytest.mark.skipif(os.environ.get('SKIP_SMOKE') == '1', reason='smoke disabled')
def test_confirm_plan_really_cuts_video():
    if not os.path.exists(VIDEO):
        pytest.skip('素材缺失：' + VIDEO)
    try:
        with urllib.request.urlopen(BASE + '/', timeout=3) as r:
            r.read(64)
    except Exception as e:
        pytest.skip('服务未启动：%s' % e)

    with open(VIDEO, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()

    out = _post('/api/plan', {
        'type': 'narrate',
        'video': {'name': os.path.basename(VIDEO), 'data': b64},
        'params': {'maxSeg': MAX_SEG, 'w': 1280, 'h': 720, 'fps': 30, 'autoCut': True},
    })
    assert out.get('ok'), out

    plan, deadline = None, time.time() + 900
    while time.time() < deadline:
        p = _get('/api/progress?run=' + out['runid'])
        if p.get('plan_ready'):
            plan = p['plan']
            break
        if p.get('done'):
            pytest.fail('分析失败：%s' % p.get('error'))
        time.sleep(1)
    assert plan and plan['segs'], '未产出方案'

    segs = plan['segs']
    # 不依赖分段算法切出几段（短样片常只分出 1 段）：直接把整片等分成 4 段提交，
    # 只保留首尾两段 → 成片应当只剩约一半时长。
    src_dur = max(float(s['end']) for s in segs)
    if src_dur < 2:
        pytest.skip('素材过短，无法验证剪切')
    n = 4
    step = src_dur / n
    edits = {'segs': [{'start': round(i * step, 3), 'end': round((i + 1) * step, 3),
                       'caption': '第%d段解说' % (i + 1), 'on': (i in (0, n - 1))}
                      for i in range(n)]}
    conf = _post('/api/confirm', {'runid': out['runid'], 'edits': edits,
                                  'params': {'autoCut': True}})
    assert conf.get('ok'), conf

    last, deadline = {}, time.time() + 900
    while time.time() < deadline:
        last = _get('/api/progress?run=' + conf['runid'])
        if last.get('done'):
            break
        time.sleep(1)

    assert not last.get('error'), '合成失败：%s' % last.get('error')
    cut = (last.get('diag') or {}).get('cut') or {}
    assert cut.get('cut_sec', 0) > 0.3, '取消了勾选却没有剪掉任何画面：%s' % cut
    assert cut.get('out_dur', 0) < cut.get('src_dur', 0), '成片未短于原片：%s' % cut

"""冒烟回归：剧情驱动剪辑（/api/plan · type=narrate 且带 plot）曾因 outline 未赋值
在拼装 plan 时抛 UnboundLocalError（出错阶段显示「剧情↔片段对齐」）。
本用例用素材库里的小视频跑完整分析阶段，断言能产出方案且每段都有解说词。
需要本机服务已启动（默认 http://127.0.0.1:8765）；未启动则自动 skip。
"""

import base64
import json
import os
import time
import urllib.request

import pytest

BASE = os.environ.get('SMOKE_BASE', 'http://127.0.0.1:8765')
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
VIDEO = os.path.join(ROOT, 'material_library', '冒烟测试.mp4')


def _post(path, payload, timeout=60):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


@pytest.mark.skipif(os.environ.get('SKIP_SMOKE') == '1', reason='smoke disabled')
def test_plan_narrate_plot_driven():
    if not os.path.exists(VIDEO):
        pytest.skip('素材缺失：' + VIDEO)
    try:                              # 探活：用 HTML 首页（不解析 JSON）
        with urllib.request.urlopen(BASE + '/', timeout=3) as r:
            r.read(64)
    except Exception as e:            # 服务没起：不算测试失败
        pytest.skip('服务未启动：%s' % e)

    with open(VIDEO, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    out = _post('/api/plan', {
        'type': 'narrate',
        'video': {'name': os.path.basename(VIDEO), 'data': b64},
        'plot': '1. 开场：镜头缓缓推进。\n2. 中段：主角出场。\n3. 结尾：收束。',
        'params': {'maxSeg': 25, 'w': 1280, 'h': 720, 'fps': 30},
    })
    assert out.get('ok'), out
    run = out['runid']

    last, deadline = {}, time.time() + 900
    while time.time() < deadline:
        last = _get('/api/progress?run=' + run)
        if last.get('plan_ready') or last.get('done'):
            break
        time.sleep(1)

    assert not last.get('error'), '剧情驱动分析报错：%s' % last.get('error')
    assert last.get('plan_ready'), '未产出规划方案，末次阶段：%s' % last.get('phase')
    segs = last['plan']['segs']
    assert segs, '方案段落为空'
    # 剧情驱动：兜底会把剧情句铺满全片，不应留下没有解说词的空段
    assert all(s.get('caption') for s in segs), '存在无解说词的段落'

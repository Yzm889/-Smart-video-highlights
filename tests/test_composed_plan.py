# -*- coding: utf-8 -*-
"""v1.4.45 回归钉：成片时间轴必须落盘并经 /api/tts_state 下发。

主人实测：调整页预览是全片/整条成片从头播、时间轴是源片坐标——因为剪后的
new_spans/voice_spans 用完即弃，前端只能拿源片坐标，对不上成片，「调不了」。
修法：compose 落盘 composed_plan.json（每段 out_start/out_end 成片坐标 +
src_start/src_end 源片坐标 + 配音时长），/api/tts_state 附 composed 节下发。

运行：pytest tests/test_composed_plan.py -q
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402  先导宿主，规避循环导入


def _mk_run(tmp_path, name='run1'):
    import movie_narrator as mn
    run = tmp_path / name
    run.mkdir()
    dummy_vid = run / 'src.mp4'
    dummy_vid.write_bytes(b'')
    state = {
        'video_path': str(dummy_vid),
        'segs': [[0, 2], [2, 4], [4, 6]],
        'narr': ['甲句', '乙句', '丙句'],
        'narr_map': [0, 1, 2],
        'params': {'autoCut': True},
        'tts_results': [[0, str(run / 'narr0.mp3')],
                        [1, str(run / 'narr1.mp3')],
                        [2, str(run / 'narr2.mp3')]],
    }
    (run / 'tts_state.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    return run, mn


def _stub_pipeline(monkeypatch, mn, cut_shift=100.0):
    """真剪平移 +100s（区分成片坐标与源片坐标），混音替身直接返回 final。"""
    monkeypatch.setattr(mn, '_fit_spans_to_voice',
                        lambda sp, d, src=None, tail=0.35, **kw:
                            (list(sp), {'grew': 0, 'added': 0.0, 'overflow': 0.0}))
    monkeypatch.setattr(mn, '_cut_video_by_spans',
                        lambda vp, sp, rd, prog, **kw:
                            ('cut.mp4', [(float(s) + cut_shift, float(e) + cut_shift) for s, e in sp], 5.0))
    monkeypatch.setattr(mn, 'probe_audio_len', lambda p: 3.0)
    monkeypatch.setattr(mn, '_compose_narration_video',
                        lambda *a, **kw: str(_last_run[0] / 'final.mp4'))


_last_run = [None]


def test_compose_writes_composed_plan(tmp_path, monkeypatch):
    run, mn = _mk_run(tmp_path)
    _last_run[0] = run
    _stub_pipeline(monkeypatch, mn)

    final = mn.compose_movie_from_tts(str(run), progress={}, skip=None, adjusted_items=None)

    assert final.endswith('final.mp4')
    plan = json.load(open(run / 'composed_plan.json', encoding='utf-8'))
    assert plan['final'] == 'final.mp4'
    assert plan['total'] == 3.0                    # probe 替身值
    rows = plan['segments']
    assert [r['index'] for r in rows] == [0, 1, 2]
    # 成片坐标 = 剪后 +100；源片坐标 = fit 后、真剪前的原位
    assert rows[0]['out_start'] == 100.0 and rows[0]['out_end'] == 102.0
    assert rows[0]['src_start'] == 0.0 and rows[0]['src_end'] == 2.0
    assert rows[1]['out_start'] == 102.0 and rows[2]['out_end'] == 106.0
    assert rows[0]['duration'] == 3.0
    assert rows[0]['text'] == '甲句'
    assert rows[0]['audio'].endswith('narr0.mp3')


def test_handler_tts_state_attaches_composed(tmp_path, monkeypatch):
    run, mn = _mk_run(tmp_path, name='run9')
    _last_run[0] = run
    _stub_pipeline(monkeypatch, mn)
    mn.compose_movie_from_tts(str(run), progress={}, skip=None, adjusted_items=None)

    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 3.0)
    captured = {}
    h = object.__new__(S.Handler)
    h.path = '/api/tts_state?run_dir=run9'
    h._send = lambda code, body, ct: captured.update(
        {'code': code, 'body': json.loads(body.decode('utf-8'))})
    h._get_tts_state()

    body = captured['body']
    assert body['ok'] is True
    comp = body.get('composed')
    assert comp and comp['total'] == 3.0, 'composed_plan 必须经 /api/tts_state 下发'
    assert comp['segments'][0]['out_start'] == 100.0
    # tts_list 与 composed 可按 index 对上（前端双表 join）
    assert body['tts_list'][0]['index'] == comp['segments'][0]['index']


def test_handler_tts_state_without_plan_has_no_composed_key(tmp_path, monkeypatch):
    _mk_run(tmp_path, name='run10')
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 3.0)
    captured = {}
    h = object.__new__(S.Handler)
    h.path = '/api/tts_state?run_dir=run10'
    h._send = lambda code, body, ct: captured.update(
        {'code': code, 'body': json.loads(body.decode('utf-8'))})
    h._get_tts_state()

    assert captured['body']['ok'] is True
    assert 'composed' not in captured['body'], '旧任务没有 plan 就不带 composed 键（前端回落演算）'


def test_desktop_adjust_page_consumes_composed():
    """桌面版调整页必须消费 composed 节：成片坐标时间轴 + final 直读 + 段短警示。"""
    js = open(os.path.join(ROOT, 'static', 'desktop', 'desktop.js'), encoding='utf-8').read()
    assert 'let _composed = null;' in js
    assert 'function _outMode()' in js and 'function _tlTotal()' in js and 'function _relayout()' in js
    assert '_composed.final' in js, '成片模式预览必须直读 final.mp4'
    assert 'it.out_start' in js, '时间轴/预览必须用成片坐标'
    assert 'if(_outMode()) return;' in js, '成片模式跳过 720p 代理'
    # 下限钳制（主人红线：段长 ≥ 配音 + 0.3 气口）：拖拽、检查器起点、检查器终点三处
    assert js.count('(it.duration || 0) + 0.3') >= 3, '三处下限钳制缺一不可'
    css = open(os.path.join(ROOT, 'static', 'desktop', 'desktop.css'), encoding='utf-8').read()
    assert '.tl-block.short' in css, '段长不足配音必须有红色警示样式'


def test_tts_failures_visible_end_to_end(tmp_path, monkeypatch):
    """v1.4.52 配音失败可见化：失败段随 tts_state 落盘 + /api/tts_state 下发。"""
    run, mn = _mk_run(tmp_path, name="runfail")
    # tts_state 里手工塞 tts_failures（模拟 _generate_all_tts 写入 progress 后的状态）
    sp = run / "tts_state.json"
    state = json.load(open(sp, encoding="utf-8"))
    state["tts_failures"] = [{"idx": 1, "engine": "none", "error": "all_engines_failed"}]
    sp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(S, "OUTDIR", str(tmp_path))
    monkeypatch.setattr(S, "probe_audio_len", lambda p: 3.0)
    captured = {}
    h = object.__new__(S.Handler)
    h.path = "/api/tts_state?run_dir=runfail"
    h._send = lambda code, body, ct: captured.update(
        {"code": code, "body": json.loads(body.decode("utf-8"))})
    h._get_tts_state()
    body = captured["body"]
    assert body["tts_failures"] == [{"idx": 1, "engine": "none", "error": "all_engines_failed"}]
    # 前端消费钉：进度条实时计数 + 完成提示 + 列表 ⚠ 标记
    js = open(os.path.join(ROOT, "static", "desktop", "desktop.js"), encoding="utf-8").read()
    assert "首轮失败" in js and "p.tts_failures" in js
    assert "_ttsFail" in js and "配音曾失败" in js

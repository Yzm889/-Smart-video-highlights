"""剧情驱动·留/剪接线集成测试（轻量 monkeypatch，不跑模型/ffmpeg）。

锁定本次修复的关键链路：
- _narrate_by_plot 写入 prog['outline']（低重要度段 keep=False）后，
  _analyze_plan_job 必须把它作为真实 outline 透传，而不是写死全保留；
- prog['cut_plan'] 必须透传到预览方案 plan['cut_plan']，前端透明面板才显示。
"""

import webui_server as S  # noqa: F401  先导入入口解开循环依赖
import workflows as W


def _fake_narrate(vp, plot, params, run_dir, progress, movie_name='', events=None):
    # 模拟「剧情驱动」：4 个 span，其中第 3 段（transition）被智能剪辑标为可剪
    segs = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 8.0)]
    narr = ['开场', '发展', '过渡', '收尾']
    outline = [
        {'start': 0.0, 'end': 2.0, 'importance': 'key', 'keep': True},
        {'start': 2.0, 'end': 4.0, 'importance': 'advance', 'keep': True},
        {'start': 4.0, 'end': 6.0, 'importance': 'transition', 'keep': False},
        {'start': 6.0, 'end': 8.0, 'importance': 'advance', 'keep': True},
    ]
    cut_plan = {
        'src_dur': 8.0, 'coverage': 75.0, 'kept': 3, 'removed': 1,
        'removed_sec': 2.0,
        'segments': [
            {'i': 0, 'start': 0.0, 'end': 2.0, 'keep': True, 'importance': 'key', 'reason': 'k', 'caption': '开场', 'asr': ''},
            {'i': 1, 'start': 2.0, 'end': 4.0, 'keep': True, 'importance': 'advance', 'reason': 'a', 'caption': '发展', 'asr': ''},
            {'i': 2, 'start': 4.0, 'end': 6.0, 'keep': False, 'importance': 'transition', 'reason': 'r', 'caption': '', 'asr': ''},
            {'i': 3, 'start': 6.0, 'end': 8.0, 'keep': True, 'importance': 'advance', 'reason': 'a', 'caption': '收尾', 'asr': ''},
        ],
    }
    if progress is not None:
        progress['outline'] = outline
        progress['cut_plan'] = cut_plan
    return segs, narr, [], {}, 'movie', [{'desc': t, 'keywords': []} for t in narr], [0, 1, 2, 3]


def test_plan_wiring_propagates_outline_and_cutplan(monkeypatch, tmp_path):
    monkeypatch.setattr(S, '_narrate_by_plot', _fake_narrate)
    monkeypatch.setattr(S, '_narrate_candidate_shots', lambda vp, params: [])
    monkeypatch.setattr(S, '_resolve_music', lambda m: None)
    monkeypatch.setattr(S, '_plan_thumbs', lambda *a, **k: {})
    monkeypatch.setattr(S, '_resolve_upload_video', lambda vobj, run_dir, tag: 'dummy.mp4')

    prog = {'run_dir': str(tmp_path), 'runid': 'wiring_test_001'}
    req = {'type': 'narrate', 'plot': '一段测试剧情', 'params': {}, 'video': {}}
    W._analyze_plan_job(req, prog)

    plan = prog['plan']
    assert plan is not None, '规划方案应已生成'
    segs = plan['segs']
    assert len(segs) == 4
    # 第 3 段（index 2）是 transition 且被剪 → keep 必须为 False，不能是写死的全保留
    assert segs[2]['keep'] is False
    assert segs[2]['importance'] == 'transition'
    # 其余关键/推进段默认保留
    assert segs[0]['keep'] is True and segs[0]['importance'] == 'key'
    # cut_plan 必须透传到预览（透明面板才能显示「剪了什么/为什么」）
    assert plan.get('cut_plan') is not None
    assert plan['cut_plan']['removed'] == 1
    assert plan['cut_plan']['coverage'] == 75.0
    # 清理：别污染全局 PLANS
    S.PLANS.pop('wiring_test_001', None)

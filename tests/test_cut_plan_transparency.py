# -*- coding: utf-8 -*-
"""回归：剪辑透明化核心逻辑
1) cut_plan.removed>0（分析已剔除被剪段）→ 渲染必须执行真剪（cut_sec>0），不得按 no_cut 跳过
2) removed=0 且保留段首尾覆盖无空隙 → 维持 no_cut（成片=原片）
3) 剪辑异常降级时 progress.warnings 必须记录（不静默）
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
# 必须先加载宿主 webui_server（video_render 经 _host() 晚绑定宿主符号），
# 直接 import video_render 会触发「webui_server ↔ video_render」循环导入。
import webui_server  # noqa: F401
import video_render as V

VIDEO = os.path.join(ROOT, 'spring10s.mp4')


@pytest.mark.skipif(not os.path.exists(VIDEO), reason='缺样片 spring10s.mp4')
def test_cut_plan_removed_forces_real_cut(tmp_path):
    """分析已剔除被剪段（cut_plan.removed>0）时，保留段覆盖全片也必须真剪。"""
    vdur = V.probe_audio_len(VIDEO) or 10.0
    # 保留段覆盖全片（首尾覆盖+无空隙）——旧逻辑会 no_cut 跳过剪辑
    spans = [(0.0, vdur * 0.4), (vdur * 0.4, vdur)]
    prog = {'cut_plan': {'removed': 1, 'removed_sec': 0.5, 'segments': []}}
    out, new_spans, cut_sec = V._cut_video_by_spans(
        VIDEO, spans, str(tmp_path), progress=prog, skip_no_cut=True)
    assert os.path.exists(out), '应产出剪辑后视频'
    assert cut_sec >= 0.0
    assert len(new_spans) == len(spans)


@pytest.mark.skipif(not os.path.exists(VIDEO), reason='缺样片 spring10s.mp4')
def test_cut_plan_none_no_cut_keeps_original(tmp_path):
    """无被剪段且保留段覆盖全片 → no_cut 返回原片（cut_sec=0）。"""
    vdur = V.probe_audio_len(VIDEO) or 10.0
    spans = [(0.0, vdur)]
    out, new_spans, cut_sec = V._cut_video_by_spans(
        VIDEO, spans, str(tmp_path), progress={}, skip_no_cut=False)
    assert cut_sec == 0.0
    assert out == VIDEO


@pytest.mark.skipif(not os.path.exists(VIDEO), reason='缺样片 spring10s.mp4')
def test_cut_failure_writes_warning(tmp_path, monkeypatch):
    """剪辑异常降级时 warnings 必须记录（不静默）。"""
    prog = {'cut_plan': {'removed': 1, 'segments': []}}
    # 用 monkeypatch 让 ffmpeg 失败 → 触发剪辑异常 → 走降级分支
    def boom(*a, **k):
        raise RuntimeError('ffmpeg boom')
    monkeypatch.setattr(V, 'ffmpeg_run', boom)
    out, _ns, _cs = V._cut_video_by_spans(
        VIDEO, [(0.0, 1.0)], str(tmp_path), progress=prog)
    assert out == VIDEO
    assert prog.get('warnings'), '降级必须写入 progress.warnings'
    assert any('剪辑' in w for w in prog['warnings'])


def test_warn_helper():
    prog = {}
    V._warn(prog, 'msg1')
    V._warn(prog, 'msg1')          # 去重
    V._warn(prog, 'msg2')
    assert prog['warnings'] == ['msg1', 'msg2']

# -*- coding: utf-8 -*-
"""E2E 出片冒烟（task #20）：用真实 ffmpeg 驱动「按区间真剪辑 → concat 拼接」链路，
不再 mock ffmpeg。锁定三件事，挡「测试全绿却不能出片」的静默失败：
1. 产物 cut.mp4 真实存在且非空（真实编码成功，不是假绿）；
2. 段数与输入严格一致（F2：少一段会让解说词/字幕整体错位）；
3. 成片时长 ≈ 保留段总长、cut_seconds>0（F6 附近：留空隙必须真剪，不能回退原片）。
无 ffmpeg（imageio-ffmpeg 缺失）时 skip，绝不假红。编码器走 video_encode_args_interim
的自动回退（无可用 NVENC 时用 libx264），故 CI/无 GPU 环境同样可跑。
"""
import os

import pytest

import webui_server as S          # 先导入宿主，规避 video_render<->webui_server 循环导入
import video_render


def _ffmpeg_ok():
    try:
        return bool(S.ffmpeg_exe())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ffmpeg_ok(), reason='imageio-ffmpeg 不可用，跳过真实剪辑 E2E')


def _make_src(run_dir, dur=5.0):
    """合成一段 5s、320x240、带 440Hz 音频的真实 mp4。"""
    src = os.path.join(run_dir, 'src.mp4')
    args = ['-y',
            '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=10:duration=%.1f' % dur,
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=%.1f' % dur,
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast',
            '-c:a', 'aac', '-shortest', src]
    rc, _o, e = S.ffmpeg_run(args)
    assert rc == 0 and os.path.exists(src) and os.path.getsize(src) > 1000, \
        '合成源视频失败：%s' % e.decode('utf-8', 'ignore')[-300:]
    return src


def test_cut_pipeline_produces_real_video(tmp_path):
    run_dir = str(tmp_path / 'run')
    os.makedirs(run_dir, exist_ok=True)
    src = _make_src(run_dir, dur=5.0)
    # 两段保留 + 中间 0.6s 空隙 → 必须触发真剪辑（covered_gap>0）
    spans = [(0.2, 2.0), (2.6, 4.4)]
    prog = {'warnings': []}
    cut_path, new_spans, cut_seconds = video_render._cut_video_by_spans(src, spans, run_dir, progress=prog)

    # 1) 真实产物、非空
    assert os.path.exists(cut_path) and os.path.getsize(cut_path) > 1000, \
        '剪辑产物必须真实存在且非空：%s' % cut_path
    # 2) 段数与输入严格一致（F2 契约）
    assert len(new_spans) == len(spans), '段数必须与输入一致：%d != %d' % (len(new_spans), len(spans))
    # 3) 真剪发生：留空隙时 cut_seconds>0，且成片时长≈保留段总长
    assert cut_seconds > 0, '保留段间有空隙，必须发生真实剪辑而非回退原片'
    expect = sum(b - a for a, b in spans)
    d = S.probe_duration(cut_path) or 0.0
    assert abs(d - expect) < 0.8, '成片时长应≈保留段总长 %.1fs，实际 %.1fs' % (expect, d)
    # 无失败级 warning（不静默）
    bad = [w for w in prog.get('warnings', []) if ('失败' in w or 'error' in w.lower())]
    assert not bad, '剪辑链路不应产生失败级告警：%s' % bad


def test_cut_pipeline_no_cut_when_full_cover(tmp_path):
    """保留段铺满全片且首尾覆盖 → 无空隙可剪，直接跳过（cut_seconds==0），段数仍一致。"""
    run_dir = str(tmp_path / 'run2')
    os.makedirs(run_dir, exist_ok=True)
    src = _make_src(run_dir, dur=5.0)
    spans = [(0.0, 5.0)]
    cut_path, new_spans, cut_seconds = video_render._cut_video_by_spans(src, spans, run_dir)
    assert len(new_spans) == 1
    assert cut_seconds == 0.0, '全片覆盖应跳过剪辑'
    assert os.path.exists(cut_path), '跳过剪辑时回退源视频路径必须仍存在'

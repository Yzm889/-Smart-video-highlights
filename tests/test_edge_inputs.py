# -*- coding: utf-8 -*-
"""极端输入门禁：坏素材进得来，流程也要走得稳、错要说人话。

覆盖此前完全没测过的一类输入——**视频文件本身是坏的**（现有测试里的 "corrupt" 都是
history.json / 队列文件，不是视频）：
  · 0 字节视频、损坏视频（内容是随机字节但后缀是 .mp4）
  · 无音轨视频（容器有、音频流没有）
  · 超短视频（0.3s）
  · 中文 / 空格 / emoji 文件名（Windows 路径地狱）
  · max_seg 极小 → 段数必须被段数上限拦住
  · 缩略图全失败（磁盘满 / 无权限的替身）→ **必须给告警**，不能静默一片空白

原则：**失败要可见、降级要声明**，绝不静默。
"""
import json
import os

import pytest

import webui_server            # 必须先导入宿主，规避循环导入
import analyze_pipeline as A
import ai_providers as AP
from ffmpeg_utils import ffmpeg_run


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """隔离重型依赖：LLM 探针放行、草稿固定；ASR 直接返回空（否则每次都要加载
    3GB 的 faster-whisper large-v3，测试会慢到超时——ASR 行为另有专项测试覆盖）。"""
    monkeypatch.setattr(AP, 'local_llm_infer_probe', lambda *a, **k: (True, '推理正常'))
    monkeypatch.setattr(A, 'asr_segments', lambda *a, **k: [])

    def _llm(prompt, system='', timeout=180, max_tokens=None):
        n = prompt.count('\n[')
        return json.dumps([{'i': i, '简洁': 'a', '悬念': 'b', '口语': 'c'} for i in range(max(1, n))],
                          ensure_ascii=False)
    monkeypatch.setattr(A, '_llm_light', _llm)


def _mk_video(path, dur=2.0, with_audio=True, size='320x240'):
    args = ['-y', '-f', 'lavfi', '-i', 'testsrc2=size=%s:rate=15:duration=%s' % (size, dur)]
    if with_audio:
        args += ['-f', 'lavfi', '-i', 'sine=frequency=220:duration=%s' % dur]
    args += ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p']
    if with_audio:
        args += ['-c:a', 'aac', '-shortest']
    args += [path]
    rc, _o, _e = ffmpeg_run(args)
    return rc == 0 and os.path.exists(path)


def _run(video_path, tmp_path, options=None, segs_override=None):
    """跑一次真实 dispatch_analyze，返回 (prog, result)。"""
    if segs_override is not None:
        monkey = segs_override
    run_dir = tmp_path / ('run_%d' % abs(hash(str(video_path))))
    run_dir.mkdir(parents=True, exist_ok=True)
    prog = {'run_dir': str(run_dir), 'warnings': []}
    A.dispatch_analyze({'video_path': str(video_path), 'options': options or {'draft': True}}, prog)
    return prog, prog.get('analyze_result')


# ---------------------------------------------------------------------------
# 坏文件
# ---------------------------------------------------------------------------
def test_zero_byte_video_fails_with_clear_error(tmp_path):
    """0 字节视频 → 明确报错，不崩、不静默。"""
    v = tmp_path / 'empty.mp4'
    v.write_bytes(b'')
    prog, res = _run(v, tmp_path)
    assert prog.get('error'), '0 字节视频应给出明确错误，实际：%r' % prog.get('error')
    assert res is None or not res.get('segments')


def test_corrupt_video_fails_with_clear_error(tmp_path):
    """损坏视频（随机字节冒充 mp4）→ 明确报错。"""
    v = tmp_path / 'broken.mp4'
    v.write_bytes(b'\x00\x01\x02' * 5000)
    prog, res = _run(v, tmp_path)
    assert prog.get('error'), '损坏视频应给出明确错误'
    assert '时长' in prog['error'] or '切分' in prog['error'] or '不存在' in prog['error'], prog['error']


def test_missing_video_path_errors(tmp_path):
    """根本不存在的文件 → 明确报错。"""
    prog, _res = _run(tmp_path / 'nope.mp4', tmp_path)
    assert prog.get('error')
    assert '不存在' in prog['error']


# ---------------------------------------------------------------------------
# 特殊但可用的输入
# ---------------------------------------------------------------------------
def test_video_without_audio_track_still_processed(tmp_path):
    """无音轨视频：容器时长可探 → 流程走完，只在 warnings 里说明没台词。"""
    v = tmp_path / 'silent.mp4'
    assert _mk_video(str(v), dur=2.0, with_audio=False), '测试视频合成失败'
    prog, res = _run(v, tmp_path)
    assert not prog.get('error'), prog.get('error')
    assert res and res.get('segments'), '无音轨视频也应产出分段'
    warns = ' '.join(res.get('warnings') or [])
    assert '台词' in warns or 'ASR' in warns, warns


def test_very_short_video_completes(tmp_path):
    """0.3 秒超短视频：不能崩，至少产出 1 段。"""
    v = tmp_path / 'tiny.mp4'
    assert _mk_video(str(v), dur=0.3), '测试视频合成失败'
    prog, res = _run(v, tmp_path)
    assert not prog.get('error'), prog.get('error')
    assert res and len(res.get('segments', [])) >= 1


@pytest.mark.parametrize('fname', ['中文 名字.mp4', 'emoji🎬测试.mp4', 'a b c.mp4'])
def test_unicode_and_space_filename(tmp_path, fname):
    """中文/空格/emoji 文件名：Windows 上最容易出问题的路径。"""
    v = tmp_path / fname
    assert _mk_video(str(v), dur=1.5), '测试视频合成失败'
    prog, res = _run(v, tmp_path)
    assert not prog.get('error'), prog.get('error')
    assert res and res.get('segments')
    for s in res['segments']:
        assert s.get('thumbnail_url'), '缩略图 URL 缺失（路径处理有问题）'


def test_segment_count_capped_when_max_seg_tiny(tmp_path):
    """max_seg 极小（0.5s）→ 段数必须被段数上限拦住，不能几百段把机器拖死。"""
    v = tmp_path / 'segcap.mp4'
    assert _mk_video(str(v), dur=30.0), '测试视频合成失败'
    prog, res = _run(v, tmp_path, options={'draft': True, 'max_seg': 0.5})
    assert not prog.get('error'), prog.get('error')
    n = len(res.get('segments', []))
    assert 0 < n <= 60, '段数应被上限拦住，实际 %d' % n


# ---------------------------------------------------------------------------
# 静默失败：缩略图全失败必须有告警
# ---------------------------------------------------------------------------
def test_all_thumbnails_failed_produces_warning(tmp_path, monkeypatch):
    """磁盘满/无权限 → 缩略图全失败：必须告警，不能让用户对着空白编辑台发呆。"""
    v = tmp_path / 'diskfull.mp4'
    assert _mk_video(str(v), dur=2.0), '测试视频合成失败'

    def _fail(*a, **k):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(A, '_make_thumbnail', _fail)
    prog, res = _run(v, tmp_path)
    warns = ' '.join((res or {}).get('warnings') or [])
    assert '缩略图' in warns and ('失败' in warns), '缩略图全失败必须告警，实际 warnings=%s' % warns
    assert '磁盘' in warns or '空间' in warns, warns

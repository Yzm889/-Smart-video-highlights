# -*- coding: utf-8 -*-
"""预览代理（2026-11-06 计划#2）回归钉：720p 轻量副本供预览，合成仍用原片。

钉住：
1. >720p 源片 → 生成 proxy.mp4（720p、无 .part 残留）；
2. ≤720p 源片 → 直接用原片（mode=original），不产生副本；
3. 已就绪时二次调用不得重建（mtime 不变）；
4. 非法/不存在的 run_dir → ok=False 不崩；
5. 高度探测可用。
"""
import os

import pytest

import webui_server as S


def _make_clip(path, w, h, dur=1.0):
    rc, _o, e = S.ffmpeg_run([
        '-y', '-f', 'lavfi', '-i', 'testsrc2=duration=%s:size=%dx%d:rate=15' % (dur, w, h),
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=%s' % dur,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', path], timeout=120)
    assert rc == 0, e.decode('utf-8', 'ignore')[-200:]


@pytest.fixture
def in_outdir(tmp_path, monkeypatch):
    """把 OUTDIR 指到 tmp_path 的**父目录**，run_dir 用相对名（过 _safe_dir 校验）。"""
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path.parent))
    return tmp_path


def test_probe_video_height(in_outdir):
    p = str(in_outdir / 'src.mp4')
    _make_clip(p, 1280, 800)
    assert S._probe_video_height(p) == 800
    # 【2026-11-09 B6】宽高一起探测（历史记录 w/h 字段用）
    assert S._probe_video_size(p) == (1280, 800)


def test_proxy_built_for_high_res(in_outdir):
    d = in_outdir
    _make_clip(str(d / 'src.mp4'), 1280, 800)
    out = S._ensure_preview_proxy(d.name, wait=True)
    assert out['ok'] and out['mode'] == 'proxy' and out['ready'], out
    assert out['file'] == 'proxy.mp4'
    dst = str(d / 'proxy.mp4')
    assert os.path.isfile(dst) and os.path.getsize(dst) > 1024
    assert S._probe_video_height(dst) == 720, '副本必须缩到 720p'
    assert not os.path.exists(dst + '.part'), '不得残留 .part 半截文件'


def test_proxy_skipped_for_720p(in_outdir):
    _make_clip(str(in_outdir / 'src.mp4'), 1280, 720)
    out = S._ensure_preview_proxy(in_outdir.name, wait=True)
    assert out['ok'] and out['mode'] == 'original' and out['ready'], out
    assert not os.path.exists(str(in_outdir / 'proxy.mp4'))


def test_proxy_second_call_reuses(in_outdir):
    _make_clip(str(in_outdir / 'src.mp4'), 1280, 800)
    out1 = S._ensure_preview_proxy(in_outdir.name, wait=True)
    assert out1['ready']
    m = os.path.getmtime(str(in_outdir / 'proxy.mp4'))
    out2 = S._ensure_preview_proxy(in_outdir.name, wait=True)
    assert out2['ready'] and out2['mode'] == 'proxy'
    assert os.path.getmtime(str(in_outdir / 'proxy.mp4')) == m, '二次调用不得重建'


def test_proxy_bad_run_dir(in_outdir):
    out = S._ensure_preview_proxy('no_such_run_dir_xyz', wait=True)
    assert not out['ok'] and out['error']


def test_proxy_name_hints_source(in_outdir):
    """name 参数（后端下发的真实文件名）优先于 src.* 探测。"""
    _make_clip(str(in_outdir / 'movie.mkv'), 1280, 800)
    out = S._ensure_preview_proxy(in_outdir.name, name='movie.mkv', wait=True)
    assert out['ok'] and out['mode'] == 'proxy' and out['ready'], out

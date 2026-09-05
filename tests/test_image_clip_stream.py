# -*- coding: utf-8 -*-
"""batch5-5.3 图片 Ken Burns 逐帧流式编码的回归锁定。

旧实现把全部 N 帧 float32 堆在内存再 np.stack + astype + tobytes（三份全量
拷贝），1080p/30fps/5s 单张峰值约 3.7GB×3——这是审计里「图片合成全帧驻留内存」
的直接来源。现在逐帧生成即写入 ffmpeg stdin，峰值只有一帧 uint8。
"""
import inspect
import os

from PIL import Image

import webui_server as S


def test_ffmpeg_run_accepts_generator_input(tmp_path):
    """ffmpeg_run 流式输入：生成器喂入与 bytes 喂入的成片解码后逐字节一致。"""
    w, h, n = 64, 48, 12
    frames = [bytes([(i * 23) % 256] * (w * h * 3)) for i in range(n)]
    out1, out2 = str(tmp_path / 'a.mp4'), str(tmp_path / 'b.mp4')
    args = ['-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '%dx%d' % (w, h),
            '-r', '6', '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p']
    rc1, _, _ = S.ffmpeg_run(args + [out1], input_data=b''.join(frames))
    rc2, _, _ = S.ffmpeg_run(args + [out2], input_data=iter(list(frames)))
    assert rc1 == 0 and rc2 == 0, '两次编码都应成功'
    raw1, raw2 = str(tmp_path / 'a.raw'), str(tmp_path / 'b.raw')
    assert S.ffmpeg_run(['-y', '-i', out1, '-f', 'rawvideo', '-pix_fmt', 'rgb24', raw1])[0] == 0
    assert S.ffmpeg_run(['-y', '-i', out2, '-f', 'rawvideo', '-pix_fmt', 'rgb24', raw2])[0] == 0
    assert open(raw1, 'rb').read() == open(raw2, 'rb').read(), '流式输入必须与 bytes 输入产出一致'


def test_make_image_clip_streaming_output(tmp_path):
    """逐帧流式版 make_image_clip：产出有效 mp4，时长≈dur。"""
    img = str(tmp_path / 'img.jpg')
    Image.new('RGB', (800, 600), (200, 30, 40)).save(img)
    out = str(tmp_path / 'clip.mp4')
    S.make_image_clip(img, 1.0, 0, out, 320, 240, 10)
    assert os.path.getsize(out) > 1000, '应产出非空 mp4'
    d = S.probe_duration(out)
    assert d is not None and abs(d - 1.0) < 0.25, '时长应约为 1.0s，实际 %s' % d


def test_make_image_clip_no_frame_accumulation():
    """源码 pin：禁止再把全部帧堆在内存（np.stack(...)/out_frames 属于代码特征，
    docstring 里的历史说明不算）。"""
    src = inspect.getsource(S.make_image_clip)
    assert 'np.stack(' not in src and 'out_frames' not in src, '不允许全帧常驻内存'
    assert 'def _frames' in src and 'input_data=_frames()' in src, '必须经生成器流式写入'

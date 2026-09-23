# -*- coding: utf-8 -*-
"""CI 诊断：复现 test_make_image_clip_streaming_output 的 Linux ffmpeg 失败，
逐组参数打印 stderr，定位是哪个参数/编码器不可用。只在 CI 上运行。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui_server as S
from PIL import Image

d = tempfile.mkdtemp()
img = os.path.join(d, 'i.jpg')
Image.new('RGB', (800, 600), (200, 30, 40)).save(img)
out = os.path.join(d, 'clip.mp4')

print('FFMPEG_EXE', S.ffmpeg_exe())

# 1) 测试同款调用
try:
    S.make_image_clip(img, 1.0, 0, out, 320, 240, 10)
    print('CLIP_EXIST', os.path.exists(out), os.path.getsize(out) if os.path.exists(out) else None)
except Exception as exc:
    print('CLIP_ERR', repr(exc))

# 2) 手动重跑拿 stderr（生成器喂入）
N = 10
def _frames():
    for _ in range(N):
        yield bytes(320 * 240 * 3)

args = ['-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '320x240',
        '-r', '10', '-i', '-'] + S.video_encode_args(20) + ['-threads', '0', os.path.join(d, 'c2.mp4')]
rc, _o, e = S.ffmpeg_run(args, input_data=_frames())
print('FFMPEG_RC', rc)
print('FFMPEG_ERR_TAIL', (e or '')[-3000:])

# 3) 裸参数对照（test_ffmpeg_run_accepts_generator_input 用的，CI 上通过）
args2 = ['-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '320x240',
         '-r', '10', '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', os.path.join(d, 'c3.mp4')]
rc2, _o2, e2 = S.ffmpeg_run(args2, input_data=_frames())
print('FFMPEG_BARE_RC', rc2)
print('FFMPEG_BARE_ERR_TAIL', (e2 or '')[-1500:])

# 4) 逐参数定位：去掉 tune film
args3 = ['-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '320x240',
         '-r', '10', '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
         '-preset', 'medium', '-crf', '20', '-threads', '0', os.path.join(d, 'c4.mp4')]
rc3, _o3, e3 = S.ffmpeg_run(args3, input_data=_frames())
print('FFMPEG_NOTUNE_RC', rc3)
print('FFMPEG_NOTUNE_ERR_TAIL', (e3 or '')[-1500:])

# 5) 完整参数组合（CI 无 GPU 时的实际路径：libx264 + tune film）
args4 = ['-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '320x240',
         '-r', '10', '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
         '-preset', 'medium', '-tune', 'film', '-crf', '20', '-threads', '0', os.path.join(d, 'c5.mp4')]
rc4, _o4, e4 = S.ffmpeg_run(args4, input_data=_frames())
print('FFMPEG_FULL_RC', rc4)
print('FFMPEG_FULL_EXIST', os.path.exists(os.path.join(d, 'c5.mp4')))
print('FFMPEG_FULL_ERR_TAIL', (e4 or '')[-2000:])

# -*- coding: utf-8 -*-
"""混音滤镜顺序回归测试：配音必须落在各自的起始时间上，不能被裁没。

【真实故障】成片「配了第一句之后就没有声音」。
根因是滤镜顺序：
    [a]adelay=3000|3000,atrim=0:2.00   ← 先延迟到 3s，再把流裁成 [0,2] 秒
延迟后的 [0,2] 秒全是静音，等于把这一句语音整段删掉。只有 od=0 的第一段能出声，
所以表现正好是「第一句之后全哑」。正确顺序必须先 atrim 再 adelay。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui_server as _W  # noqa: F401  解除循环依赖
import video_render as VR

imageio_ffmpeg = pytest.importorskip('imageio_ffmpeg')


def _ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


def _mean_volume(path, ss, t):
    r = subprocess.run([_ffmpeg(), '-ss', str(ss), '-t', str(t), '-i', path,
                        '-af', 'volumedetect', '-f', 'null', '-'],
                       capture_output=True, text=True, errors='ignore')
    m = re.search(r'mean_volume: (-?[\d.]+) dB', r.stderr)
    return float(m.group(1)) if m else -999.0


@pytest.fixture()
def tmp_media():
    d = tempfile.mkdtemp(prefix='mix_')
    try:
        exe = _ffmpeg()
        # 6 秒纯画面（无原声），避免原声音量干扰判断
        src = os.path.join(d, 'src.mp4')
        subprocess.run([exe, '-y', '-f', 'lavfi', '-i', 'color=c=black:s=160x120:r=10:d=6',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', src],
                       capture_output=True)
        clips = []
        for i in range(3):
            p = os.path.join(d, 'c%d.wav' % i)
            subprocess.run([exe, '-y', '-f', 'lavfi',
                            '-i', 'sine=frequency=%d:duration=1' % (440 + i * 110), p],
                           capture_output=True)
            clips.append(p)
        yield d, src, clips
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_second_and_third_clip_are_audible(tmp_media):
    d, src, clips = tmp_media
    segs = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
    # 三句配音分别落在 0s / 2s / 4s，各占 2 秒
    tts = [(clips[0], 0.0, 2.0), (clips[1], 2.0, 4.0), (clips[2], 4.0, 6.0)]
    out = VR._compose_narration_video(src, segs, ['一', '二', '三'], tts, d, {}, burned=True)
    assert out and os.path.exists(out)

    # 每一句都必须在自己那 2 秒里出声（阈值 -60dB，纯静音约 -91dB）
    for i, start in enumerate((0.0, 2.0, 4.0)):
        vol = _mean_volume(out, start + 0.3, 1.0)
        assert vol > -60, '第 %d 句配音在 %.1fs 处没有声音（%.1f dB）' % (i + 1, start, vol)


def test_trim_keeps_audio_at_delayed_position(tmp_media):
    """直接验证滤镜语义：atrim 必须在 adelay 之前。"""
    d, src, clips = tmp_media
    segs = [(0.0, 2.0), (2.0, 4.0)]
    tts = [(clips[0], 0.0, 2.0), (clips[1], 2.0, 4.0)]
    out = VR._compose_narration_video(src, segs, ['一', '二'], tts, d, {}, burned=True)
    assert _mean_volume(out, 2.3, 1.0) > _mean_volume(out, 1.0, 0.5)

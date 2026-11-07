# -*- coding: utf-8 -*-
"""成片音频规格回归钉（v1.4.7）。

真缺陷：`video_render.py` 最终交付的 3 条混流命令只写了 `-c:a aac`，没钉采样率，
而同一文件的其它 4 处（单段渲染 / 兜底编码）都写了 `-ar 44100`。20 分钟真实素材
实测出片是 **96000 Hz** AAC（narr 源 22050、链上 aresample=44100，loudnorm 内部
192k 协商把率带跑），96k 在电视/车机/部分手机解码器上兼容差。

这条钉死：凡是 `-c:a aac` 的 ffmpeg 调用，附近必须出现 `44100`。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'video_render.py')


def _aac_calls(src):
    """返回所有含 '-c:a', 'aac' 的 ffmpeg 调用片段（按 500 字符窗口截取）。"""
    out = []
    for m in re.finditer(r"'-c:a',\s*'aac'", src):
        out.append(src[m.start():m.start() + 500])
    return out


def test_every_aac_mux_pins_44100():
    with open(SRC, encoding='utf-8') as f:
        src = f.read()
    calls = _aac_calls(src)
    assert len(calls) >= 5, 'ffmpeg aac 调用数量异常（可能被重构了）：%d' % len(calls)
    bad = [c[:160] for c in calls if '44100' not in c]
    assert not bad, '这些 aac 调用没钉 44100：\n%s' % '\n'.join(bad)


def test_final_output_pins_stereo():
    """成片必须是立体声，避免 mono 源一路带下去。"""
    with open(SRC, encoding='utf-8') as f:
        src = f.read()
    final_blocks = [b for b in _aac_calls(src) if 'final.mp4' in b or "'-ac'" in b]
    assert final_blocks, '找不到成片 mux 调用'
    assert any("'-ac', '2'" in b for b in final_blocks), '成片 mux 没钉 -ac 2'

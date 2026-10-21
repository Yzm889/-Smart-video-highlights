# -*- coding: utf-8 -*-
"""F6 回归：concat 兜底必须走 concat demuxer 清单（单 -i concat.txt），
绝不能再把 N 路 -i 塞进单条 filter_complex 命令——段数多时 ffmpeg 会因
命令行/滤镜复杂度炸掉，导致整段剪辑回退原片、成片完全没剪。

强制第一次 copy 拼接失败，断言兜底命令：① 用 `-f concat`；② 只有 1 个 -i
（即 concat.txt，而非每片一个 -i）；③ 全程不出现 filter_complex。
"""
import os

import webui_server as _w
import video_render as V


def test_concat_fallback_uses_demuxer_not_n_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(V, 'probe_audio_len', lambda *a, **k: 100.0)
    monkeypatch.setattr(V, '_has_audio_track', lambda *a, **k: True)
    monkeypatch.setattr(_w, 'video_encode_args_interim',
                        lambda: ['-c:v', 'libx264', '-preset', 'veryfast'])

    calls = []

    def _fake_run(cmd):
        calls.append(list(cmd))
        if '-ss' in cmd and '-t' in cmd:        # 逐段剪切：成功并落盘
            with open(cmd[-1], 'wb') as f:
                f.write(b'x' * 100)
            return (0, b'', b'')
        if 'concat' in cmd:
            if 'copy' in cmd:                  # 第一次 copy 拼接故意失败 → 触发兜底
                return (1, b'', b'copy concat failed')
            outp = cmd[-1]                     # 兜底重编码：成功并落盘
            with open(outp, 'wb') as f:
                f.write(b'x' * 100)
            return (0, b'', b'')
        return (0, b'', b'')

    monkeypatch.setattr(V, 'ffmpeg_run', _fake_run)

    spans = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0), (6.0, 7.0), (8.0, 9.0)]  # 多段
    cut_path, new_spans, cut_seconds = V._cut_video_by_spans(
        'fake.mp4', spans, str(tmp_path), progress={})

    # 成片应是真实剪出来的 cut.mp4，而非回退原片
    assert os.path.basename(cut_path) == 'cut.mp4', '兜底应成功出片，不应回退原片：%s' % cut_path
    assert cut_seconds > 0, '应有实际剪辑时长'

    # 兜底命令：用 concat demuxer 清单，且只有 1 个 -i（concat.txt）
    fallback = calls[-1]
    assert '-f' in fallback and 'concat' in fallback, '兜底必须走 concat demuxer：%s' % fallback
    assert fallback.count('-i') == 1, '兜底只能有 1 个 -i（concat.txt），不能是 N 路 -i：%s' % fallback
    idx = fallback.index('-i')
    assert 'concat.txt' in fallback[idx + 1], '唯一的 -i 必须是 concat.txt 清单：%s' % fallback
    # 全程不得再出现 N 路 -i 的 filter_complex 兜底
    assert 'filter_complex' not in ' '.join(' '.join(map(str, c)) for c in calls), \
        '不应再使用 filter_complex 的 N 路 -i 兜底'

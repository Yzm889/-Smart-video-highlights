# -*- coding: utf-8 -*-
"""真剪辑链路回归：concat 清单必须写绝对路径。

真机证据（22 分钟片 / 105 段）：
  `_cut_video_by_spans` 把相对路径写进 concat.txt，concat 分离器按「清单所在目录」
  解析，路径被拼成 `cuts/webui_output/.../cuts/cut000.mp4` → 拼接必然失败 →
  静默回退原片 → **成片时长恒等于原片，剪辑等于没做**（1035 秒白白没剪掉）。
  因为回退是「静默降级」，用户只看到「没剪短」，完全不知道原因。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as _W  # noqa: F401  先导入宿主解循环依赖
import video_render as VR


def test_concat_list_uses_absolute_paths(tmp_path, monkeypatch):
    """concat.txt 里的每个条目都必须是绝对路径，否则分离器会二次拼接目录。"""
    work = str(tmp_path)
    run_dir = os.path.join(work, 'run-1')
    os.makedirs(os.path.join(run_dir, 'cuts'), exist_ok=True)
    src = os.path.join(work, 'src.mp4')
    open(src, 'wb').write(b'x' * 8192)

    calls = []
    state = {'concat_seen': None}

    def fake_ffmpeg(args):
        calls.append(list(args))
        # 拼接那一步（-f concat）时把清单内容读出来供断言
        if '-f' in args and 'concat' in args:
            idx = args.index('-i')
            txt = args[idx + 1]
            state['concat_seen'] = open(txt, encoding='utf-8').read()
            out = args[-1]
            open(out, 'wb').write(b'y' * 4096)     # 假装拼接成功
            return 0, b'', b''
        # 其余（切片段）按最后参数生成文件
        out = args[-1]
        open(out, 'wb').write(b'z' * 4096)
        return 0, b'', b''

    monkeypatch.setattr(VR, 'ffmpeg_run', fake_ffmpeg)
    monkeypatch.setattr(VR, 'probe_audio_len', lambda p: 100.0)
    monkeypatch.setattr(VR, '_has_audio_track', lambda p: True)
    monkeypatch.setattr(VR, '_display_factor', lambda a, b: 1.0)

    # 关键：run_dir 用相对路径（正是触发 bug 的条件）
    cwd = os.getcwd()
    os.chdir(work)
    try:
        rel_run = os.path.join('run-rel')
        os.makedirs(os.path.join(rel_run, 'cuts'), exist_ok=True)
        open(os.path.join(rel_run, 'cuts', '.keep'), 'w').close()
        out, new_spans, cut_sec = VR._cut_video_by_spans(src, [(10.0, 20.0), (40.0, 50.0)], rel_run, {})
    finally:
        os.chdir(cwd)

    assert state['concat_seen'], '没有走到拼接步骤'
    entries = [ln for ln in state['concat_seen'].splitlines() if ln.strip()]
    assert entries, 'concat 清单为空'
    for ln in entries:
        p = ln.split("'")[1]
        assert os.path.isabs(p), 'concat 清单出现相对路径（会被二次拼接目录）：%r' % p
    assert cut_sec > 0, '剪辑被跳过（cut_sec=0 说明回退原片）'
    assert os.path.abspath(out) != os.path.abspath(src), '返回的仍是原片，剪辑没生效'


def test_subtitle_text_has_no_markup():
    """字幕文本必须剥掉 TTS 标记，否则画面里会出现 {/情绪}{/停顿}。"""
    import tts_engines as T
    s = '{情绪:紧张}他冲了出去{/情绪}{停顿:0.5}没人拦得住{/停顿}'
    out = T.strip_tts_markup(s)
    assert '{' not in out and '}' not in out, out
    assert '他冲了出去' in out and '没人拦得住' in out

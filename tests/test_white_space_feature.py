# -*- coding: utf-8 -*-
"""v1.4.22 回归钉：「（留白）」功能一个字节都不许丢。

用户 2026-11-09 特别交代：解说之后留出原声画面（钩子引出后面的原声）是
**必须功能**，到一定关键词才触发，不能被「补画面 / 气口」这些改动带走。

「（留白）」是稿子里的功能标记，四个地方必须认识它：
  1. 配音：不念（否则观众听到「括号留白括号」）
  2. 字幕：不显示（narr_srt 置空）
  3. 混音：原声 1.0 全量（默认缝隙只有 0.5，不抬起来钩子等于白钩）
  4. 补画面：没有配音就不补（不许把别的句子的时长套上去）

实测过的坑：movie 链路 `_generate_all_tts` 只跳过空串、不跳留白标记，
留白段会被真念出来 —— 而 `_render_narrate`（另一条链路）是跳过的。
同一份稿子走不同入口结果不同，本次统一到 text_utils.is_white_line。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import movie_narrator as MN  # noqa: E402
import video_render as VR  # noqa: E402
from text_utils import is_white_line  # noqa: E402


def test_marker_recognised_in_both_forms():
    assert is_white_line('（留白）')
    assert is_white_line('(留白)')
    assert is_white_line('  （留白）  ')
    assert not is_white_line('注意看这个男人')
    assert not is_white_line('')
    assert not is_white_line(None)


def test_white_segment_is_not_voiced(tmp_path, monkeypatch):
    """留白段必须一点都不念，也不能被当成「空串」计入失败。"""
    narr = ['第一句解说词', '（留白）', '第二句解说词']
    spoken = []

    def fake_speak(text, out):
        spoken.append(text)
        open(out, 'wb').write(b'RIFF' + b'0' * 400)
        return True, 'fake', out

    monkeypatch.setattr(MN, 'local_tts_speak', fake_speak)
    monkeypatch.setattr(MN, 'edge_tts_available', lambda: False)
    monkeypatch.setattr(MN, 'load_ai_config', lambda: {})
    res = MN._generate_all_tts(narr, str(tmp_path))
    assert '（留白）' not in spoken, '留白标记被念出来了，功能失效'
    assert len(res) == 2
    assert sorted(i for i, _ in res) == [0, 2]


def test_white_segment_does_not_get_padded(tmp_path, monkeypatch):
    """留白段没有配音 → 补画面时不许被别的句子的时长拉长。"""
    spans = [(0.0, 4.0), (10.0, 14.0), (20.0, 24.0)]
    durs = {0: 6.0, 2: 6.0}          # 第 1 段是留白，没有配音
    got, st = VR._fit_spans_to_voice(spans, durs, src_dur=100.0, tail=0.5)
    assert got[1] == (10.0, 14.0), '留白段的画面被改长了：%r' % (got[1],)
    assert got[0][1] - got[0][0] >= 6.5
    assert got[2][1] - got[2][0] >= 6.5


def test_mixer_accepts_white_spans_with_full_volume():
    """混音端必须把留白区间与「原声引用」同等对待（原声 1.0）。"""
    src = open(VR.__file__, encoding='utf-8').read()
    assert 'white_spans=None' in src
    assert '_loud_spans = list(quote_spans or []) + list(white_spans or [])' in src
    assert src.count('for _qs, _qe in _loud_spans:') == 2, '两处混音分支都要用合并后的区间'


def test_all_compose_paths_pass_white_spans():
    """三条合成入口（剧情驱动/一步直出/一键合成/重合成）都得把留白区间传下去。"""
    mn = open(MN.__file__, encoding='utf-8').read()
    assert mn.count('white_spans=_white_spans') == 2
    rp = open(os.path.join(ROOT, 'render_pipeline.py'), encoding='utf-8').read()
    assert 'white_spans=_white_spans' in rp
    hd = open(os.path.join(ROOT, 'handler.py'), encoding='utf-8').read()
    assert 'white_spans=_ws' in hd

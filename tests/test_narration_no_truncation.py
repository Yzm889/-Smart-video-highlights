# -*- coding: utf-8 -*-
"""v1.4.21 回归钉：配音必须念完再切镜头。

事故（2026-11-09 实跑惩罚者前二十分钟）：用户听成片说
「有的字幕没念完就进入下一个镜头念第二段字幕了，更别谈画面留白」。

量化：37 段配音合计 **235.1s**，画面只保留 **208.8s**，成片 209.5s
—— 25.6 秒的语音被切掉了。

真因：合成端把每段配音裁在本镜头段内（防跨段重叠），而剪辑端只在拿到
`voice_durs` 时才做 ±15% 微变速贴合；两步走合成调用 `_cut_video_by_spans`
时**根本没传这个参数**，于是段长就是画面长度，配音超出的部分直接被砍。
±15% 也救不了 20% 以上的差距，必须补素材（延长画面段）。

本文件钉住：
  1. 段短于配音 → 补长到 v_len+tail；
  2. 段已经够长 → 不动（留白本来就够，不许把画面拉长）；
  3. 补长后仍保序、不重叠、不越过片尾；
  4. 用户那次的实际数字补完必须逐段装得下；
  5. 留白秒数可配，默认 0.5s。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import video_render as V  # noqa: E402
import movie_narrator as N  # noqa: E402


def test_short_span_grows_to_fit_voice():
    spans = [(10.0, 12.0)]
    got, st = V._fit_spans_to_voice(spans, {0: 5.0}, src_dur=100.0, tail=0.5)
    assert got[0][1] - got[0][0] >= 5.5
    assert got[0][0] == 10.0, '够补时不该动段首'
    assert st['grew'] == 1


def test_long_enough_span_untouched():
    """画面本来就有 9 秒、配音 5 秒 → 留白已经够，不许再拉长。"""
    got, st = V._fit_spans_to_voice([(10.0, 19.0)], {0: 5.0}, src_dur=100.0, tail=0.5)
    assert got[0] == (10.0, 19.0)
    assert st['grew'] == 0


def test_spans_stay_ordered_and_disjoint():
    got, _ = V._fit_spans_to_voice(
        [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0)],
        {0: 6.0, 1: 6.0, 2: 6.0}, src_dur=100.0, tail=0.5)
    for i in range(len(got) - 1):
        assert got[i][1] <= got[i + 1][0] + 1e-6, '补长不许造成重叠'
    for a, b in got:
        assert b > a
        assert b <= 100.0


def test_tail_end_shifts_start_back():
    """片尾不够往后延 → 段首往前挪，仍然装得下，且不越过片尾。"""
    got, _ = V._fit_spans_to_voice([(95.0, 98.0)], {0: 4.0}, src_dur=100.0, tail=0.5)
    a, b = got[0]
    assert b <= 100.0
    assert b - a >= 4.5


def test_missing_voice_leaves_span_alone():
    """该段配音失败（时长未知）→ 不许凭空补长。"""
    got, st = V._fit_spans_to_voice([(10.0, 13.0)], {}, src_dur=100.0)
    assert got[0] == (10.0, 13.0)
    assert st['grew'] == 0


def test_real_run_numbers_now_fit():
    """用那次实跑的分布：44 个短镜头 + 37 句配音，补完必须段段装得下。"""
    src = 1200.0
    # 每段配音时长（取自 tts_state 的实测均值约 6.35s，最长的约 10s）
    durs = {i: (10.0 if i % 7 == 0 else 5.6 + (i % 3)) for i in range(37)}
    spans = []
    cursor = 12.0
    for i in range(37):
        spans.append((cursor, cursor + 5.0))
        cursor += 31.5          # 原实现平均每段只留 ~5s 画面，间隔很大
    got, st = V._fit_spans_to_voice(spans, durs, src_dur=src, tail=0.5)
    for i, (a, b) in enumerate(got):
        assert b - a >= durs[i] + 0.5 - 1e-6, '第 %d 段仍然装不下配音' % i
    assert st['grew'] == 37
    assert cursor < src, '测试前提：源片有足够空余'


def test_grouped_spans_fit_per_sentence():
    """43 段画面 ↔ 37 句解说（1 句可占多镜头）：按 narr_map 归组补长，
    每句的总画面长度都必须装得下它自己的配音。

    这是第一次修复漏掉的坑：按「第 i 段配第 i 句」硬套时，
    37 句里仍有 12 句被切——补到了别的句子身上。
    """
    groups = []
    for i in range(37):
        groups += [i, i] if i < 6 else [i]
    assert len(groups) == 43
    spans = [(i * 5.0, i * 5.0 + 4.0) for i in range(43)]
    durs = {i: 7.0 for i in range(37)}
    got, st = V._fit_spans_to_voice(spans, durs, src_dur=1000.0, tail=0.5, groups=groups)
    for g in range(37):
        total = sum(got[j][1] - got[j][0] for j, gg in enumerate(groups) if gg == g)
        assert total >= 7.5 - 1e-6, '第 %d 句仍装不下：%.2fs' % (g, total)
    # 结构不变：段数一致、保序不重叠
    assert len(got) == 43
    for i in range(42):
        assert got[i][1] <= got[i + 1][0] + 1e-6


def test_voice_tail_configurable():
    assert N._voice_tail({}) == 0.5
    assert N._voice_tail({'voiceTail': 1.2}) == 1.2
    assert N._voice_tail({'voiceTail': 99}) == 2.0
    assert N._voice_tail({'voiceTail': -3}) == 0.0


def test_display_factor_honours_tail():
    """微变速也要按同一口径留白：D = v_len + tail（在 ±15% 带内时）。"""
    # 配音 9.6s + 留白 0.5s = 10.1s，落在 8.5~11.5 带内 → f = 1.01
    assert abs(V._display_factor(10.0, 9.6, tail=0.5) - 1.01) < 1e-9
    # 老口径 0.35 → D = 9.95 → f = 0.995（其余链路不受影响）
    assert abs(V._display_factor(10.0, 9.6) - 0.995) < 1e-9
    # 超出带外仍按 ±15% 夹紧，不许无限变速
    assert abs(V._display_factor(10.0, 4.0, tail=0.5) - 0.85) < 1e-9
    assert abs(V._display_factor(10.0, 9.9, tail=2.0) - 1.15) < 1e-9

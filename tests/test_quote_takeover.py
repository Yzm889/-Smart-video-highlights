# -*- coding: utf-8 -*-
"""v1.4.24 回归钉：解说稿里引用台词的那句，整句让位给原声。

用户 2026-11-09（选 A 方案）：「这些解说词就是从台词里挑出来的……这里我希望
别读稿而是剪辑这个对应画面代替解说」。

难点不在剪辑，在**找到是哪一句**：稿子是意译，官方字幕是另一版译法，
「我不想交朋友」对「但我不是在找同伴」实词重合 0，字面匹配完全失效。
实测三级策略：
  · 字面（SequenceMatcher）能中「我死了，柯蒂斯」对「我是死了，科提斯」这类；
  · 语义：8B 模型「从这些里挑一个」会硬选（12 条都不对也挑）；
    改成**逐条判 0/1 + 写死判据**后校准正常——正确区间命中 905.4s
    「但我不是在找同伴」，错误区间返回空。
硬约束：只能引用片内的台词。用户举例的第二句在 20:45，而素材只有 20 分钟，
必须拒绝（宁可念稿也不能播不存在的画面）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import movie_narrator as MN  # noqa: E402


def _asr(*rows):
    return [{'start': a, 'end': b, 'text': t} for a, b, t in rows]


def test_only_dialogue_quotes_are_picked():
    """称谓/术语的引号不能当台词：「惩罚者」的制服、「厨房爱尔兰帮」的幸存者。"""
    assert MN._quote_texts('弗兰克回了一句：“我不想交朋友。”起身走人。') == ['我不想交朋友']
    assert MN._quote_texts('他把当“惩罚者”的背心烧了。') == []
    assert MN._quote_texts('“厨房爱尔兰帮”最后一个幸存者准备逃回都柏林。') == []
    assert MN._quote_texts('他低声说：“我死了，你没听说吗？”') == ['我死了，你没听说吗']


def test_literal_match_hits_same_line():
    """官方译法与人名写法不同，字面仍能命中。"""
    asr = _asr((1245.2, 1247.0, '-我是死了，科提斯，你没听说吗？'),
               (1247.0, 1248.0, '-胡说'))
    hit = MN._pick_quote_span('我死了，柯蒂斯，你没听说吗', asr, near=1240.0, src_dur=1300)
    assert hit and hit[3] == 'literal'
    assert hit[2].startswith('-我是死了')


def test_quote_outside_source_is_rejected():
    """台词在 20:45，素材只有 20 分钟 → 必须拒绝，不能引用不存在的画面。"""
    asr = _asr((1245.2, 1247.0, '-我是死了，科提斯，你没听说吗？'))
    assert MN._pick_quote_span('我死了，柯蒂斯，你没听说吗', asr, near=1240.0,
                               src_dur=1200.0) is None


def test_no_match_returns_none_without_model(monkeypatch):
    """本地模型不可用且字面不中时，必须老实返回 None（照常念稿）。"""
    monkeypatch.setattr(MN, '_local_model_available', lambda: False)
    asr = _asr((905.4, 907.2, '但我不是在找同伴'))
    assert MN._pick_quote_span('我不想交朋友', asr, near=900.0, src_dur=1200) is None


def test_detect_skips_lines_without_dialogue():
    narr = ['注意看，这个男人叫弗兰克。', '他把“惩罚者”的背心烧了。']
    segs = [(0.0, 5.0), (5.0, 10.0)]
    slots = MN._detect_quote_slots(narr, segs, _asr((1.0, 2.0, '随便')), progress={})
    assert slots == {}


def test_tts_skips_quote_slots(tmp_path, monkeypatch):
    """让位段不配音（否则观众会同时听到解说和原声）。"""
    narr = ['第一句解说', '弗兰克回了一句：“我不想交朋友。”', '第三句解说']
    spoken = []

    def fake_speak(text, out):
        spoken.append(text)
        open(out, 'wb').write(b'RIFF' + b'0' * 400)
        return True, 'fake', out

    monkeypatch.setattr(MN, 'local_tts_speak', fake_speak)
    monkeypatch.setattr(MN, 'edge_tts_available', lambda: False)
    monkeypatch.setattr(MN, 'load_ai_config', lambda: {})
    res = MN._generate_all_tts(narr, str(tmp_path), skip_idx={1})
    assert [i for i, _ in res] == [0, 2]
    assert all('我不想交朋友' not in t for t in spoken)

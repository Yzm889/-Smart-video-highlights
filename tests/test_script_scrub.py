# -*- coding: utf-8 -*-
"""稿件「去镜头化」后处理的回归测试。

背景（实测，RTX 3060 12GB + qwen3:8b，22 分钟片）：
  8. 警察仔细检查事故现场，每一处细节都不放过，眼神专注，仿佛遗漏一个细节就可能改变一切。
 10. 男子在停车场手持红色油桶，步伐坚定，神情似乎藏着某种目的，令人不禁猜测他的意图。
 25. 警车在乡村公路上疾驰，远处山丘隐约可见，无线电里传来「Highway 18 eastbound,
     GTA ADW 217-243. Advise extreme caution.」
后处理必须解决三件事：猜测定语、抄英文台词、同一事件重复。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui_server as _W  # noqa: F401  先解除循环依赖
import movie_narrator as M
from tts_engines import _fix_unclosed_tags, strip_tts_markup


def _beats(*texts):
    return [{'text': t, 'keywords': [], 'importance': 'advance'} for t in texts]


def test_drop_speculation_removes_guess_clause():
    t = '警察仔细检查事故现场，每一处细节都不放过，眼神专注，仿佛遗漏一个细节就可能改变一切。'
    out = M._drop_speculation(t)
    assert '仿佛' not in out
    assert out.startswith('警察仔细检查事故现场')
    assert out.endswith('。')


def test_drop_speculation_keeps_text_without_clues():
    t = '警局接到通报，两名嫌犯在公路开枪击伤警员，正沿18号公路逃窜。'
    assert M._drop_speculation(t) == t


def test_drop_speculation_never_empties_sentence():
    # 两个分句时不动它，避免把一句话删空
    t = '他站在门口，仿佛在等人。'
    assert M._drop_speculation(t) == t


def test_strip_foreign_removes_radio_code_whole_clause():
    t = '警车在乡村公路上疾驰，远处山丘隐约可见，无线电里传来「Highway 18 eastbound, GTA ADW 217-243. Advise extreme caution.」'
    out = M._strip_foreign(t)
    assert 'Highway' not in out and 'ADW' not in out
    # 不能留下「无线电里传来」这种残句
    assert '传来' not in out
    assert out == '警车在乡村公路上疾驰，远处山丘隐约可见。'


def test_strip_foreign_removes_inline_english():
    t = '男子走向加油站，却看到牌子上写着「NO GAS」，他皱了皱眉。'
    out = M._strip_foreign(t)
    assert 'NO GAS' not in out
    assert '写着' not in out
    assert '他皱了皱眉' in out


def test_strip_foreign_keeps_english_narration():
    # 英文片的英文稿不能被清空
    t = 'Shane walks out of the hospital, and nothing has changed.'
    assert M._strip_foreign(t) == t


def test_strip_foreign_keeps_plain_chinese():
    t = '洛莉当着小儿子卡尔的面质问肖恩，夫妻矛盾彻底摆上台面。'
    assert M._strip_foreign(t) == t


def test_beats_scrub_pipeline():
    beats = _beats(
        '警察仔细检查事故现场，眼神专注，仿佛遗漏一个细节就可能改变一切。',
        '警车在乡村公路上疾驰，远处山丘隐约可见，无线电里传来「Highway 18 eastbound, Advise extreme caution.」',
        '他低声说「你看着我，保持清醒」，手一直没松开。',
    )
    out = M._beats_scrub(beats)
    texts = [b['text'] for b in out]
    assert len(texts) == 3
    assert not any('仿佛' in t for t in texts)
    assert not any('Highway' in t for t in texts)
    # 英文台词整句删掉，但不能连带把上下文一起删掉
    assert '警车在乡村公路上疾驰' in texts[1]
    # 中文短引用是转述的一部分，不该被删
    assert any('保持清醒' in t for t in texts)


def test_beats_scrub_drops_all_english_beat_in_chinese_script():
    beats = _beats(
        '肖恩在布道时拿妻子洛莉开涮，洛莉脸色难看。',
        '警局接到通报，两名嫌犯在公路上开枪击伤警员。',
        '「No gas, no gas, no gas.」',
    )
    out = M._beats_scrub(beats)
    assert len(out) == 2
    assert all('No gas' not in b['text'] for b in out)


def test_beats_scrub_keeps_english_script():
    # 英文稿：不能因为「中文节拍不足」就把整篇删空
    beats = _beats('Shane leaves the hospital, but nothing has changed.',
                   'Leon keeps asking him to stay focused.')
    out = M._beats_scrub(beats)
    assert len(out) == 2


def test_orphan_close_tag_removed():
    # 上半句被删掉后只剩 {/情绪}
    t = '他重复着身份，眼神透露出对真相的执着。{/情绪}{停顿:0.3}'
    fixed = _fix_unclosed_tags(t)
    assert '{/情绪}' not in fixed
    assert '{停顿:0.3}' in fixed


def test_scrub_output_has_no_visible_markup_in_subtitle():
    beats = _beats(
        '洛莉当着儿子的面质问肖恩，{情绪:紧张}气氛僵到极点。{/情绪}{停顿:0.3}',
    )
    out = M._beats_scrub(beats)
    sub = strip_tts_markup(out[0]['text'])
    assert '{' not in sub and '}' not in sub
    assert '气氛僵到极点' in sub

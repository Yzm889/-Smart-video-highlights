# -*- coding: utf-8 -*-
"""v1.4.90 三项小升级回归钉（全部来自实测数据，不是拍脑袋）。

① 译名归一防互换：e2e 实锤「弗兰克↔彼特」双向互换把本名/化名的叙事抹掉
   （稿子「彼特·卡索还活着」被换成「弗兰克·卡索还活着」）——双向映射必须丢弃。
② 验证器议论句分级：e2e 的 wrong 里约半数是议论/抒情句苛判——钩子/过渡句
   不描述具体事件，画面是引子就算 related。
③ 对齐率透传前端：验证闭环的最后一公里——主人不用打开 json 看报告。
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402,F401
import movie_narrator as M  # noqa: E402


def test_name_unify_drops_bidirectional_swap(monkeypatch):
    """双向映射（弗兰克↔彼特）=本名/化名，必须丢弃；单向映射（柯蒂斯→科提斯）保留。"""
    resp = json.dumps({'mapping': {'柯蒂斯': '科提斯', '弗兰克': '彼特', '彼特': '弗兰克'}})
    monkeypatch.setattr(M, 'local_llm_chat', lambda *a, **k: resp)
    texts = ['弗兰克化名彼特，与柯蒂斯对话。']
    asr = [{'text': '科提斯：彼特，你这家伙。'}, {'text': '弗兰克，站起来。'}]
    new_texts, mapping = M._unify_names_with_dialog(texts, asr, movie_name='测试')
    assert '弗兰克' not in mapping and '彼特' not in mapping, \
        '双向映射必须被丢弃: %s' % mapping
    assert mapping.get('柯蒂斯') == '科提斯', '单向译名映射保留: %s' % mapping
    assert new_texts[0] == '彼特化名弗兰克，与科提斯对话。' or '柯蒂斯' not in new_texts[0], new_texts


def test_name_unify_drops_source_present_and_substring(monkeypatch):
    """e2e-1490 实锤两条新守卫：源译名在字幕也出现→不映射（弗兰克两边都有）；
    目标包含源（卡斯帝欧尼→彼特卡斯帝欧尼）→叠字灾难，丢弃。"""
    resp = json.dumps({'mapping': {'弗兰克': '彼特', '卡斯帝欧尼': '彼特卡斯帝欧尼',
                                   '柯蒂斯': '科提斯'}})
    monkeypatch.setattr(M, 'local_llm_chat', lambda *a, **k: resp)
    texts = ['弗兰克·卡斯帝欧尼与柯蒂斯对话。']
    asr = [{'text': '科提斯：彼特卡斯帝欧尼，你这家伙。'},
           {'text': '弗兰克，站起来。彼特·卡斯帝欧尼还活着。'}]
    new_texts, mapping = M._unify_names_with_dialog(texts, asr, movie_name='测试')
    assert '弗兰克' not in mapping, '源在字幕出现→必须丢弃: %s' % mapping
    assert '卡斯帝欧尼' not in mapping, '子串包含→必须丢弃: %s' % mapping
    assert mapping.get('柯蒂斯') == '科提斯'
    assert new_texts[0].count('彼特') == 0, new_texts


def test_name_unify_swap_guard_source_pin():
    """源码钉：防互换守卫必须存在。"""
    src = open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8').read()
    i = src.index('def _unify_names_with_dialog')
    assert '_swap' in src[i:i + 3000], '防互换守卫必须存在'
    assert '本名/化名' in src[i:i + 3500]


def test_verify_prompt_notes_hook_sentences(monkeypatch):
    """钩子/过渡句：判 prompt 附带「引子画面算 related」说明，减少议论句苛判。"""
    holder = {}

    def fake_text(prompt, system='', **kw):
        holder['prompt'] = prompt
        return '{"verdict": "related", "why": "引子画面"}'

    def fake_chat(*a, **k):
        return '两人在屋顶眺望城市。'
    monkeypatch.setattr(S, 'vlm_text', fake_text)
    monkeypatch.setattr(S, 'vlm_chat', fake_chat)
    monkeypatch.setattr(S, 'ffmpeg_run', lambda args, *a, **k: (
        open(args[-1], 'wb').write(b'x' * 2000), 0, b'', b''))
    segs = [{'index': 0, 'out_start': 0.0, 'out_end': 6.0, 'duration': 5.0,
             'text': '所以前二十分钟，是一个复仇完成后被掏空的人。'}]
    report, n = M._verify_final_alignment('no.mp4', segs)
    assert '开场钩子/过渡句' in holder['prompt'], '议论句提示必须进 prompt'
    assert report[0]['verdict'] == 'related'


def test_verify_plain_concrete_sentence_has_no_note(monkeypatch):
    """具象句不附带议论提示（保持严格判断）。"""
    holder = {}

    def fake_text(prompt, system='', **kw):
        holder['prompt'] = prompt
        return '{"verdict": "match", "why": "同一动作"}'

    def fake_chat(*a, **k):
        return '一名男子挥锤砸墙。'
    monkeypatch.setattr(S, 'vlm_text', fake_text)
    monkeypatch.setattr(S, 'vlm_chat', fake_chat)
    monkeypatch.setattr(S, 'ffmpeg_run', lambda args, *a, **k: (
        open(args[-1], 'wb').write(b'x' * 2000), 0, b'', b''))
    segs = [{'index': 0, 'out_start': 0.0, 'out_end': 6.0, 'duration': 5.0,
             'text': '弗兰克抡起大锤砸向水泥墙。'}]
    M._verify_final_alignment('no.mp4', segs)
    assert '开场钩子' not in holder['prompt']


def test_handler_passes_align_summary():
    """后端任务状态透传 align_summary（前端显示的数据通道）。"""
    src = open(os.path.join(ROOT, 'handler.py'), encoding='utf-8').read()
    assert "p.get('align_summary')" in src
    js = open(os.path.join(ROOT, 'static', 'app.js'), encoding='utf-8').read()
    assert 'align_summary' in js and '对齐验证' in js

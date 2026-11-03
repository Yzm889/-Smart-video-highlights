# -*- coding: utf-8 -*-
"""解说稿「占位符」与坏配音标记的拦截回归。

真机证据（22 分钟全片端到端）：
  · 润色提示词里写了字面样例 `"第%d段润色后"`，模型把它当台词返回，
    成片第 4-7 段变成 `{慢}第4段润色后{/慢}` —— 配音会真的念出这句话；
  · 模型还给单点标记 {停顿:x} 自行补了闭合标签（{停顿:0.3}{/停顿}）并重复叠放，
    导致念稿出现异常停顿。
两者都属于「提示词样例被当成内容」这一类 bug，必须双向防：
  ① 提示词里不再出现可照抄的字面样例；
  ② 结果入口做拦截/规范化，避免以后任何模型复现同类问题。
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as _W  # noqa: F401  必须先导入宿主，解除 video_render/movie_narrator 循环依赖
import movie_narrator as M
import tts_engines as T


# ---------- ① 占位符识别 ----------

@pytest.mark.parametrize('text', [
    '第4段润色后',
    '第 12 段润色后',
    '第0段',
    '...',
    '…',
    '<润色结果>',
    '【】',
    '',
    '   ',
])
def test_placeholder_detected(text):
    assert M._looks_like_placeholder(text), '未识别出占位符：%r' % text


@pytest.mark.parametrize('text', [
    '肖恩拿洛莉开玩笑，说她从不关灯，没想到这成了家庭矛盾的导火索。',
    '警局突然通报，两名白人嫌犯在85号州际公路上开枪，情况紧急。',
    '第4段他冲进房间，所有人都愣住了。',   # 有「第4段」但确实是一句台词
])
def test_real_sentence_kept(text):
    assert not M._looks_like_placeholder(text), '误杀真台词：%r' % text


def test_prompts_have_no_literal_refine_sample():
    """提示词里不允许再出现可被照抄的「第N段润色后」样例。"""
    src = open(os.path.join(ROOT, 'movie_narrator.py'), encoding='utf-8').read()
    # 只看提示词字符串里的样例，不看本测试文件/注释
    literal = re.findall(r'"润色"\s*:\s*\[\s*"第', src)
    assert not literal, '提示词里仍有可照抄的润色样例：%r' % literal


# ---------- ② 配音标记规范化 ----------

def test_pause_closing_tag_removed():
    assert T._normalize_markup('他说{停顿:0.3}{/停顿}走了') == '他说{停顿:0.3}走了'


def test_duplicate_pauses_merged():
    out = T._normalize_markup('{停顿:0.3}{停顿:0.6}{停顿:0.3}然后呢')
    assert out.count('{停顿') == 1, out
    assert '{停顿:0.6}' in out, '合并后应保留最长停顿：%r' % out


def test_duplicate_closers_collapsed():
    out = T._normalize_markup('{情绪:紧张}快走{/情绪}{/情绪}')
    assert out.count('{/情绪}') == 1, out


def test_pause_capped():
    out = T._normalize_markup('{停顿:3.0}{停顿:2.5}')
    m = re.search(r'\{停顿:([0-9.]+)\}', out)
    assert m and float(m.group(1)) <= 2.0, '停顿时长未封顶：%r' % out


def test_normal_markup_untouched():
    s = '{情绪:紧张}他冲了出去{/情绪}{停顿:0.5}没人拦得住'
    assert T._normalize_markup(s) == s


def test_enhance_applies_normalization():
    """_enhance_tts_markup 必须真的走到规范化（否则上面的修复不会生效）。"""
    out = T._enhance_tts_markup(['他说{停顿:0.3}{/停顿}走了。'])
    assert '{/停顿}' not in out[0], out

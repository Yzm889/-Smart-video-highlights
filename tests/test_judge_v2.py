# -*- coding: utf-8 -*-
"""源码钉：判官可信化 v2（BASELINE.md 陷阱⑦）。

主人 11-18 质疑「你的对齐分数准吗？标准错了分数就没意义」——
GLM 承认字幕自证污染但没修。本文件钉住三处修复 + 版本标记，
防止旧尺子（2 帧 / 读烧录字幕 / 48%）悄悄回来。
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')


def _src(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


def test_judge_crops_subtitle_band():
    """抽帧必须裁掉底部字幕带——否则判官读到的就是解说词自己（自证）。"""
    s = _src('movie_narrator.py')
    i = s.index('def _verify_final_alignment')
    body = s[i:i + 4000]
    assert 'crop=iw:trunc(ih*0.88/2)*2:0:0' in body, '抽帧必须裁掉底部 12% 字幕带'
    assert '字幕带' in body, '裁字幕的意图要写进注释防回退'


def test_judge_uses_five_frames_not_two():
    """2 帧孤立瞬间 → 5 帧均匀网格，覆盖整个输出窗口。"""
    s = _src('movie_narrator.py')
    body = s[s.index('def _verify_final_alignment'):][:4000]
    assert '_judge_fracs = (0.1, 0.3, 0.5, 0.7, 0.9)' in body, '必须 5 帧均匀网格'
    assert "('m', 0.5), ('l', 0.8)" not in body, '旧的 2 帧写法不许回来'


def test_judge_prompt_ignores_on_screen_text():
    """描述与判定 prompt 都要显式禁读画面文字，模型层再堵一道自证。"""
    s = _src('movie_narrator.py')
    body = s[s.index('def _verify_final_alignment'):][:5000]
    assert '忽略画面中的任何文字、字幕、台词、水印' in body, '描述 prompt 要禁读文字'
    assert '不要把文字/字幕当成画面证据' in body, '判定 prompt 要禁当证据'
    assert '已裁掉字幕带' in body, '判定 prompt 要声明帧的来源'


def test_judge_version_marks_new_ruler():
    """报告带 judge 版本——旧报告无此字段即坏尺子，compare 工具要能区分。"""
    s = _src('movie_narrator.py')
    assert "n['judge'] = 'v2-nosub-5f'" in s, '新尺子必须打版本标记'
    assert '一律作废' in s, '旧数字作废的边界要写进 docstring'

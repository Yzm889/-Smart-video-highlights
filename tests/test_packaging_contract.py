# -*- coding: utf-8 -*-
"""打包契约：faster_whisper 的数据文件必须进包（v1.4.8 真缺陷）。

真事故：faster_whisper 1.2.x 的 VAD 模型是数据文件
    faster_whisper/assets/silero_vad_v6.onnx
不在模块图里。FrameCut.spec 原来只有 datas=[('static','static')]，于是打包版运行时：

    NoSuchFile: [ONNXRuntimeError] : 3 : NO_SUCHFILE :
    Load model from ...\\_internal\\faster_whisper\\assets\\silero_vad_v6.onnx failed

后果：**打包版 Whisper 转写全废**。因为有字幕的用户走「字幕优先于 ASR」，
无感；而没字幕的用户等于没有台词轨 —— 而且只以 warning 形式出现，极易漏看。

这条钉死：spec 里必须收集 faster_whisper 的数据文件，且确实喂进 datas。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, 'FrameCut.spec')


def _spec():
    with open(SPEC, encoding='utf-8') as f:
        return f.read()


def test_spec_collects_faster_whisper_data():
    src = _spec()
    assert "collect_data_files('faster_whisper')" in src, \
        'spec 没收集 faster_whisper 数据文件（VAD 模型会缺失，ASR 全废）'
    assert 'import' in src.split("collect_data_files('faster_whisper')")[0], \
        'collect_data_files 必须在 spec 顶部导入'


def test_collected_datas_actually_fed_to_analysis():
    src = _spec()
    m = re.search(r'(?ms)^\s*datas\s*=(.*?)^\s*hiddenimports', src)
    assert m, '找不到 datas 定义'
    block = m.group(1)
    assert '_whisper_datas' in block or 'collect_data_files' in block, \
        '收集到的 faster_whisper 数据没进 datas：%r' % block[:200]


def test_static_still_packaged():
    """别忘了前端：static 必须还在 datas 里。"""
    assert "('static', 'static')" in _spec()


def test_faster_whisper_assets_known_files():
    """本机装了 faster_whisper 时，顺带确认 assets 里的 VAD 模型确实存在。"""
    try:
        import faster_whisper
    except Exception:
        return                      # CI 未装则跳过（spec 契约已由上面三条覆盖）
    assets = os.path.join(os.path.dirname(faster_whisper.__file__), 'assets')
    if not os.path.isdir(assets):
        return
    names = os.listdir(assets)
    assert any(n.endswith('.onnx') for n in names), \
        'faster_whisper assets 里没有 .onnx（版本变了？检查打包是否需要改路径）：%r' % names

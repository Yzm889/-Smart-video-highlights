# -*- coding: utf-8 -*-
"""外挂字幕编码回归钉（2026-11-08 主人《惩罚者》S01E01 官方字幕实测）。

背景：官方简体字幕是 GB18030 编码，而 parse_subtitle_file 原先硬编码
`encoding='utf-8-sig', errors='ignore'`，中文被整段吞掉：
  - 首条「-准备好了？ -好了」变成乱码「-׼ˣ -」
  - 593 条只解析出 557 条（含中文的条目因文本空被静默丢弃）
字幕是「按台词找画面」的命脉，丢条等于画面抽样落点全歪。

本文件钉死：UTF-8 / UTF-8-BOM / GB18030 / UTF-16 四种编码都必须原样还原。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_providers as A  # noqa: E402


SRT = ('1\n'
       '00:00:15,015 --> 00:00:16,099\n'
       '-准备好了？\n'
       '-好了\n'
       '\n'
       '2\n'
       '00:00:26,068 --> 00:00:27,069\n'
       '弗兰克·卡索\n')


def _parse(tmp_path, data):
    p = tmp_path / 'sub.srt'
    p.write_bytes(data)
    return A.parse_subtitle_file(str(p))


def test_utf8_subtitle_unchanged(tmp_path):
    segs = _parse(tmp_path, SRT.encode('utf-8'))
    assert len(segs) == 2
    assert segs[0]['start'] == 15.015
    assert segs[0]['text'] == '-准备好了？ -好了'
    assert segs[1]['text'] == '弗兰克·卡索'


def test_utf8_bom_subtitle_unchanged(tmp_path):
    segs = _parse(tmp_path, SRT.encode('utf-8-sig'))
    assert len(segs) == 2
    assert segs[0]['text'] == '-准备好了？ -好了'


def test_gb18030_subtitle_not_garbled_or_dropped(tmp_path):
    """主人实测坑：GB18030 字幕不得乱码、不得丢条。"""
    segs = _parse(tmp_path, SRT.encode('gb18030'))
    assert len(segs) == 2, 'GB18030 字幕被丢条：%r' % segs
    assert segs[0]['text'] == '-准备好了？ -好了', '中文被吞成乱码：%r' % segs[0]['text']
    assert segs[1]['text'] == '弗兰克·卡索'
    assert '\ufffd' not in segs[0]['text']


def test_utf16_bom_subtitle(tmp_path):
    segs = _parse(tmp_path, SRT.encode('utf-16'))
    assert len(segs) == 2
    assert segs[0]['text'] == '-准备好了？ -好了'


def test_binary_garbage_returns_empty_not_raise(tmp_path):
    """兜底不能抛异常，解析不了返回 []。"""
    assert _parse(tmp_path, b'\x00\x01\x02not a subtitle') == []

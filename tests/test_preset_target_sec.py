# -*- coding: utf-8 -*-
"""预设模板 targetSec 单位回归（task：D 预设时长单位错乱）。

为什么需要它：内置模板把「1/3/5 分钟」误写成 targetSec:'1'/'3'/'5'，
而前端下拉 narTargetSec 的单位是「秒」（选项 0/60/180/300/600）。
应用模板会把目标时长设成 1/3/5 秒 → 成片被极限浓缩成几秒。
本文件锁死：每个预设的 targetSec 必须是 'auto' 或下拉里的合法秒值，
杜绝再出现「应用模板 → 成片几秒」这类坑。
"""
import os
import re


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        return f.read()


def _dropdown_secs():
    html = _read(os.path.join(_ROOT, 'static', 'index.html'))
    # 仅取 narTargetSec 这个 select 内部 option 的 value（数字）；option 可能带
    # selected 等属性（默认档），正则不能要求 value 后紧跟 '>'
    m = re.search(r'<select id="narTargetSec">(.*?)</select>', html, re.S)
    assert m, 'index.html 找不到 narTargetSec 下拉'
    return set(re.findall(r'<option value="(\d+)"[^>]*>', m.group(1)))


def _preset_target_secs():
    js = _read(os.path.join(_ROOT, 'static', 'app.js'))
    # 只匹配字符串字面量 targetSec:'...'（排除 parseFloat(...) 等非字面量写法）
    return re.findall(r"targetSec:\s*'([^']+)'", js)


def test_preset_target_sec_within_dropdown():
    valid = _dropdown_secs() | {'auto'}
    for val in _preset_target_secs():
        assert val in valid, (
            "预设 targetSec='%s' 不在下拉合法值 %s 内——会被当成秒导致成片极限浓缩"
            % (val, sorted(valid))
        )


def test_dropdown_has_minute_options():
    secs = _dropdown_secs()
    # 至少要有 1/3/5 分钟档，模板才可能映射到正确秒数
    for needed in ('60', '180', '300'):
        assert needed in secs, '下拉缺少 %s 秒档（约 %s 分钟）' % (needed, int(needed) // 60)

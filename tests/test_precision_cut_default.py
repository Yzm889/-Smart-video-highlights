# -*- coding: utf-8 -*-
"""回归：剧情精剪必须是默认行为（2026-11-06 主人反馈「不要的片段一直在」）。

背景：留/剪机制（按剧情重要度保高光、真剪低重要度段）早已完整落地并有测试，
但它有个总开关「目标成片时长」，默认「自动=覆盖全片」→ 机制等于从未生效，
解说被摊满全片、低重要度 footage 全保留——正是主人抱怨的「没剪掉、对不上」。
本文件钉死：下拉默认选「约 10 分钟（剧情精剪）」，旗舰模板「电影解说（经典）」
同向；「极简旁白（保留全片）」模板必须保持 auto（它的名字就是承诺）。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def test_target_sec_defaults_to_precision_cut():
    html = _read(os.path.join(ROOT, 'static', 'index.html'))
    m = re.search(r'<select id="narTargetSec">(.*?)</select>', html, re.S)
    assert m, 'index.html 找不到 narTargetSec 下拉'
    sel = re.findall(r'<option value="(\d+)" selected>', m.group(1))
    assert sel == ['600'], '目标时长默认必须选中「约 10 分钟（剧情精剪）」，实际 selected=%s' % sel
    # 覆盖全片选项必须保留（显式选择权不能丢），且不再默认选中
    assert '覆盖全片' in m.group(1), '「覆盖全片」选项被删了——想保留全片的用户没有退路'


def test_flagship_movie_template_cuts_by_default():
    js = _read(os.path.join(ROOT, 'static', 'app.js'))
    # 模板对象里有嵌套的 tts:{...},，按「下一个顶层模板名或列表结尾」取整块
    i = js.index("'⭐ 电影解说（经典）'")
    nxt = js.find("'⭐ ", i + 10)
    block = js[i:nxt if nxt > 0 else len(js)]
    assert "targetSec: '600'" in block, '旗舰模板「电影解说（经典）」必须默认剧情精剪（600s）'
    # 保留全片模板保持 auto：名字即承诺
    j = js.index("'⭐ 极简旁白（保留全片）'")
    nxt2 = js.find("'⭐ ", j + 10)
    block2 = js[j:nxt2 if nxt2 > 0 else len(js)]
    assert "targetSec: 'auto'" in block2, '「极简旁白（保留全片）」必须保持 auto，否则名不副实'

# -*- coding: utf-8 -*-
"""字幕「烧进画面 vs 另出 .srt」的开关（2026-11-05）。

用户原话：希望可以「不做解说字幕的硬烧录，而是先导出已经剪辑好的视频，
再给用户一套 srt 字幕文件，让用户可以在其他软件调整解说字幕的位置」。

这不是"少做一步"，而是把字幕控制权交回去：
  · 硬烧之后想改字幕位置/字体只能整片重渲染（几分钟起步）；
  · 很多人的实际工作流是「先出片 → 在剪辑软件里统一调字幕样式」；
  · 烧录还会把字幕永久压在画面上，想改文案就只能回源。

设计约束（本文件钉住的就是这几条）：
  ① **默认必须是烧录**，且默认路径的滤镜串与改动前逐字符一致（老用户零感知）；
  ② 不烧录时 .srt 必须照常生成——它就是唯一的字幕载体，不生成等于功能缺失；
  ③ 「不烧录 + 不调色 + 不缩放」时不能拼出 `-vf ''`（ffmpeg 会直接报错）；
  ④ 前端只有明确选 '0' 才关，缺元素/异值一律按烧录（避免配置缺失变成静默变更）。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VR_PATH = os.path.join(ROOT, 'video_render.py')
JS_PATH = os.path.join(ROOT, 'static', 'app.js')
HTML_PATH = os.path.join(ROOT, 'static', 'index.html')


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def _burn_enabled():
    """从 video_render.py 里抠出 _burn_enabled 单独求值。

    不能直接 `import video_render`：该模块顶层要 import webui_server，
    而 webui_server 又反过来从 video_render 导入常量（循环导入，靠宿主
    回注解决）。单测里绕开更简单，也更能保证"只测这一个纯函数"。
    """
    src = _read(VR_PATH)
    i = src.index('def _burn_enabled(params):')
    j = src.index('\ndef ', i + 10)
    ns = {}
    exec(src[i:j], ns)
    return ns['_burn_enabled']


def test_default_is_burn():
    """默认必须烧录 —— 老用户重跑同一套参数，成片必须与改动前一致。"""
    b = _burn_enabled()
    assert b({}) is True, '空 params 必须默认烧录'
    assert b(None) is True, 'params 为 None 时必须默认烧录'
    assert b({'subtitle': None}) is True
    assert b({'subtitle': {}}) is True
    assert b({'subtitle': {'burn': True}}) is True


def test_only_strict_false_disables():
    """只有严格 False 才关。

    写成 `if not sub.get('burn')` 会把 0 / '' / None 全当成"关闭"——
    一旦某个调用方传了个空值，全站字幕会静默消失，且没有任何报错。
    """
    b = _burn_enabled()
    assert b({'subtitle': {'burn': False}}) is False
    for v in (0, '0', '', None, [], {}):
        assert b({'subtitle': {'burn': v}}) is True, (
            'burn=%r 被当成了关闭，应当只有严格 False 才关（否则配置缺失会让字幕静默消失）' % (v,))


def test_srt_always_generated_regardless_of_burn():
    """两条渲染路径都必须调用 _build_narr_srt —— 不烧录时它就是唯一字幕载体。"""
    src = _read(VR_PATH)
    # 单遍管线（含 no_cut 分支）与两遍管线各至少一次
    calls = len(re.findall(r'_build_narr_srt\(', src))
    assert calls >= 3, '字幕构建调用点少于 3 处（期望：no_cut / 单遍 / 两遍 各一次）'
    # 关键：不烧录分支里也不能因为跳过 subtitles 滤镜而连带跳过 srt 生成
    i = src.index("if _burn_enabled(params):")
    before = src[max(0, i - 400):i]
    assert '_build_narr_srt' in before, (
        '两遍管线里 _build_narr_srt 必须在 `if _burn_enabled` 之外调用，'
        '否则选不烧录时 srt 不会生成，用户拿不到任何字幕')


def test_no_empty_vf_string():
    """「不烧录 + 无调色 + 无缩放」不得拼出 -vf ''（ffmpeg 会报参数错误）。"""
    src = _read(VR_PATH)
    # 两遍管线：空滤镜时走 -c:v copy 分支
    assert "if not vf_parts:" in src, '两遍管线缺少空滤镜保护'
    # 单遍管线：空滤镜时走 copy 分支
    assert "if vf:" in src, '单遍管线缺少空滤镜保护'
    # join 进 -vf 的地方，其所在函数里必须有对应的空值分支
    # （两种写法都算：`if vf:` 走有滤镜分支 / `if not vf_parts:` 走 copy 分支）
    for m in re.finditer(r"'-vf',\s*','\.join\((vf|vf_parts)\)", src):
        var = m.group(1)
        assert ('if %s:' % var) in src or ('if not %s:' % var) in src, (
            '变量 %s 拼进 -vf 前没有空值检查（空列表会拼出 -vf \'\'，ffmpeg 直接报错）' % var)


def test_frontend_toggle_defaults_to_burn():
    """前端只有明确选 '0' 才关；缺元素时按烧录。"""
    js = _read(JS_PATH)
    i = js.index('function getSubtitleStyle')
    body = js[i:i + 900]
    assert '_burnSel' in body and "value !== '0'" in body, (
        'getSubtitleStyle 未按「只有 0 才关」实现，缺元素时可能静默关闭烧录')
    assert 'burn:' in body, 'subtitles 参数里缺少 burn 字段'


def test_frontend_has_srt_download_entry():
    """不烧录时必须给 srt 下载入口，否则用户拿不到字幕。"""
    html = _read(HTML_PATH)
    js = _read(JS_PATH)
    assert 'id="narSrtDl"' in html, '结果区缺少 srt 下载链接'
    assert 'id="narSrtTip"' in html, '缺少「本次未烧录字幕」的说明位'
    assert 'function fillNarSrt' in js, '缺少 fillNarSrt 回填函数'
    # 两处结果回填（首次完成 / 调整后重渲）都要调用，否则某条路径会没有链接
    assert js.count('fillNarSrt(') >= 3, (
        'fillNarSrt 至少要有「定义 + 两处结果回填」三次出现，当前 %d 次' % js.count('fillNarSrt('))


def test_burn_switch_has_ui():
    """开关必须有 UI 入口，且默认选中「烧进画面」。"""
    html = _read(HTML_PATH)
    assert 'id="subBurn"' in html, '缺少字幕交付方式下拉框'
    m = re.search(r'<select id="subBurn"[^>]*>(.*?)</select>', html, re.S)
    assert m, 'subBurn 不是 select'
    opt0 = re.search(r'<option value="1"[^>]*>', m.group(1))
    assert opt0 and 'selected' in opt0.group(0), '默认必须选中「烧进画面」(value=1)'

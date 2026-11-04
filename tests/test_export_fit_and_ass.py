# -*- coding: utf-8 -*-
"""竖屏满屏 + 字幕淡入淡出回归（分册 D / C）。

守四条契约：
  1. 竖屏目标默认满屏（无 pad 黑边）；横屏保持旧行为一字不变；
  2. 「有效画面区」只有一份计算，letterbox 时字幕 MarginV 被抬回画面内；
  3. ASS 的文案与时间轴与 SRT 逐条一致，只多 120ms 淡入淡出；
  4. 任何一步失败都能回退到纯 SRT，出片链路不中断。
"""
import re

import webui_server as S  # noqa: F401  必须先导入宿主，规避循环导入
import video_render as VR


def test_fit_defaults():
    assert S.effective_fit({'resolution': '1080x1920'}) == 'crop'
    assert S.effective_fit({'resolution': '1920x1080'}) == 'letterbox'
    assert S.effective_fit({'resolution': '1080x1920', 'fit': 'letterbox'}) == 'letterbox'
    assert S.effective_fit({'resolution': '1080x1920', 'fit': 'auto'}) == 'crop'
    assert S.effective_fit({}) == 'letterbox'


def test_build_video_filter_crop_has_no_black_bars():
    f = S.build_video_filter({'resolution': '1080x1920'})
    assert 'force_original_aspect_ratio=increase' in f
    assert 'crop=1080:1920' in f
    assert 'pad=' not in f


def test_build_video_filter_landscape_is_unchanged():
    f = S.build_video_filter({'resolution': '1280x720'})
    assert 'force_original_aspect_ratio=decrease' in f
    assert 'pad=1280:720' in f
    assert S.build_video_filter({'resolution': 'original'}) is None
    assert S.build_video_filter(None) is None


def test_effective_frame_rect_single_source_of_truth():
    lb = S.effective_frame_rect({'resolution': '1080x1920', 'fit': 'letterbox'})
    assert lb[0:2] == (1080, 1920)
    assert lb[5] > 500                    # 上下各约 656px 黑边
    crop = S.effective_frame_rect({'resolution': '1080x1920'})
    assert crop[2:] == (1080, 1920, 0, 0)   # 满屏，无黑边


def test_subtitle_margin_moves_back_into_picture():
    def margin(p):
        return int(re.search(r'MarginV=(\d+)', VR._build_subtitle_style(p)).group(1))
    lb = {'resolution': '1080x1920', 'fit': 'letterbox', 'subtitle': {'marginV': 50}}
    assert margin(lb) > 50, 'letterbox 时字幕应抬出黑边'
    crop = {'resolution': '1080x1920', 'subtitle': {'marginV': 50}}
    assert margin(crop) == 50, '满屏时不应改变用户设置'


def test_subtitle_font_scales_with_canvas_height():
    def fontsize(p):
        return int(re.search(r'FontSize=(\d+)', VR._build_subtitle_style(p)).group(1))
    base = {'subtitle': {'fontSize': 22}}
    tall = {'resolution': '1080x1920', 'subtitle': {'fontSize': 22}}
    assert fontsize(base) == 22
    assert fontsize(tall) > 40, '竖屏字号必须随画布高度放大'


def test_ass_keeps_text_and_timeline(tmp_path):
    segs = [(0.0, 1.5), (1.5, 5.0)]
    narr = ['第一句。', '第二句。']
    path = VR._build_narr_srt(segs, narr, str(tmp_path), None, {})
    assert path.endswith('.ass')
    srt = (tmp_path / 'narr.srt').read_text(encoding='utf-8')
    ass = (tmp_path / 'narr.ass').read_text(encoding='utf-8')
    assert '第一句。' in srt and '第一句。' in ass
    assert '第二句。' in srt and '第二句。' in ass
    assert ass.count('Dialogue:') == 2
    assert ass.count('{\\fad(120,120)}') == 2
    assert '[Script Info]' in ass and 'PlayResY:' in ass


def test_ass_falls_back_to_srt(tmp_path, monkeypatch):
    monkeypatch.setenv('FRAMECUT_ASS', '0')
    path = VR._build_narr_srt([(0.0, 2.0)], ['一句。'], str(tmp_path), None, {})
    assert path.endswith('.srt')
    path2 = VR._build_narr_srt([(0.0, 2.0)], ['一句。'], str(tmp_path), None,
                               {'subtitle': {'ass': False}})
    assert path2.endswith('.srt')

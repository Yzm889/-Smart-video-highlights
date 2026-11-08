# -*- coding: utf-8 -*-
"""分辨率别名回归钉（v1.4.12）。

真缺陷：桌面版「高级参数 → 分辨率」下拉传的是别名（`original` / `1080p` / `720p`），
而三处读取（`effective_fit` / `effective_frame_rect` / `build_video_filter`）都按
`WxH` 解析 —— 别名过不去，等于**选了跟没选一样**。

实测证据：4K 片源选 1080p 再合成，成片仍是 `3840x2160`、790.8MB，与「原始分辨率」
一字不差（还发现这次白跑了 10.8 分钟）。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402


def test_alias_normalized():
    assert S._res_str({'resolution': '1080p'}) == '1920x1080'
    assert S._res_str({'resolution': '720p'}) == '1280x720'
    assert S._res_str({'resolution': '4K'}) == '3840x2160'
    assert S._res_str({'resolution': '1920x1080'}) == '1920x1080'
    assert S._res_str({'resolution': 'original'}) == 'original'
    assert S._res_str({}) == ''
    assert S._res_str(None) == ''


def test_build_video_filter_accepts_alias():
    f = S.build_video_filter({'resolution': '1080p'})
    assert f and 'scale=1920:1080' in f, f
    f2 = S.build_video_filter({'resolution': '720p'})
    assert f2 and 'scale=1280:720' in f2, f2


def test_original_and_empty_produce_no_filter():
    assert S.build_video_filter({'resolution': 'original'}) is None
    assert S.build_video_filter({}) is None
    assert S.build_video_filter(None) is None


def test_wxh_still_works():
    f = S.build_video_filter({'resolution': '1920x1080'})
    assert f and 'scale=1920:1080' in f


def test_frame_rect_and_fit_use_alias():
    rect = S.effective_frame_rect({'resolution': '1080p'})
    assert rect and rect[0] == 1920 and rect[1] == 1080, rect
    assert S.effective_fit({'resolution': '1080p'}) == 'letterbox'
    # 竖屏别名（未来加）走同一套解析
    assert S.effective_fit({'resolution': '1080x1920'}) == 'crop'

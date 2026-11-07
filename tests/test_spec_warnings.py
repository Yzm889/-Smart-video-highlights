# -*- coding: utf-8 -*-
"""片源规格提示（4K / HDR）回归钉（v1.4.11）。

为什么不阻断：主人自己选的「原始分辨率」就该照做，但要知道代价 ——
整集 50 分钟 4K 实测合成 7.1 分钟、未调色时成片 791MB；1080p 能省约一半时间
和大量体积，而解说视频发平台本来也会被压到 1080p。
HDR 那句是说明「已自动转 SDR(bt709)」，免得以为画质被压了。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: F401,E402
import movie_narrator as M  # noqa: E402
import ffmpeg_utils as F  # noqa: E402

UHD = b'''  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x2160
'''
FHD = b'''  Stream #0:0: Video: h264 (High), yuv420p(tv, bt709, progressive), 1280x720
'''


def test_parse_size():
    assert F._parse_size(UHD) == (3840, 2160)
    assert F._parse_size(FHD) == (1280, 720)
    assert F._parse_size(b'') is None
    assert F._parse_size(b'Audio: aac, 48000 Hz, stereo') is None


def _spec(monkeypatch, size, hdr, resolution):
    monkeypatch.setattr(M, 'probe_size', lambda p: size)
    monkeypatch.setattr(M, 'probe_is_hdr', lambda p: hdr)
    return M._spec_warnings('x.mkv', {'resolution': resolution})


def test_uhd_original_warns(monkeypatch):
    msgs = _spec(monkeypatch, (3840, 2160), False, 'original')
    assert len(msgs) == 1 and '4K' in msgs[0] and '1080p' in msgs[0], msgs


def test_uhd_1080p_no_warn(monkeypatch):
    """已经选了 1080p 就别再唠叨。"""
    assert _spec(monkeypatch, (3840, 2160), False, '1080p') == []


def test_hdr_warns_even_at_1080p(monkeypatch):
    msgs = _spec(monkeypatch, (3840, 2160), True, '1080p')
    assert len(msgs) == 1 and 'HDR' in msgs[0] and 'bt709' in msgs[0], msgs


def test_sdr_1080p_silent(monkeypatch):
    assert _spec(monkeypatch, (1920, 1080), False, '1080p') == []


def test_probe_failure_is_silent(monkeypatch):
    def boom(*a, **k):
        raise OSError('ffmpeg missing')

    monkeypatch.setattr(M, 'probe_size', boom)
    monkeypatch.setattr(M, 'probe_is_hdr', boom)
    assert M._spec_warnings('x.mkv', {}) == []

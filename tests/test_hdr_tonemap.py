# -*- coding: utf-8 -*-
"""HDR → SDR 色调映射回归钉（v1.4.10）。

背景：4K 片源多为 HDR（PQ=smpte2084 / HLG=arib-std-b67）。原管线把 10bit HDR
直接解成 8bit 再编码，ffmpeg 又把输入的色彩标记原样带出去 → 成品是
「3840x2160 yuv420p + bt2020nc/bt2020/smpte2084」这种「贴了 HDR 标签只装 8bit」
的怪东西：识别 HDR 的播放器上偏暗发灰，8bit PQ 还容易出色带。
实测整集压测成片即如此（791MB / 8 分钟）。

这里钉四件事：
  1. 探测只认 PQ/HLG，bt2020 但 SDR 传输的不乱 tonemap
  2. HDR 片源走 make_video_clip 时必须插 tonemap 链并钉 bt709
  3. SDR 片源不能插（别给普通片子平白加一倍开销）
  4. tonemap 链跑挂了要自动降级重试，绝不能因此出不了片
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ffmpeg_utils as F  # noqa: E402
import webui_server as S  # noqa: E402


HDR_STDERR = b'''Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mkv':
  Duration: 00:50:20.67, start: 0.000000, bitrate: 14334 kb/s
  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084, progressive), 3840x2160
  Stream #0:1(eng): Audio: eac3, 48000 Hz, 5.1(side), fltp, 768 kb/s
'''

SDR_STDERR = b'''Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'y.mp4':
  Duration: 00:20:00.00, start: 0.000000, bitrate: 4187 kb/s
  Stream #0:0: Video: h264 (High), yuv420p(tv, bt709, progressive), 1280x720
'''

HLG_STDERR = b'''  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67), 1920x1080
'''

BT2020_SDR_STDERR = b'''  Stream #0:0: Video: h264, yuv420p(tv, bt2020nc/bt2020/bt709), 1280x720
'''


def test_hdr_detection():
    assert F._parse_is_hdr(HDR_STDERR) is True
    assert F._parse_is_hdr(HLG_STDERR) is True
    assert F._parse_is_hdr(SDR_STDERR) is False
    assert F._parse_is_hdr(BT2020_SDR_STDERR) is False, 'bt2020 但 SDR 传输不该当 HDR'
    assert F._parse_is_hdr(b'') is False
    assert F._parse_is_hdr(b'\xff\xfe garbage \x00') is False


def test_probe_missing_file_is_false(tmp_path):
    assert F.probe_is_hdr(str(tmp_path / 'nope.mp4')) is False


def test_tonemap_chain_shape():
    c = S.tonemap_chain()
    for token in ('zscale=t=linear', 'tonemap=tonemap=hable', 'zscale=t=bt709', 'format=yuv420p'):
        assert token in c, 'tonemap 链缺 %s：%r' % (token, c)
    assert 'bt2020' not in c and 'smpte2084' not in c, '输出必须是 SDR'


def _capture(monkeypatch, hdr, fail_first=False):
    """替换 ffmpeg_run，捕获命令，不真的编码。

    注意：video_encode_args 首次调用会跑一次 nvenc 探测（也是一条 ffmpeg 命令），
    必须先钉死探测结果，否则探测命令会被当成剪辑命令数进来。"""
    calls = []

    def fake(cmd, **kw):
        calls.append(list(cmd))
        # 第一次「剪辑」调用若模拟 tonemap 失败，返回 rc=1
        if fail_first and len([c for c in calls if '-vf' in c]) == 1:
            return 1, b'', b'No such filter: zscale'
        return 0, b'', b''

    monkeypatch.setattr(S, 'ffmpeg_run', fake)
    monkeypatch.setattr(S, 'probe_is_hdr', lambda p: hdr)
    monkeypatch.setattr(S, 'probe_duration', lambda p: 30.0)
    # tonemap_available 会抽真实片源一帧试跑，单测里直接钉死结果
    monkeypatch.setattr(S.ffmpeg_utils, 'tonemap_available', lambda p=None: True)
    monkeypatch.setitem(S.ffmpeg_utils._TM_PROBE, 'done', True)
    monkeypatch.setitem(S._ENC_CACHE, 'probe', False)      # 跳过 nvenc 实测探测
    # 让"产物存在且够大"检查通过
    real_exists, real_size = os.path.exists, os.path.getsize
    monkeypatch.setattr(S.os.path, 'exists', lambda p: True)
    monkeypatch.setattr(S.os.path, 'getsize', lambda p: 999999)
    try:
        S._TM_STATE['ok'] = True
        out = S.make_video_clip('src.mp4', 5.0, 'clip.mp4', 1280, 720, 25, start=1.0)
    finally:
        monkeypatch.setattr(S.os.path, 'exists', real_exists)
        monkeypatch.setattr(S.os.path, 'getsize', real_size)
    return [c for c in calls if '-vf' in c], out


def test_hdr_source_gets_tonemap_and_bt709(monkeypatch):
    calls, _out = _capture(monkeypatch, hdr=True)
    assert len(calls) == 1
    cmd = calls[0]
    vf = cmd[cmd.index('-vf') + 1]
    assert vf.startswith('zscale=t=linear'), 'HDR 片源没插 tonemap：%r' % vf
    assert 'scale=1280:720' in vf, 'tonemap 之后仍要缩放'
    for flag, val in (('-color_primaries', 'bt709'), ('-color_trc', 'bt709'), ('-colorspace', 'bt709')):
        assert flag in cmd and cmd[cmd.index(flag) + 1] == val, '缺输出色彩标记 %s' % flag


def test_sdr_source_not_tonemapped(monkeypatch):
    calls, _out = _capture(monkeypatch, hdr=False)
    cmd = calls[0]
    vf = cmd[cmd.index('-vf') + 1]
    assert vf.startswith('scale='), 'SDR 片源不该插 tonemap：%r' % vf
    assert '-color_primaries' not in cmd, 'SDR 片源不该改色彩标记'


def test_tonemap_failure_falls_back(monkeypatch):
    """zscale 不可用时要自动降级重试，并且全局关掉（不能每段都白跑一次）。"""
    calls, _out = _capture(monkeypatch, hdr=True, fail_first=True)
    assert len(calls) == 2, '失败后应重试一次（实际 %d 次）' % len(calls)
    vf2 = calls[1][calls[1].index('-vf') + 1]
    assert vf2.startswith('scale='), '重试时应该不带 tonemap：%r' % vf2
    assert S._TM_STATE['ok'] is False, '失败后应全局关闭 tonemap'

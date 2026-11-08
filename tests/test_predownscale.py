# -*- coding: utf-8 -*-
"""4K 片源「剪辑时前置降采样」回归钉（v1.4.13）。

背景：v1.4.12 修好分辨率下拉后，实测 4K HDR 整集选 1080p 仍要 8.8 分钟
（4K 原始 11.6 分钟，只快 24%）。原因是单遍合并的滤镜图
    trim → concat → tonemap → 字幕 → scale → 编码
整条链都在 3840x2160 上跑，只在最末端缩一次；最贵的 zscale/tonemap、
N 路 4K 帧复制、烧字幕、编码输入全是 4K 像素。

修法：每段 trim 之后立刻降到目标尺寸（只等比缩小，不 pad / 不 crop），
后面所有像素运算都在 1080p 上做；trim 在前，只有保留帧才吃这次缩放。
严格触发条件保证非 4K / 未选分辨率 / 探测失败时滤镜图逐字符不变。

这里钉四件事：
  1. 触发条件（4K + 更小目标才插；放大 / 原始 / 探测失败一律不插）
  2. 滤镜串形状（不能带逗号，否则会把 filter_complex 的分隔符打乱）
  3. 单遍合并每段 trim 链都必须插入，且位置在 trim 之后、concat 之前
  4. 无裁剪分支与普通 720p 片源零影响
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402  （必须先导入，video_render 有循环导入约束）
import video_render as V  # noqa: E402

PRE = 'force_divisible_by=2'


# ---------------------------------------------------------------------------
# 1. 触发条件
# ---------------------------------------------------------------------------

def test_trigger_only_for_4k_and_smaller_target():
    assert S.build_predownscale_filter({'resolution': '1080p'}, (3840, 2160))
    assert S.build_predownscale_filter({'resolution': '720p'}, (3840, 2160))
    assert S.build_predownscale_filter({'resolution': '1920x1080'}, (3840, 2160))
    # 别名归一后等于源尺寸 → 不插（只降不放）
    assert S.build_predownscale_filter({'resolution': '4k'}, (3840, 2160)) is None
    # 目标比源大
    assert S.build_predownscale_filter({'resolution': '3840x2160'}, (2560, 1440)) is None
    # 普通片源一律不插
    assert S.build_predownscale_filter({'resolution': '1080p'}, (1280, 720)) is None
    assert S.build_predownscale_filter({'resolution': '720p'}, (1920, 1080)) is None


def test_no_filter_without_explicit_resolution():
    for p in ({'resolution': 'original'}, {}, None, {'resolution': ''},
              {'resolution': 'garbage'}):
        assert S.build_predownscale_filter(p, (3840, 2160)) is None, p


def test_bad_size_never_raises():
    for size in (None, (), (3840,), ('x', 'y'), (0, 0), (-1, 2160)):
        assert S.build_predownscale_filter({'resolution': '1080p'}, size) is None, size
    assert S.build_predownscale_filter({'resolution': '1080x100'}, (3840, 2160)) is None


def test_filter_shape_is_safe_for_filter_complex():
    f = S.build_predownscale_filter({'resolution': '1080p'}, (3840, 2160))
    assert f.startswith('scale=1920:1080:'), f
    for token in ('force_original_aspect_ratio=decrease', 'flags=lanczos', PRE):
        assert token in f, '缺 %s：%r' % (token, f)
    # 逗号是 filter_complex 的分隔符，滤镜串里绝不能出现；也不能在这里 pad/crop，
    # 画布适配由末端的 build_video_filter 统一负责。
    assert ',' not in f and ';' not in f, f
    assert 'pad=' not in f and 'crop=' not in f, f
    f2 = S.build_predownscale_filter({'resolution': '720p'}, (3840, 2160))
    assert f2.startswith('scale=1280:720:'), f2


# ---------------------------------------------------------------------------
# 2. 单遍合并滤镜图（不真编码，只捕获命令）
# ---------------------------------------------------------------------------

def _capture(monkeypatch, tmp_path, size=(3840, 2160), segs=None, params=None,
             probe_raises=False):
    """跑一次 _cut_and_burn_video，捕获所有 ffmpeg 命令。"""
    calls = []

    def fake_ff(cmd, **kw):
        calls.append(list(cmd))
        # 让 os.path.exists(out) 通过（命令最后一个参数是输出路径）
        try:
            out = str(cmd[-1])
            if out.endswith('.mp4'):
                with open(out, 'wb') as fh:
                    fh.write(b'x')
        except OSError:
            pass
        return 0, b'', b''

    monkeypatch.setattr(V, 'ffmpeg_run', fake_ff)
    # 源文件 20s，输出文件返回 0 让「时长偏差」分支不触发
    monkeypatch.setattr(V, 'probe_audio_len',
                        lambda p: 0.0 if str(p).endswith('vburn.mp4') else 20.0)
    monkeypatch.setattr(V, '_has_audio_track', lambda p: False)
    if probe_raises:
        def _boom(p):
            raise RuntimeError('probe down')
        monkeypatch.setattr(V, 'probe_size', _boom)
    else:
        monkeypatch.setattr(V, 'probe_size', lambda p: size)
    monkeypatch.setattr(V, 'probe_is_hdr', lambda p: False)
    monkeypatch.setattr(V, 'tonemap_available', lambda p=None: False)
    # 绕开 nvenc 探测与调色，保证捕获到的只有滤镜图本身
    monkeypatch.setattr(S, 'video_encode_args', lambda **kw: ['-c:v', 'libx264'])
    monkeypatch.setattr(S, 'grade_filter', lambda p: None)

    p = params if params is not None else {'resolution': '1080p'}
    spans = list(segs) if segs is not None else [(0.0, 5.0), (10.0, 15.0)]
    V._cut_and_burn_video('src.mp4', spans, {}, str(tmp_path), p,
                          ['第一句', '第二句'], {}, None)
    return calls


def _graph(calls):
    return [c[c.index('-filter_complex') + 1] for c in calls if '-filter_complex' in c]


def test_4k_source_inserts_predownscale_per_segment(monkeypatch, tmp_path):
    calls = _capture(monkeypatch, tmp_path)
    graphs = _graph(calls)
    assert len(graphs) == 1, graphs
    g = graphs[0]
    # 两段 trim 链各带一条前置 scale，且排在 trim 之后、concat 之前
    assert g.count(PRE) == 2, '每段 trim 都必须降采样一次：%r' % g
    assert g.count('[0:v]trim=') == 2, g
    assert g.index(PRE) < g.index('concat=n=2'), '降采样必须在拼接之前'
    assert 'scale=1920:1080' in g, g
    # 末端的画布适配仍在（两处不冲突）
    assert g.count('scale=1920:1080') >= 3, g


def test_720p_source_filtergraph_unchanged(monkeypatch, tmp_path):
    calls = _capture(monkeypatch, tmp_path, size=(1280, 720))
    g = _graph(calls)[0]
    assert PRE not in g, '普通片源不该插前置降采样：%r' % g
    assert g.count('[0:v]trim=') == 2


def test_probe_failure_falls_back_silently(monkeypatch, tmp_path):
    calls = _capture(monkeypatch, tmp_path, probe_raises=True)
    g = _graph(calls)[0]
    assert PRE not in g, '探测失败必须降级成原滤镜图：%r' % g


def test_kill_switch_returns_to_v1412_graph(monkeypatch, tmp_path):
    """FRAMECUT_PREDOWN=0 必须能整条关掉，回到 v1.4.12 的滤镜图。"""
    monkeypatch.setattr(V, '_PREDOWN_ENABLED', False)
    calls = _capture(monkeypatch, tmp_path)
    g = _graph(calls)[0]
    assert PRE not in g, '关掉开关后不该有任何前置降采样：%r' % g
    assert g.count('[0:v]trim=') == 2


def test_kill_switch_is_env_backed():
    src = open(os.path.join(ROOT, 'video_render.py'), encoding='utf-8').read()
    assert "os.environ.get('FRAMECUT_PREDOWN'" in src, '必须留一个可回滚的环境开关'
    assert 'if _PREDOWN_ENABLED:' in src


def test_crossfade_path_also_predownscaled(monkeypatch, tmp_path):
    """xfade 要求各段尺寸/像素格式一致，前置降采样后反而更稳，不能漏插。"""
    calls = _capture(monkeypatch, tmp_path,
                     params={'resolution': '1080p', 'transition': 'crossfade'})
    g = _graph(calls)[0]
    assert 'xfade=' in g, g
    assert g.count(PRE) == 2, '交叉淡化分支每段也必须降采样：%r' % g


def test_no_cut_branch_unchanged(monkeypatch, tmp_path):
    """铺满全片（无实质剪辑）走 -vf 分支，本轮刻意不动它。"""
    calls = _capture(monkeypatch, tmp_path, segs=[(0.0, 10.0), (10.0, 20.0)])
    assert not _graph(calls), '无裁剪分支不该走 filter_complex'
    vf_calls = [c for c in calls if '-vf' in c]
    assert vf_calls, '无裁剪分支应该走 -vf'
    vf = vf_calls[0][vf_calls[0].index('-vf') + 1]
    assert PRE not in vf, '无裁剪分支保持原样：%r' % vf

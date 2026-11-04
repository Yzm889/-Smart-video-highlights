# -*- coding: utf-8 -*-
"""调色 + 配乐情绪包络回归（分册 E）。

守五条契约：
  1. 默认「原片」= 零影响：滤镜串里不许出现 eq/colorbalance，与改动前逐字符一致；
  2. 调色强度有上限（不是滤镜味），且必须排在字幕**之前**（白字不被染色）；
  3. 包络档位可推导、可合并、有限幅；信息不足时返回空 → 回退旧的常量音量；
  4. 混音链顺序固定：包络 → sidechain → afade → alimiter → loudnorm（loudnorm 参数一字不改）；
  5. 包络表达式是**真的**能分段产生 ≥3dB 音量差（用 ffmpeg 实测，不靠读代码）。
"""
import os
import re
import sys
import subprocess

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui_server as S  # noqa: F401  必须先导入宿主，规避循环导入
import video_render as VR

imageio_ffmpeg = pytest.importorskip('imageio_ffmpeg')


# ---------------------------------------------------------------------------
# P0-1 调色
# ---------------------------------------------------------------------------
def test_color_style_defaults_to_native():
    assert S.color_style({}) == 'native'
    assert S.color_style({'colorStyle': ''}) == 'native'
    assert S.color_style({'colorStyle': '原片'}) == 'native'
    assert S.color_style(None) == 'native'


def test_color_style_accepts_chinese_and_case():
    assert S.color_style({'colorStyle': '清冷'}) == 'cool'
    assert S.color_style({'colorStyle': 'COOL'}) == 'cool'
    assert S.color_style({'colorStyle': '暖调'}) == 'warm'
    assert S.color_style({'colorStyle': ' Warm '}) == 'warm'
    # 无法识别的取值必须安全回退，不能哑失败成「选了却没效果」
    assert S.color_style({'colorStyle': '赛博朋克'}) == 'native'


def test_native_grade_is_byte_identical_off():
    # 默认档必须是 None，这样调用方拼接的滤镜串与改动前完全一样
    assert S.grade_filter({}) is None
    assert S.grade_filter({'colorStyle': '原片'}) is None
    for name in ('cool', 'warm'):
        g = S.grade_filter({'colorStyle': name})
        assert g and 'eq=' in g and 'colorbalance=' in g


def test_grade_strength_is_restrained():
    """强度上限写死：对比不超过 1.12、饱和落在 [0.90, 1.15]。
    这一步的目的是「把镜头色调拉齐」，不是加滤镜味——超了就该被这个断言拦下来。"""
    for name in ('cool', 'warm'):
        g = S.grade_filter({'colorStyle': name})
        c = float(re.search(r'contrast=([\d.]+)', g).group(1))
        sat = float(re.search(r'saturation=([\d.]+)', g).group(1))
        assert 1.0 <= c <= 1.12, (name, c)
        assert 0.90 <= sat <= 1.15, (name, sat)


# ---------------------------------------------------------------------------
# P0-2 包络：档位推导
# ---------------------------------------------------------------------------
def _txt(emo):
    return {'紧张': '他猛地中枪倒地，血溅了一地',
            '悲伤': '房间空荡，没有回应，只剩残破的碎屑',
            '温柔': '她轻轻拍了拍孩子的头，说别怕',
            '激动': '门终于被撞开，火花轰地炸开',
            '解说': '画面一转，故事继续往下走'}[emo]


def test_envelope_disabled_returns_empty():
    segs = [(0, 4), (4, 8)]
    assert VR.bgm_envelope(segs, ['a', 'b'], enabled=False) == []
    assert VR.bgm_envelope([], []) == []
    assert VR.bgm_envelope(segs, []) != []      # 无文案也要有兜底档，不能整条消失


def test_envelope_hook_then_tense_then_close():
    segs = [(0.0, 3.0), (3.0, 9.0), (9.0, 15.0), (15.0, 21.0)]
    narr = [_txt('解说'), _txt('紧张'), _txt('解说'), _txt('悲伤')]
    env = VR.bgm_envelope(segs, narr, vdur=21.0)
    assert env, '有分段信息时必须产出包络'
    assert env[0]['tier'] == 'hook'              # 前 7s 是开场钩子
    assert env[-1]['tier'] == 'close'            # 收尾段降到最低（留白）
    assert env[0]['vol'] > env[-1]['vol']


def test_envelope_sad_soft_outrank_key():
    """悲伤/温柔段要让位给人声——即使 LLM 标了 key，也不该把 BGM 推满。"""
    segs = [(10.0, 16.0), (16.0, 22.0)]
    narr = [_txt('悲伤'), _txt('温柔')]
    env = VR.bgm_envelope(segs, narr, importance=['key', 'key'], vdur=40.0)
    assert [e['tier'] for e in env] == ['sad', 'soft']
    assert all(e['vol'] < VR._BGM_TIER['key'] for e in env)


def test_envelope_merges_adjacent_same_tier_and_clamps():
    # 全部落在钩子窗之外，避免「钩子切开首段」干扰合并判定
    segs = [(10.0, 12.0), (12.0, 14.0), (14.0, 16.0)]
    narr = [_txt('解说')] * 3
    env = VR.bgm_envelope(segs, narr, vdur=100.0)
    # 三段同档且首尾相接 → 合并成一档（表达式台阶数越少越稳）
    assert len(env) == 1
    for e in env:
        assert VR._BGM_LO <= e['vol'] <= VR._BGM_HI


def test_envelope_hook_does_not_swallow_long_opening_shot():
    """一个 15 秒的长开场不能把中段高光一起吞成 hook 档，否则整片 BGM 是平线。"""
    segs = [(0.0, 15.0), (15.0, 21.0)]
    narr = [_txt('解说'), _txt('紧张')]
    env = VR.bgm_envelope(segs, narr, vdur=21.0)
    assert env[0] == {'start': 0.0, 'end': 7.0, 'vol': VR._BGM_TIER['hook'], 'tier': 'hook'}
    # 钩子窗之后必须切回该段本该有的档位，而不是继续 hook
    assert any(e['tier'] in ('advance', 'tense') for e in env[1:])


# ---------------------------------------------------------------------------
# P0-2 包络：表达式
# ---------------------------------------------------------------------------
def test_volume_expr_none_when_empty():
    assert VR.bgm_volume_expr([]) is None
    assert VR.bgm_volume_expr(None) is None


def test_volume_expr_shape():
    env = [{'start': 0.0, 'end': 3.0, 'vol': 0.225, 'tier': 'hook'},
           {'start': 3.0, 'end': 9.0, 'vol': 0.14, 'tier': 'transition'}]
    e = VR.bgm_volume_expr(env)
    assert e.startswith('%.4f' % VR._BGM_BASE)
    assert e.count('clip(') == 2                  # 一档一次斜坡，线性增长不膨胀
    assert 'if(' not in e                         # 不用 if 嵌套（段数一多就爆炸）
    # 换档点是「斜坡中点」，避免音量台阶的抽动感
    assert '(t-2.775)' in e


# ---------------------------------------------------------------------------
# 混音链契约：顺序 + loudnorm 参数不动
# ---------------------------------------------------------------------------
def _fake_ffmpeg(monkeypatch, sink):
    def fake(args, *a, **k):
        sink.append(list(args))
        try:
            open(args[-1], 'wb').close()       # 让调用方认为产物已生成
        except Exception:
            pass
        return 0, b'', b''
    monkeypatch.setattr(VR, 'ffmpeg_run', fake)


@pytest.fixture()
def media(tmp_path):
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    d = str(tmp_path)
    src = os.path.join(d, 'src.mp4')
    subprocess.run([exe, '-y', '-f', 'lavfi', '-i', 'color=c=black:s=160x120:r=10:d=12',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', src], capture_output=True)
    music = os.path.join(d, 'bgm.wav')
    subprocess.run([exe, '-y', '-f', 'lavfi', '-i', 'sine=frequency=330:duration=12', music],
                   capture_output=True)
    clips = []
    for i in range(3):
        p = os.path.join(d, 'c%d.wav' % i)
        subprocess.run([exe, '-y', '-f', 'lavfi',
                        '-i', 'sine=frequency=%d:duration=1' % (500 + i * 60), p],
                       capture_output=True)
        clips.append(p)
    return d, src, music, clips


def test_mix_chain_order_and_loudnorm_untouched(monkeypatch, media, tmp_path):
    d, src, music, clips = media
    segs = [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0)]
    narr = [_txt('解说'), _txt('紧张'), _txt('悲伤')]
    tts = [(clips[0], 0.0, 4.0), (clips[1], 4.0, 8.0), (clips[2], 8.0, 12.0)]
    params = {'_importance': ['advance', 'key', 'transition']}
    sink = []
    _fake_ffmpeg(monkeypatch, sink)
    VR._compose_narration_video(src, segs, narr, tts, d, params, music_path=music, burned=True)

    assert sink, '必须真的构造过 ffmpeg 命令'
    fc = sink[0][sink[0].index('-filter_complex') + 1]
    # 包络：BGM 走 eval=frame 的分段音量，且用的是判定出的情绪档
    assert "volume='" in fc and 'eval=frame' in fc and 'clip(' in fc
    # 开场钩子档相对基线 +0.045（0.225 vs 0.18）确实进来了
    assert '+0.0450*clip(' in fc
    # 顺序：包络(volume) → afade → sidechain → alimiter → loudnorm
    # 注意 afade 在 sidechain 之前是**旧有行为**（淡入淡出作用在 BGM 干声上，
    # 再交给闪避压缩），本次刻意不动它——动了就等于改了所有既有成片的听感。
    i_vol = fc.index('volume=')
    i_fade = fc.index('afade=t=out')
    i_sc = fc.index('sidechaincompress')
    i_lim = fc.index('alimiter')
    i_ln = fc.index('loudnorm')
    assert i_vol < i_fade < i_sc < i_lim < i_ln
    # loudnorm 参数一字不改（响度标准是底线，不许为了包络去动它）
    assert 'loudnorm=I=-16:TP=-1.5:LRA=11' in fc


def test_envelope_off_falls_back_to_constant(monkeypatch, media):
    d, src, music, clips = media
    segs = [(0.0, 4.0), (4.0, 8.0)]
    narr = [_txt('解说'), _txt('悲伤')]
    tts = [(clips[0], 0.0, 4.0), (clips[1], 4.0, 8.0)]
    sink = []
    _fake_ffmpeg(monkeypatch, sink)
    VR._compose_narration_video(src, segs, narr, tts, d, {'bgmEnvelope': False},
                                music_path=music, burned=True)
    fc = sink[0][sink[0].index('-filter_complex') + 1]
    # 关掉包络 = 回到改动前的常量音量，逐字符一致
    assert 'volume=0.18,' in fc
    assert "volume='" not in fc


def test_grade_applied_before_subtitles_in_single_pass(monkeypatch, media):
    """单遍管线的调色必须排在 subtitles 之前：白字被 colorbalance 染色就废了。"""
    d, src, _music, clips = media
    segs = [(0.0, 4.0), (4.0, 8.0)]
    narr = ['一', '二']
    sink = []
    _fake_ffmpeg(monkeypatch, sink)
    VR._cut_and_burn_video(src, segs, {}, d, {'colorStyle': 'cool'}, narr, {},
                           skip_no_cut=True)
    fc = sink[0][sink[0].index('-filter_complex') + 1]
    assert 'eq=' in fc and 'colorbalance=' in fc
    assert fc.index('colorbalance') < fc.index('subtitles=')
    # 调色在 concat/xfade 之后（整片一次运算），不是每段各加一遍
    assert fc.index('concat=') < fc.index('eq=')


def test_no_grade_by_default_in_single_pass(monkeypatch, media):
    d, src, _music, clips = media
    segs = [(0.0, 4.0), (4.0, 8.0)]
    sink = []
    _fake_ffmpeg(monkeypatch, sink)
    VR._cut_and_burn_video(src, segs, {}, d, {}, ['一', '二'], {}, skip_no_cut=True)
    fc = sink[0][sink[0].index('-filter_complex') + 1]
    assert 'eq=' not in fc and 'colorbalance=' not in fc


# ---------------------------------------------------------------------------
# 【致命缺陷回归】配音 + 配乐同时开启时，成片必须真的有人声
# ---------------------------------------------------------------------------
def test_voice_and_music_together_really_produce_audio(media, tmp_path):
    """真实故障：ffmpeg 的 link label 是一次性消费的。

    旧实现把 [t0][t1][t2] 同时接到「侧链压缩的触发信号」和「最终 amix」两处，
    报 `Stream specifier 't0' ... matches no streams` → 音频链 loudnorm/no_loudnorm
    两级全败 → 落到「丢弃配音」兜底。成片有画面、有字幕、有原声、有配乐，
    唯独**没有解说配音**。这条用例真的跑 ffmpeg 并测每段是否出声，
    不是只看字符串（只看字符串永远发现不了这个 bug）。
    """
    d, src, music, clips = media
    segs = [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0)]
    narr = ['一', '二', '三']
    tts = [(clips[0], 0.0, 4.0), (clips[1], 4.0, 8.0), (clips[2], 8.0, 12.0)]
    out = VR._compose_narration_video(src, segs, narr, tts, d,
                                      {'_importance': ['advance', 'key', 'transition']},
                                      music_path=music, burned=True)
    assert out and os.path.exists(out), '配音+配乐必须能出片'
    # 每一段都必须有自己的声音（配音 500/560/620Hz 与 BGM 330Hz 不同，分别测能量）
    levels = [_mean_volume(out, i * 4 + 0.4, 2.5) for i in range(3)]
    assert all(v > -60.0 for v in levels), '三段配音都必须出声，实测 %s' % levels


def test_mix_input_count_matches_labels(monkeypatch, media):
    """amix 的 inputs=N 必须等于实际接进去的标签数，多一个少一个都会让整链失败。"""
    d, src, music, clips = media
    segs = [(0.0, 4.0), (4.0, 8.0)]
    tts = [(clips[0], 0.0, 4.0), (clips[1], 4.0, 8.0)]
    for has_music in (True, False):
        sink = []
        _fake_ffmpeg(monkeypatch, sink)
        VR._compose_narration_video(src, segs, ['一', '二'], tts, d, {},
                                    music_path=music if has_music else None, burned=True)
        fc = sink[0][sink[0].index('-filter_complex') + 1]
        last = fc.split(';')[-1]
        m = re.search(r'amix=inputs=(\d+)', last)
        n = int(m.group(1))
        labels = re.findall(r'\[[a-z0-9]+\]', last.split('amix=inputs=')[0])
        assert n == len(labels), (has_music, n, labels)


# ---------------------------------------------------------------------------
# 真·实测：包络表达式必须真的产生分段音量差
# ---------------------------------------------------------------------------
def _mean_volume(path, ss, dur):
    r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-ss', str(ss), '-t', str(dur),
                        '-i', path, '-af', 'volumedetect', '-f', 'null', '-'],
                       capture_output=True, text=True, errors='ignore')
    m = re.search(r'mean_volume: (-?[\d.]+) dB', r.stderr)
    return float(m.group(1)) if m else -999.0


def test_envelope_really_lifts_highlight_over_transition(monkeypatch, tmp_path):
    """高光/紧张段的 BGM 必须实测比收尾段响 ≥3dB（不靠读代码下结论）。
    这里刻意不带 loudnorm：要验的是「包络本身有没有做出差距」，
    响度归一由上面 test_mix_chain_order_and_loudnorm_untouched 守。"""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    mono = os.path.join(str(tmp_path), 'sine.wav')
    subprocess.run([exe, '-y', '-f', 'lavfi', '-i', 'sine=frequency=330:duration=12',
                    '-ac', '2', mono], capture_output=True)
    segs = [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0), (9.0, 12.0)]
    narr = [_txt('解说'), _txt('解说'), _txt('紧张'), _txt('紧张')]
    env = VR.bgm_envelope(segs, narr, vdur=12.0)
    expr = VR.bgm_volume_expr(env)
    out = os.path.join(str(tmp_path), 'graded.wav')
    r = subprocess.run([exe, '-y', '-i', mono, '-af', "volume='%s':eval=frame" % expr, out],
                       capture_output=True, text=True, errors='ignore')
    assert r.returncode == 0, r.stderr[-400:]

    tense = _mean_volume(out, 6.6, 1.8)     # 紧张段（已过斜坡）
    tail = _mean_volume(out, 10.2, 1.6)     # 收尾段
    assert tense > tail + 3.0, '高光段应比收尾段响 ≥3dB，实测 %.1f vs %.1f' % (tense, tail)

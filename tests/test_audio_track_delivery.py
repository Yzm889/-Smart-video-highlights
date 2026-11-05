# -*- coding: utf-8 -*-
"""解说音轨交付（2026-11-05）。

背景：现有混音把「原声 + 各段配音 + BGM」全部 amix 成**一条** AAC 音轨，
`-map '[aout]'` 只输出一条。导入 PR 后只有一条轨，想单独调解说音量、
替换某句、或对齐字幕都得整轨重做 —— 和硬烧字幕是一模一样的问题。

用户要两件事：
  ① 单独导出一条纯解说音轨文件（拖进剪辑软件就是独立 A2）；
  ② 成片内分两层：A1 完整混音（默认播放，保证普通播放器能听到解说）、
     A2 纯解说（PR/FCP 里可单独静音/调音量/替换）。

关键设计约束（本文件钉住）：
  · **默认零影响**：两个开关都不勾时，成片与改动前一致（单轨、不生成附件）；
  · 附赠产物失败不得拖垮成片（一律 try/except + 返回 None，不抛）；
  · 纯解说轨的时间轴必须与成片一致：先 atrim 再 adelay（顺序反了会静音），
    apad 到成片总长，这样 0 秒对 0 秒；
  · 分层音轨用二次 mux（-c:v copy），不去改带 asplit/sidechaincompress
    的主混音链 —— 那条链动错一个标签就整链降级到「丢弃配音」。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VR_PATH = os.path.join(ROOT, 'video_render.py')
JS_PATH = os.path.join(ROOT, 'static', 'app.js')
HTML_PATH = os.path.join(ROOT, 'static', 'index.html')

sys.path.insert(0, ROOT)
import webui_server as _W  # noqa: F401,E402  解除循环依赖
import video_render as VR  # noqa: E402

imageio_ffmpeg = pytest.importorskip('imageio_ffmpeg')


def _read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def _extract_func(src, name):
    i = src.index('def %s(' % name)
    j = src.index('\ndef ', i + 10)
    ns = {}
    exec(src[i:j], ns)
    return ns[name]


def test_switches_default_to_off():
    """默认必须单轨混音、不导出 wav —— 老用户重跑参数结果一致。"""
    src = _read(VR_PATH)
    layers = _extract_func(src, '_audio_layers_enabled')
    export = _extract_func(src, '_voice_track_enabled')
    for p in ({}, None, {'audio': None}, {'audio': {}}):
        assert layers(p) is False, '默认不该分层: %r' % (p,)
        assert export(p) is False, '默认不该导出 wav: %r' % (p,)
    assert layers({'audio': {'layers': True}}) is True
    assert export({'audio': {'exportVoice': True}}) is True
    # 只有严格 True 才开：字符串 '1' / 数字 1 都不算（前端发的是 JSON bool）
    assert layers({'audio': {'layers': '1'}}) is False
    assert export({'audio': {'exportVoice': 1}}) is False


def test_voice_track_uses_same_timeline_rules():
    """纯解说轨必须与主混音链用同样的定位规则，否则拖进 PR 会对不上画面。"""
    src = _read(VR_PATH)
    i = src.index('def build_voice_track(')
    body = src[i:i + 3000]
    # 先 atrim 再 adelay：顺序反了只保留「延迟后」的静音段（真机踩过，全片只有首句有声）
    assert body.index('atrim=start=0') < body.index('adelay='), \
        '必须先 atrim 再 adelay，否则第二句起全哑'
    assert 'apad=whole_dur=' in body, '缺 apad，音轨会比成片短，导入 PR 后错位'
    assert "atrim=0:{vdur" in body, '缺总长截断，音轨可能长于成片'
    assert "pcm_s16le" in body and "'-ar', '44100'" in body, \
        '应使用无损 PCM WAV（剪辑软件无编码延迟、无 gapless 歧义）'
    assert 'ffmpeg_run(cmd)' in body, '未走统一的 ffmpeg 封装'


def test_voice_track_failure_is_nonfatal():
    """附赠产物失败必须返回 None，不能抛异常把成片拖垮。"""
    src = _read(VR_PATH)
    i = src.index('def build_voice_track(')
    body = src[i:i + 3000]
    assert 'return None' in body and 'if rc != 0' in body
    # 调用点必须包 try/except
    j = src.index('_voice_wav = build_voice_track(')
    caller = src[max(0, j - 400):j + 300]
    assert 'try:' in caller and 'except Exception' in caller, \
        '调用点未包 try/except，附赠物失败会让整条出片链路失败'


def test_layered_uses_copy_mux_not_core_mix_chain():
    """分层音轨必须走二次 mux（-c:v copy），不能改主混音滤镜图。"""
    src = _read(VR_PATH)
    assert "'-c:v', 'copy', '-c:a:0', 'copy'" in src, \
        '未用无损 copy 二次封装（重编码会掉画质且慢）'
    assert "'-map', '0:a:0', '-map', '1:a:0'" in src, '未映射两条音频流'
    assert "'-disposition:a:0', 'default'" in src, \
        'A1 必须标 default，否则播放器可能默认播到纯解说轨'
    # 主链的 asplit / sidechaincompress 必须原样保留
    assert 'asplit=2[vbus][vmix]' in src and 'sidechaincompress' in src, \
        '主混音链被改动了 —— 这条链动错会整链降级到「丢弃配音」'


def test_progress_exposes_voice_file():
    """前端拿不到路径就没有下载入口，功能等于没做。"""
    src = _read(VR_PATH)
    assert "progress['voice_file']" in src
    assert "progress['voice_track_layered']" in src
    assert "progress['subtitle_file']" in src, '字幕下载入口不能被误删'


def test_frontend_has_controls_and_download_entry():
    """两个开关、说明、wav 下载按钮都必须有 UI 入口。"""
    html = _read(HTML_PATH)
    js = _read(JS_PATH)
    for anchor in ('id="audioLayers"', 'id="exportVoice"', 'id="audioHint"',
                   'id="narVoiceDl"', 'id="narVoiceTip"', 'id="subBurn"'):
        assert anchor in html, '缺 UI 锚点: %s' % anchor
    assert 'function getAudioDelivery' in js
    assert 'function updateAudioHint' in js
    i = js.index('function fillNarSrt')
    body = js[i:i + 2500]
    assert 'voiceFile' in body and 'narVoiceDl' in body, \
        'fillNarSrt 未回填 wav 下载链接'
    assert js.count('fillNarSrt(') >= 3, '两处结果回填 + 定义，至少 3 次调用'


def test_default_selected_option_is_single_track():
    """下拉框默认必须选中「单轨混音」。"""
    html = _read(HTML_PATH)
    m = re.search(r'<select id="audioLayers"[^>]*>(.*?)</select>', html, re.S)
    assert m
    opt = re.search(r'<option value="0"[^>]*>', m.group(1))
    assert opt and 'selected' in opt.group(0), '默认必须是单轨混音 (value=0)'
    assert 'value="1"' in m.group(1), '缺双轨分层选项'


# ---- 「成片不含解说」档（2026-11-06 主人需求：素材包式交付）----

def test_narr_out_switch_parses_layers_2():
    """audio.layers==2 = 成片不含解说；与双轨(true)/默认(False)互不误触。"""
    narr_out = _extract_func(_read(VR_PATH), '_narr_out_enabled')
    assert narr_out({'audio': {'layers': 2}}) is True
    for p in ({}, None, {'audio': {}}, {'audio': {'layers': True}},
              {'audio': {'layers': False}}, {'audio': {'layers': '2'}}):
        assert narr_out(p) is False, '不该触发: %r' % (p,)


def test_frontend_has_narr_out_option():
    """第三档选项、数值映射、提示文案都必须在。"""
    html = _read(HTML_PATH)
    js = _read(JS_PATH)
    m = re.search(r'<select id="audioLayers"[^>]*>(.*?)</select>', html, re.S)
    assert m and 'value="2"' in m.group(1), '缺「成片不含解说」选项'
    i = js.index('function getAudioDelivery')
    assert "? 2 :" in js[i:i + 400] or "? 2:" in js[i:i + 400], \
        'getAudioDelivery 未把档位 2 映射成数值 layers=2'


def _mean_volume(path, ss, t):
    r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-ss', str(ss), '-t', str(t),
                        '-i', path, '-af', 'volumedetect', '-f', 'null', '-'],
                       capture_output=True, text=True, errors='ignore')
    m = re.search(r'mean_volume: (-?[\d.]+) dB', r.stderr)
    return float(m.group(1)) if m else -999.0


def test_narr_out_excludes_narration_from_final_mix():
    """真跑 ffmpeg：成片不含解说时，解说段窗口只剩原声（明显轻于含解说版）。"""
    d = tempfile.mkdtemp(prefix='narrout_')
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        # 原声视频：6s 黑画面 + 330Hz 正弦原声
        src = os.path.join(d, 'src.mp4')
        subprocess.run([exe, '-y', '-f', 'lavfi', '-i', 'color=c=black:s=160x120:r=10:d=6',
                        '-f', 'lavfi', '-i', 'sine=frequency=330:duration=6',
                        '-shortest', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', src], capture_output=True)
        assert os.path.exists(src)
        narr = os.path.join(d, 'n.wav')
        subprocess.run([exe, '-y', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=1', narr],
                       capture_output=True)
        segs = [(0.0, 6.0)]
        tts = [(narr, 1.0, 2.0)]
        # 两档必须用不同 run 目录：compose 固定写 run_dir/final.mp4，同目录会互相覆盖
        d_a = os.path.join(d, 'a'); d_b = os.path.join(d, 'b')
        os.makedirs(d_a); os.makedirs(d_b)
        # 默认档：解说混进成片
        out_a = VR._compose_narration_video(src, segs, ['一'], tts, d_a, {}, burned=True)
        assert out_a and os.path.exists(out_a), '默认档成片失败'
        # 不含解说档：audio.layers=2
        out_b = VR._compose_narration_video(src, segs, ['一'], tts, d_b,
                                            {'audio': {'layers': 2}}, burned=True)
        assert out_b and os.path.exists(out_b), '不含解说档成片失败'

        def _band_880(path, ss, t):
            """880Hz 频段能量（解说音所在频带）：loudnorm 会把整轨响度拉平，
            比总音量失灵，必须用带通看「解说音在不在」。330Hz 原声被 highpass 挡掉。"""
            r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-ss', str(ss), '-t', str(t),
                                '-i', path, '-af', 'highpass=f=600,lowpass=f=1200,volumedetect',
                                '-f', 'null', '-'], capture_output=True, text=True, errors='ignore')
            m = re.search(r'mean_volume: (-?[\d.]+) dB', r.stderr)
            return float(m.group(1)) if m else -999.0

        va = _band_880(out_a, 1.3, 0.5)   # 解说播放窗口（解说 880Hz + 原声 330Hz）
        vb = _band_880(out_b, 1.3, 0.5)   # 同窗口应只剩被挡掉大半的 330Hz
        assert va - vb >= 6.0, \
            '「不含解说」档在解说窗口仍检出 880Hz 解说能量（含 %.1f dB vs 不含 %.1f dB）——旁路未生效' % (va, vb)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_narr_out_still_exports_voice_wav():
    """不含解说档必须强制出解说 .wav——否则解说就整个丢了。"""
    src = _read(VR_PATH)
    j = src.index('_voice_wav = build_voice_track(')
    caller = src[max(0, j - 260):j + 60]
    assert '_narr_out_enabled(params)' in caller, \
        'wav 导出条件未包含「成片不含解说」档：该档位下解说不进混音，不出 wav = 解说丢失'

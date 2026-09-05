"""解说「真剪辑」回归测试。

背景：此前解说链路（剧情驱动 / AI 解说 / 人机协同确认）全程不做任何剪切，
`_compose_narration_video` 只烧字幕 + 混音，成片时长恒等于原片 —— 用户在预览里
取消勾选的段落画面照样留在成片里，「剪辑解说」名不副实。

本文件锁定 `_cut_video_by_spans` 的四个契约：
1. 区间合并正确（重叠/紧邻不重复拼接）
2. 连续覆盖全片时不重编码（省时间，cut_sec=0）
3. 有空隙时真剪辑，并返回拼接后的新时间轴（字幕/配音靠它对齐）
4. ffmpeg 失败时安全降级为"不剪"，绝不因此出不了片
"""

import os

import pytest

import webui_server as S


def _ff(monkeypatch, fail=False):
    """打桩 ffmpeg：成功时按最后一个参数创建输出文件（代码靠 os.path.exists 判定）。"""
    calls = []

    def fake_ffmpeg(args, input_data=None):
        calls.append(list(args))
        if fail:
            return 1, b'', b'boom'
        out = args[-1]
        if isinstance(out, str) and out.endswith('.mp4'):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as f:
                f.write(b'x')
        return 0, b'', b'Stream #0:1: Audio: aac'

    monkeypatch.setattr(S, 'ffmpeg_run', fake_ffmpeg)
    return calls


def test_merge_spans_merges_overlap_and_adjacent():
    assert S._merge_spans([(5, 10), (0, 6)]) == [(0.0, 10.0)], '重叠区间应合并'
    assert S._merge_spans([(0, 5), (5.02, 9)]) == [(0.0, 9.0)], '紧邻区间(差<eps)应合并'
    assert S._merge_spans([(0, 5), (6, 9)]) == [(0.0, 5.0), (6.0, 9.0)], '有空隙不应合并'
    assert S._merge_spans([(0, 0.01)]) == [], '过短区间应丢弃'


def test_no_cut_when_spans_cover_whole_video(monkeypatch, tmp_path):
    """全片连续覆盖 → 跳过剪辑，避免无谓的全片重编码。"""
    _ff(monkeypatch)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 30.0)
    out, spans, cut = S._cut_video_by_spans('v.mp4', [(0, 10), (10, 20), (20, 30)], str(tmp_path))
    assert out == 'v.mp4', '整片保留时不应重编码'
    assert cut == 0.0
    # 关键契约：段数必须与输入一致（= 解说词条数），否则字幕会整体错位
    assert len(spans) == 3, '返回的段数必须等于输入段数：%s' % (spans,)
    assert spans == [(0, 10), (10, 20), (20, 30)], '未剪辑时应原样返回时间轴'


def test_cut_removes_gaps_and_remaps_timeline(monkeypatch, tmp_path):
    """保留 0-10 与 20-30（剪掉中间 10 秒）→ 新时间轴为 (0,10) 与 (10,20)。"""
    calls = _ff(monkeypatch)
    # mock 探测需区分原片(30s)与剪辑后成片(20s)：无差别返回 30s 会误触发
    # 「拼接时长偏差按比例缩放」逻辑，把正确的时间轴拉回 30s
    monkeypatch.setattr(S, 'probe_audio_len',
                        lambda p: 20.0 if 'cut' in str(p) else 30.0)
    monkeypatch.setattr(S, '_has_audio_track', lambda p: True)
    out, spans, cut = S._cut_video_by_spans('v.mp4', [(0, 10), (20, 30)], str(tmp_path))
    assert out.endswith('cut.mp4'), '应输出剪辑后的视频：%s' % out
    assert os.path.exists(out)
    assert spans == [(0.0, 10.0), (10.0, 20.0)], '新时间轴应首尾相接：%s' % (spans,)
    assert abs(cut - 10.0) < 0.01, '被剪时长应为 10s：%s' % cut
    # 两段各切一次 + 一次 concat
    cuts = [c for c in calls if '-ss' in c]
    assert len(cuts) == 2, '每个保留段各剪切一次'
    assert any('concat' in ' '.join(c) for c in calls), '应做拼接'


def test_cut_degrades_gracefully_on_ffmpeg_failure(monkeypatch, tmp_path):
    """剪辑属增强项：ffmpeg 失败应退回原片，不得让任务失败。"""
    _ff(monkeypatch, fail=True)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 30.0)
    monkeypatch.setattr(S, '_has_audio_track', lambda p: True)
    out, spans, cut = S._cut_video_by_spans('v.mp4', [(0, 10), (20, 30)], str(tmp_path))
    assert out == 'v.mp4', '失败应安全降级'
    assert cut == 0.0


def test_render_narrate_cuts_before_voicing(monkeypatch, tmp_path):
    """_render_narrate 必须先剪辑再配音：配音/字幕按剪辑后的新时间轴对齐。"""
    run_dir = str(tmp_path)
    seen = {}

    def fake_cut(video_path, spans, rd, progress=None):
        seen['spans'] = list(spans)
        return 'cut.mp4', [(0.0, 3.0), (3.0, 6.0)], 4.0

    monkeypatch.setattr(S, '_cut_video_by_spans', fake_cut)
    monkeypatch.setattr(S, '_tts_available', lambda: False)
    # 配音引擎全部打桩为不可用：本用例只验证「先剪辑、再按新时间轴配音」的顺序，不依赖真实网络
    monkeypatch.setattr(S, 'edge_tts_available', lambda: False)
    monkeypatch.setattr(S, 'sherpa_tts_available', lambda: False)
    monkeypatch.setattr(S, 'sapi_tts', lambda t, p: False)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 6.0)
    monkeypatch.setattr(S, '_has_audio_track', lambda p: False)

    def fake_compose(video_path, segs, narr, tts_paths, rd, params, music_path=None, voice_spans=None):
        seen['compose_video'] = video_path
        seen['compose_segs'] = list(segs)
        return os.path.join(rd, 'final.mp4')

    monkeypatch.setattr(S, '_compose_narration_video', fake_compose)

    final, vc, cut_info = S._render_narrate(
        'v.mp4', [(0, 3), (3, 7), (7, 10)], ['a', 'b', 'c'], {}, run_dir, progress={})
    assert seen['compose_video'] == 'cut.mp4', '合成必须用剪辑后的视频'
    assert seen['compose_segs'] == [(0.0, 3.0), (3.0, 6.0)], '字幕/配音必须用新时间轴'
    assert cut_info['cut_sec'] == 4.0 and cut_info['segs'] == 2
    assert final.endswith('final.mp4') and vc == 0


@pytest.mark.parametrize('auto_cut,expect_cut', [(True, True), (False, False)])
def test_render_narrate_respects_auto_cut(monkeypatch, tmp_path, auto_cut, expect_cut):
    """autoCut 关闭时不剪辑（保留旧行为，供用户对照）。"""
    called = {'cut': False}

    def fake_cut(video_path, spans, rd, progress=None):
        called['cut'] = True
        return video_path, spans, 0.0

    monkeypatch.setattr(S, '_cut_video_by_spans', fake_cut)
    monkeypatch.setattr(S, '_tts_available', lambda: False)
    monkeypatch.setattr(S, 'edge_tts_available', lambda: False)
    monkeypatch.setattr(S, 'sherpa_tts_available', lambda: False)
    monkeypatch.setattr(S, 'sapi_tts', lambda t, p: False)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 10.0)
    monkeypatch.setattr(S, '_has_audio_track', lambda p: False)
    monkeypatch.setattr(S, '_compose_narration_video',
                        lambda *a, **k: os.path.join(str(tmp_path), 'final.mp4'))

    S._render_narrate('v.mp4', [(0, 5)], ['a'], {}, str(tmp_path), progress={}, auto_cut=auto_cut)
    assert called['cut'] is expect_cut

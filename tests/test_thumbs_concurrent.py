# -*- coding: utf-8 -*-
"""M2-2 回归：分镜/VLM 抽帧必须并发执行（受并发度上限保护），而非逐段串行。
旧实现 for 循环逐段起 ffmpeg，长视频（60+ 段）缩略图阶段要 10~30 分钟。

用假的 ffmpeg_run 记录「同时在跑的抽帧数」，断言多段时出现并发（max_inflight>1），
且全部帧都产出、调用次数等于段数。
"""
import threading

import webui_server as _w


def test_extract_segment_frames_concurrent(monkeypatch, tmp_path):
    state = {'inflight': 0, 'max_inflight': 0, 'calls': 0}
    _lk = threading.Lock()

    def _fake_ffmpeg_run(cmd):
        with _lk:
            state['inflight'] += 1
            state['max_inflight'] = max(state['max_inflight'], state['inflight'])
            state['calls'] += 1
        import time
        time.sleep(0.05)  # 放大并发窗口，便于观测
        with _lk:
            state['inflight'] -= 1
        outp = cmd[-1]
        with open(outp, 'wb') as f:
            f.write(b'x' * 100)
        return (0, b'', b'')

    monkeypatch.setattr(_w, 'ffmpeg_run', _fake_ffmpeg_run)
    monkeypatch.setattr(_w, 'probe_audio_len', lambda *a, **k: 1000.0)

    segs = [(i * 2.0, i * 2.0 + 1.0) for i in range(20)]
    frames = _w.extract_segment_frames('fake.mp4', segs, str(tmp_path), max_side=220)

    assert len(frames) == 20, '应产出全部 20 张帧：%s' % frames
    assert state['calls'] == 20, 'ffmpeg_run 应被调用 20 次'
    assert state['max_inflight'] > 1, '多段抽帧必须并发执行（max_inflight>1）：%s' % state

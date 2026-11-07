# -*- coding: utf-8 -*-
"""配音首段串行预热回归钉（v1.4.11）。

真数据：整集 79 段那次，并发池里前 3 段各花 80736 / 80739 / 80742 ms，第 4 段起
回到 1852ms。原因：三段同时在 worker 里触发「edge → sapi 降级探测 + SAPI COM
初始化」，探测全局互斥，互相排队。

修法：并发之前先在主线程串行跑一段把引擎烧热，并把选定的引擎当提示留给
worker 线程（引擎锁是线程本地的，否则每个线程各付一次探测）。实测修后：
首段 11156ms（一次性），其余 1845/1852/1845ms。
"""

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: F401,E402  建立完整初始化入口（避免循环导入）
import movie_narrator as M  # noqa: E402


def _run(monkeypatch, tmp_path, n=5, concurrency=3):
    calls = []

    def fake_speak(text, out_path):
        calls.append({
            'thread': threading.current_thread().name,
            'text': text,
            'locked': getattr(M._TLS, 'tts_engine', None),
        })
        time.sleep(0.03)
        with open(out_path, 'wb') as f:
            f.write(b'0' * 2048)
        return True, 'sapi', out_path

    monkeypatch.setattr(M, 'load_ai_config', lambda: {'tts': {}})
    monkeypatch.setattr(M, 'edge_tts_available', lambda: True)
    monkeypatch.setattr(M, 'edge_tts_dead_reason', lambda: '')
    monkeypatch.setattr(M, '_edge_probe_recover', lambda rd: None)
    monkeypatch.setattr(M, '_aborted', lambda: False)
    monkeypatch.setattr(M, 'local_tts_speak', fake_speak)
    monkeypatch.setenv('TTS_CONCURRENCY', str(concurrency))

    prog = {'phase': '', 'pct': 0}
    narr = ['第%d句' % i for i in range(n)]
    res = M._generate_all_tts(narr, str(tmp_path), progress=prog)
    return res, calls, prog


def test_first_segment_runs_on_main_thread_before_pool(monkeypatch, tmp_path):
    res, calls, _prog = _run(monkeypatch, tmp_path)
    assert len(res) == len(calls) == 5
    assert calls[0]['thread'] == 'MainThread', \
        '首段必须在主线程串行预热，实际在 %s' % calls[0]['thread']
    assert sum(1 for c in calls if c['thread'] == 'MainThread') == 1, \
        '预热只该有一段，多了就白等了'


def test_worker_threads_reuse_warmed_engine(monkeypatch, tmp_path):
    _res, calls, _prog = _run(monkeypatch, tmp_path)
    workers = [c for c in calls if c['thread'] != 'MainThread']
    assert workers, '没有并发 worker，测试前提不成立'
    for c in workers:
        assert c['locked'] == 'sapi', \
            'worker 线程没沿用首段引擎（会各付一次 ~11s 探测）：%r' % (c['locked'],)


def test_serial_mode_has_no_warmup_split(monkeypatch, tmp_path):
    """并发=1 时不该有「预热 + 池」的拆分，全部主线程串行。"""
    res, calls, _prog = _run(monkeypatch, tmp_path, n=3, concurrency=1)
    assert len(res) == 3
    assert all(c['thread'] == 'MainThread' for c in calls), \
        '串行模式出现了非主线程调用：%r' % [c['thread'] for c in calls]

# -*- coding: utf-8 -*-
"""v1.4.39 回归钉：常驻 TTS worker 启动互斥（至多一个常驻）。

风险链（交接文档 P0-2）：ask 失败/超时故意不杀进程 × _start 只让 Ollama 让路、
不看兄弟 worker × 引擎降级循环 / TTS 线程池 / 试听入口 → qwen3tts(5.2GB) +
indextts2(6.6GB) = 11.8GB 在 12GB 卡上擦边共存 → WDDM 静默换页 → 双双变慢。
修法：_START_LOCK 全局串行启动 + 启动前 _stop_idle_engine 停兄弟的空闲 worker。

运行：pytest tests/test_worker_mutex.py -q
"""
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tts2_engines as T2  # noqa: E402


class _FakeProc:
    """够 _start_inner/_stop_idle_engine 用的假进程。"""

    def __init__(self, lines=()):
        self.stdout = iter(list(lines))
        self.killed = False
        self.stdin = self

    def kill(self):
        self.killed = True

    def write(self, *a):
        pass

    def flush(self):
        pass

    def close(self):
        pass

    def poll(self):
        return None


def _fake_srv(name, proc=None):
    srv = T2._Serve(name, ['nope'], need_mb=T2.VRAM_NEED[name])
    srv.starting = False
    srv.proc = proc
    return srv


def test_stop_idle_engine_stops_idle_worker():
    proc = _FakeProc()
    srv = _fake_srv('qwen3tts', proc)
    assert T2._stop_idle_engine(srv) is True
    assert srv.proc is None and proc.killed, '空闲 worker 应被停掉'


def test_stop_idle_engine_spares_busy_worker():
    """0.5s 内拿不到锁 = 正在合成：让路不杀（杀在忙的等于打断别人的句子）。"""
    proc = _FakeProc()
    srv = _fake_srv('qwen3tts', proc)
    srv.lock.acquire()                      # 模拟 ask 全程持锁
    try:
        t0 = time.time()
        assert T2._stop_idle_engine(srv, wait=0.3) is False
        assert time.time() - t0 < 2.0, '必须是有界等待，不能阻塞到句子算完'
        assert srv.proc is proc and not proc.killed, '在忙的 worker 不许被动'
    finally:
        srv.lock.release()
    assert T2._stop_idle_engine(srv) is True, '闲下来之后才停'


def test_stop_idle_engine_spares_starting_worker():
    srv = _fake_srv('indextts2', _FakeProc())
    srv.starting = True
    assert T2._stop_idle_engine(srv) is False, '正在拉起的杀了等于自杀'
    assert srv.proc is not None


def test_stop_idle_engine_none_is_noop():
    assert T2._stop_idle_engine(None) is False


def test_start_inner_stops_idle_sibling_before_release_vram(monkeypatch, tmp_path):
    """启动 qwen3tts 前必须先停空闲的 indextts2 兄弟（顺序也要对：先停再探显存）。"""
    order = []
    sibling_proc = _FakeProc()
    sibling = _fake_srv('indextts2', sibling_proc)

    real_release_vram = T2.release_vram
    monkeypatch.setattr(T2, '_QWEN', None)
    monkeypatch.setattr(T2, '_IDX', sibling)
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path))

    def _fake_stop_idle(srv, wait=0.5):
        order.append('stop_sibling:%s' % (getattr(srv, 'name', None)))
        return real_release_vram and T2._stop_engine(srv)

    def _fake_release_vram(want=0, wait_sec=15):
        order.append('release_vram')
        return True, ''

    monkeypatch.setattr(T2, '_stop_idle_engine', _fake_stop_idle)
    monkeypatch.setattr(T2, 'release_vram', _fake_release_vram)
    monkeypatch.setattr(T2, 'sync_worker', lambda src: src)
    monkeypatch.setattr(T2, 'child_env', lambda: {})

    def _fake_popen(cmd, **kw):
        order.append('popen')
        return _FakeProc(lines=[json.dumps({'event': 'ready'}) + '\n'])

    monkeypatch.setattr(T2.subprocess, 'Popen', _fake_popen)

    srv = T2._get_qwen()
    try:
        assert srv._start() is True
        assert sibling_proc.killed and sibling.proc is None, '空闲兄弟必须让位'
        assert order == ['stop_sibling:indextts2', 'release_vram', 'popen'], order
    finally:
        # 收尾：别把 fake worker 留给别的测试
        T2._stop_engine(srv)


def test_start_is_globally_serialized(monkeypatch, tmp_path):
    """两引擎并发启动必须串行（_START_LOCK），起我/停他不能交错出并存窗口。"""
    events = []
    gate = threading.Lock()

    def _make(name):
        srv = T2._Serve(name, ['nope'], need_mb=T2.VRAM_NEED[name])

        def _inner():
            with gate:
                events.append('in:%s' % name)
            time.sleep(0.15)
            with gate:
                events.append('out:%s' % name)
            return True

        srv._start_inner = _inner
        return srv

    monkeypatch.setattr(T2, '_QWEN', None)
    monkeypatch.setattr(T2, '_IDX', None)
    a, b = _make('qwen3tts'), _make('indextts2')
    ts = [threading.Thread(target=s._start) for s in (a, b)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    ins = [i for i, e in enumerate(events) if e.startswith('in:')]
    outs = [i for i, e in enumerate(events) if e.startswith('out:')]
    assert len(ins) == 2 and len(outs) == 2
    second_in = max(ins)
    first_out = min(outs)
    assert first_out < second_in, (
        '两个引擎的启动重叠了（%r）：停兄弟→起自己不是原子的' % events)


def test_get_engines_lazy_init_is_race_safe(monkeypatch):
    """并发 _get_qwen/_get_idx 必须返回同一单例（否则孤儿 worker 进程）。"""
    monkeypatch.setattr(T2, '_QWEN', None)
    monkeypatch.setattr(T2, '_IDX', None)
    got = [[], []]
    bar = threading.Barrier(8)

    def _grab(slot):
        bar.wait()
        got[slot].append(T2._get_qwen())

    ts = [threading.Thread(target=_grab, args=(i % 2,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    uniq = {id(s) for pair in got for s in pair}
    assert len(uniq) == 1, '懒初始化竞态造出了 %d 个 _Serve 实例' % len(uniq)

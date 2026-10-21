# -*- coding: utf-8 -*-
"""F9 回归：排队任务启动运行后必须登记 / 清除 active_task，否则崩溃恢复与
UI 活动态对排队任务失效。直接任务路径已做，这里只测排队路径（_queued_runner）。

用一个假任务塞进队列，monkeypatch 掉写盘/积分等副作用，启动后断言
_save_active_task 在运行开始被调用、_clear_active_task 在结束后被调用。
"""
import threading
import time

import webui_server as _w
import workflows as W


def test_queued_task_registers_and_clears_active_task(monkeypatch, tmp_path):
    # 隔离队列与名额，避免影响真实运行态
    monkeypatch.setattr(_w, '_TASK_QUEUE', [])
    monkeypatch.setattr(_w, '_TASK_SEM', threading.Semaphore(1))
    # 去掉写盘 / 积分等副作用
    monkeypatch.setattr(_w, '_persist_queue_unlocked', lambda: None)
    monkeypatch.setattr(_w, '_finish_task_credits', lambda *a, **k: None)

    calls = {'save': 0, 'clear': 0}
    monkeypatch.setattr(_w, '_save_active_task',
                        lambda *a, **k: calls.__setitem__('save', calls['save'] + 1))
    monkeypatch.setattr(_w, '_clear_active_task',
                        lambda *a, **k: calls.__setitem__('clear', calls['clear'] + 1))

    started = threading.Event()

    def _fake_fn(req, prog):
        started.set()

    rd = str(tmp_path)
    prog = {'phase': '排队中', 'pct': 0, 'run_dir': rd, 'done': False}
    _w._TASK_QUEUE.append((_fake_fn, {}, 'run-q1', rd, prog))

    W._start_next_queued()  # 实际是 _w._start_next_queued（注入的同名函数）
    assert started.wait(timeout=5), '排队任务应被启动运行'
    # 让 runner 线程走到 finally（清除 active_task）
    time.sleep(0.4)

    assert calls['save'] >= 1, '排队任务开始运行必须登记 active_task：%s' % calls
    assert calls['clear'] >= 1, '排队任务结束必须清除 active_task：%s' % calls

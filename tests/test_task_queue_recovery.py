# -*- coding: utf-8 -*-
"""T3 回归：任务排队不动 / 并发名额回收。

背景（task-5）：主人现场踩到「任务显示卡在『配音已生成（N/N段），请确认后合成』，
新提交的合成任务一直排队不动，手工清 task_queue.json + 重启服务才恢复」。
原归因「卡在该状态时占住 _TASK_SEM 名额不放」已被实测**证伪**（名额在 fn 返回后
的 finally 里归还，handler.py:331-333）。本文件把查清后的三层事实分别钉住：

  ① 机制必然：2 个名额被**真正未结束**的任务占满时，第 3 个必然排队——
     这是 _TASK_SEM 的设计语义，不是缺陷，但排队任务必须在前面任务收尾后自动启动。
  ② 可观测性误导：prog['done'] / phase 与「名额是否归还」**不等价**。
     movie_narrator.py:6779-6795 先写 phase+awaiting_confirm、再置 done=True，
     **之后**才做封面 ffmpeg 收尾并 return；此窗口内前端已显示「请确认后合成」，
     名额却仍被占着。锁住这个窗口的存在，避免后人把它误诊成「泄漏」。
  ③ 真泄漏（本次修复）：`_start_next_queued()` 里 `t.start()` 若抛
     RuntimeError（线程耗尽），旧实现会让 :1182 已拿到的名额永不归还、且该排队项
     已被 :1176 pop 掉而彻底消失。handler.py:344 的直接任务路径有 try 兜底，
     排队路径此前没有——本文件钉住修复后的行为。

全部用例确定性、离线：不依赖 ffmpeg / TTS / 网络 / 8899 实例。
"""
import threading
import time

import pytest

import webui_server as W
import workflows as WF


def _wait_until(pred, timeout=5.0, interval=0.02):
    """轮询等待条件成立（避免固定 sleep 在全量并发跑时随机红）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


@pytest.fixture(autouse=True)
def _isolate_queue(monkeypatch, tmp_path):
    """隔离队列 / 名额 / 落盘副作用。

    conftest 已把 W.OUTDIR 指向 tmp_path，_task_queue_path() 因而指向测试专用路径；
    这里再把队列、信号量、落盘与收尾副作用全部替掉，保证用例互不干扰且不写真实目录。
    """
    monkeypatch.setattr(W, '_TASK_QUEUE', [])
    monkeypatch.setattr(W, '_TASK_QUEUE_LOCK', threading.Lock())
    monkeypatch.setattr(W, '_TASK_SEM', threading.Semaphore(2))
    monkeypatch.setattr(W, '_persist_queue_unlocked', lambda: None)
    monkeypatch.setattr(W, '_finish_task_credits', lambda *a, **k: None)
    monkeypatch.setattr(WF._w, '_finish_task_credits', lambda *a, **k: None)
    yield


def _mk_prog(runid, run_dir=''):
    return {'phase': '排队中（前面还有1个任务）', 'pct': 0, 'runid': runid,
            'run_dir': run_dir, 'done': False, 'queued': True}


# ---------------------------------------------------------------------------
# ① 机制必然：名额满 → 排队；前面收尾 → 自动启动
# ---------------------------------------------------------------------------
def test_third_task_queues_while_two_slots_held_then_auto_starts():
    """2 个名额被真在跑的任务占满时，第 3 个必须排队且不启动；释放后自动启动。"""
    holds = {}
    lock = threading.Lock()

    def _long(req, prog):
        rid = prog.get('runid')
        ev = threading.Event()
        with lock:
            holds[rid] = ev
        ev.wait(timeout=10)
        prog['done'] = True

    def _release(rid):
        assert _wait_until(lambda: rid in holds), '任务 %s 应已注册' % rid
        with lock:
            holds[rid].set()

    def _hold_count():
        with lock:
            return len(holds)

    h = W.Handler.__new__(W.Handler)   # _spawn 不依赖实例属性
    r1 = h._spawn(_long, {})
    assert _wait_until(lambda: _hold_count() == 1), '第 1 个任务应启动'
    assert W._TASK_SEM._value == 1, '1 个任务在跑 → 剩 1 个名额'
    r2 = h._spawn(_long, {})
    assert _wait_until(lambda: _hold_count() == 2), '第 2 个任务应启动'
    assert W._TASK_SEM._value == 0, '两个名额应被占满'

    started3 = threading.Event()

    def _third(req, prog):
        prog['phase'] = '开始执行'
        prog['done'] = True
        started3.set()

    r3 = h._spawn(_third, {})
    assert W.PROGRESS[r3].get('queued') is True, '第 3 个任务应标记为排队'
    assert not started3.wait(0.3), '名额满时排队任务不得启动'
    assert W._TASK_SEM._value == 0

    _release(r1)                          # 放掉一个 → 第 3 个应自动接上
    assert started3.wait(5), '前面任务收尾后排队任务必须自动启动'
    assert _wait_until(lambda: len(W._TASK_QUEUE) == 0), '队列应被清空'

    _release(r2)
    assert _wait_until(lambda: W._TASK_SEM._value == 2), '全部收尾后名额必须回到满值'


# ---------------------------------------------------------------------------
# ② 可观测性误导：done=True / phase=「请确认后合成」时名额仍被占着
# ---------------------------------------------------------------------------
def test_done_true_is_not_equivalent_to_slot_released():
    """Phase1 写序复刻（movie_narrator.py:6779-6795）：done 置位时名额尚未归还。

    这是原归因「等确认时占着名额」看起来成立的真实来源——但它是**收尾未完成的
    瞬态窗口**（后面还有封面 ffmpeg 等），不是「永久占住」。
    """
    entered = threading.Event()
    gate = threading.Event()
    observed = {}

    def _phase1(req, prog):
        prog['phase'] = '配音已生成（52/52段），请确认后合成'
        prog['pct'] = 100
        prog['diag'] = {'awaiting_confirm': True}
        prog['done'] = True                 # movie_narrator.py:6795 done 最后置位
        observed['sem_at_done'] = W._TASK_SEM._value
        entered.set()
        gate.wait(timeout=10)               # 真实链路里是封面 ffmpeg 等收尾
        observed['sem_at_return'] = W._TASK_SEM._value

    h = W.Handler.__new__(W.Handler)
    rid = h._spawn(_phase1, {})
    assert entered.wait(5), 'Phase1 应进入「done 已置位」窗口'

    prog = W.PROGRESS[rid]
    assert prog['done'] is True
    assert prog['phase'] == '配音已生成（52/52段），请确认后合成'
    assert (prog.get('diag') or {}).get('awaiting_confirm') is True
    # 关键断言：前端已显示「done + 请确认后合成」，但名额【仍被占着】= 1（上限 2）
    assert observed['sem_at_done'] == 1, (
        'done/phase 与「名额已归还」不等价：done 置位时名额应仍被该任务占着')

    gate.set()                              # fn 返回 → finally 才 release
    assert _wait_until(lambda: W._TASK_SEM._value == 2), (
        'fn 返回后名额必须归还（handler.py:331-333）；若此处超时则是真泄漏')
    assert observed['sem_at_return'] == 1, (
        'return 之前（仍在 fn 内）名额仍应被占着')


# ---------------------------------------------------------------------------
# ③ 真泄漏修复：_start_next_queued 的 t.start() 失败必须归还名额 + 放回队列项
# ---------------------------------------------------------------------------
def test_start_next_queued_returns_slot_when_thread_start_fails(monkeypatch):
    """线程耗尽（start() 抛 RuntimeError）时：名额归还 + 队列项放回，不丢任务。

    旧实现 t.start() 在 try/finally 之外 → 名额永久泄漏、任务从队列消失。
    """
    monkeypatch.setattr(W, '_TASK_SEM', threading.Semaphore(1))   # 有 1 个空名额
    assert W._TASK_SEM._value == 1

    prog = _mk_prog('run-fail')
    W._TASK_QUEUE.append((lambda req, p: None, {}, 'run-fail', '', prog))

    orig_start = threading.Thread.start

    def _boom(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, 'start', _boom)
    WF._start_next_queued()             # 不应向外抛（在 finally 里被调用）

    assert W._TASK_SEM._value == 1, (
        'start() 失败后名额必须归还，否则永久泄漏（旧 bug）')
    assert len(W._TASK_QUEUE) == 1, '失败的任务必须放回队列，不能凭空消失'
    got = W._TASK_QUEUE[0]
    assert got[2] == 'run-fail', '放回的应是同一个 runid（保持 FIFO 队首）'
    assert got[4].get('queued') is True, '放回后应重新标记为排队中'
    # 位置显示由重排循环（唯一写入点）按 `i + 1` 口径给出；队伍只有它自己 → 1。
    assert got[4].get('phase') == '排队中（前面还有1个任务）', (
        '放回后应重排「前面还有N个」的位置显示')

    # 具备启动条件后，该任务仍能被正常启动（证明它不是「死了」）
    monkeypatch.setattr(threading.Thread, 'start', orig_start)
    started = threading.Event()
    W._TASK_QUEUE.clear()
    W._TASK_QUEUE.append((lambda req, p: started.set(), {}, 'run-fail', '', _mk_prog('run-fail')))
    spawned = []
    _real_start = threading.Thread.start

    def _rec_start(self):
        _real_start(self)
        spawned.append(self)

    monkeypatch.setattr(threading.Thread, 'start', _rec_start)
    WF._start_next_queued()
    assert started.wait(5), '名额恢复后放回的任务应能被正常启动'
    # 必须等线程真正跑完 finally 再结束用例：否则它稍后执行的 `_TASK_SEM.release()`
    # 会落到**下一个用例**刚装上的新信号量上（_value 2→3），让
    # test_start_next_queued_no_slot_leak_on_normal_path 偶发红（门禁漂移）。
    for _th in spawned:
        _th.join(timeout=5)
    assert all(not _th.is_alive() for _th in spawned), '后台线程必须在用例内跑完，不得跨用例泄漏'


def test_start_next_queued_no_slot_leak_on_normal_path():
    """正常路径回归：排队任务启动→收尾后名额回到满值，队列清空。"""
    done = threading.Event()
    prog = _mk_prog('run-ok')
    W._TASK_QUEUE.append((lambda req, p: done.set(), {}, 'run-ok', '', prog))
    WF._start_next_queued()
    assert done.wait(5), '排队任务应被启动'
    assert _wait_until(lambda: W._TASK_SEM._value == 2), '收尾后名额必须回到满值'
    assert prog.get('queued') is False
    assert prog.get('phase') == '开始执行'
    assert len(W._TASK_QUEUE) == 0


def test_start_next_queued_puts_item_back_when_slot_taken(monkeypatch):
    """极端竞态：出列后名额又被占 → 放回队首且不丢名额（既有兜底，回归锁定）。"""
    monkeypatch.setattr(W, '_TASK_SEM', threading.Semaphore(1))
    W._TASK_SEM.acquire()               # 名额被别的任务抢走
    prog = _mk_prog('run-race')
    W._TASK_QUEUE.append((lambda req, p: None, {}, 'run-race', '', prog))

    WF._start_next_queued()

    assert W._TASK_SEM._value == 0, '抢不到名额时不得凭空多出名额'
    assert len(W._TASK_QUEUE) == 1, '任务应放回队列'
    assert W._TASK_QUEUE[0][2] == 'run-race'


def test_evict_finished_progress_never_drops_unfinished(monkeypatch):
    """候选④复核：未结束（done=False）的条目即便不在 RUN_PROCS 也不得被淘汰。

    旧条件的坑：RUN_PROCS 仅 ffmpeg 真执行时有条目，正在跑 Whisper/LLM/配音的
    任务会被误淘汰 → 前端查不到、取消失效、任务白跑完还占着名额。
    """
    monkeypatch.setattr(W, 'PROGRESS', {})
    W.PROGRESS['run-live'] = {'phase': '配音生成中', 'pct': 30, 'done': False}
    W.PROGRESS['run-old'] = {'phase': '完成', 'pct': 100, 'done': True}
    for i in range(120):
        W.PROGRESS['run-fill-%d' % i] = {'phase': '完成', 'pct': 100, 'done': True}

    W._evict_finished_progress(keep=100)

    assert 'run-live' in W.PROGRESS, '未结束的任务绝不能被淘汰'
    assert 'run-old' not in W.PROGRESS, '已结束的旧条目应被淘汰'
    assert len(W.PROGRESS) <= 101, '淘汰后条目数应回落到 keep 附近'

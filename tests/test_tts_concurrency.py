# -*- coding: utf-8 -*-
"""S6 回归：_generate_all_tts 并发生成（本地 edge 引擎）。

锁定：
  1. edge 可用 + TTS_CONCURRENCY>1 → 多段并行合成（max_active>=2）
  2. TTS_CONCURRENCY=1 → 完全回退串行（max_active==1）
  3. 断点续跑：已存在的 narr%d.mp3 跳过，只生成缺失段
  4. 失败段第二轮重试补上
  5. mimo（云端）路径不并发（保守）
"""
import os
import threading
import time

# 先 import webui_server 建立模块入口，再 import movie_narrator：
# 直接导入 movie_narrator 会触发 video_render._host() → import webui_server → 回读半初始化的
# video_render（_NAR_CPS 等尚未定义）→ ImportError（循环依赖，webui 入口方向是安全的）。
import webui_server as S  # noqa: F401  建立 webui_server 完整初始化入口
del S
import movie_narrator as M  # noqa: E402


def _fake_state():
    return {'cur': 0, 'max': 0, 'calls': 0, 'lock': threading.Lock()}


def _install_base_mocks(monkeypatch, state):
    """公共 mock：无 mimo、edge 可用、无熔断、无取消。"""
    monkeypatch.setattr(M, 'load_ai_config', lambda: {'tts': {}})
    monkeypatch.setattr(M, 'edge_tts_available', lambda: True)
    monkeypatch.setattr(M, 'edge_tts_dead_reason', lambda: '')
    monkeypatch.setattr(M, '_edge_probe_recover', lambda rd: None)
    monkeypatch.setattr(M, '_aborted', lambda: False)
    monkeypatch.setattr(M.time, 'sleep', lambda s: None)   # 重试轮等待不真睡

    def fake_speak(text, out_path):
        with state['lock']:
            state['cur'] += 1
            state['max'] = max(state['max'], state['cur'])
            state['calls'] += 1
        time.sleep(0.03)   # 模拟合成耗时，拉大并发窗口
        with open(out_path, 'wb') as f:
            f.write(b'0' * 2048)
        with state['lock']:
            state['cur'] -= 1
        return True, 'edge', out_path

    monkeypatch.setattr(M, 'local_tts_speak', fake_speak)


def _run(narr, run_dir, monkeypatch, concurrency=None):
    if concurrency is not None:
        monkeypatch.setenv('TTS_CONCURRENCY', str(concurrency))
    else:
        monkeypatch.delenv('TTS_CONCURRENCY', raising=False)
    state = _fake_state()
    _install_base_mocks(monkeypatch, state)
    prog = {'phase': '', 'pct': 0}
    res = M._generate_all_tts(narr, str(run_dir), progress=prog)
    return res, state, prog


def test_tts_generates_concurrently(monkeypatch, tmp_path):
    """默认路径（本地 edge）3 路并发：活跃窗口 >= 2，且全部生成。"""
    narr = ['第%d句' % i for i in range(6)]
    res, state, prog = _run(narr, tmp_path, monkeypatch, concurrency=3)
    assert len(res) == 6
    assert state['max'] >= 2, '并发未生效：max_active=%d' % state['max']
    for i in range(6):
        assert os.path.exists(os.path.join(str(tmp_path), 'narr%d.mp3' % i))
    assert '逐段配音' in prog.get('phase', ''), prog.get('phase')
    assert prog.get('pct', 0) > 55, '进度应越过 55 起步区间'


def test_tts_serial_when_concurrency_one(monkeypatch, tmp_path):
    """TTS_CONCURRENCY=1：完全回退串行（行为与旧版等价）。"""
    narr = ['第%d句' % i for i in range(6)]
    res, state, _prog = _run(narr, tmp_path, monkeypatch, concurrency=1)
    assert len(res) == 6
    assert state['max'] == 1, '并发=1 时必须串行：max_active=%d' % state['max']


def test_tts_skips_existing_segments(monkeypatch, tmp_path):
    """断点续跑：预置第 2 段 → 只生成缺失段，跳过计数正确。"""
    narr = ['句0', '句1', '句2', '句3']
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(os.path.join(str(tmp_path), 'narr1.mp3'), 'wb') as f:
        f.write(b'0' * 2048)
    res, state, _prog = _run(narr, tmp_path, monkeypatch, concurrency=3)
    assert len(res) == 4, '应返回全部段（含跳过的缓存段）'
    assert state['calls'] == 3, '只应生成 3 个缺失段，实际 %d' % state['calls']
    assert all(i in [r[0] for r in res] for i in range(4))


def test_tts_retries_failed_segment(monkeypatch, tmp_path):
    """第一轮失败段在重试轮补上。"""
    narr = ['句0', '句1']
    calls = {'n': 0}

    def flaky(text, out_path):
        calls['n'] += 1
        if calls['n'] == 1:          # 第一段第一轮失败
            return False, 'edge', None
        with open(out_path, 'wb') as f:
            f.write(b'0' * 2048)
        return True, 'edge', out_path

    monkeypatch.setattr(M, 'load_ai_config', lambda: {'tts': {}})
    monkeypatch.setattr(M, 'edge_tts_available', lambda: True)
    monkeypatch.setattr(M, 'edge_tts_dead_reason', lambda: '')
    monkeypatch.setattr(M, '_edge_probe_recover', lambda rd: None)
    monkeypatch.setattr(M, '_aborted', lambda: False)
    monkeypatch.setattr(M.time, 'sleep', lambda s: None)
    monkeypatch.setattr(M, 'local_tts_speak', flaky)
    # 串行模式保证调用顺序确定
    res = M._generate_all_tts(narr, str(tmp_path), progress={})
    assert len(res) == 2, '重试后应两段齐全：%r' % res
    assert calls['n'] >= 2


def test_tts_mimo_stays_serial(monkeypatch, tmp_path):
    """云端 mimo 路径保持串行且走 ai_tts，不触碰 local_tts_speak。"""
    monkeypatch.setattr(M, 'load_ai_config', lambda: {'tts': {'api_key': 'k', 'model': 'm'}})
    monkeypatch.setattr(M, 'edge_tts_dead_reason', lambda: '')
    monkeypatch.setattr(M, '_edge_probe_recover', lambda rd: None)
    monkeypatch.setattr(M, '_aborted', lambda: False)
    monkeypatch.setattr(M.time, 'sleep', lambda s: None)
    local_calls = {'n': 0}

    def fake_local(text, out_path):
        local_calls['n'] += 1
        return True, 'sapi', out_path

    monkeypatch.setattr(M, 'local_tts_speak', fake_local)
    seen = {'active': 0, 'max': 0}

    def fake_ai_tts(text, out_path):
        seen['active'] += 1
        seen['max'] = max(seen['max'], seen['active'])
        time.sleep(0.03)
        with open(out_path, 'wb') as f:
            f.write(b'0' * 2048)
        seen['active'] -= 1
        return True

    monkeypatch.setattr(M._w, 'ai_tts', fake_ai_tts)
    res = M._generate_all_tts(['甲', '乙', '丙'], str(tmp_path), progress={})
    assert len(res) == 3
    assert seen['max'] == 1, 'mimo 必须串行：max_active=%d' % seen['max']
    assert local_calls['n'] == 0, 'mimo 成功时不走本地引擎'

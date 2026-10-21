# -*- coding: utf-8 -*-
"""TTS 引擎降级回归：降级必须可见，不能让用户拿到机械音还蒙在鼓里。

真机 P0-1 实测：edge-tts（晓晓/云希，自然）连不上微软朗读服务时，
管线静悄悄把 4 段配音全部落到 SAPI（系统自带，机械感重），
tts_log 里 success=4、failed=0、无一条错误，
用户在成片里听到机械音只以为「配音就这样」——直接违反「不静默」红线。
"""
import os

import pytest

import webui_server as S  # noqa: F401  先导入宿主（符号晚绑定）
import movie_narrator as MN
import tts_engines as T


def test_sapi_degradation_is_surfaced():
    """A. 实际用了 SAPI 时，进度里必须有明确告警。"""
    prog = {}
    diag = [{'idx': i, 'engine': 'sapi', 'ok': True, 'error': '', 'duration_ms': 1}
            for i in range(3)]
    MN._warn_tts_engine_degraded(diag, prog)
    assert prog.get('tts_engine_degraded') is True, '降级到 SAPI 必须打标记'
    assert 'SAPI' in (prog.get('warn') or ''), '用户必须看到降级原因：%r' % prog.get('warn')
    assert prog.get('tts_engine') == 'sapi'


def test_normal_engine_no_false_alarm():
    """A2. 正常使用 edge 时不得误报降级。"""
    prog = {}
    diag = [{'idx': 0, 'engine': 'edge', 'ok': True, 'error': '', 'duration_ms': 1},
            {'idx': 1, 'engine': 'edge', 'ok': True, 'error': '', 'duration_ms': 1}]
    MN._warn_tts_engine_degraded(diag, prog)
    assert prog.get('tts_engine_degraded') is None
    assert not (prog.get('warn') or ''), 'edge 正常工作时不应告警'


def test_warn_tts_handles_empty_and_no_progress():
    """A3. 边界：空诊断 / 无 progress 时不得抛异常。"""
    MN._warn_tts_engine_degraded([], {})
    MN._warn_tts_engine_degraded(None, None)
    prog = {}
    MN._warn_tts_engine_degraded([{'idx': 0, 'engine': 'cache', 'ok': True}], prog)
    assert 'tts_engine' not in prog, 'cache 命中不算真实引擎，不该记录'


def test_edge_availability_checks_reachability(monkeypatch):
    """B. 只装了模块但服务不可达时，必须判为不可用。
    否则每段都要白跑满 _EDGE_RETRY 轮重试，还误导并发路径判断。"""
    monkeypatch.setattr(T, 'edge_tts_dead_reason', lambda: '')
    monkeypatch.setattr(T, '_edge_tts_installed', lambda: True)
    T._EDGE_NET_CACHE['ok'] = None
    T._EDGE_NET_CACHE['at'] = 0.0

    def _no_conn(*a, **k):
        raise OSError('network unreachable')
    monkeypatch.setattr('socket.create_connection', _no_conn)
    assert T.edge_tts_available() is False, '服务不可达时必须判不可用'

    monkeypatch.setattr('socket.create_connection', lambda *a, **k: _FakeSock())
    T._EDGE_NET_CACHE['ok'] = None
    T._EDGE_NET_CACHE['at'] = 0.0
    assert T.edge_tts_available() is True


def test_edge_availability_false_when_not_installed(monkeypatch):
    """B2. 没装模块就是不可用，哪怕网络通。"""
    monkeypatch.setattr(T, 'edge_tts_dead_reason', lambda: '')
    monkeypatch.setattr(T, '_edge_tts_installed', lambda: False)
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: _FakeSock())
    T._EDGE_NET_CACHE['ok'] = None
    T._EDGE_NET_CACHE['at'] = 0.0
    assert T.edge_tts_available() is False


def test_drop_empty_removes_zero_byte_artifact(tmp_path):
    """C. 失败留下的 0 字节 mp3 必须被清理，不能赖在产物目录。"""
    p = str(tmp_path / 'narr0.mp3')
    open(p, 'wb').close()
    T._drop_empty(p)
    assert not os.path.exists(p), '0 字节残留必须删除'


def test_drop_empty_keeps_real_audio(tmp_path):
    """C2. 有效音频不得被误删（这是破坏性操作，必须保守）。"""
    p = str(tmp_path / 'narr1.mp3')
    with open(p, 'wb') as f:
        f.write(b'\xff\xfb' * 5000)   # 10KB，远大于 100 字节阈值
    T._drop_empty(p)
    assert os.path.exists(p), '有效音频不得被删除'


def test_drop_empty_tolerates_missing_file(tmp_path):
    """C3. 文件不存在时不得抛异常。"""
    T._drop_empty(str(tmp_path / 'nope.mp3'))


class _FakeSock:
    def close(self):
        pass
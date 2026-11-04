# -*- coding: utf-8 -*-
"""TTS 引擎降级回归：降级必须可见，不能让用户拿到机械音还蒙在鼓里。

真机 P0-1 实测：edge-tts（晓晓/云希，自然）连不上微软朗读服务时，
管线静悄悄把 4 段配音全部落到 SAPI（系统自带，机械感重），
tts_log 里 success=4、failed=0、无一条错误，
用户在成片里听到机械音只以为「配音就这样」——直接违反「不静默」红线。
"""
import os


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
    """B. 三条件（已装 + 未熔断 + 服务端可达）缺一即不可用。

    只查「装没装」会造成假阳性：edge 是联网服务，网络不通时每段都去跑满
    子进程超时，长视频直接卡死几十分钟。"""
    monkeypatch.setattr(T, 'edge_tts_dead_reason', lambda: '')
    monkeypatch.setattr(T, '_edge_tts_installed', lambda: True)

    monkeypatch.setattr(T, '_edge_reachable', lambda *a, **k: False)
    assert T.edge_tts_available() is False, '服务不可达时必须判不可用'

    monkeypatch.setattr(T, '_edge_reachable', lambda *a, **k: True)
    assert T.edge_tts_available() is True


def test_edge_reachable_uses_tls_not_just_tcp(monkeypatch):
    """B3. 连通性探测必须做 TLS 握手，只建 TCP 会误判。

    实测本机能连上 443（TCP OK）但 TLS 握手被拦：只探 TCP 会认为 edge 可用，
    结果每段都卡满超时。探测还必须对准 edge 实际用的朗读服务域名——
    用「任一域名通即可」是错的（login.live.com 通而朗读服务被拦时仍会误判）。
    """
    import socket
    import ssl
    assert T._EDGE_HOSTS == ('speech.platform.bing.com',), '必须只探实际依赖的端点'
    T._EDGE_NET_CACHE['ok'] = None
    T._EDGE_NET_CACHE['at'] = 0.0
    # TCP 通但 TLS 失败 → 必须判不可达
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: _FakeSock())
    monkeypatch.setattr(ssl.SSLContext, 'wrap_socket',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('TLS reset')))
    assert T._edge_reachable() is False, 'TCP 通但 TLS 握手失败时必须判不可达'


def test_edge_availability_false_when_not_installed(monkeypatch):
    """B2. 没装模块就是不可用，哪怕网络通。"""
    monkeypatch.setattr(T, 'edge_tts_dead_reason', lambda: '')
    monkeypatch.setattr(T, '_edge_tts_installed', lambda: False)
    monkeypatch.setattr(T, '_edge_reachable', lambda *a, **k: True)
    assert T.edge_tts_available() is False


def test_edge_subproc_timeout_is_bounded():
    """B4. edge 子进程超时必须有上限且够短。

    原值 180s 在服务端连不上时每轮跑满超时：3 轮 × 数十段 = 卡死几十分钟，
    长视频可达数小时，用户只看到进度条不动。下限也要留够正常合成时间。"""
    assert 5 <= T._EDGE_SUBPROC_TIMEOUT <= 60, \
        '超时过长会卡死合成、过短会误杀正常请求：%s' % T._EDGE_SUBPROC_TIMEOUT


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
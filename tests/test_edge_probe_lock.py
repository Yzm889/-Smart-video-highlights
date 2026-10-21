# -*- coding: utf-8 -*-
"""F7 回归：edge 连通/安装探测缓存必须加锁，避免并发 worker 各起一次
25s 子进程 / TLS 握手。并发调用时，昂贵的探测只应执行一次（其余等待复用结果）。
"""
import socket
import threading

import tts_engines as T
import importlib.util as _iu


def test_edge_probes_run_once_under_concurrency(monkeypatch):
    # 保存并强制缓存失效
    saved_inst = dict(T._EDGE_INSTALLED_CACHE)
    saved_net = dict(T._EDGE_NET_CACHE)
    T._EDGE_INSTALLED_CACHE.update(val=None, ts=0)
    T._EDGE_NET_CACHE.update(ok=None, at=0.0)
    probes = {'installed': 0, 'reachable': 0}
    try:
        # edge-tts 未安装：find_spec -> None，subprocess 返回非 0
        monkeypatch.setattr(_iu, 'find_spec', lambda *a, **k: None)

        def _fake_run(cmd, **k):
            probes['installed'] += 1
            class _R:
                returncode = 1
                stderr = b''
            return _R()
        monkeypatch.setattr(T.subprocess, 'run', _fake_run)

        # 网络不可达：create_connection 直接抛错（函数内 `import socket` 取到的是 sys.modules 里的）
        def _fake_conn(*a, **k):
            probes['reachable'] += 1
            raise OSError('unreachable')
        monkeypatch.setattr(socket, 'create_connection', _fake_conn)

        def _worker():
            T._edge_tts_installed()
            T._edge_reachable()

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert probes['installed'] == 1, '安装探测应只跑一次：%s' % probes
        assert probes['reachable'] == 1, '连通探测应只跑一次：%s' % probes
    finally:
        T._EDGE_INSTALLED_CACHE.clear()
        T._EDGE_INSTALLED_CACHE.update(saved_inst)
        T._EDGE_NET_CACHE.clear()
        T._EDGE_NET_CACHE.update(saved_net)

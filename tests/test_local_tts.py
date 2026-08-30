"""本地配音引擎回归测试。

背景：界面上原先只有「云端 TTS（要 Key）」的选项，本地配音只能吃系统 SAPI，
既没有引擎选择也没有下载入口（多数 Windows 还只有一个中文音色）。
本文件锁定：配置归一化、引擎选择优先级、失败回退、edge-tts 熔断。
"""

import webui_server as S


def _ok_edge(text, out_path, *a, **k):
    with open(out_path, 'wb') as f:
        f.write(b'0' * 2048)
    return True


def _ok_sapi(text, out_path, *a, **k):
    with open(out_path, 'wb') as f:
        f.write(b'0' * 2048)
    return True


def test_tts_local_cfg_normalizes_rate(monkeypatch):
    """语速输入容错：'10' / '10%' / '+10%' 都归一成 '+10%'。"""
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'tts_local': {'engine': 'edge', 'rate': '10'}})
    assert S.tts_local_cfg()['rate'] == '+10%'
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'tts_local': {'rate': '-20%'}})
    assert S.tts_local_cfg()['rate'] == '-20%'
    monkeypatch.setattr(S, 'load_ai_config', lambda: {})
    cfg = S.tts_local_cfg()
    assert cfg['engine'] == 'auto' and cfg['voice'] == 'zh-CN-XiaoxiaoNeural'


def test_local_tts_prefers_edge_then_falls_back(monkeypatch, tmp_path):
    """auto：优先 edge-tts；edge 不可用时退到离线模型；再退到系统 SAPI。"""
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'tts_local': {'engine': 'auto'}})
    out = str(tmp_path / 'a.mp3')

    monkeypatch.setattr(S, 'edge_tts_available', lambda: True)
    monkeypatch.setattr(S, 'edge_tts_speak', _ok_edge)
    monkeypatch.setattr(S, 'sherpa_tts_available', lambda: True)
    monkeypatch.setattr(S, 'sherpa_tts_speak', lambda t, p, speed=1.0: False)
    monkeypatch.setattr(S, 'sapi_tts', _ok_sapi)
    ok, eng, _p = S.local_tts_speak('测试', out)
    assert (ok, eng) == (True, 'edge')

    monkeypatch.setattr(S, 'edge_tts_available', lambda: False)
    monkeypatch.setattr(S, 'sherpa_tts_speak', lambda t, p, speed=1.0: _ok_edge(t, p))
    ok, eng, _p = S.local_tts_speak('测试', out)
    assert (ok, eng) == (True, 'sherpa')

    monkeypatch.setattr(S, 'sherpa_tts_available', lambda: False)
    ok, eng, _p = S.local_tts_speak('测试', out)
    assert (ok, eng) == (True, 'sapi')


def test_local_tts_respects_explicit_engine(monkeypatch, tmp_path):
    """显式指定 sapi 时不该偷偷用 edge。"""
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'tts_local': {'engine': 'sapi'}})
    monkeypatch.setattr(S, 'edge_tts_available', lambda: True)
    monkeypatch.setattr(S, 'sherpa_tts_available', lambda: True)
    monkeypatch.setattr(S, 'sapi_tts', _ok_sapi)
    ok, eng, _p = S.local_tts_speak('测试', str(tmp_path / 'b.mp3'))
    assert (ok, eng) == (True, 'sapi')


def test_edge_failure_breaks_circuit(monkeypatch, tmp_path):
    """edge-tts 连不上时连续失败应熔断，避免几十段逐段重试把任务拖成假死。
    subprocess 全部打桩为失败，测试不依赖真实网络。"""
    import subprocess as _sp

    class _R:
        returncode = 1
        stderr = b'Cannot connect to host speech.platform.bing.com'

    S._EDGE_STATE.update(fails=0, dead_until=0.0, reason='')
    monkeypatch.setattr(S.subprocess, 'run', lambda *a, **k: _R())
    monkeypatch.setattr(S, '_EDGE_RETRY', 1)      # 关掉重试，单独验证熔断计数
    monkeypatch.setattr(S, '_EDGE_RETRY_SLEEP', 0)
    out = str(tmp_path / 'c.mp3')
    for _ in range(S._EDGE_MAX_FAILS):
        S.edge_tts_speak('测试', out)
    assert S.edge_tts_dead_reason(), '连续失败后应进入熔断'
    assert S.edge_tts_available() is False, '熔断期内 edge 应视为不可用'

    # 成功一次即复位熔断（subprocess.run 的首个位置参数就是命令列表）
    monkeypatch.setattr(S.subprocess, 'run', lambda *a, **k: _make_ok(a[0]))
    S._EDGE_STATE.update(dead_until=0.0)
    assert S.edge_tts_speak('测试', out) is True
    assert S.edge_tts_dead_reason() == '', '成功后应清除熔断'
    assert S.edge_tts_available() is True


def _make_ok(args):
    """按 edge-tts 命令的最后一个参数写出一个"像样"的 mp3，模拟合成成功。"""
    class _R:
        returncode = 0
        stderr = b''
    out = args[-1]
    if isinstance(out, str) and out.endswith('.mp3'):
        with open(out, 'wb') as f:
            f.write(b'0' * 4096)
    return _R()


def test_edge_speak_retries_on_transient_failure(monkeypatch, tmp_path):
    """实测本机 edge-tts 单次成功率仅约 2/3，连接被随时重置。
    必须有重试，否则动不动就掉到机械感重的离线模型。"""
    calls = {'n': 0}

    class _R:
        returncode = 1
        stderr = b'connection reset'

    def flaky(*a, **k):
        calls['n'] += 1
        if calls['n'] >= 2:            # 第一次失败，第二次成功
            return _make_ok(a[0])
        return _R()

    monkeypatch.setattr(S.subprocess, 'run', flaky)
    monkeypatch.setattr(S, '_EDGE_RETRY_SLEEP', 0)
    S._EDGE_STATE.update(fails=0, dead_until=0.0, reason='')
    out = str(tmp_path / 'r.mp3')
    assert S.edge_tts_speak('测试', out) is True, '瞬时抖动应靠重试救回来'
    assert calls['n'] >= 2
    assert S._EDGE_STATE['fails'] == 0, '重试成功不应计入熔断'


def test_task_locks_tts_engine(monkeypatch, tmp_path):
    """同一任务内引擎必须锁定：否则前半段 edge、后半段掉到离线模型，音色突变。"""
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'tts_local': {'engine': 'auto'}})
    monkeypatch.setattr(S, 'edge_tts_available', lambda: True)
    # 必须打桩真正的合成动作：否则会真的联网（本机 edge 成功率仅约 2/3），测试不稳定
    monkeypatch.setattr(S, 'edge_tts_speak', lambda text, out, *a, **k: _ok_edge(text, out))
    monkeypatch.setattr(S, 'sherpa_tts_available', lambda: True)
    # 必须打桩 sherpa 合成：否则走真实离线合成，依赖本机是否装了 sherpa-onnx 与模型，测试不稳定
    monkeypatch.setattr(S, 'sherpa_tts_speak', lambda t, p, speed=1.0: _ok_edge(t, p))
    monkeypatch.setattr(S, 'sapi_tts', _ok_sapi)

    S._TLS.runid = 'run-tts-lock'
    try:
        if hasattr(S._TLS, 'tts_engine'):
            del S._TLS.tts_engine
        # 第一段：走 edge
        assert S.local_tts_speak('第一句', str(tmp_path / '1.mp3'))[1] == 'edge'
        # 第二段：edge 突然不可用了，必须回退——但要改锁到新引擎，不能还是 edge
        monkeypatch.setattr(S, 'edge_tts_available', lambda: False)
        eng2 = S.local_tts_speak('第二句', str(tmp_path / '2.mp3'))[1]
        assert eng2 == 'sherpa', '锁定的引擎失效后应改锁到备用：%s' % eng2
        assert S._TLS.tts_engine == 'sherpa'
        # 第三段：即使 edge 恢复了，也要继续用 sherpa（保证全片音色一致）
        monkeypatch.setattr(S, 'edge_tts_available', lambda: True)
        assert S.local_tts_speak('第三句', str(tmp_path / '3.mp3'))[1] == 'sherpa'
    finally:
        try:
            del S._TLS.tts_engine
        except Exception:
            pass
        try:
            del S._TLS.runid
        except Exception:
            pass


def test_local_tts_label_reflects_state(monkeypatch):
    """前端要显示「到底用的哪种声音」，文案必须随可用性变化。"""
    monkeypatch.setattr(S, '_tts_available', lambda: False)
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'tts_local': {'engine': 'auto'}})
    monkeypatch.setattr(S, 'edge_tts_available', lambda: True)
    monkeypatch.setattr(S, 'sherpa_tts_available', lambda: False)
    assert 'edge-tts' in S.local_tts_label()
    monkeypatch.setattr(S, 'edge_tts_available', lambda: False)
    assert 'SAPI' in S.local_tts_label()
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'tts_local': {'engine': 'sherpa'}})
    monkeypatch.setattr(S, 'sherpa_tts_available', lambda: True)
    assert '离线' in S.local_tts_label()

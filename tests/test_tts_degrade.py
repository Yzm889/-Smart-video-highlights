# -*- coding: utf-8 -*-
"""task #22 回归：配音「静默质量降级」必须可见。

local_tts_speak 的契约是「要么出声、要么明确失败」，但首选引擎（edge-tts，情感/顿挫最好）
失败后会自动回退系统 SAPI（机器音），返回值仍是 ok=True —— 用户只觉得「这版配音变难听」，
却不知道是引擎降级、也无法自助修复（装 edge-tts / 点「重试配音引擎」）。
这是最典型的一类「不报错但体验劣化」。

修复后：实际引擎 ≠ 首选引擎时记录降级，经 tts_degrade_msg()（/api/tts/voices 的
degrade_msg）暴露给 UI。本文件锁定：① 降级必留痕；② 首选成功不留痕。
"""
import tts_engines as T


def _fresh_state(monkeypatch, edge_ok):
    """搭好一个可控的引擎环境：edge 可用但成败可控，其余引擎不可用，sapi 兜底成功。"""
    for attr in ('tts_engine', 'tts_downgraded', 'tts_engine_fails'):
        try:
            delattr(T._TLS, attr)
        except Exception:
            pass
    with T._TTS_DEGRADE_LOCK:
        T._TTS_DEGRADE.update({'from': None, 'to': None, 'msg': '', 'count': 0})
    monkeypatch.setattr(T, 'tts_local_cfg',
                        lambda: {'engine': 'auto', 'voice': None, 'rate': '+0%'})
    monkeypatch.setattr(T, 'edge_tts_available', lambda: True)
    monkeypatch.setattr(T, 'cosyvoice_available', lambda: False)
    monkeypatch.setattr(T, 'chattts_available', lambda: False)
    monkeypatch.setattr(T, 'sherpa_tts_available', lambda: False)

    def _mk(fn_ok):
        def _speak(text, out):
            if not fn_ok:
                return False
            with open(out, 'wb') as f:
                f.write(b'0' * 1024)
            return True
        return _speak

    monkeypatch.setattr(T, 'edge_tts_speak', _mk(edge_ok))
    monkeypatch.setattr(T, 'sapi_tts', _mk(True))


def test_degrade_edge_to_sapi_is_reported(monkeypatch, tmp_path):
    """首选 edge 失败、落到 sapi：仍返回 ok=True，但必须留下降级提示。"""
    _fresh_state(monkeypatch, edge_ok=False)
    ok, eng, _path = T.local_tts_speak('你好', str(tmp_path / 'a.mp3'))
    assert ok is True and eng == 'sapi', (ok, eng)
    msg = T.tts_degrade_msg()
    assert '降级' in msg and 'edge' in msg, '降级必须可见：%r' % msg


def test_no_degrade_msg_when_preferred_engine_succeeds(monkeypatch, tmp_path):
    """首选 edge 成功 → 本次不产生降级提示。"""
    _fresh_state(monkeypatch, edge_ok=True)
    ok, eng, _path = T.local_tts_speak('你好', str(tmp_path / 'b.mp3'))
    assert ok is True and eng == 'edge', (ok, eng)
    assert T.tts_degrade_msg() == '', '首选引擎成功不应产生降级提示'


def test_degrade_msg_reachable_from_host(monkeypatch):
    """契约：handler 经 _w.tts_degrade_msg 访问，宿主必须 re-export（防运行时 AttributeError）。"""
    import webui_server as S
    assert callable(getattr(S, 'tts_degrade_msg', None)), 'webui_server 必须 re-export tts_degrade_msg'

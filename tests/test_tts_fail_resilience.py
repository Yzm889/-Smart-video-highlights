# -*- coding: utf-8 -*-
"""v1.4.77 失败韧性回归钉。

背景：主人报「偶发失败」——资产缺失静默跳过、音色铸不出静默换示例音、
ask 层无本句重试（只靠编排层整轮兜一次）、失败无分类码。本文件钉住：
ask 分类码 + timeout/crash 自愈重试一次、vram/asset 不浪费重启、
分类码一路流到 diag 与 tts_failures、音色铸造降级可见、engine_status 健康化。
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tts_engines as T  # noqa: E402
import tts2_engines as T2  # noqa: E402
from ffmpeg_utils import _TLS  # noqa: E402


# ---------------------------------------------------------------------------
# ask：分类码 + 自愈重试
# ---------------------------------------------------------------------------

def _fresh_serve():
    return T2._Serve('t', ['x'])


def test_ask_retries_once_on_timeout(monkeypatch):
    """timeout → 重启 worker 本句重试一次；重试成功则整句救回。"""
    srv = _fresh_serve()
    calls, starts = [], []
    def fake_once(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            return False, '合成超时（240 秒）', 'timeout'
        return True, '', ''
    monkeypatch.setattr(srv, '_ask_once', fake_once)
    monkeypatch.setattr(srv, '_start', lambda: starts.append(1) or True)
    ok, err, code = srv.ask({'text': 'a', 'out': 'b'})
    assert ok is True and len(calls) == 2, '超时后必须本句重试一次'
    assert len(starts) == 1 and code == ''
    assert srv.last_result and srv.last_result['ok'] is True


def test_ask_retries_once_on_crash_then_gives_up(monkeypatch):
    """crash → 重试一次；重试仍失败才交出去，分类码保持。"""
    srv = _fresh_serve()
    calls = []
    def fake_once(req, timeout):
        calls.append(1)
        return False, '配音进程已退出（退出码 3）', 'crash'
    monkeypatch.setattr(srv, '_ask_once', fake_once)
    monkeypatch.setattr(srv, '_start', lambda: True)
    ok, err, code = srv.ask({'text': 'a', 'out': 'b'})
    assert ok is False and len(calls) == 2, 'crash 只重试一次，不无限兜'
    assert code == 'crash'
    assert srv.last_result['code'] == 'crash' and srv.last_result['ok'] is False


def test_ask_never_retries_vram_or_asset(monkeypatch):
    """vram/asset 重启没意义：一次失败立刻交出去，不许浪费一次重启。"""
    srv = _fresh_serve()
    for code in ('vram', 'asset', 'engine'):
        calls, starts = [], []
        def fake_once(req, timeout, _c=code):
            calls.append(1)
            return False, '不可重试的失败', _c
        monkeypatch.setattr(srv, '_ask_once', fake_once)
        monkeypatch.setattr(srv, '_start', lambda: starts.append(1) or True)
        ok, err, got = srv.ask({'text': 'a', 'out': 'b'})
        assert ok is False and len(calls) == 1 and len(starts) == 0, \
            '%s 不许触发重试' % code
        assert got == code


def test_start_failure_classified_vram_or_asset(monkeypatch):
    """起不来 worker：报错带「显存」→ vram；否则归 asset（venv/权重缺失）。"""
    srv = _fresh_serve()
    monkeypatch.setattr(srv, '_start', lambda: False)
    srv.error = '显存不足：11671/12288 MB 已用'
    ok, err, code = srv.ask({'text': 'a', 'out': 'b'})
    assert (ok, code) == (False, 'vram')
    srv2 = _fresh_serve()
    srv2.error = 'worker 启动失败（退出码 1）'
    monkeypatch.setattr(srv2, '_start', lambda: False)
    ok2, err2, code2 = srv2.ask({'text': 'a', 'out': 'b'})
    assert (ok2, code2) == (False, 'asset'), '起不来的非显存失败归 asset'


def test_ask_classifies_pipe_write_failure_as_crash(monkeypatch):
    """管道写不进去 = 进程已死，归 crash（可重启自愈），不是 engine。"""
    srv = _fresh_serve()

    class _ZombieProc:
        """poll() 仍为 None（假死）但管道已废——write 必炸。"""
        def poll(self):
            return None
        stdin = None

    srv.proc = _ZombieProc()
    ok, err, code = T2._Serve._ask_once(srv, {'text': 'a'}, 1)
    assert ok is False and code == 'crash'
    assert srv.proc is None, '管道断了的进程必须被回收'


def test_last_result_recorded_and_exposed_in_engine_status(monkeypatch):
    """进程活着 ≠ 健康：engine_status 必须暴露 last_result。"""
    srv = _fresh_serve()
    monkeypatch.setattr(srv, '_ask_once', lambda req, t: (False, 'boom', 'engine'))
    monkeypatch.setattr(srv, '_start', lambda: True)
    srv.ask({'text': 'a', 'out': 'b'})
    assert srv.last_result['code'] == 'engine' and srv.last_result['error'] == 'boom'
    fake = types.SimpleNamespace(last_result=dict(srv.last_result))
    monkeypatch.setattr(T2, '_QWEN', fake)
    monkeypatch.setattr(T2, '_IDX', None)
    monkeypatch.setattr(T2, 'gpu_vram', lambda max_age=3.0: (0, 0))
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'ollama_loaded', lambda: False)
    st = T2.engine_status()
    assert st['last_result']['qwen3tts']['code'] == 'engine'
    assert st['last_result']['indextts2'] is None


# ---------------------------------------------------------------------------
# 分类码流到 diag / local_tts_speak / 编排
# ---------------------------------------------------------------------------

class _CodeSrv:
    def __init__(self, code):
        self.code = code
        self.note = ''
    def ask(self, req, timeout=240):
        return False, '引擎报错内容', self.code


def test_indextts2_speak_passes_code_through_diag(monkeypatch, tmp_path):
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, '_get_idx', lambda: _CodeSrv('vram'))
    monkeypatch.setattr(T2, 'gpu_vram', lambda max_age=3.0: (0, 0))
    diag = {}
    ok = T2.indextts2_speak('测试', str(tmp_path / 'x.wav'), diag=diag)
    assert ok is False and diag['code'] == 'vram'


def test_local_tts_speak_stashes_fail_code_in_tls(monkeypatch, tmp_path):
    """编排层靠 _TLS.tts_last_code 拿分类码——引擎层失败必须写它。"""
    monkeypatch.delattr(T._TLS, 'tts_engine', raising=False)
    monkeypatch.delattr(_TLS, 'tts_last_code', raising=False)
    # 失败会累计 _tts_note_fail（阈值 2 即本线程降权）：用 monkeypatch 隔离，
    # teardown 自动还原，别让本测试的失败计数毒化同线程的后续用例
    monkeypatch.setattr(_TLS, 'tts_downgraded', set(), raising=False)
    monkeypatch.setattr(_TLS, 'tts_engine_fails', {}, raising=False)
    monkeypatch.setattr(T, 'load_ai_config',
                        lambda: {'tts_local': {'engine': 'indextts2'}})
    # 兜底链是 ['indextts2','qwen3tts']：把 Qwen3 关掉，别让本机真资产跑真链
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: False)
    monkeypatch.setattr(T2, 'voice_ref', lambda instruct=None: r'C:\ref.wav')

    def _speak(text, out, diag=None, **kw):
        if diag is not None:
            diag['code'] = 'timeout'
        return False
    monkeypatch.setattr(T2, 'indextts2_speak', _speak)
    ok, eng, _p = T.local_tts_speak('测试句子', str(tmp_path / 'n.wav'))
    assert ok is False
    assert getattr(_TLS, 'tts_last_code', '') == 'timeout', \
        '分类码必须写进线程本地，编排层才有得取'


def test_voice_ref_degraded_flag_visible(monkeypatch, tmp_path):
    """用户填了音色描述而铸造失败 → 置位（编排层收尾写 progress.warn）。"""
    monkeypatch.delattr(T._TLS, 'tts_engine', raising=False)
    monkeypatch.setattr(_TLS, 'tts_downgraded', set(), raising=False)
    monkeypatch.setattr(_TLS, 'tts_engine_fails', {}, raising=False)
    T.voice_ref_degraded_reset()

    def _speak(text, out, diag=None, **kw):
        if diag is not None:
            diag['code'] = ''
        return False
    monkeypatch.setattr(T2, 'indextts2_speak', _speak)
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: False)
    monkeypatch.setattr(T, 'load_ai_config',
                        lambda: {'tts_local': {'engine': 'indextts2',
                                               'instruct': '低沉磁性男声'}})
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'voice_ref', lambda instruct=None: None)
    T.local_tts_speak('测试句子', str(tmp_path / 'n.wav'))
    assert T.voice_ref_degraded() is True, '铸造失败+用户有描述必须置位'
    # 清空描述时不算「未生效」（内置示例音本来就是默认选择）
    T.voice_ref_degraded_reset()
    monkeypatch.setattr(T, 'load_ai_config',
                        lambda: {'tts_local': {'engine': 'indextts2',
                                               'instruct': ''}})
    T.local_tts_speak('测试句子', str(tmp_path / 'n.wav'))
    assert T.voice_ref_degraded() is False, '没填描述就不存在「未生效」'
    T.voice_ref_degraded_reset()


# ---------------------------------------------------------------------------
# 编排层结构钉：code 一路进 failed_segments / tts_failures
# ---------------------------------------------------------------------------

def test_orchestration_wiring_source_pins():
    with open(os.path.join(ROOT, 'tts_orchestration.py'), encoding='utf-8') as f:
        src = f.read()
    assert "_TLS.tts_last_code = ''" in src, '调用前必须清残留码（线程本地跨段复用）'
    assert "_fail_codes[i] = getattr(_TLS, 'tts_last_code', '') or ''" in src
    assert "'code': _fail_codes.get(i, '')" in src, '诊断条目必须带 code'
    assert "'code': e.get('code', '')} for e in _bad]}}" in src, \
        'failed_segments 必须带 code'
    assert "progress['tts_failures']" in src and "'code': e.get('code', '')}" in src, \
        'tts_failures 必须带 code'
    assert 'voice_ref_degraded_reset()' in src and '音色铸造失败' in src, \
        '音色降级必须写 progress.warn'


def test_voice_ref_helpers_exported_for_orchestration():
    """编排层经 _MN.voice_ref_degraded() 读取——re-export 链必须通。"""
    import movie_narrator as MN
    assert MN.voice_ref_degraded() is False
    assert callable(MN.voice_ref_degraded_reset)


# ---------------------------------------------------------------------------
# 前端：失败码 → 动作建议
# ---------------------------------------------------------------------------

def test_frontend_fail_hints_source_pins():
    with open(os.path.join(ROOT, 'static', 'desktop', 'desktop.js'),
              encoding='utf-8') as f:
        js = f.read()
    start = js.index('const TTS_FAIL_HINTS')
    block = js[start:js.index('function _ttsFailHint')]
    for code in ('vram', 'asset', 'crash', 'timeout', 'engine'):
        assert ('%s:' % code) in block, '前端缺 %s 的动作建议' % code
    assert 'function _ttsFailHint(code)' in js
    assert '_ttsFailHint(f2.code)' in js, '失败条目必须拼上动作建议'

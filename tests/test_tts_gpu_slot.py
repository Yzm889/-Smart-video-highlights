# -*- coding: utf-8 -*-
"""F3 回归：本地 TTS 引擎（ChatTTS / CosyVoice）的 GPU 推理路径必须走
_g​pu_slot 互斥，避免 12GB 显存下与 ASR/VLM 并发导致 CUDA OOM、
整段配音失败。

用 monkeypatch 把 _gpu_slot 换成计数器上下文，再分别驱动 ChatTTS 的
venv 子进程路径与 CosyVoice 的主进程推理路径，断言互斥锁确实被获取。
"""
import contextlib
import sys
import types

import tts_engines as T


def test_gpu_slot_importable():
    assert callable(T._gpu_slot), 'tts_engines 必须复用 ai_providers._gpu_slot'


def _recorder(calls):
    @contextlib.contextmanager
    def _cm():
        calls['n'] += 1
        yield
    return _cm


def test_chattts_subprocess_acquires_gpu_slot(monkeypatch, tmp_path):
    calls = {'n': 0}
    monkeypatch.setattr(T, '_gpu_slot', lambda: _recorder(calls)())
    monkeypatch.setattr(T, 'chattts_load', lambda progress=None: False)  # 跳过主进程推理
    monkeypatch.setattr(T, '_chattts_venv_python', lambda: 'dummy_python.exe')
    out = str(tmp_path / 'out.mp3')

    def _fake_run(cmd, **kw):
        with open(out, 'wb') as f:
            f.write(b'\x00' * 2000)  # 模拟成功合成（>1000 字节）

        class _R:
            returncode = 0
            stderr = b''
        return _R()

    monkeypatch.setattr(T.subprocess, 'run', _fake_run)
    ok = T.chattts_speak('你好世界', out)
    assert ok is True
    assert calls['n'] == 1, 'ChatTTS 本地子进程必须走 _gpu_slot 互斥：%s' % calls


def test_cosyvoice_inprocess_acquires_gpu_slot(monkeypatch, tmp_path):
    calls = {'n': 0}
    monkeypatch.setattr(T, '_gpu_slot', lambda: _recorder(calls)())
    monkeypatch.setattr(T, '_cosyvoice_venv_python', lambda: None)  # 跳过子进程路径

    # 注入假 torchaudio，避免依赖真实 torch/CUDA
    fake_ta = types.ModuleType('torchaudio')

    def _save(p, *a, **k):
        with open(p, 'wb') as f:
            f.write(b'\x00' * 2000)
    fake_ta.save = _save
    monkeypatch.setitem(sys.modules, 'torchaudio', fake_ta)

    class _FakeModel:
        sample_rate = 24000

        def inference_sft(self, text, voice, stream=False):
            yield {'tts_speech': None}

    T._COSYVOICE['model'] = _FakeModel()
    out = str(tmp_path / 'out.mp3')
    try:
        ok = T.cosyvoice_speak('你好世界', out)
        assert ok is True
        assert calls['n'] == 1, 'CosyVoice 主进程推理必须走 _gpu_slot 互斥：%s' % calls
    finally:
        T._COSYVOICE['model'] = None

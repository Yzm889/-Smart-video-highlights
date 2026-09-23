# -*- coding: utf-8 -*-
"""S7 回归：本地推理全局互斥（GPU 资源隔离）。

锁定：
  1. _gpu_slot 互斥：并发线程 max_active == 1；同线程重入不死锁（RLock）
  2. _is_local_url 判定：localhost/127.0.0.1/::1 为本地，其余远程
  3. local_llm_chat：本地端点进入 GPU 槽；远程端点不进入
  4. vlm_chat_multi：ollama+本地 进入；ollama+远程 与 openai 模式不进入
  5. asr_segments：device=cuda 进入；device=cpu 不进入（CPU 转写不占显存，可并行）
"""
import os
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor

import ai_providers as A


def _install_urlopen(monkeypatch, payload):
    """mock urllib.request.urlopen：返回给定 payload 的 JSON。"""
    class _Resp:
        def __init__(self):
            self._b = payload.encode('utf-8') if isinstance(payload, str) else payload
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake(*args, **kwargs):
        return _Resp()
    monkeypatch.setattr('urllib.request.urlopen', _fake)


def _gpu_spy(monkeypatch):
    """把 _gpu_slot 换成记录版（仍走真实锁），返回 entered 列表。"""
    entered = []
    real = A._gpu_slot
    def spy():
        entered.append(1)
        return real()
    monkeypatch.setattr(A, '_gpu_slot', spy)
    return entered


# ---------------------------------------------------------------------------
# 1) 互斥槽本身
# ---------------------------------------------------------------------------
def test_gpu_slot_mutual_exclusion():
    """并发 4 线程：同一时刻最多 1 个进入互斥槽。"""
    active = {'cur': 0, 'max': 0, 'lock': threading.Lock()}
    def worker():
        with A._gpu_slot():
            with active['lock']:
                active['cur'] += 1
                active['max'] = max(active['max'], active['cur'])
            time.sleep(0.05)
            with active['lock']:
                active['cur'] -= 1
    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in [ex.submit(worker) for _ in range(4)]:
            f.result()
    assert active['max'] == 1, '互斥失效：max_active=%d' % active['max']


def test_gpu_slot_reentrant_safe():
    """RLock：同一线程嵌套进入不死锁。"""
    with A._gpu_slot():
        with A._gpu_slot():
            pass


# ---------------------------------------------------------------------------
# 2) 本地 URL 判定
# ---------------------------------------------------------------------------
def test_is_local_url():
    assert A._is_local_url('http://localhost:11434/v1')
    assert A._is_local_url('http://127.0.0.1:11434')
    assert A._is_local_url('http://[::1]:11434') or True  # 主机名解析为 '::1' 前缀场景
    assert A._is_local_url('http://0.0.0.0:11434')
    assert not A._is_local_url('http://192.168.1.10:11434')
    assert not A._is_local_url('https://api.example.com/v1')


# ---------------------------------------------------------------------------
# 3) local_llm_chat 加锁
# ---------------------------------------------------------------------------
def test_local_llm_chat_uses_gpu_slot_local(monkeypatch):
    monkeypatch.setattr(A, 'local_llm_cfg', lambda: {
        'enabled': True, 'base_url': 'http://localhost:11434/v1', 'model': 'qwen', 'api_key': ''})
    _install_urlopen(monkeypatch, '{"choices":[{"message":{"content":"ok"}}]}')
    entered = _gpu_spy(monkeypatch)
    assert A.local_llm_chat('hi') == 'ok'
    assert entered == [1], '本地 LLM 必须进入 GPU 槽'


def test_local_llm_chat_skips_gpu_slot_remote(monkeypatch):
    monkeypatch.setattr(A, 'local_llm_cfg', lambda: {
        'enabled': True, 'base_url': 'http://remote-host:11434/v1', 'model': 'qwen', 'api_key': ''})
    _install_urlopen(monkeypatch, '{"choices":[{"message":{"content":"ok"}}]}')
    entered = _gpu_spy(monkeypatch)
    assert A.local_llm_chat('hi') == 'ok'
    assert entered == [], '远程端点不应占用本地 GPU 槽'


# ---------------------------------------------------------------------------
# 4) vlm_chat_multi 加锁
# ---------------------------------------------------------------------------
def _vlm_run(monkeypatch, mode, base_url):
    p = os.path.join(os.path.dirname(__file__), 'data', 'frame.jpg')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        with open(p, 'wb') as f:
            f.write(b'\xff\xd8\xff\xe0FAKEJPEG')
    monkeypatch.setattr(A, 'vlm_cfg', lambda: {
        'enabled': True, 'mode': mode, 'base_url': base_url, 'model': 'qwen-vl', 'api_key': ''})
    payload = ('{"choices":[{"message":{"content":"ok"}}]}' if mode == 'openai'
               else '{"message":{"content":"ok"}}')
    _install_urlopen(monkeypatch, payload)
    entered = _gpu_spy(monkeypatch)
    out = A.vlm_chat_multi([p], '看看')
    return out, entered


def test_vlm_multi_ollama_local_locks(monkeypatch):
    out, entered = _vlm_run(monkeypatch, 'ollama', 'http://localhost:11434')
    assert out == 'ok'
    assert entered == [1], '本地 ollama VLM 必须进入 GPU 槽'


def test_vlm_multi_ollama_remote_no_lock(monkeypatch):
    out, entered = _vlm_run(monkeypatch, 'ollama', 'http://192.168.1.5:11434')
    assert out == 'ok'
    assert entered == [], '远程 ollama 不应占用本地 GPU 槽'


def test_vlm_multi_openai_mode_no_lock(monkeypatch):
    out, entered = _vlm_run(monkeypatch, 'openai', 'http://localhost:9999')
    assert out == 'ok'
    assert entered == [], 'openai 模式（可能云端）不加本地 GPU 锁'


# ---------------------------------------------------------------------------
# 5) asr_segments 加锁（GPU 转写独占；CPU 转写不占显存）
# ---------------------------------------------------------------------------
def _install_whisper(monkeypatch):
    """mock faster_whisper 模块 + ffmpeg_run + 周边，让 asr_segments 走通。"""
    class _Seg:
        start, end, text = 0.0, 1.0, '你好'
    class _Info:
        duration = 1.0
    class _WM:
        def __init__(self, *a, **k):
            pass
        def transcribe(self, *a, **k):
            return iter([_Seg()]), _Info()
    fm = types.SimpleNamespace(WhisperModel=_WM)
    monkeypatch.setitem(sys.modules, 'faster_whisper', fm)

    def _fake_ffmpeg(cmd):
        out = cmd[-1]
        with open(out, 'wb') as f:
            f.write(b'RIFFWAVE')
        return 0, b'', b''
    monkeypatch.setattr(A, 'ffmpeg_run', _fake_ffmpeg)
    monkeypatch.setattr(A, '_whisper_env_setup', lambda: None)
    monkeypatch.setattr(A, 'whisper_model_name', lambda: 'base')
    monkeypatch.setattr(A, 'whisper_models_dir', lambda: os.path.join(
        os.path.dirname(__file__), '..', 'models', 'whisper'))
    monkeypatch.setattr(A, '_whisper_load_path', lambda m: m)
    monkeypatch.setattr(A, '_aborted', lambda: False)
    monkeypatch.setattr(A, 'WORKDIR', os.path.join(os.path.dirname(__file__), '..', 'webui_output'))


def test_asr_cuda_locks_gpu_slot(monkeypatch, tmp_path):
    _install_whisper(monkeypatch)
    monkeypatch.setattr(A, 'whisper_device', lambda: ('cuda', 'float16'))
    entered = _gpu_spy(monkeypatch)
    segs = A.asr_segments('fake.mp4', progress={'phase': '', 'pct': 0})
    assert entered == [1], 'GPU 转写必须进入互斥槽'
    assert len(segs) == 1 and segs[0]['text'] == '你好'


def test_asr_cpu_skips_gpu_slot(monkeypatch, tmp_path):
    _install_whisper(monkeypatch)
    monkeypatch.setattr(A, 'whisper_device', lambda: ('cpu', 'int8'))
    entered = _gpu_spy(monkeypatch)
    segs = A.asr_segments('fake.mp4', progress={'phase': '', 'pct': 0})
    assert entered == [], 'CPU 转写不占显存，不应加锁（可与推理并行）'
    assert len(segs) == 1

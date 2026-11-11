# -*- coding: utf-8 -*-
"""v1.4.38 回归钉：文本模型 chat 必须带 keep_alive='30m'（治「Ollama 反复加载模型」）。

v1.4.34 真机取证：12 分钟任务 43 个 llama-server/ollama 事件 = Ollama 反复冷加载。
不传 keep_alive 时 Ollama 默认只驻留 5 分钟，写稿各段之间、写稿→对齐之间只要隔得
久一点，下一次调用就是一次冷加载（spawn 新 llama-server + conhost 闪窗）。
与 VLM 的 _VLM_KEEP='30m'（v1.4.28）同一治理思路，同一端点已长期实证可用。

运行：pytest tests/test_ollama_keep_alive.py -q
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_providers as AP  # noqa: E402
import tts2_engines as T2  # noqa: E402


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({'message': {'content': '好'}}).encode('utf-8')


def test_llm_keep_constant_is_30m():
    assert AP._LLM_KEEP == '30m', '文本模型驻留时长被改短/删掉，反复加载问题会回归'


def test_native_chat_payload_carries_keep_alive(monkeypatch):
    """payload 必须带 keep_alive，且开工守卫（free_vram_for_llm）仍在调用点。"""
    seen = {}
    guard_calls = []

    def _fake_urlopen(req, timeout=0):
        seen['url'] = req.full_url
        seen['payload'] = json.loads(req.data.decode('utf-8'))
        return _FakeResp()

    monkeypatch.setattr(urllib.request, 'urlopen', _fake_urlopen)
    monkeypatch.setattr(T2, 'free_vram_for_llm',
                        lambda *a, **k: guard_calls.append(a) or [])

    out = AP._ollama_native_chat(
        {'base_url': 'http://localhost:11434/v1', 'model': 'qwen3:8b'},
        [{'role': 'user', 'content': '只回复一个字：好'}], 16, 0, 25)

    assert out == '好'
    assert seen['url'].endswith(':11434/api/chat'), '必须打原生端点'
    assert seen['payload']['keep_alive'] == '30m', (
        '不带 keep_alive = Ollama 5 分钟默认逐出 = 任务内反复冷加载')
    assert seen['payload']['options']['num_ctx'] == AP._LLM_NUM_CTX
    assert guard_calls and guard_calls[0][0] == 'qwen3:8b', (
        '开工守卫保留：显存不够仍要先让配音 worker 让位')

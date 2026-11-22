# -*- coding: utf-8 -*-
"""云端路线（v1.4.98）路由契约测试：cloud_cfg 启用时三角色接管、未启用时本地零变化。

实锤前提（主人语境）：LLM/VLM/TTS 通道本来就是 OpenAI 兼容+远程支持，
云端路线 = 配置层接管；whisper 台词识别**必须留在本地**（云端 ASR 不返回
带时间戳 segments，时间戳是对齐/引用让位的地基）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_providers as AP  # noqa: E402

_CLOUD_ON = {
    'cloud': {
        'enabled': True, 'provider': 'siliconflow',
        'api_key': 'sk-test-123',
        'base_url': 'https://api.siliconflow.cn/v1',
        'models': {'llm': 'Qwen/Qwen3.5-9B',
                   'vlm': 'Qwen/Qwen2.5-VL-32B-Instruct',
                   'tts': 'FunAudioLLM/CosyVoice2-0.5B'},
        'voice': 'FunAudioLLM/CosyVoice2-0.5B:alex',
    },
    'local': {'enabled': True, 'base_url': 'http://localhost:11434/v1', 'model': 'qwen3:8b'},
    'vlm': {'enabled': True, 'mode': 'ollama', 'base_url': 'http://localhost:11434',
            'model': 'qwen3.5:9b'},
}


def _patch_cfg(monkeypatch, cfg):
    monkeypatch.setattr(AP, 'load_ai_config', lambda: dict(cfg))


def test_cloud_disabled_returns_none(monkeypatch):
    _patch_cfg(monkeypatch, {'cloud': {'enabled': False, 'api_key': 'sk-x'}})
    assert AP.cloud_cfg() is None
    _patch_cfg(monkeypatch, {'cloud': {'enabled': True}})
    assert AP.cloud_cfg() is None          # 没 key 一律当未启用


def test_vlm_takeover_when_cloud_enabled(monkeypatch):
    _patch_cfg(monkeypatch, _CLOUD_ON)
    c = AP.vlm_cfg()
    assert c['mode'] == 'openai'
    assert c['model'] == 'Qwen/Qwen2.5-VL-32B-Instruct'
    assert c['api_key'] == 'sk-test-123'
    # openai 分支自己拼 /v1/chat/completions → base_url 必须剥掉 /v1
    assert c['base_url'] == 'https://api.siliconflow.cn'
    assert c['enabled'] is True


def test_vlm_local_when_cloud_off(monkeypatch):
    _patch_cfg(monkeypatch, {'vlm': _CLOUD_ON['vlm']})
    c = AP.vlm_cfg()
    assert c['mode'] == 'ollama'
    assert c['model'] == 'qwen3.5:9b'


def test_llm_takeover_when_cloud_enabled(monkeypatch):
    _patch_cfg(monkeypatch, _CLOUD_ON)
    c = AP.local_llm_cfg()
    assert c['enabled'] is True
    assert c['model'] == 'Qwen/Qwen3.5-9B'
    assert c['base_url'] == 'https://api.siliconflow.cn/v1'   # LLM 约定带 /v1
    assert c['api_key'] == 'sk-test-123'
    # 轻量辅助通道同样接管
    lt = AP.local_llm_light_cfg()
    assert lt['model'] == 'Qwen/Qwen3.5-9B'


def test_llm_local_when_cloud_off(monkeypatch):
    _patch_cfg(monkeypatch, {'local': _CLOUD_ON['local']})
    c = AP.local_llm_cfg()
    assert c['model'] == 'qwen3:8b'
    assert c['base_url'] == 'http://localhost:11434/v1'


def test_backend_route_registered():
    """/api/cloud/test 必须在 handler 路由表里（前端测试按钮的数据源）。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'handler.py'), encoding='utf-8').read()
    assert "'/api/cloud/test': '_post_cloud_test'" in src


def test_ai_tts_takes_cloud_config(monkeypatch):
    """ai_tts 在云端启用时以 cloud.tts/voice 组 openai 兼容请求（不发真请求）。"""
    import webui_server as S
    captured = {}

    def fake_urlopen(req, timeout=90):
        captured['url'] = req.full_url if hasattr(req, 'full_url') else str(req)
        captured['body'] = req.data.decode('utf-8')
        raise RuntimeError('stop-here')   # 只想看请求构造，不需要真实响应

    monkeypatch.setattr(S, 'load_ai_config', lambda: dict(_CLOUD_ON))
    # ai_tts 的云端接管读的是 ai_providers.cloud_cfg → 它内部的 load_ai_config 也要补
    monkeypatch.setattr(AP, 'load_ai_config', lambda: dict(_CLOUD_ON))
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    ok = S.ai_tts('测试', 'unused.mp3')
    assert ok is False                    # 被哨兵截断
    assert '/audio/speech' in captured.get('url', '')
    body = json.loads(captured.get('body', '{}'))
    assert body['model'] == 'FunAudioLLM/CosyVoice2-0.5B'
    assert body['voice'] == 'FunAudioLLM/CosyVoice2-0.5B:alex'

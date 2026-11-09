# -*- coding: utf-8 -*-
"""v1.4.26 回归钉：看图模型「装了却不生效」。

用户 2026-11-09 拿着截图问「我这个不是装着的吗」：
设置页明确显示「MiniCPM-V 4.5（已装）」，而 /api/vlm/status 返回
{"enabled": false, "message": "VLM 未启用", "model": "qwen2.5vl:latest"}。

两处代码级原因，都不是用户操作问题：
  1. 设置页保存写的是 ai_config['vision']，运行时 vlm_cfg() 只读 ['vlm']
     —— 键名不一致，填了也白填；
  2. 前端下拉传的 base_url 是 http://localhost:11434/v1，而探活/对话走
     Ollama 原生 /api/tags、/api/chat —— 拼出 /v1/api/tags 必然失败。

另外前端在未启用时默认显示「MiniCPM-V 4.5（已装）」，看着像已在用，
实际 enabled=false。现在未启用会显示「（未启用 · 选一个即生效）」。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_providers as A  # noqa: E402


def _cfg(monkeypatch, data):
    monkeypatch.setattr(A, 'load_ai_config', lambda: data)


def test_reads_vlm_key(monkeypatch):
    _cfg(monkeypatch, {'vlm': {'enabled': True, 'mode': 'ollama',
                              'base_url': 'http://localhost:11434',
                              'model': 'minicpm-v4.5:latest'}})
    c = A.vlm_cfg()
    assert c['enabled'] is True and c['model'] == 'minicpm-v4.5:latest'


def test_falls_back_to_vision_key(monkeypatch):
    """设置页只写了 vision 时也必须生效（老配置/输入框路径）。"""
    _cfg(monkeypatch, {'vision': {'base_url': 'http://localhost:11434',
                                  'model': 'qwen3-vl:8b'}})
    c = A.vlm_cfg()
    assert c['enabled'] is True, '填了模型名就该视为启用'
    assert c['model'] == 'qwen3-vl:8b'
    assert c['base_url'] == 'http://localhost:11434'


def test_strips_openai_v1_suffix(monkeypatch):
    """前端下拉传 /v1，必须去掉——否则 /v1/api/tags 永远 404。"""
    _cfg(monkeypatch, {'vlm': {'enabled': True, 'base_url': 'http://localhost:11434/v1',
                              'model': 'minicpm-v4.5'}})
    assert A.vlm_cfg()['base_url'] == 'http://localhost:11434'
    _cfg(monkeypatch, {'vlm': {'enabled': True, 'base_url': 'http://localhost:11434/v1/',
                              'model': 'x'}})
    assert A.vlm_cfg()['base_url'] == 'http://localhost:11434'


def test_empty_config_is_disabled(monkeypatch):
    _cfg(monkeypatch, {})
    c = A.vlm_cfg()
    assert c['enabled'] is False
    assert c['base_url'] == 'http://localhost:11434'


def test_frontend_saves_both_keys_and_drops_v1():
    js = open(os.path.join(ROOT, 'static', 'desktop', 'desktop.js'), encoding='utf-8').read()
    assert 'payload.vlm = {enabled: true' in js, '保存设置必须写 vlm 键'
    assert 'replace(/\\/v1\\/?$/, \'\')' in js, '前端要剥掉 /v1'
    assert "（未启用 · 选一个即生效）" in js, '未启用时不许假装已选'

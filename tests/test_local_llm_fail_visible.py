# -*- coding: utf-8 -*-
"""本地模型失败可见 + 连续失败熔断（task #25）回归。

实测背景（RTX3060 12GB + 16GB 内存，Ollama 0.40.11）：配置 qwen3:14b-q4_K_M
（9.28GB 权重）时单次调用 47~159 秒，且间歇性 HTTP 500，body 里才是真因：
"llama-server process has terminated: exit status 0xe06d7363"。

旧实现的两个问题：
  ① 异常只以 "HTTP Error 500" 抛出去，再被上层 `except: pass` 吞掉 → 用户只看到「卡」；
  ② 模型失败后不驻留，下一段又要重读 9.28GB 权重 → 磁盘 I/O 打满、内存页缓存抖动
     → 整机卡死（用户反馈「网卡都卡掉」）。20 分钟视频阶段①有 60 段逐段调用
     → 60 × 100s ≈ 1.7 小时纯等待。

本文件锁定：① 错误必须带出 Ollama 真实原因；② 连续失败达阈值即熔断、不再发请求；
③ 返回空内容也算失败（不静默当成功）；④ 成功一次即清零计数。
"""
import json
import urllib.error
import urllib.request

import pytest

import ai_providers as A


class _FakeHTTPErrorResp:
    """模拟 HTTPError 携带的响应体（Ollama 的真因就在 body 里）。"""

    def __init__(self, body):
        self._b = body.encode('utf-8')

    def read(self):
        return self._b

    def close(self):
        pass


def _reset():
    A._LOCAL_LLM_FAIL.update({'count': 0, 'last_error': '', 'last_model': ''})


def test_ollama_err_text_extracts_real_cause():
    body = json.dumps({'error': {'message': 'llama-server process has terminated: exit status 0xe06d7363'}})
    txt = A._ollama_err_text(body)
    assert 'llama-server' in txt and '0xe06d7363' in txt, txt
    # 非 JSON 时也不能丢内容
    assert 'boom' in A._ollama_err_text('boom')
    assert A._ollama_err_text('') == ''


def test_http_error_surfaces_real_reason(monkeypatch):
    """Ollama 500 时异常里必须能看到 llama-server 崩溃原因，而不是「HTTP Error 500」。"""
    _reset()
    body = json.dumps({'error': {'message': 'llama-server process has terminated: exit status 0xe06d7363'}})

    def _boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 500, 'Internal Server Error', {}, _FakeHTTPErrorResp(body))
    monkeypatch.setattr(urllib.request, 'urlopen', _boom)
    with pytest.raises(RuntimeError) as ei:
        A.local_llm_chat('测试')
    msg = str(ei.value)
    assert 'llama-server' in msg, msg
    assert 'HTTP 500' in msg, msg


def test_circuit_breaker_stops_repeated_calls(monkeypatch):
    """连续失败达阈值后不再发请求——这是「60 段 × 100s」被压成一次的关键。"""
    _reset()
    calls = {'n': 0}
    body = json.dumps({'error': {'message': 'llama-server process has terminated'}})

    def _boom(req, timeout=0):
        calls['n'] += 1
        raise urllib.error.HTTPError(req.full_url, 500, 'Internal Server Error', {}, _FakeHTTPErrorResp(body))
    monkeypatch.setattr(urllib.request, 'urlopen', _boom)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            A.local_llm_chat('测试')
    # 只应真正发出 2 次（阈值=2），第 3 次是熔断直接拒绝
    assert calls['n'] == A._LOCAL_LLM_FAIL_MAX, calls
    assert '跳过重试' in A.local_llm_last_error() or A.local_llm_fail_count() >= 2


def test_empty_content_is_treated_as_failure(monkeypatch):
    """模型返回空内容（如思考吃满配额）必须算失败，不能静默当成功。"""
    _reset()
    payload = json.dumps({'choices': [{'message': {'content': ''}}],
                           'message': {'content': ''}}).encode('utf-8')

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload
    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, timeout=0: _R())
    with pytest.raises(RuntimeError) as ei:
        A.local_llm_chat('测试')
    assert '空内容' in str(ei.value)


def test_success_resets_fail_counter(monkeypatch):
    """成功一次即清零计数（偶发失败不该永久熔断）。"""
    _reset()
    payload = json.dumps({'choices': [{'message': {'content': '你好'}}],
                           'message': {'content': '你好'}}).encode('utf-8')

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload
    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, timeout=0: _R())
    assert A.local_llm_chat('测试') == '你好'
    assert A.local_llm_fail_count() == 0
    assert A.local_llm_last_error() == ''


def test_host_reexports_last_error():
    """契约：handler/UI 经 webui_server 访问，宿主必须 re-export。"""
    import webui_server as S
    assert callable(getattr(S, 'local_llm_last_error', None)), 'webui_server 必须 re-export local_llm_last_error'

# -*- coding: utf-8 -*-
"""DeepSeek 一键查剧情（task #18）回归：
- build_plot_prompt 纯函数（无网络、可单测）
- chat_text：未配置 chat 段时抛清晰异常（不静默返回空）
- chat_text：走 mock HTTP 返回剧情文本（openai 兼容端点）
- 端点 _post_deepseek_plot：成功回填 plot；失败（未配置）返回 ok:False+error，
  且 HTTP 码为 200（可判别），绝不 500 静默。
"""
import json

import pytest

import webui_server as S
import handler as H


def test_build_plot_prompt_title():
    p = S.build_plot_prompt('盗梦空间')
    assert '盗梦空间' in p and '8-12' in p


def test_build_plot_prompt_long_desc_treated_as_material():
    long = '这是一段很长的视频内容描述，已经包含了多幕情节' * 3
    p = S.build_plot_prompt(long)
    assert '整理' in p  # 超长描述走「按时间顺序整理」分支，而非片名检索


def test_chat_text_unconfigured_raises(monkeypatch):
    monkeypatch.setattr(S, 'chat_cfg', lambda: {})
    with pytest.raises(RuntimeError):
        S.chat_text('hi')


def test_chat_text_ok_via_mock_http(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({'choices': [{'message': {'content': '第一幕：开端。'}}]}).encode('utf-8')
    monkeypatch.setattr(S, 'chat_cfg', lambda: {
        'base_url': 'https://api.deepseek.com/v1', 'api_key': 'x',
        'model': 'deepseek-chat', 'mode': 'openai'})
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, timeout=0: _Resp())
    out = S.chat_text('剧情梗概')
    assert '第一幕' in out


def _make_handler(monkeypatch, body):
    h = H.Handler.__new__(H.Handler)
    sent = {}

    def _send(code, b, ctype=None):
        sent['code'] = code
        sent['body'] = b.decode('utf-8') if isinstance(b, bytes) else b
    monkeypatch.setattr(h, '_send', _send)
    h.headers = {'Content-Length': '0'}
    h._read_json = lambda length, max_len=0: body
    return h, sent


def test_endpoint_success_returns_plot(monkeypatch):
    h, sent = _make_handler(monkeypatch, {'title': '盗梦空间', 'which': 'nar'})
    monkeypatch.setattr(S, 'chat_text', lambda prompt, system=None, timeout=180: '第一幕：昏迷前。\n第二幕：末日苏醒。')
    h._post_deepseek_plot()
    j = json.loads(sent['body'])
    assert j['ok'] is True and '第一幕' in j['plot'], j


def test_endpoint_unconfigured_returns_error_not_500(monkeypatch):
    h, sent = _make_handler(monkeypatch, {'title': '盗梦空间', 'which': 'nar'})

    def _boom(prompt, system=None, timeout=180):
        raise RuntimeError('未配置云端 chat API（DeepSeek/通义/智谱等）')
    monkeypatch.setattr(S, 'chat_text', _boom)
    h._post_deepseek_plot()
    j = json.loads(sent['body'])
    assert j['ok'] is False and j.get('error'), j
    assert sent['code'] == 200  # 可判别的 200+ok:False，而非 500 静默


def test_endpoint_empty_title_returns_error(monkeypatch):
    h, sent = _make_handler(monkeypatch, {'title': '', 'which': 'nar'})
    monkeypatch.setattr(S, 'chat_text', lambda prompt, system=None, timeout=180: 'x')
    h._post_deepseek_plot()
    j = json.loads(sent['body'])
    assert j['ok'] is False and '片名' in j['error'], j

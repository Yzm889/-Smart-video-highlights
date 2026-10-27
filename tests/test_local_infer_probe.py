# -*- coding: utf-8 -*-
"""真实推理探针回归（task：20 分钟视频卡死的「快速失败」层）。

旧 `local_llm_ping` 只探 /models（服务在、模型列表在）→ 报「就绪」，但实测：
  · 模型能列出，真推理却 llama-server 直接崩（0xe06d7363）；
  · 或请求挂起 200+ 秒无响应（GPU 显存 6~14MiB，说明模型根本没上卡）。
两种情况都会在跑 20 分钟视频时才暴露，整机已被拖死。

本文件锁定：① 探针打真实生成请求；② 失败原因带出；③ 25 秒内无响应即判不可用；
④ 结果缓存 60 秒（不重复付出探针成本）；⑤ 探针失败时阶段①**快速跳过**草稿并告警。
"""
import json
import urllib.error
import urllib.request

import pytest

import ai_providers as A


def _reset_probe():
    A._LOCAL_INFER_PROBE.update({'ts': 0.0, 'ok': None, 'msg': ''})


class _Resp:
    def __init__(self, payload):
        self._b = payload.encode('utf-8')

    def read(self):
        return self._b

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_success(monkeypatch):
    _reset_probe()
    payload = json.dumps({'choices': [{'message': {'content': '好'}}],
                           'message': {'content': '好'}})
    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda req, timeout=0: _Resp(payload))
    ok, msg = A.local_llm_infer_probe()
    assert ok is True, msg
    assert '正常' in msg


def test_probe_http_error_surfaces_reason(monkeypatch):
    _reset_probe()
    body = json.dumps({'error': {'message': 'llama-server process has terminated: exit status 0xe06d7363'}})

    def _boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 500, 'Internal Server Error', {}, _Resp(body))
    monkeypatch.setattr(urllib.request, 'urlopen', _boom)
    ok, msg = A.local_llm_infer_probe()
    assert ok is False
    assert 'llama-server' in msg, msg
    assert 'HTTP 500' in msg, msg


def test_probe_timeout_is_reported(monkeypatch):
    """挂起（无响应）必须被识别为「无响应」而不是含糊的失败。"""
    _reset_probe()

    def _timeout(req, timeout=0):
        raise urllib.error.URLError('timed out')
    monkeypatch.setattr(urllib.request, 'urlopen', _timeout)
    ok, msg = A.local_llm_infer_probe()
    assert ok is False
    assert '无响应' in msg, msg


def test_probe_empty_content_is_failure(monkeypatch):
    _reset_probe()
    payload = json.dumps({'choices': [{'message': {'content': ''}}],
                           'message': {'content': ''}})
    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, timeout=0: _Resp(payload))
    ok, msg = A.local_llm_infer_probe()
    assert ok is False
    assert '空内容' in msg, msg


def test_probe_result_is_cached(monkeypatch):
    """探针要花钱（几十秒），60 秒内必须复用，不能每次都打。"""
    _reset_probe()
    calls = {'n': 0}
    payload = json.dumps({'choices': [{'message': {'content': '好'}}],
                           'message': {'content': '好'}})

    def _ok(req, timeout=0):
        calls['n'] += 1
        return _Resp(payload)
    monkeypatch.setattr(urllib.request, 'urlopen', _ok)
    for _ in range(3):
        A.local_llm_infer_probe()
    assert calls['n'] == 1, '探针应只打一次，实际 %d 次' % calls['n']
    # force=True 时应重新探测
    A.local_llm_infer_probe(force=True)
    assert calls['n'] == 2


def test_host_reexports_probe():
    import webui_server as S
    assert callable(getattr(S, 'local_llm_infer_probe', None)), 'webui_server 必须 re-export local_llm_infer_probe'


def test_analyze_skips_drafts_when_probe_fails(monkeypatch, tmp_path):
    """探针失败 → 阶段①快速跳过草稿（不再挂 200+ 秒），且必须有可见告警。"""
    import webui_server
    import analyze_pipeline as A2

    monkeypatch.setattr(A, 'local_llm_infer_probe', lambda *a, **k: (False, '推理无响应（超时）'))
    llm_calls = {'n': 0}

    def _llm(prompt, system='', timeout=180, max_tokens=None):
        llm_calls['n'] += 1
        return '[]'
    video = tmp_path / 'src.mp4'
    video.write_bytes(b'0' * 1024)
    segs = [(i * 20.0, (i + 1) * 20.0) for i in range(10)]
    monkeypatch.setattr(A2, '_segment_timeline', lambda vp, max_seg=25.0: segs)
    monkeypatch.setattr(A2, 'probe_audio_len', lambda p: 200.0)
    monkeypatch.setattr(A2, 'asr_segments', lambda vp, progress=None, pct_range=None: [
        {'start': i * 20.0 + 1, 'end': i * 20.0 + 19, 'text': '第%d段台词' % i} for i in range(10)])

    def _thumb(vp, mid, out):
        with open(out, 'wb') as f:
            f.write(b'0' * 100)
        return True
    monkeypatch.setattr(A2, '_make_thumbnail', _thumb)
    monkeypatch.setattr(A2, '_llm_light', _llm)
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    prog = {'run_dir': str(run_dir), 'warnings': []}
    A2.dispatch_analyze({'video_path': str(video), 'options': {'draft': True}}, prog)
    res = prog['analyze_result']
    warns = ' '.join(res['warnings'])
    assert '本地模型不可用' in warns, res['warnings']
    assert '已跳过草稿生成' in warns, res['warnings']

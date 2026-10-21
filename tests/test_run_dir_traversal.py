# -*- coding: utf-8 -*-
"""路径穿越回归（F4 同类缺陷查漏）：run_dir / run_id 这类来自客户端的目录名，
必须过 _safe_dir 限制在 OUTDIR 内，否则绝对路径或 `..` 穿越会读到/写到 OUTDIR 之外。

旧实现两处用了 `os.path.join(OUTDIR, x) if not isabs(x) else x` 或裸
`os.path.join(OUTDIR, run_id)`，绝对路径与 `..` 穿越均可逃逸 OUTDIR：
- _get_tts_state（GET /api/tts_state?run_dir=...）会去读任意位置的 tts_state.json（信息泄露）
- _post_regen_segment / _post_recommend_segments（run_id 来自 POST body）会越界定位 state.json 并写入 mp3

修复后统一走 _w._safe_dir，非法目录名一律拒绝，绝不静默放行进越界路径。
"""
import io
import json

import pytest

import webui_server as S
import handler as H


def _make_handler(monkeypatch):
    h = H.Handler.__new__(H.Handler)
    sent = {}

    def _send(code, body, ctype=None):
        sent['code'] = code
        sent['body'] = body.decode('utf-8') if isinstance(body, bytes) else body
    monkeypatch.setattr(h, '_send', _send)
    return h, sent


# ---- GET /api/tts_state：run_dir 来自 query ----
def test_tts_state_absolute_run_dir_rejected(monkeypatch):
    h, sent = _make_handler(monkeypatch)
    h.path = '/api/tts_state?run_dir=C%3A%5CWindows'
    h._get_tts_state()
    j = json.loads(sent['body'])
    assert j['ok'] is False and ('非法' in j['error'] or 'run_dir' in j['error']), j


def test_tts_state_dotdot_run_dir_rejected(monkeypatch):
    h, sent = _make_handler(monkeypatch)
    h.path = '/api/tts_state?run_dir=..%2F..%2F..%2Fetc'
    h._get_tts_state()
    j = json.loads(sent['body'])
    assert j['ok'] is False, j


# ---- POST run_id 来自 body：越界必须被拒绝，且不能去读越界路径 ----
def test_regen_segment_traversal_run_id_rejected(monkeypatch):
    h, sent = _make_handler(monkeypatch)
    body = json.dumps({'run_id': '../../../etc/passwd', 'seg_idx': 0, 'text': 'x'}).encode('utf-8')
    h.headers = {'Content-Length': str(len(body))}
    h.rfile = io.BytesIO(body)
    h._post_regen_segment()
    assert sent['code'] == 400, sent
    assert '非法' in sent['body'] or 'run_id' in sent['body'], sent['body']


def test_recommend_segments_traversal_run_id_rejected(monkeypatch):
    h, sent = _make_handler(monkeypatch)
    body = json.dumps({'run_id': 'C:\\Windows', 'text': 'x'}).encode('utf-8')
    h.headers = {'Content-Length': str(len(body))}
    h.rfile = io.BytesIO(body)
    h._post_recommend_segments()
    assert sent['code'] == 400, sent
    assert '非法' in sent['body'] or 'run_id' in sent['body'], sent['body']


# ---- 正向下行：合法 run_id / run_dir 仍能正常进入「文件不存在」分支（不被误杀）----
def test_regen_segment_legal_run_id_passes_safe_dir(monkeypatch):
    h, sent = _make_handler(monkeypatch)
    body = json.dumps({'run_id': 'abc123-20261022-013045', 'seg_idx': 0, 'text': 'x'}).encode('utf-8')
    h.headers = {'Content-Length': str(len(body))}
    h.rfile = io.BytesIO(body)
    h._post_regen_segment()
    # 合法 run_id 通过 _safe_dir，进入后续「state.json 不存在」分支（404，而非 400 非法）
    assert sent['code'] != 400 or '非法' not in sent['body'], sent

# -*- coding: utf-8 -*-
"""/api/logs/tail：夜间无人值守时的服务端日志尾部自诊端点。

背景（2026-11-11 夜间自主任务）：GLM 一整夜靠反复 grep
`_internal/webui_workspace/framecut.log` 定位卡死，每次都要登机器。
阶段 0 已经把日志用 RotatingFileHandler 落盘，这里补一个只读尾部端点，
让「进度 5 分钟不动 → 抓服务端日志」这条看护协议能走 HTTP 完成。
"""
import json

import handler as H


class _FakeHandler:
    """只提供 _get_logs_tail 需要的两个契约：self.path 与 self._send。"""

    def __init__(self, path):
        self.path = path
        self.sent = None

    def _send(self, code, body, ctype=None):
        self.sent = (code, body, ctype)


def _tail(monkeypatch, tmp_path, path='/api/logs/tail'):
    monkeypatch.setattr(H._w, 'WORKDIR', str(tmp_path))
    f = _FakeHandler(path)
    H.Handler._get_logs_tail(f)
    assert f.sent is not None
    code, body, ctype = f.sent
    assert code == 200 and ctype == 'application/json'
    return json.loads(body.decode('utf-8'))


def test_logs_tail_route_is_registered_and_resolvable():
    """路由表字符串必须能解析到真实方法（拼错一个字母该端点必 500）。"""
    name = H.Handler.GET_EXACT.get('/api/logs/tail')
    assert name == '_get_logs_tail'
    assert callable(getattr(H.Handler, name))


def test_logs_tail_returns_last_n_lines(monkeypatch, tmp_path):
    (tmp_path / 'framecut.log').write_text(
        ''.join('第%d行\n' % i for i in range(1, 11)), encoding='utf-8')
    d = _tail(monkeypatch, tmp_path, '/api/logs/tail?lines=3')
    assert d['ok'] is True
    assert d['count'] == 3
    assert d['lines'] == ['第8行', '第9行', '第10行']
    assert d['path'] == str(tmp_path / 'framecut.log')


def test_logs_tail_clamps_lines_parameter(monkeypatch, tmp_path):
    (tmp_path / 'framecut.log').write_text('只有一行\n', encoding='utf-8')
    # 非法值回退默认 200，超大值钳到 2000，负数/0 钳到 1 —— 都不许抛异常
    for q in ('lines=abc', 'lines=99999', 'lines=0', 'lines=-5'):
        d = _tail(monkeypatch, tmp_path, '/api/logs/tail?' + q)
        assert d['lines'] == ['只有一行'], q
    d = _tail(monkeypatch, tmp_path)
    assert d['lines'] == ['只有一行']


def test_logs_tail_missing_file_is_not_fatal(monkeypatch, tmp_path):
    """日志尚未生成时调试端点必须优雅返回，不能把主请求打挂。"""
    d = _tail(monkeypatch, tmp_path)
    assert d['ok'] is False
    assert d['count'] == 0 and d['lines'] == []


def test_logs_tail_handles_non_utf8_bytes(monkeypatch, tmp_path):
    """GBK 控制台残留字节用 errors='replace' 兜住，绝不 500。"""
    (tmp_path / 'framecut.log').write_bytes(b'ok line\n\xb9\xfe\xca\xfd line\n')
    d = _tail(monkeypatch, tmp_path, '/api/logs/tail?lines=2')
    assert d['ok'] is True and d['count'] == 2
    assert d['lines'][0] == 'ok line'

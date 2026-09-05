"""[P0 错误体验] 回归：_classify_exception 必须把常见异常映射到前端 ERR_RULES 的 kind。

不应让 `OSError: [Errno 28] No space left on device`、`TimeoutError: ...`、
`ConnectionRefusedError(...)` 这类机器消息原样泄漏给前端。
"""
import os
import sys

import pytest
import webui_server as S
import handler as H


def test_classify_exception_path_kind():
    """FileNotFoundError 应该归到 path(kind='path')。"""
    kind, _msg = H._classify_exception(FileNotFoundError("找不到那个 .mp4"))
    assert kind == 'path', f'预期 path, 实际 {kind}'


def test_classify_exception_disk_kind():
    """OSError 28 = no space → disk。"""
    kind, _msg = H._classify_exception(OSError(28, 'No space left on device'))
    assert kind == 'disk', f'预期 disk, 实际 {kind}'


def test_classify_exception_timeout_kind():
    kind, _msg = H._classify_exception(TimeoutError('请求超时'))
    assert kind == 'timeout'


def test_classify_exception_net_kind():
    """connection refused / 401 / 403 都归 net。"""
    for ex in [ConnectionRefusedError('远程拒绝'),
               OSError('401 unauthorized'),
               OSError('ssl certificate verify failed'),
               OSError('apikey 无效')]:
        kind, _msg = H._classify_exception(ex)
        assert kind == 'net', f'预期 net, 实际 {kind} (例 {ex!r})'


def test_classify_exception_perm_kind():
    kind, _msg = H._classify_exception(PermissionError('拒绝访问'))
    assert kind == 'perm'


def test_classify_exception_model_kind():
    kind, _msg = H._classify_exception(RuntimeError('whisper 模型未下载'))
    assert kind == 'model'


def test_classify_exception_ffmpeg_kind():
    """抽帧 / ffmpeg 报错归 frame,与前端 ERR_RULES 一致。"""
    kind, _msg = H._classify_exception(RuntimeError('ffmpeg exit code 1'))
    assert kind == 'frame'


def test_classify_exception_fallback_other():
    """不命中的归 other。"""
    kind, _msg = H._classify_exception(RuntimeError('完全无关的字符串'))
    assert kind == 'other'
    assert _msg  # 必须返回非空 msg


def test_classify_exception_none_safe():
    """None 输入不应崩。"""
    kind, msg = H._classify_exception(None)
    assert kind == 'other'
    assert msg

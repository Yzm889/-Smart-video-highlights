# -*- coding: utf-8 -*-
"""P0-1 审计后修复的回归：临时文件清理 + 写路径越界防护。

F2：ASR 中间 wav 只在异常分支删，成功路径直接 return，
    实测 webui_workspace/ 累计沉淀 247MB（98 分钟一次就 181MB）。
F3：三处 run_dir 用 `X if not os.path.isabs(X) else X`，
    把请求里的任意绝对路径当信任根 → 任意目录可写。
"""
import os

import pytest

import webui_server as S


# ---------------------------------------------------------------------------
# F2 临时文件清理
# ---------------------------------------------------------------------------
def test_silent_remove_deletes_and_tolerates(tmp_path):
    """_silent_remove 删得掉就删，删不掉也不该让任务失败。"""
    import ai_providers as A
    p = str(tmp_path / 'tmp.wav')
    open(p, 'wb').write(b'x' * 100)
    A._silent_remove(p)
    assert not os.path.exists(p)
    A._silent_remove(str(tmp_path / 'nope.wav'))   # 不存在
    A._silent_remove('')                            # 空路径


def test_asr_cleans_wav_on_success(monkeypatch, tmp_path):
    """成功路径也必须删掉中间 wav（旧代码只在异常分支删）。"""
    import ai_providers as A
    from ffmpeg_utils import _TLS
    monkeypatch.setattr(A, 'WORKDIR', str(tmp_path))
    monkeypatch.setattr(A, 'whisper_device', lambda: ('cpu', 'int8'))
    monkeypatch.setattr(A, '_whisper_env_setup', lambda: None)
    monkeypatch.setattr(A, '_aborted', lambda: False)
    monkeypatch.setattr(_TLS, 'runid', None, raising=False)

    def _fake_run(cmd, **kw):
        out = cmd[-1]
        # 写一个像样的 16bit 单声道 wav
        import struct
        import wave
        with wave.open(out, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b''.join(struct.pack('<h', 0) for _ in range(1600)))
        return 0, '', ''
    monkeypatch.setattr(A, 'ffmpeg_run', _fake_run)

    class _WM:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            class _Info:
                duration = 1.0
            return iter([]), _Info()
    import sys
    import types
    monkeypatch.setitem(sys.modules, 'faster_whisper',
                        types.SimpleNamespace(WhisperModel=_WM))

    A.asr_segments('fake.mp4')
    left = [f for f in os.listdir(str(tmp_path)) if f.startswith('asr_')]
    assert left == [], '成功路径也必须清理中间 wav，残留：%s' % left


def test_asr_cleans_wav_on_failure(monkeypatch, tmp_path):
    """失败路径同样要清理（回归旧行为）。"""
    import ai_providers as A
    from ffmpeg_utils import _TLS
    monkeypatch.setattr(A, 'WORKDIR', str(tmp_path))
    monkeypatch.setattr(A, 'whisper_device', lambda: ('cpu', 'int8'))
    monkeypatch.setattr(A, '_whisper_env_setup', lambda: None)
    monkeypatch.setattr(_TLS, 'runid', None, raising=False)
    monkeypatch.setattr(A, '_load_wav_as_float32',
                        lambda p: (_ for _ in ()).throw(RuntimeError('boom')))

    def _fake_run(cmd, **kw):
        open(cmd[-1], 'wb').write(b'x' * 100)
        return 0, '', ''
    monkeypatch.setattr(A, 'ffmpeg_run', _fake_run)

    class _WM:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            raise RuntimeError('boom')
    import sys
    import types
    monkeypatch.setitem(sys.modules, 'faster_whisper',
                        types.SimpleNamespace(WhisperModel=_WM))

    assert A.asr_segments('fake.mp4') == []
    left = [f for f in os.listdir(str(tmp_path)) if f.startswith('asr_')]
    assert left == [], '失败路径也要清理中间 wav，残留：%s' % left


# ---------------------------------------------------------------------------
# F3 目录越界防护
# ---------------------------------------------------------------------------
def test_safe_dir_rejects_absolute_and_traversal(tmp_path):
    base = str(tmp_path)
    # 绝对路径 / 盘符 / 越界 一律拒绝
    for bad in ('C:\\Windows\\System32', '/etc', '../../etc', '..\\..\\Windows',
                'a/../../etc', '', '   '):
        with pytest.raises(ValueError):
            S._safe_dir(base, bad)


def test_safe_dir_accepts_normal_name(tmp_path):
    base = str(tmp_path)
    full = S._safe_dir(base, 'run-1-20261021')
    assert full == os.path.join(os.path.abspath(base), 'run-1-20261021')


def test_render_rejects_absolute_run_dir(monkeypatch, tmp_path):
    """render 的 task_id 不得是任意绝对路径（旧写法直接信任）。"""
    import render_pipeline as RP
    prog = {}
    req = {'task_id': 'C:\\Windows\\System32',
           'segments': [{'segment_id': 0, 'keep': True, 'narration': 'x',
                         'start': 0, 'end': 1}]}
    RP.dispatch_render(req, prog)
    assert prog.get('error'), '绝对路径 task_id 必须被拒绝'
    assert '越界' in prog['error'] or '非法' in prog['error'], prog['error']
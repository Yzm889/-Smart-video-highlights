# -*- coding: utf-8 -*-
"""合成不应留下空 run 目录（v1.4.8）。

现象：`_spawn` 给每个任务预建 `run-N-时间戳` 目录，但合成是复用配音阶段的
run_dir，于是每合成一次就在 webui_output 下多一个空目录（实测 0 文件 0 字节）。
修法：合成开始时把**确实为空**的预建目录 rmdir 掉（非空/占用则原样保留，
绝不删数据），并把 prog['run_dir'] 指向真实产物目录。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402
import workflows as W  # noqa: E402


def _prepare(tmp_path, monkeypatch, pre_files=0):
    outdir = tmp_path / 'webui_output'
    outdir.mkdir()
    real = outdir / 'run-1-20260101-000000'
    real.mkdir()
    (real / 'tts_state.json').write_text('{}', encoding='utf-8')
    pre = outdir / 'run-2-20260101-000001'
    pre.mkdir()
    for i in range(pre_files):
        (pre / ('x%d.txt' % i)).write_text('keep', encoding='utf-8')

    monkeypatch.setattr(S, 'OUTDIR', str(outdir))
    monkeypatch.setattr(S, '_resolve_music', lambda m: None)
    monkeypatch.setattr(S, '_record_history', lambda *a, **k: None)

    def fake_compose(run_dir, prog, **kw):
        f = os.path.join(run_dir, 'final.mp4')
        with open(f, 'wb') as fh:
            fh.write(b'x' * 4096)
        prog['file'] = os.path.relpath(f, str(outdir)).replace('\\', '/')
        return f

    monkeypatch.setattr(S, 'compose_movie_from_tts', fake_compose)
    return outdir, real, pre


def test_empty_preallocated_dir_is_removed(tmp_path, monkeypatch):
    outdir, real, pre = _prepare(tmp_path, monkeypatch)
    prog = {'run_dir': str(pre)}
    W.dispatch_movie_compose({'run_dir': 'run-1-20260101-000000'}, prog)
    assert prog.get('done') is True, '合成没成功：%r' % prog.get('error')
    assert not pre.exists(), '预建的空目录没被清掉'
    assert prog['run_dir'] == 'run-1-20260101-000000', \
        'prog.run_dir 必须是相对目录名（前端要拿它调 /api/tts_state）：%r' % prog.get('run_dir')


def test_non_empty_dir_is_never_deleted(tmp_path, monkeypatch):
    """宁可留脏目录，也绝不删有内容的目录。"""
    outdir, real, pre = _prepare(tmp_path, monkeypatch, pre_files=2)
    prog = {'run_dir': str(pre)}
    W.dispatch_movie_compose({'run_dir': 'run-1-20260101-000000'}, prog)
    assert prog.get('done') is True, '合成没成功：%r' % prog.get('error')
    assert pre.exists() and len(list(pre.iterdir())) == 2, '有内容的目录被动了'


def test_missing_tts_state_still_errors(tmp_path, monkeypatch):
    outdir, real, pre = _prepare(tmp_path, monkeypatch)
    (real / 'tts_state.json').unlink()
    prog = {'run_dir': str(pre)}
    W.dispatch_movie_compose({'run_dir': 'run-1-20260101-000000'}, prog)
    assert prog.get('error'), '配音状态缺失时应当报错'
    assert pre.exists(), '报错路径上不该动那个目录'

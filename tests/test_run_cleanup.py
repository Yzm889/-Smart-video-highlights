# -*- coding: utf-8 -*-
"""batch5-5.1 run_dir 磁盘清扫（sweep_run_artifacts）的回归锁定。

策略要点（改坏任何一条都会让用户磁盘悄悄涨满或误删成片）：
- 孤儿目录（历史不引用、无任务在用、超期）→ 整目录删除
- 历史引用的目录（成片所在）→ 只删 src.* 副本，final 必须保留
- N 天内的新目录绝不动（两步走 Phase1 等待用户确认/调整）
- 运行中任务的目录绝不动
- cleanup_src_days=0 → 完全关闭
- 源片被清后再合成 → 明确报错指引，而非 ffmpeg 深处炸出一堆找不到文件
"""
import json
import os
import time

import pytest

import webui_server as S


def _mk_run(outdir, name, age_days, files=('src.mp4', 'final.mp4'), size=1024):
    d = os.path.join(outdir, name)
    os.makedirs(d, exist_ok=True)
    for f in files:
        with open(os.path.join(d, f), 'wb') as fh:
            fh.write(b'x' * size)
    old = time.time() - age_days * 86400
    os.utime(d, (old, old))
    return d


def _isolate(tmp_path, monkeypatch, cfg=None, history=None):
    out = str(tmp_path / 'webui_output')
    os.makedirs(out, exist_ok=True)
    monkeypatch.setattr(S, 'OUTDIR', out)
    monkeypatch.setattr(S, 'load_ai_config', lambda: (cfg if cfg is not None else {'cleanup_src_days': 3}))
    monkeypatch.setattr(S, 'load_history', lambda *a, **k: (history or []))
    return out


def test_sweep_deletes_orphan_old_run(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    _mk_run(out, 'run-1-old', age_days=10)
    freed = S.sweep_run_artifacts()
    assert not os.path.exists(os.path.join(out, 'run-1-old'))
    assert freed > 0


def test_sweep_keeps_recent_and_active(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    recent = _mk_run(out, 'run-2-recent', age_days=1)      # N 天内：两步走保护
    active = _mk_run(out, 'run-3-active', age_days=10)     # 运行中：任务保护
    monkeypatch.setitem(S.PROGRESS, 'run-3-active', {'run_dir': active, 'done': False})
    try:
        S.sweep_run_artifacts()
        assert os.path.isdir(recent), 'N 天内的目录不允许被清扫（两步走待确认）'
        assert os.path.isdir(active), '运行中任务的目录不允许被清扫'
    finally:
        S.PROGRESS.pop('run-3-active', None)


def test_sweep_referenced_old_run_keeps_final_deletes_src(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch, history=[{'file': 'run-4-old/final.mp4'}])
    d = _mk_run(out, 'run-4-old', age_days=10, files=('src.mp4', 'final.mp4', 'cover.jpg'))
    S.sweep_run_artifacts()
    assert os.path.isfile(os.path.join(d, 'final.mp4')), '历史成片绝不能删'
    assert os.path.isfile(os.path.join(d, 'cover.jpg'))
    assert not os.path.exists(os.path.join(d, 'src.mp4')), '过期源片副本应被回收'


def test_sweep_disabled_by_config(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch, cfg={'cleanup_src_days': 0})
    d = _mk_run(out, 'run-5', age_days=10)
    assert S.sweep_run_artifacts() == 0
    assert os.path.isfile(os.path.join(d, 'src.mp4'))


def test_sweep_non_run_dirs_untouched(tmp_path, monkeypatch):
    out = _isolate(tmp_path, monkeypatch)
    other = os.path.join(out, 'bili')
    os.makedirs(other, exist_ok=True)
    open(os.path.join(other, 'x.mp4'), 'wb').write(b'x' * 16)
    old = time.time() - 10 * 86400
    os.utime(other, (old, old))
    S.sweep_run_artifacts()
    assert os.path.isfile(os.path.join(other, 'x.mp4')), '只允许动 run-* 目录'


def test_compose_missing_src_friendly_error(tmp_path):
    d = tmp_path / 'run-6'
    d.mkdir()
    (d / 'tts_state.json').write_text(json.dumps({
        'video_path': str(d / 'src.mp4'), 'segs': [[0, 1]], 'narr': ['x'],
    }), encoding='utf-8')
    with pytest.raises(RuntimeError, match='源片副本已被磁盘清理'):
        S.compose_movie_from_tts(str(d))

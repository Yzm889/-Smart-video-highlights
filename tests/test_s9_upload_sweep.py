"""S9 上传残片周期清理回归：sweep_run_artifacts 兜底调用 _upload_prune，
不依赖用户下次上传触发（审计遗留「分片上传残片无定时清理」收口）。
判定与 _upload_prune 一致：会话目录 >24h 无写入即视为放弃（活跃会话每片写入刷新 mtime）。
"""

import os
import time

import webui_server as S


def _mk_session(tmp_path, name, age_hours, has_final=False):
    d = os.path.join(str(tmp_path / 'uploads'), name)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'part_0000'), 'wb').write(b'x')
    if has_final:
        open(os.path.join(d, 'final__v.mp4'), 'wb').write(b'F')
    old = time.time() - age_hours * 3600
    os.utime(d, (old, old))
    return d


def test_sweep_prunes_stale_upload_sessions(monkeypatch, tmp_path):
    """超过 24h 无活动的上传残片被周期清扫回收。"""
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    os.makedirs(str(tmp_path / 'uploads'))
    stale = _mk_session(tmp_path, 'up-stale', 25)
    _mk_session(tmp_path, 'up-fresh', 1)
    S.sweep_run_artifacts()
    assert not os.path.exists(stale), '超 24h 残片应被清理'
    assert os.path.isdir(os.path.join(str(tmp_path / 'uploads'), 'up-fresh')), '活动会话保留'


def test_sweep_keeps_recent_final(monkeypatch, tmp_path):
    """合并成品待取走的会话（24h 内）不受周期清扫影响。"""
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    os.makedirs(str(tmp_path / 'uploads'))
    d = _mk_session(tmp_path, 'up-final', 2, has_final=True)
    S.sweep_run_artifacts()
    assert os.path.exists(os.path.join(d, 'final__v.mp4')), '待取走成品保留'


def test_sweep_idempotent_and_safe(monkeypatch, tmp_path):
    """连续清扫幂等；UPLOAD_DIR 不存在时安全返回。"""
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    os.makedirs(str(tmp_path / 'uploads'))
    _mk_session(tmp_path, 'up-stale', 30)
    S.sweep_run_artifacts()
    S.sweep_run_artifacts()  # 第二次不报错
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'nonexistent'))
    assert S.sweep_run_artifacts() >= 0  # 目录缺失安全


def test_sweep_disabled_skips_upload_prune(monkeypatch, tmp_path):
    """cleanup_src_days=0（自动清理整体关闭）时不触发上传残片清扫。"""
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    os.makedirs(str(tmp_path / 'uploads'))
    d = _mk_session(tmp_path, 'up-stale', 25)
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'cleanup_src_days': 0})
    S.sweep_run_artifacts()
    assert os.path.isdir(d), '关闭自动清理时残片保留'

# -*- coding: utf-8 -*-
"""v1.4.36 回归钉：成片目录可删 + 删目录同步清历史记录。

需求（2026-11-11）：存储管理面板里成片目录显示「🔒 不可删」，用户没有
删除以前跑完的视频缓存与记录的选择。历史面板（桌面版）也只有单条删除，
没有批量清空。

改动：
  · _storage_scan：成片分组从 keep/不可删 → review/可删（成片是计算产物，
    删了不可恢复，review 语义最贴切）
  · _STORAGE_ALLOW：白名单从 run-*/_warmup_* 两种前缀放宽到 webui_output 下
    所有单段目录（run-* / live-e2e-* / live-long-* 等命名格式都能删）
  · _purge_history_prefix：删成片目录时同步清 history 里指向该目录的记录，
    避免留下「⚠️ 成片文件已丢失」的悬挂条目
  · 前端：存储面板成片组徽章「🟡 可删·不可恢复」+ 删除确认文案明示同步删记录；
    桌面版历史面板加「🗑 清空」按钮（调 /api/history/clear，后端本来就有）
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as W  # noqa: E402


def _setup_env(tmp_path, monkeypatch):
    """构造一个隔离的 OUTDIR，里面放两种命名格式的成片目录 + 一个孤儿目录。"""
    outdir = tmp_path / 'webui_output'
    outdir.mkdir()
    # run-* 格式（正常任务）
    run_dir = outdir / 'run-1-20261111-120000'
    run_dir.mkdir()
    (run_dir / 'final.mp4').write_bytes(b'x' * 2048)
    # live-* 格式（直播剪辑任务，命名不含 run- 前缀）
    live_dir = outdir / 'live-e2e-20261111-014525'
    live_dir.mkdir()
    (live_dir / 'clip01_00h35m50s.mp4').write_bytes(b'x' * 4096)
    # 孤儿目录（无历史引用，应归残留组）
    orphan = outdir / 'run-9-20261110-000000'
    orphan.mkdir()
    (orphan / 'junk.bin').write_bytes(b'x' * 8192)
    monkeypatch.setattr(W, 'OUTDIR', str(outdir))
    # 隔离 WORKDIR：_storage_scan 会扫 webui_workspace 下的 asr_/music_/up_ 残留组，
    # 不隔离的话真实项目的临时文件会混进 safe 组，污染 reclaimable 断言。
    ws = tmp_path / 'webui_workspace'
    ws.mkdir(exist_ok=True)
    monkeypatch.setattr(W, 'WORKDIR', str(ws))
    # UPLOAD_DIR 是模块导入时由 WORKDIR 求值的常量，monkeypatch WORKDIR 不会带动它，
    # 必须单独隔离，否则真实项目 webui_workspace/uploads 的上传会话会混进 safe 组。
    monkeypatch.setattr(W, 'UPLOAD_DIR', str(ws / 'uploads'))
    # 写历史记录引用 run-* 与 live-* 两个目录
    hist = tmp_path / 'history.json'
    hist.write_text(json.dumps([
        {'file': 'run-1-20261111-120000/final.mp4', 'time': 't1'},
        {'file': 'live-e2e-20261111-014525/clip01_00h35m50s.mp4', 'time': 't2'},
    ], ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(W, 'HISTORY_PATH', str(hist))
    return outdir


def test_scan_outputs_group_now_deletable(tmp_path, monkeypatch):
    """成片分组必须是 review 档 + deletable=True。"""
    _setup_env(tmp_path, monkeypatch)
    d = W._storage_scan()
    outs = [g for g in d['groups'] if g['key'] == 'outputs'][0]
    assert outs['tier'] == 'review', '成片档位应为 review'
    assert outs['deletable'] is True, '成片必须可删'
    assert outs['total'] > 0
    assert len(outs['items']) == 2, '两种命名格式的目录都要扫到'


def test_scan_orphan_goes_to_residual(tmp_path, monkeypatch):
    """无历史引用的孤儿目录归残留组（safe，可一键清理），不进成片组。"""
    _setup_env(tmp_path, monkeypatch)
    d = W._storage_scan()
    outs = [g for g in d['groups'] if g['key'] == 'outputs'][0]
    runs = [g for g in d['groups'] if g['key'] == 'run_residual'][0]
    names_out = {it['name'] for it in outs['items']}
    names_run = {it['name'] for it in runs['items']}
    assert names_out == {'run-1-20261111-120000', 'live-e2e-20261111-014525'}
    assert names_run == {'run-9-20261110-000000'}, '孤儿目录必须归残留组'
    assert runs['tier'] == 'safe' and runs['deletable'] is True


def test_scan_reclaimable_excludes_films(tmp_path, monkeypatch):
    """可回收只算 safe 组——一键清理不删成片，summary 数字不能虚高。"""
    _setup_env(tmp_path, monkeypatch)
    d = W._storage_scan()
    outs = [g for g in d['groups'] if g['key'] == 'outputs'][0]
    runs = [g for g in d['groups'] if g['key'] == 'run_residual'][0]
    assert d['reclaimable_bytes'] == runs['total'], (
        'reclaimable 应等于 safe 组（残留）体积，不能包含成片：%r vs %r'
        % (d['reclaimable_bytes'], runs['total']))
    assert d['reclaimable_bytes'] < outs['total'] + runs['total']


def test_whitelist_allows_all_run_dir_naming(tmp_path, monkeypatch):
    """白名单放行所有 webui_output 单段目录（run-* / live-* / 任意任务目录名）。"""
    _setup_env(tmp_path, monkeypatch)
    for rel in ('webui_output/run-1-20261111-120000',
                'webui_output/live-e2e-20261111-014525',
                'webui_output/live-long-031348'):
        assert W._storage_resolve_deletable(rel) is not None, rel + ' 应可删'


def test_whitelist_still_rejects_traversal(tmp_path, monkeypatch):
    """白名单放宽后穿越防护不能松：../ 与绝对路径必须拒绝。"""
    _setup_env(tmp_path, monkeypatch)
    for rel in ('webui_output/../history.json',
                'webui_output/../../etc/passwd',
                '/webui_output/run-1',
                'webui_output/sub/dir',
                'webui_workspace/../webui_output/run-1'):
        assert W._storage_resolve_deletable(rel) is None, rel + ' 应被拒绝'


def test_purge_history_prefix_removes_only_matching(tmp_path, monkeypatch):
    """_purge_history_prefix 只删指定目录前缀的记录，其他记录保留。"""
    hist = tmp_path / 'history.json'
    hist.write_text(json.dumps([
        {'file': 'run-1-20261111-120000/final.mp4', 'time': 't1'},
        {'file': 'live-e2e-20261111-014525/clip01_00h35m50s.mp4', 'time': 't2'},
        {'file': 'run-2-20261111-130000/final.mp4', 'time': 't3'},
    ], ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(W, 'HISTORY_PATH', str(hist))

    removed = W._purge_history_prefix('run-1-20261111-120000')
    assert removed == 1
    items = json.loads(hist.read_text(encoding='utf-8'))
    files = [it['file'] for it in items]
    assert 'run-1-20261111-120000/final.mp4' not in files
    assert 'live-e2e-20261111-014525/clip01_00h35m50s.mp4' in files
    assert 'run-2-20261111-130000/final.mp4' in files


def test_purge_history_prefix_empty_and_missing(tmp_path, monkeypatch):
    """空前缀 / 不存在的目录 → 不误删任何记录。"""
    hist = tmp_path / 'history.json'
    hist.write_text(json.dumps([{'file': 'run-1/final.mp4'}]), encoding='utf-8')
    monkeypatch.setattr(W, 'HISTORY_PATH', str(hist))
    assert W._purge_history_prefix('') == 0
    assert W._purge_history_prefix('nonexistent-dir') == 0
    assert len(json.loads(hist.read_text(encoding='utf-8'))) == 1

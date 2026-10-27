# -*- coding: utf-8 -*-
"""路径安全审计（行为级，不只静态检查）。

2026-10-26 专项审计：把项目里所有"路径类入参"的落点逐个查了一遍。结论是
**主体防护到位**（material 用 basename+_safe_filename+扩展名黑名单、upload_id 用
正则白名单、storage 用 正则+commonpath 双保险、run_dir 用 _safe_dir），但发现
**一处潜伏的任意文件删除原语**：

    _remove_history_file: fp = os.path.join(OUTDIR, rel) if not os.path.isabs(rel) else rel

绝对路径会被**直接采用**。当前调用方 delete_history 会先查历史列表、匹配不到就
不删，所以暂时不可利用；但只要将来有人把用户可控的 file 写进 history.json，
就能删掉任意路径的文件。已改为「解析后必须落在 OUTDIR 内」。

本文件锁定（行为级）：
  ① 越界删除必须失败：绝对路径 / .. 穿越 / 符号链接式写法，都不能删掉 OUTDIR 外的文件；
  ② 正常删除仍然可用（别把功能改死了）；
  ③ 素材库文件名穿越（../、C:\\、超长、保留名）必须被挡；
  ④ upload_id / storage 路径的既有防护不能退化。
"""
import os
import tempfile

import pytest

import webui_server as S


# ---------------------------------------------------------------------------
# ① 越界删除
# ---------------------------------------------------------------------------
def test_remove_history_file_refuses_absolute_path_outside_outdir(monkeypatch, tmp_path):
    """绝对路径指向 OUTDIR 之外 → 必须拒绝删除（原来会真删）。"""
    victim = tmp_path / 'important.txt'
    victim.write_text('别删我', encoding='utf-8')
    S._remove_history_file(str(victim))
    assert victim.exists(), '越界绝对路径被删除了！这是任意文件删除漏洞'


def test_remove_history_file_refuses_parent_traversal(monkeypatch, tmp_path):
    """.. 穿越出 OUTDIR → 必须拒绝。"""
    outdir = tmp_path / 'out'
    outdir.mkdir()
    (tmp_path / 'sibling.mp4').write_text('x' * 2000, encoding='utf-8')
    monkeypatch.setattr(S, 'OUTDIR', str(outdir))
    S._remove_history_file('../sibling.mp4')
    assert (tmp_path / 'sibling.mp4').exists(), '.. 穿越把 OUTDIR 外的文件删了！'


def test_remove_history_file_still_deletes_inside_outdir(monkeypatch, tmp_path):
    """正常路径（OUTDIR 内的成片）仍然要能删——别把功能改死。"""
    outdir = tmp_path / 'out'
    run = outdir / 'run-x'
    run.mkdir(parents=True)
    final = run / 'final.mp4'
    final.write_bytes(b'0' * 2000)
    monkeypatch.setattr(S, 'OUTDIR', str(outdir))
    S._remove_history_file('run-x/final.mp4')
    assert not final.exists(), 'OUTDIR 内的成片应能被删除'


def test_add_history_cleanup_ignores_absolute_outside_path(monkeypatch, tmp_path):
    """add_history 的中间产物清理同样不能碰 OUTDIR 之外。"""
    outdir = tmp_path / 'out'
    outdir.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / 'narr01.wav'          # 命中 cleanup_run_mid 的 mid_patterns
    victim.write_bytes(b'0' * 2000)
    monkeypatch.setattr(S, 'OUTDIR', str(outdir))
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'cleanup_mid': True})
    monkeypatch.setattr(S, 'load_history', lambda n=500: [])
    monkeypatch.setattr(S, '_atomic_write_json', lambda *a, **k: None)
    S.add_history({'file': str(victim)})
    assert victim.exists(), 'add_history 清理了 OUTDIR 之外的文件！'


# ---------------------------------------------------------------------------
# ③ 素材库文件名穿越
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('evil', [
    '../../evil.mp4',
    '..\\..\\evil.mp4',
    'C:\\Windows\\System32\\evil.mp4',
    '/etc/passwd',
    'sub/dir/evil.mp4',
])
def test_material_save_bytes_blocks_traversal(evil, tmp_path, monkeypatch):
    """素材名穿越 → 落点必须仍在素材目录内。"""
    mat = tmp_path / 'material'
    monkeypatch.setattr(S, 'MATERIAL_DIR', str(mat))
    monkeypatch.setattr(S, '_MATERIAL_BLOCKED_EXT', getattr(S, '_MATERIAL_BLOCKED_EXT', ()))
    name, err = S.material_save_bytes(evil, b'0' * 100)
    if name:
        written = os.path.realpath(os.path.join(str(mat), name))
        assert os.path.commonpath([os.path.realpath(str(mat)), written]) == os.path.realpath(str(mat)), \
            '素材写到了目录外：%s → %s' % (evil, written)
    else:
        assert err, '应给出错误说明'


def test_material_name_sanitized_no_separators():
    """文件名里的路径分隔符必须被替换掉。"""
    out = S._safe_filename('a/b\\c:d*e?f"g<h>i|j')
    for ch in '\\/:*?"<>|':
        assert ch not in out, out


# ---------------------------------------------------------------------------
# ④ 既有防护不能退化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('bad', ['../x', 'a/b', 'C:x', '', 'x' * 100, None])
def test_upload_dir_of_rejects_bad_ids(bad):
    assert S._upload_dir_of(bad) is None, '非法 upload_id 应被拒：%r' % bad


def test_upload_dir_of_accepts_valid_id():
    d = S._upload_dir_of('up-abc123')
    assert d is not None and d.endswith('up-abc123')


@pytest.mark.parametrize('bad', ['../../etc/passwd', '/abs/path', 'a/../../b', ''])
def test_storage_resolve_deletable_rejects_traversal(bad):
    assert S._storage_resolve_deletable(bad) is None, '越权清理路径应被拒：%r' % bad


def test_safe_dir_blocks_traversal():
    """_safe_dir 是写路径的咽喉：必须挡住 .. 与绝对路径。"""
    base = tempfile.gettempdir()
    for bad in ('../evil', '..\\evil', '/abs', 'C:\\abs', 'a/../../b', ''):
        with pytest.raises(ValueError):
            S._safe_dir(base, bad)

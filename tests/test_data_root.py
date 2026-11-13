# -*- coding: utf-8 -*-
"""v1.4.80 数据目录外置回归钉。

背景：成片/素材/音乐/历史/工作区/配置/模型此前全埋在 `HERE`（打包态=EXE 的
`_internal/`）下，导致每次替换桌面副本都要整目录搬 GB 级数据（2026-11-13 真机
替换踩中 robocopy 残留 → bad magic number）。本轮外置到数据根，TTS 资产
（tts2_engines.TTS2_DIR）是既有正确先例。

钉住的契约：
  · 数据根推导：环境变量 > 打包态(%LOCALAPPDATA%\\FrameCut) > 源码态(HERE)
  · **源码态行为零变化**（数据根=HERE，与改动前逐字一致）——CI/测试不受影响
  · 各数据常量确实经数据根派生（OUTDIR/HISTORY_PATH/MATERIAL_DIR/WORKDIR/AI_CONFIG_PATH）
  · 只读资源（STATIC_DIR/FONT_DIR）仍走 HERE，不随数据根移动
  · 自动迁移：打包态首启把程序目录旁的旧数据搬到数据根，旧数据保留
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import paths as P  # noqa: E402


# ---------------------------------------------------------------------------
# 数据根推导
# ---------------------------------------------------------------------------

def test_source_mode_root_is_here():
    """源码态（无 frozen）：数据根 = HERE（仓库根）——与改动前逐字一致。"""
    fake_here = os.path.join('C:\\', 'some', 'repo')
    assert P.default_data_root(fake_here) == fake_here
    assert P.data_root(fake_here) == os.path.abspath(fake_here)


def test_frozen_mode_root_is_localappdata(monkeypatch):
    """打包态：数据根 = %LOCALAPPDATA%\\FrameCut（与既有 tts2\\ 平级）。"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('LOCALAPPDATA', r'C:\Users\x\AppData\Local')
    monkeypatch.delenv('FRAMECUT_DATA_DIR', raising=False)
    assert P.data_root(r'C:\pkg\_internal') == \
        os.path.abspath(os.path.join(r'C:\Users\x\AppData\Local', 'FrameCut'))


def test_env_var_overrides_everything(monkeypatch):
    """FRAMECUT_DATA_DIR 优先级最高（测试/多实例隔离用）。"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('LOCALAPPDATA', r'C:\Users\x\AppData\Local')
    monkeypatch.setenv('FRAMECUT_DATA_DIR', r'D:\framecut-data')
    assert P.data_root(r'C:\pkg\_internal') == os.path.abspath(r'D:\framecut-data')


def test_data_root_falls_back_without_localappdata(monkeypatch):
    """没有 LOCALAPPDATA/APPDATA 时退回用户目录，不许返回空路径。"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    monkeypatch.delenv('APPDATA', raising=False)
    monkeypatch.delenv('FRAMECUT_DATA_DIR', raising=False)
    r = P.data_root(r'C:\pkg\_internal')
    assert r and r.endswith('FrameCut') and os.path.isabs(r)


# ---------------------------------------------------------------------------
# 各常量确实经数据根派生（源码态 = 仓库根）
# ---------------------------------------------------------------------------

def test_runtime_constants_derive_from_data_root():
    """各数据常量都挂在数据根下。

    注意 conftest 的 autouse fixture 会把 OUTDIR/HISTORY_PATH/AI_CONFIG_PATH
    改到 tmp_path（既有的正确隔离），所以这里**重新 import 源码文件检查派生
    公式**，而不是读运行时值。"""
    import webui_server as S
    dr = S.DATA_ROOT
    for name, sub in (('HISTORY_PATH', 'history.json'),
                      ('MATERIAL_DIR', 'material_library'),
                      ('MUSIC_DIR', 'music_library'),
                      ('OUTDIR', 'webui_output')):
        with open(os.path.join(ROOT, 'webui_server.py'), encoding='utf-8') as f:
            src = f.read()
        assert "os.path.join(DATA_ROOT, '%s')" % sub in src, \
            '%s 必须经 DATA_ROOT 派生' % name
    assert os.path.basename(S.OUTDIR) == 'webui_output'
    # PROGRESS_FILE 是 import 期从原始 OUTDIR 派生的（conftest 只 patch S.OUTDIR），
    # 故它指向真实数据根下的 webui_output——钉它「属于数据根」即可。
    assert os.path.dirname(os.path.normpath(S.PROGRESS_FILE)) == \
        os.path.normpath(os.path.join(S.DATA_ROOT, 'webui_output'))
    with open(os.path.join(ROOT, 'cache_utils.py'), encoding='utf-8') as f:
        cu = f.read()
    assert "os.path.join(_paths.data_root(HERE), 'webui_workspace')" in cu
    with open(os.path.join(ROOT, 'ai_providers.py'), encoding='utf-8') as f:
        ap = f.read()
    assert "_paths.data_root(os.path.dirname(os.path.abspath(__file__)))" in ap
    assert dr  # 数据根非空


def test_source_mode_constants_equal_old_layout():
    """源码态：数据根=HERE（派生公式与「外置之前」逐字相同——零回归保证）。"""
    import webui_server as S
    assert S.DATA_ROOT == S.HERE


def test_readonly_resources_stay_in_here():
    """只读资源（static/字体）仍走 HERE，不随数据根移动。"""
    import webui_server as S
    assert S.STATIC_DIR == os.path.join(S.HERE, 'static')
    assert S.FONT_DIR == os.path.join(S.HERE, 'assets', 'fonts')


def test_models_dirs_derive_from_root():
    import webui_server as S
    import ai_providers as A
    import tts_engines as T
    dr = S.DATA_ROOT
    assert os.path.normpath(A.whisper_models_dir()) == \
        os.path.normpath(os.path.join(dr, 'models', 'whisper'))
    assert os.path.normpath(T.tts_models_dir()) == \
        os.path.normpath(os.path.join(dr, 'models', 'tts'))


# ---------------------------------------------------------------------------
# 自动迁移
# ---------------------------------------------------------------------------

def test_auto_migrate_noop_in_source_mode(monkeypatch, tmp_path):
    """源码态不迁移（数据根=HERE，源=目标）。"""
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    moved, notes = P.auto_migrate_if_needed(str(tmp_path))
    assert moved == [] and notes == []


def test_auto_migrate_moves_old_data(monkeypatch, tmp_path):
    """打包态：程序目录旁有旧数据 + 数据根为空 → 搬到数据根，旧数据保留不删。"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    here = tmp_path / 'pkg' / '_internal'
    data_root = tmp_path / 'userdata' / 'FrameCut'
    (here / 'webui_output' / 'run-1').mkdir(parents=True)
    (here / 'webui_output' / 'run-1' / 'final.mp4').write_bytes(b'x' * 10)
    (here / 'ai_config.json').write_text('{"x":1}', encoding='utf-8')
    moved, notes = P.auto_migrate_if_needed(str(here), str(data_root))
    # 数据到位
    assert (data_root / 'webui_output' / 'run-1' / 'final.mp4').is_file()
    assert (data_root / 'ai_config.json').is_file()
    assert any('webui_output' in m for m in moved), moved
    assert 'ai_config.json' in moved
    # 旧配置保留（copy 不 move——用户回退旧版 exe 时配置仍在）
    assert (here / 'ai_config.json').is_file(), '旧配置不该被删'
    # 目录迁移后源目录骨架仍在（逐文件并入，不整目录 move→不丢骨架）
    assert (here / 'webui_output').is_dir(), '源目录骨架不该被移走'


def test_auto_migrate_is_idempotent(monkeypatch, tmp_path):
    """迁移幂等：连跑两次不报错、不重复搬、数据不错乱。"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    here = tmp_path / 'pkg' / '_internal'
    root = tmp_path / 'userdata'
    (here / 'webui_output' / 'r1').mkdir(parents=True)
    (here / 'webui_output' / 'r1' / 'final.mp4').write_bytes(b'x' * 8)
    m1, _n1 = P.auto_migrate_if_needed(str(here), str(root))
    m2, _n2 = P.auto_migrate_if_needed(str(here), str(root))
    assert m1 and not m2, '第二次应无新增（文件已搬走）: %r' % (m2,)
    assert (root / 'webui_output' / 'r1' / 'final.mp4').read_bytes() == b'x' * 8


def test_auto_migrate_merges_without_overwrite(monkeypatch, tmp_path):
    """数据根已有同名数据时只补缺、不覆盖（用户现有数据优先）。"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    here = tmp_path / 'pkg' / '_internal'
    root = tmp_path / 'userdata'
    (here / 'webui_output' / 'run-old').mkdir(parents=True)
    (here / 'webui_output' / 'run-old' / 'a.txt').write_text('old', encoding='utf-8')
    (here / 'webui_output' / 'run-new').mkdir(parents=True)
    (here / 'webui_output' / 'run-new' / 'b.txt').write_text('new', encoding='utf-8')
    (root / 'webui_output' / 'run-old').mkdir(parents=True)
    (root / 'webui_output' / 'run-old' / 'a.txt').write_text('KEEP', encoding='utf-8')
    moved, _notes = P.auto_migrate_if_needed(str(here), str(root))
    assert (root / 'webui_output' / 'run-old' / 'a.txt').read_text(
        encoding='utf-8') == 'KEEP', '同名文件绝不能被旧数据覆盖'
    assert (root / 'webui_output' / 'run-new' / 'b.txt').read_text(
        encoding='utf-8') == 'new', '缺的目录要补进来'
    assert moved


def test_auto_migrate_file_keeps_existing_root_version(monkeypatch, tmp_path):
    """数据根本来就有 ai_config.json → 不覆盖（用户自己配过 key）。"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    here = tmp_path / 'pkg' / '_internal'
    root = tmp_path / 'userdata'
    (here).mkdir(parents=True)
    (here / 'ai_config.json').write_text('OLD', encoding='utf-8')
    root.mkdir(parents=True)
    (root / 'ai_config.json').write_text('MINE', encoding='utf-8')
    moved, notes = P.auto_migrate_if_needed(str(here), str(root))
    assert (root / 'ai_config.json').read_text(encoding='utf-8') == 'MINE'
    assert 'ai_config.json' not in moved
    assert any('ai_config.json' in n for n in notes)


# ---------------------------------------------------------------------------
# 接线钉：desktop 桥与启动链都指向数据根
# ---------------------------------------------------------------------------

def test_desktop_uses_data_root():
    with open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8') as f:
        src = f.read()
    assert 'new_here = webui_server.DATA_ROOT' in src, \
        'migrate_from 目标必须是数据根，否则迁完还在 _internal 里'
    assert "webui_server.DATA_ROOT,\n                                                       'ai_config.json'" in src \
        or "os.path.join(webui_server.DATA_ROOT" in src, \
        'has_config 判据必须指向数据根'


def test_start_server_runs_automigrate_before_makedirs():
    """自动迁移必须在建目录之前跑——否则空数据根会被先造出来，误判「无需迁移」。"""
    with open(os.path.join(ROOT, 'handler.py'), encoding='utf-8') as f:
        src = f.read()
    i = src.index('def start_server(')
    body = src[i:i + 1200]
    assert 'auto_migrate_if_needed' in body
    assert body.index('auto_migrate_if_needed') < body.index('makedirs(_w.WORKDIR')

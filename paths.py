# -*- coding: utf-8 -*-
"""paths: 运行时数据根的唯一事实来源（v1.4.80 数据目录外置）。

【为什么需要这个模块】在本轮之前，成片 / 素材库 / 音乐库 / 历史 / 工作区 / 配置 /
模型权重全部写在 `HERE`（打包态 = EXE 的 `_internal/`）下面。后果是桌面成品
**每次替换目录都要把 GB 级数据一起搬**（2026-11-13 真机替换就踩中：robocopy /E
覆盖残留旧解释器文件 → bad magic number，最后整目录删掉重拷 + 手工迁数据）。

TTS 资产早在 v1.4.16 就外置到 `%LOCALAPPDATA%\\FrameCut\\tts2`（见
tts2_engines.py 顶部），docstring 写明「不放在 EXE 的 _internal 下——桌面成品
每次发版都整体替换目录」。本模块把同一条路线推广到其余运行时数据，成为唯一入口。

【数据根推导】
    FRAMECUT_DATA_DIR 环境变量   → 最高优先（测试隔离 / 多实例并存）
    否则 sys.frozen（打包态）    → %LOCALAPPDATA%\\FrameCut（与 tts2/ 平级）
    否则（源码态）               → HERE（仓库根）——行为与改动前逐字一致

【为什么源码态留在 HERE】dev 仓库是日常调试用的，数据留在仓库内即「与今天零差异」：
CI 与本地测试无需任何改动，且调试时不会污染打包副本的真实数据。这是「自动分离」：
改代码在 dev、真实数据在桌面副本，互不干扰。

【只读资源不在这里】static / assets/fonts / worker 脚本仍走 HERE——它们随包分发、
不写盘，属于安装内容而非用户数据。
"""
import os
import sys

# 源码态 = 本文件所在目录（仓库根）；打包态 PyInstaller 会把模块解到 _MEIPASS，
# 但本项目的 HERE 语义在打包态由调用方（webui_server）显式传入包目录，故这里
# 只负责「数据根」，HERE 由调用方传参决定（见 data_root(here)）。
_HERE = os.path.dirname(os.path.abspath(__file__))

# 打包态数据根 = %LOCALAPPDATA%\FrameCut，与既有 tts2\ 同父目录（用户心智一致：
# 「FrameCut 的东西都在这一个文件夹里」）。LOCALAPPDATA 缺失时退回用户目录。
_APP_DIRNAME = 'FrameCut'


def _local_app_data():
    base = (os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
            or os.path.expanduser('~'))
    return base


def is_frozen():
    """是否打包运行（PyInstaller）。仓库既有判定方式，见 tts2_models.py:172。"""
    return bool(getattr(sys, 'frozen', False))


def default_data_root(here=None):
    """按环境推导数据根（不含环境变量覆盖）：

    - 打包态 → %LOCALAPPDATA%\\FrameCut
    - 源码态 → here（仓库根）
    """
    if is_frozen():
        return os.path.join(_local_app_data(), _APP_DIRNAME)
    return here or _HERE


def data_root(here=None):
    """数据根单一入口：环境变量 > 打包/源码推导。

    FRAMECUT_DATA_DIR 供测试与多实例并行使用（两个实例指向不同目录即互不干扰）。
    """
    env = (os.environ.get('FRAMECUT_DATA_DIR') or '').strip()
    if env:
        return os.path.abspath(env)
    return os.path.abspath(default_data_root(here))


def at(root, *parts):
    """在数据根下拼路径（统一入口，便于将来换根时一处收口）。"""
    return os.path.join(root, *parts)


# 需要在打包态从程序目录「就地迁移」到数据根的数据项。
# 只列运行时数据；static/assets/worker 脚本等只读资源不迁（它们随包分发）。
MIGRATABLE = (
    ('webui_output', 'dir'),
    ('webui_workspace', 'dir'),
    ('material_library', 'dir'),
    ('music_library', 'dir'),
    ('models', 'dir'),
    ('ai_config.json', 'file'),
    ('history.json', 'file'),
)


def _merge_tree(src, dst):
    """把 src 递归并入 dst，**同名不覆盖**（返回新增文件数）。

    与 desktop.migrate_from 的合并语义一致：迁移只补缺，绝不覆盖用户现有数据。
    """
    added = 0
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = dst if rel == '.' else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(target, f)
            if os.path.exists(d):
                continue
            try:
                os.replace(s, d)
            except OSError:
                import shutil
                try:
                    shutil.copy2(s, d)
                except OSError:
                    continue
            added += 1
    return added


def auto_migrate_if_needed(here, target_root=None):
    """打包态首启：若数据根还没有这些数据、而程序目录旁有旧数据 → 搬过去。

    这是「根治替换目录之痛」的关键一步：老用户更新到外置版后，第一次启动
    自动把 `_internal/` 里的历史数据搬到 `%LOCALAPPDATA%\\FrameCut`，此后
    升级只需换 exe。返回 (moved: list[str], notes: list[str])。

    【数据安全语义——沿用既有的「并入」约定】
      · **目录**：逐文件 `os.replace` 原子移动（同一文件要么在源要么在目标，
        永不丢）；同名已存在则跳过、绝不覆盖。跨盘时 os.replace 抛错会自动
        退回 copy2。源目录骨架保留在程序目录（不 rmtree）。
      · **配置文件**（ai_config.json / history.json）：copy **不 move**——
        用户回退旧版 exe 时配置仍在。
      · 幂等：中途失败/被打断后重跑只会补齐剩余文件，**自愈**。
      · 为什么不整目录 shutil.move：一次大 move 被打断会停在半迁移态；逐文件
        并入失败最多少搬几个文件，源目录始终还在，用户可自行重试或手工拷。

    非打包态（源码态数据根=HERE，源=目标）返回 ([], [])。
    """
    if not is_frozen():
        return [], []
    root = os.path.abspath(target_root or data_root(here))
    here = os.path.abspath(here)
    if os.path.normcase(root) == os.path.normcase(here):
        return [], []
    moved, notes = [], []
    for name, kind in MIGRATABLE:
        # 源只认程序目录（_internal）里的旧数据；数据根本身不参与
        src = os.path.join(here, name)
        dst = os.path.join(root, name)
        if not os.path.exists(src):
            continue
        if os.path.normcase(src) == os.path.normcase(dst):
            continue
        try:
            if kind == 'file':
                if os.path.exists(dst):
                    notes.append('%s 已存在，保留数据根版本' % name)
                    continue
                os.makedirs(root, exist_ok=True)
                import shutil
                shutil.copy2(src, dst)
                moved.append(name)
            else:
                os.makedirs(dst, exist_ok=True)
                cnt = _merge_tree(src, dst)
                if cnt:
                    moved.append('%s（并入 %d 个文件）' % (name, cnt))
        except OSError as e:
            notes.append('%s 迁移失败：%s' % (name, str(e)[:120]))
    return moved, notes

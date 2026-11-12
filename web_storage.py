# -*- coding: utf-8 -*-
"""存储管理族（磁盘扫描 / 可删校验 / 历史清理）——由 webui_server.py 拆出（v1.4.54）。

搬运范围（逐字搬运、零逻辑改动）：
    _storage_dir_size / _STORAGE_ALLOW / _storage_resolve_deletable /
    _storage_scan / _purge_history_prefix

【依赖解析规则——与 movie_narrator 拆分同款，勿改】本模块由 webui_server 在其
加载中途 import（_host() 返回同一个部分初始化的宿主模块对象）；凡定义在宿主
命名空间的符号（HERE/OUTDIR/UPLOAD_DIR/WORKDIR/load_history/_HIST_LOCK/
HISTORY_PATH/_atomic_write_json），一律经 `import webui_server as _WS` 后以
_WS.X(...) 调用期属性查找——测试普遍 monkeypatch.setattr(S, 'X', ...) 打在
宿主上（如 test_storage_film_delete 改 OUTDIR），按值绑定会让 patch 打空。
"""
import os
import shutil

# ---- 宿主晚绑定（照抄 movie_narrator._host）----
_HERE = os.path.dirname(os.path.abspath(__file__))
_HOST_FILE = os.path.join(_HERE, 'webui_server.py')


def _host():
    m = sys_modules_get()
    if m is not None:
        return m
    import webui_server
    return webui_server


def sys_modules_get():
    import sys
    m = sys.modules.get('webui_server')
    if m is not None:
        return m
    m = sys.modules.get('__main__')
    if m is not None and os.path.abspath(getattr(m, '__file__', '')) == _HOST_FILE:
        return m
    return None


def _storage_dir_size(p):
    """递归统计目录体积（字节）。"""
    tot = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    tot += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return tot


# 可被存储面板安全删除的路径白名单（相对项目根，正则匹配；禁止任何穿越/越界子路径）
# v1.4.36：webui_output 下放行所有「单段目录名」——run-* / live-* / _warmup_* 等任务产物目录。
#   目录名由 _safe_dir 约束生成（拒绝绝对路径/盘符/..），且 _storage_resolve_deletable 前置
#   检查已拒绝 '..' 段与越界 commonpath，因此单段通配不存在穿越风险。
_STORAGE_ALLOW = [
    r'^webui_output/[^/]+$',
    r'^webui_workspace/uploads/up-[0-9A-Za-z-]+$',
    r'^webui_workspace/asr_[0-9]+\.wav$',
    r'^webui_workspace/music_[0-9]+\.(mp3|wav)$',
    # 素材落盘名带 runid（见 dispatch_build），故第一段允许字母/数字/横线下划线
    r'^webui_workspace/up_[0-9A-Za-z_-]+_[a-z]+\.(jpg|png|webp|mp4)$',
    r'^webui_workspace/analysis_cache$',
    r'^models(/whisper)?$',
]


def _storage_resolve_deletable(rel):
    """校验 rel 是否为可清理路径，返回绝对路径；否则 None（拒绝穿越/越权删除）。"""
    import webui_server as _WS
    import re as _re
    if not rel:
        return None
    rel = rel.replace('\\', '/')
    if rel.startswith('/') or '..' in rel.split('/'):
        return None
    for pat in _STORAGE_ALLOW:
        if _re.match(pat, rel):
            full = os.path.normpath(os.path.join(_WS.HERE, rel))
            base_abs = os.path.abspath(_WS.HERE)
            if os.path.commonpath([base_abs, full]) == base_abs:
                return full
            return None
    return None


def _storage_scan():
    """扫描项目内各类磁盘占用，分组返回，供前端存储管理面板展示与清理。

    档位：keep=保留不可删 / safe=临时可回收 / review=删除需重新下载。
    """
    import webui_server as _WS
    import re as _re
    groups = []

    out_names = sorted(os.listdir(_WS.OUTDIR)) if os.path.isdir(_WS.OUTDIR) else []
    out_items, out_total = [], 0
    run_items, run_total = [], 0
    # v1.4.36：按 history 引用区分「成片目录」与「任务残留」。
    # 背景：run_dir 命名是 run-N-时间戳，history 的 file 就是 'run-N-时间戳/final.mp4'；
    # 旧逻辑按前缀分（run-*/_warmup_* → 残留）有两个反向缺陷：
    #   ① 带 final.mp4 的成片目录被归「残留」（safe）→ 一键清理会把成片删掉，
    #      历史页留一堆「⚠️ 文件已丢失」；
    #   ② live-* 成片目录（无 run- 前缀）被归 outputs（keep）→ 用户想删删不掉。
    # history 引用是唯一事实来源：有记录=成片（review 可删，删时二次确认+同步清记录）；
    # 无记录=孤儿/失败残留（safe 一键清理回收）。
    ref_dirs = set()
    try:
        for it in _WS.load_history(500):
            f = (it.get('file') or '').replace('\\', '/')
            if '/' in f:
                ref_dirs.add(f.split('/')[0])
    except Exception:
        pass
    for name in out_names:
        p = os.path.join(_WS.OUTDIR, name)
        if not os.path.isdir(p):
            continue
        s = _storage_dir_size(p)
        mtime = int(os.path.getmtime(p))
        item = {'name': name, 'rel': 'webui_output/' + name, 'size': s, 'mtime': mtime}
        if name in ref_dirs:
            out_total += s
            out_items.append(item)
        else:
            run_total += s
            run_items.append(item)
    # v1.4.36：成片目录从 keep/不可删 改为 review/可删。
    # 档位语义见函数 docstring：keep=保留不可删 / safe=临时可回收 / review=删除需重新下载。
    # 成片是计算产物（原始视频用户本地还有），删除不可恢复，用 review 语义最贴切。
    groups.append({'key': 'outputs', 'label': '成片（webui_output 下有历史记录的目录，删除不可恢复）',
                   'tier': 'review', 'deletable': True, 'total': out_total, 'items': out_items})
    groups.append({'key': 'run_residual', 'label': '任务残留（无历史引用的孤儿/失败目录与 _warmup_*）',
                   'tier': 'safe', 'deletable': True, 'total': run_total, 'items': run_items})

    up_items, up_total = [], 0
    if os.path.isdir(_WS.UPLOAD_DIR):
        for name in sorted(os.listdir(_WS.UPLOAD_DIR)):
            p = os.path.join(_WS.UPLOAD_DIR, name)
            if os.path.isdir(p):
                s = _storage_dir_size(p)
                up_total += s
                up_items.append({'name': name, 'rel': 'webui_workspace/uploads/' + name,
                                 'size': s, 'mtime': int(os.path.getmtime(p))})
    groups.append({'key': 'uploads', 'label': '上传会话成品（webui_workspace/uploads）',
                   'tier': 'safe', 'deletable': True, 'total': up_total, 'items': up_items})

    def temp_group(pattern, key, label):
        items, total = [], 0
        if os.path.isdir(_WS.WORKDIR):
            for fn in sorted(os.listdir(_WS.WORKDIR)):
                fp = os.path.join(_WS.WORKDIR, fn)
                if _re.match(pattern, fn) and os.path.isfile(fp):
                    s = os.path.getsize(fp)
                    total += s
                    items.append({'name': fn, 'rel': 'webui_workspace/' + fn,
                                  'size': s, 'mtime': int(os.path.getmtime(fp))})
        groups.append({'key': key, 'label': label, 'tier': 'safe',
                       'deletable': True, 'total': total, 'items': items})

    temp_group(r'^asr_[0-9]+\.wav$', 'asr_temp', 'ASR 临时音频（asr_*.wav）')
    temp_group(r'^music_[0-9]+\.(mp3|wav)$', 'music_temp', '音乐临时文件（music_*.mp3/wav）')
    temp_group(r'^up_[0-9A-Za-z_-]+_[a-z]+\.(jpg|png|webp|mp4)$', 'upload_leftover', '上传素材残留（up_*_*）')

    ac = os.path.join(_WS.WORKDIR, 'analysis_cache')
    ac_size = _storage_dir_size(ac) if os.path.isdir(ac) else 0
    groups.append({'key': 'analysis_cache', 'label': '分析缓存（webui_workspace/analysis_cache）',
                   'tier': 'safe', 'deletable': True, 'total': ac_size,
                   'items': [{'name': 'analysis_cache', 'rel': 'webui_workspace/analysis_cache',
                              'size': ac_size, 'mtime': int(os.path.getmtime(ac))}] if ac_size else []})

    models_dir = os.path.join(_WS.HERE, 'models')
    if os.path.isdir(models_dir):
        msize = _storage_dir_size(models_dir)
        groups.append({'key': 'models', 'label': '模型权重（models/，删除需重新下载）',
                       'tier': 'review', 'deletable': True, 'total': msize,
                       'items': [{'name': 'models', 'rel': 'models',
                                  'size': msize, 'mtime': int(os.path.getmtime(models_dir))}]})

    git_dir = os.path.join(_WS.HERE, '.git')
    if os.path.isdir(git_dir):
        gsize = _storage_dir_size(git_dir)
        groups.append({'key': 'git', 'label': '.git 版本历史（不建议删）',
                       'tier': 'keep', 'deletable': False, 'total': gsize, 'items': []})

    # v1.4.36：reclaimable 只统计「一键清理」实际会删的 safe 组。
    # 成片改为 review 档后若仍计入，summary 的「可回收」数字会虚高（一键清理不删成片）。
    reclaimable = sum(g['total'] for g in groups if g['tier'] == 'safe' and g['deletable'])
    total_all = sum(g['total'] for g in groups)
    try:
        free = shutil.disk_usage(_WS.HERE).free
    except Exception:
        free = 0
    return {'ok': True, 'groups': groups, 'total_bytes': total_all,
            'reclaimable_bytes': reclaimable, 'free_bytes': free}


def _purge_history_prefix(prefix):
    """删除 history 中 file 以 prefix/ 开头的记录，返回清除条数。

    v1.4.36：存储面板允许删除成片目录后，history 里指向该目录的记录会变成
    「⚠️ 成片文件已丢失」的悬挂条目。删目录时同步清掉对应记录，保证
    「删缓存 = 删记录」，不留用户还得再去历史页手工删的尾巴。"""
    import webui_server as _WS
    try:
        prefix = (prefix or '').replace('\\', '/').rstrip('/')
        if not prefix:
            return 0
        with _WS._HIST_LOCK:
            items = _WS.load_history(500)
            kept = [it for it in items
                    if not (it.get('file') or '').replace('\\', '/').startswith(prefix + '/')]
            removed = len(items) - len(kept)
            if removed:
                _WS._atomic_write_json(_WS.HISTORY_PATH, kept)
        return removed
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 自动清理（v1.4.68）
#
# 默认**关闭**：磁盘是用户的，不问自删不可接受；清理本身是破坏性操作，宁可让
# 用户手动点。开启后也**只碰「任务残留 + 临时文件」**，永远不碰 outputs
# （有历史记录的成片）——那些删了，历史页会整排变成「⚠️ 文件已丢失」。
# ---------------------------------------------------------------------------
_AUTOCLEAN_GROUPS = ('run_residual', 'asr_temp', 'music_temp',
                     'upload_leftover', 'analysis_cache')


def storage_cfg(cfg=None):
    """读自动清理配置。非法值一律回退默认——宁可不清，也不能清错。"""
    if cfg is None:
        try:
            import webui_server as _WS
            cfg = (_WS.load_ai_config() or {}).get('storage') or {}
        except Exception:
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    def _i(k, d, lo, hi):
        try:
            v = int(cfg.get(k, d))
        except (TypeError, ValueError):
            v = d
        return max(lo, min(hi, v))

    return {'auto_clean': bool(cfg.get('auto_clean', False)),
            'keep_days': _i('keep_days', 14, 1, 3650),
            'keep_max': _i('keep_max', 60, 2, 5000)}


def select_expired(items, now, keep_days=14, keep_max=60):
    """挑出该清理的项，两条规则取并集：

      · 天数规则：改动时间早于 keep_days 天前 → 该删
      · 数量规则：不在「最近 keep_max 个」里，且距今超过 24 小时 → 该删

    【为什么数量规则要配 24 小时硬底线】只按数量删会把「今天一口气跑出来的
    200 个 run」删掉 140 个，而用户可能正等着用其中某个。24 小时内的一律不动，
    既保住了「最多 M 个」这个上限，又不会删到手上正在用的东西。
    最新那一个无论如何都不删。

    items: [{'rel'|'name', 'mtime', 'size'}]，返回同结构的子集。"""
    lst = [it for it in (items or []) if isinstance(it, dict)]
    if not lst:
        return []
    lst.sort(key=lambda x: float(x.get('mtime') or 0), reverse=True)
    keep_n = max(1, int(keep_max or 1))
    in_count = {(it.get('rel') or it.get('name')) for it in lst[:keep_n]}
    age_cut = float(now) - float(keep_days) * 86400.0
    day_floor = float(now) - 86400.0
    out = []
    for it in lst:
        key = it.get('rel') or it.get('name')
        try:
            mt = float(it.get('mtime') or 0)
        except (TypeError, ValueError):
            mt = 0.0
        if mt >= day_floor:
            continue                       # 24 小时内：绝对不动（可能正在跑）
        if mt < age_cut or key not in in_count:
            out.append(it)
    return out


def storage_autoclean(dry_run=False, cfg=None, now=None):
    """按保留策略清理残留/临时文件，返回 {'ok','removed','freed','kept','cfg'}。

    删除前每个路径都过 _storage_resolve_deletable（单段目录名白名单 + 越界
    commonpath 校验），越权路径直接跳过——自动清理比手动点更危险，不能只靠
    「我记得目录是干净的」。
    """
    import time as _t
    c = cfg if isinstance(cfg, dict) else storage_cfg(cfg)
    now = float(now if now is not None else _t.time())
    removed, freed, skipped = [], 0, []
    try:
        scan = _storage_scan()
    except Exception as e:
        return {'ok': False, 'error': '扫描失败：%s' % e, 'removed': [], 'freed': 0, 'cfg': c}
    # 先把允许清理的几组合并成一个池子再选：「最多保留 M 个」是**全局上限**，
    # 否则每个组各留 M 个，总量永远压不下来。
    pool = []
    for g in (scan.get('groups') or []):
        if g.get('key') in _AUTOCLEAN_GROUPS:
            pool.extend([it for it in (g.get('items') or []) if isinstance(it, dict)])
    for it in select_expired(pool, now, c['keep_days'], c['keep_max']):
        rel = it.get('rel') or ''
        full = _storage_resolve_deletable(rel)
        if not full:
            skipped.append(rel)          # 不在白名单/越界：宁可不删
            continue
        size = int(it.get('size') or 0)
        if dry_run:
            removed.append({'rel': rel, 'size': size})
            freed += size
            continue
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            elif os.path.isfile(full):
                os.remove(full)
            else:
                continue
            removed.append({'rel': rel, 'size': size})
            freed += size
        except OSError as e:
            skipped.append('%s(%s)' % (rel, e))
    return {'ok': True, 'dry_run': bool(dry_run), 'removed': removed,
            'freed': freed, 'skipped': skipped, 'cfg': c,
            'groups': list(_AUTOCLEAN_GROUPS)}

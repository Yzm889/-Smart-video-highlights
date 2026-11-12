# -*- coding: utf-8 -*-
"""输出目录保留策略（v1.4.68）。

背景：webui_output 已堆到 5.9GB，只能手动清；但「自动删」比「手动点」危险得多，
所以这里锁的是**边界**，不是「删得干净」：
  1. 默认关闭（不问自删不可接受）
  2. 最近 keep_max 个 + 最新那一个永远不动
  3. 有历史记录的成片目录永远不进自动清理范围
  4. 删除前必须过白名单/越界校验（不在 HERE 之内的路径一律拒绝）
  5. dry_run 只报清单，不落盘
"""
import os
import time

import web_storage as WS


# ---------- 1) 选谁该删 ----------

def _it(name, days_ago, now):
    return {'rel': 'webui_output/' + name, 'name': name,
            'mtime': now - days_ago * 86400, 'size': 100}


def test_select_expired_by_age():
    """超过 keep_days 的一律删——哪怕它是「第二新」的，年龄规则优先。"""
    now = time.time()
    items = [_it('a', 1, now), _it('b', 30, now), _it('c', 60, now), _it('d', 90, now)]
    got = WS.select_expired(items, now, keep_days=14, keep_max=2)
    assert [x['name'] for x in got] == ['b', 'c', 'd']


def test_select_expired_single_ancient_item_is_cleanable():
    now = time.time()
    items = [_it('only', 999, now)]
    assert [x['name'] for x in WS.select_expired(items, now, keep_days=1, keep_max=60)] == ['only']


def test_select_expired_respects_keep_days():
    now = time.time()
    items = [_it('new', 1, now), _it('mid', 5, now), _it('old', 40, now)]
    got = WS.select_expired(items, now, keep_days=14, keep_max=60)
    assert [x['name'] for x in got] == ['old']


def test_select_expired_enforces_count_cap_beyond_a_day():
    """数量上限要真的生效（否则「最多 60 个」形同虚设），但 24 小时内一律不动。"""
    now = time.time()
    items = [_it('n%d' % i, 2, now) for i in range(5)]      # 都超过 24 小时
    got = WS.select_expired(items, now, keep_days=14, keep_max=3)
    assert len(got) == 2, '超出 3 个的部分要删掉：%s' % got


def test_select_expired_never_touches_last_24h():
    now = time.time()
    items = [_it('hot%d' % i, 0.2, now) for i in range(5)]
    assert WS.select_expired(items, now, keep_days=1, keep_max=2) == []


def test_select_expired_empty():
    assert WS.select_expired([], time.time()) == []
    assert WS.select_expired(None, time.time()) == []


# ---------- 2) 配置默认值与钳制 ----------

def test_storage_cfg_defaults_off():
    c = WS.storage_cfg({})
    assert c['auto_clean'] is False, '默认必须关闭：不问自删不可接受'
    assert c['keep_days'] == 14 and c['keep_max'] == 60


def test_storage_cfg_clamps_garbage():
    c = WS.storage_cfg({'auto_clean': 1, 'keep_days': 'abc', 'keep_max': -5})
    assert c['auto_clean'] is True
    assert c['keep_days'] == 14        # 非法回退默认
    assert c['keep_max'] == 2          # 下限钳制


# ---------- 3) 真实文件系统：成片不能被自动删 ----------

def _mk(monkeypatch, tmp_path):
    import webui_server as S
    root = tmp_path
    out = root / 'webui_output'
    ws = root / 'webui_workspace'
    out.mkdir()
    ws.mkdir()
    monkeypatch.setattr(S, 'HERE', str(root))
    monkeypatch.setattr(S, 'OUTDIR', str(out))
    monkeypatch.setattr(S, 'WORKDIR', str(ws))
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(ws / 'uploads'))
    return S, out


def _no_real_delete(monkeypatch):
    """拦掉真实删除：这里要验的是「删哪个」，不是 shutil 本身；而且宿主的
    批量删除守卫会直接把 pytest 进程掐掉，日志全丢。返回被删路径列表。"""
    killed = []

    def _rm(p, *a, **k):
        killed.append(os.path.abspath(str(p)))

    monkeypatch.setattr(WS.shutil, 'rmtree', _rm)
    monkeypatch.setattr(WS.os, 'remove', _rm)
    return killed


def test_autoclean_removes_residual_keeps_films(monkeypatch, tmp_path):
    S, out = _mk(monkeypatch, tmp_path)
    killed = _no_real_delete(monkeypatch)
    old = time.time() - 40 * 86400
    residual = out / 'run-9-old'
    film = out / 'run-8-film'
    for p in (residual, film):
        p.mkdir()
        (p / 'final.mp4').write_bytes(b'x' * 64)
        os.utime(p, (old, old))
    # film 有历史引用（成片），residual 是孤儿残留
    monkeypatch.setattr(S, 'load_history',
                        lambda n=500: [{'file': 'run-8-film/final.mp4'}])

    r = WS.storage_autoclean(cfg={'auto_clean': True, 'keep_days': 7, 'keep_max': 1})
    assert r['ok'] is True
    assert any(p.endswith('run-9-old') for p in killed), '孤儿残留该被清掉：%s' % killed
    assert not any(p.endswith('run-8-film') for p in killed), \
        '有历史记录的成片永远不能被自动删：%s' % killed


def test_autoclean_dry_run_deletes_nothing(monkeypatch, tmp_path):
    S, out = _mk(monkeypatch, tmp_path)
    killed = _no_real_delete(monkeypatch)
    old = time.time() - 40 * 86400
    residual = out / 'run-7-old'
    residual.mkdir()
    os.utime(residual, (old, old))
    monkeypatch.setattr(S, 'load_history', lambda n=500: [])

    r = WS.storage_autoclean(dry_run=True,
                             cfg={'auto_clean': True, 'keep_days': 7, 'keep_max': 1})
    assert r['ok'] and r['dry_run'] is True
    assert any(x['rel'].endswith('run-7-old') for x in r['removed']), '清单里要能看到它'
    assert killed == [], 'dry_run 绝不能真删'


def test_autoclean_refuses_path_outside_root(monkeypatch, tmp_path):
    """越界路径（不在 HERE 之内）必须被拒——自动清理不能只靠目录名看起来正常。"""
    S, out = _mk(monkeypatch, tmp_path)
    monkeypatch.setattr(S, 'load_history', lambda n=500: [])
    # 伪扫描结果：指向系统临时目录之外的东西
    fake = [{'key': 'run_residual', 'deletable': True, 'total': 10,
             'items': [{'rel': '../../etc/passwd', 'name': 'x', 'mtime': 0, 'size': 10}]}]
    monkeypatch.setattr(WS, '_storage_scan', lambda: {'groups': fake})
    r = WS.storage_autoclean(cfg={'auto_clean': True, 'keep_days': 1, 'keep_max': 1})
    assert r['ok'] is True
    assert r['removed'] == [] and r['skipped'], '越界路径必须进 skipped，不能删'

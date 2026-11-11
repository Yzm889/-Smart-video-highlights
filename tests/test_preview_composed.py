# -*- coding: utf-8 -*-
"""v1.4.35：TTS 完成后自动合成预览成片 + 预览区优先展示成片。

钉住三处行为：
1. _ensure_preview_proxy：final.mp4 存在 → mode=composed, file=final.mp4
2. dispatch_movie_compose：用户未调整 + final.mp4 已存在 → 复用，不重跑合成
3. dispatch_movie_compose：用户有调整 → 重跑合成
"""
import json
import os

import pytest

import webui_server as S
import workflows as W


# ──────────────────────────────────────────────────────────────────────
# 1. _ensure_preview_proxy：优先返回 final.mp4
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def in_outdir(tmp_path, monkeypatch):
    """把 OUTDIR 指到 tmp_path 的**父目录**，run_dir 用相对名（过 _safe_dir 校验）。"""
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path.parent))
    return tmp_path


def test_preview_proxy_prefers_final_mp4(in_outdir, monkeypatch):
    """final.mp4 存在且有效 → mode=composed, file=final.mp4，不生成 proxy.mp4。"""
    d = in_outdir
    (d / 'final.mp4').write_bytes(b'\x00' * 2048)
    (d / 'src.mp4').write_bytes(b'\x00' * 100)

    res = S._ensure_preview_proxy(d.name)
    assert res['ok'] is True
    assert res['mode'] == 'composed'
    assert res['file'] == 'final.mp4'
    assert not os.path.exists(str(d / 'proxy.mp4'))


def test_preview_proxy_no_final_falls_back_to_proxy(in_outdir, monkeypatch):
    """无 final.mp4 → 回退到 proxy.mp4 逻辑（>720p 源片转码）。"""
    d = in_outdir
    (d / 'src.mp4').write_bytes(b'\x00' * 100)

    monkeypatch.setattr(S, '_probe_video_height', lambda p: 1080)
    monkeypatch.setattr(S, '_build_preview_proxy', lambda src, dst: (True, ''))

    res = S._ensure_preview_proxy(d.name)
    assert res['ok'] is True
    assert res['mode'] != 'composed'


def test_preview_proxy_invalid_final_skips(in_outdir, monkeypatch):
    """final.mp4 存在但 <1KB（无效）→ 不当作 composed，回退 proxy。"""
    d = in_outdir
    (d / 'final.mp4').write_bytes(b'\x00' * 100)
    (d / 'src.mp4').write_bytes(b'\x00' * 100)

    monkeypatch.setattr(S, '_probe_video_height', lambda p: 1080)
    monkeypatch.setattr(S, '_build_preview_proxy', lambda src, dst: (True, ''))

    res = S._ensure_preview_proxy(d.name)
    assert res['mode'] != 'composed'


# ──────────────────────────────────────────────────────────────────────
# 2 & 3. dispatch_movie_compose：复用 vs 重跑
# ──────────────────────────────────────────────────────────────────────

def _prepare_compose_env(tmp_path, monkeypatch):
    """构造 dispatch_movie_compose 的测试环境。"""
    outdir = tmp_path / 'webui_output'
    outdir.mkdir()
    run = outdir / 'run-1-20260101-000000'
    run.mkdir()
    state = {
        'video_path': str(run / 'src.mp4'),
        'segs': [[0, 2]],
        'narr': ['a'],
        'narr_map': [0],
        'params': {'autoCut': True},
    }
    (run / 'src.mp4').write_bytes(b'\x00' * 100)
    (run / 'tts_state.json').write_text(json.dumps(state), encoding='utf-8')

    monkeypatch.setattr(S, 'OUTDIR', str(outdir))
    monkeypatch.setattr(S, '_resolve_music', lambda m: None)
    monkeypatch.setattr(S, '_record_history', lambda *a, **k: None)

    calls = {'compose': 0}

    def fake_compose(run_dir, prog=None, **kw):
        calls['compose'] += 1
        fp = os.path.join(run_dir, 'final.mp4')
        with open(fp, 'wb') as f:
            f.write(b'\x00' * 2048)
        return fp

    monkeypatch.setattr(S, 'compose_movie_from_tts', fake_compose)
    return {'outdir': outdir, 'run': run, 'calls': calls}


def test_compose_reuses_existing_final_when_no_adjustment(tmp_path, monkeypatch):
    """用户未调整 + final.mp4 已存在 → 复用，不调用 compose_movie_from_tts。"""
    env = _prepare_compose_env(tmp_path, monkeypatch)
    # 先放一个 final.mp4（模拟 TTS 阶段自动合成的产物）
    fp = env['run'] / 'final.mp4'
    fp.write_bytes(b'\x00' * 2048)

    prog = {'run_dir': 'run-1-20260101-000000'}
    W.dispatch_movie_compose({'run_dir': 'run-1-20260101-000000'}, prog)

    assert env['calls']['compose'] == 0, '未调整时不应重跑合成'
    assert prog.get('done') is True
    assert 'final.mp4' in prog.get('file', '')


def test_compose_reruns_when_user_adjusted(tmp_path, monkeypatch):
    """用户有调整（items 非空）→ 重跑合成。"""
    env = _prepare_compose_env(tmp_path, monkeypatch)
    fp = env['run'] / 'final.mp4'
    fp.write_bytes(b'\x00' * 2048)

    prog = {'run_dir': 'run-1-20260101-000000'}
    W.dispatch_movie_compose({
        'run_dir': 'run-1-20260101-000000',
        'items': [{'index': 0, 'text': '改后的解说', 'audio': '/media/run1/narr0.wav'}],
    }, prog)

    assert env['calls']['compose'] == 1, '有调整时应重跑合成'

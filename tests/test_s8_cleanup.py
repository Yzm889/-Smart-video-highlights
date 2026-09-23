# -*- coding: utf-8 -*-
"""S8 磁盘清理策略：_warmup_* 预热目录纳入清理体系。

锁定：
  1. _warmup_worker 成功收尾即删自己的 _warmup_<fp> run_dir（产品已入 analysis_cache）
  2. ASR 失败 / VLM 前取消 / 无台词跳过——同样清理（异常安全）
  3. sweep_run_artifacts 兜底清理孤儿 _warmup_*（超 cleanup_src_days）
  4. 过期阈值内的 _warmup_* 不动
  5. _storage_scan 把 _warmup_* 归入 run_residual（safe 可删）而非 outputs（keep）
  6. 存储面板白名单放行 webui_output/_warmup_<fp>，非法名拒绝
"""
import os
import time
import webui_server as S  # noqa: F401  先建立 webui 入口，再导入 workflows（避免循环依赖）
_S_ENTRY = S  # 显式引用：仅依赖 import webui_server 的副作用保证模块加载顺序
import workflows as W

_FP = 'a' * 32


class TestWarmupWorkerCleanup:
    def _mk_video(self, tmp_path):
        vp = tmp_path / 'v.mp4'
        vp.write_bytes(b'x' * 4096)
        return str(vp)

    def _patch_warmup_env(self, monkeypatch, vlm_ok=True, asr=None, asr_raise=None,
                          cancelled=False, vlm_ping_ok=True):
        monkeypatch.setattr(W, 'probe_audio_len', lambda p: 100.0)
        if asr_raise:
            def _boom(p):
                raise RuntimeError('asr down')
            monkeypatch.setattr(W, 'asr_segments', _boom)
        else:
            monkeypatch.setattr(W, 'asr_segments', lambda p: asr)
        monkeypatch.setattr(W, '_cache_save', lambda k, v: None)
        monkeypatch.setattr(W, '_warmup_cancelled', lambda: cancelled)
        monkeypatch.setattr(W._w, 'vlm_enabled', lambda: vlm_ok)
        if vlm_ok:
            monkeypatch.setattr(W._w, 'vlm_ping', lambda: (vlm_ping_ok, 'ok'))
        monkeypatch.setattr(W._w, '_vlm_sample_timeline', lambda *a, **k: None)

    def test_cleans_rundir_on_success(self, monkeypatch, tmp_path):
        """成功路径：预热完成即删 _warmup_* run_dir。"""
        self._patch_warmup_env(monkeypatch, asr=[{'start': 0.0, 'end': 5.0, 'text': 'hi'}])
        W._warmup_worker(self._mk_video(tmp_path), _FP)
        assert not os.path.exists(os.path.join(W._w.OUTDIR, '_warmup_' + _FP))

    def test_cleans_rundir_on_asr_failure(self, monkeypatch, tmp_path):
        """ASR 失败：except 后 finally 仍清理。"""
        self._patch_warmup_env(monkeypatch, asr_raise=True)
        W._warmup_worker(self._mk_video(tmp_path), _FP)
        assert not os.path.exists(os.path.join(W._w.OUTDIR, '_warmup_' + _FP))

    def test_cleans_rundir_when_cancelled(self, monkeypatch, tmp_path):
        """VLM 前收到取消：提前返回，finally 仍清理。"""
        self._patch_warmup_env(monkeypatch, asr=[{'start': 0.0, 'end': 5.0, 'text': 'hi'}],
                               cancelled=True)
        W._warmup_worker(self._mk_video(tmp_path), _FP)
        assert not os.path.exists(os.path.join(W._w.OUTDIR, '_warmup_' + _FP))

    def test_noop_without_rundir(self, monkeypatch, tmp_path):
        """无台词跳过 VLM：run_dir 未创建也不报错。"""
        self._patch_warmup_env(monkeypatch, asr=[])
        W._warmup_worker(self._mk_video(tmp_path), _FP)
        assert not os.path.exists(os.path.join(W._w.OUTDIR, '_warmup_' + _FP))


class TestSweepWarmup:
    def _mk_old_dir(self, name, days_old=4.0):
        d = os.path.join(S.OUTDIR, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'vlm_progress.json'), 'w', encoding='utf-8') as f:
            f.write('{}')
        old = time.time() - days_old * 86400
        os.utime(d, (old, old))
        os.utime(os.path.join(d, 'vlm_progress.json'), (old, old))
        return d

    def test_sweep_removes_orphan_warmup(self, monkeypatch, tmp_path):
        """孤儿 _warmup_*（超清理阈值、无历史引用、无运行中任务）被整目录回收。"""
        os.makedirs(S.OUTDIR, exist_ok=True)
        d = self._mk_old_dir('_warmup_' + _FP)
        freed = S.sweep_run_artifacts()
        assert freed > 0
        assert not os.path.exists(d)

    def test_sweep_keeps_recent_warmup(self, tmp_path):
        """阈值内（<3 天）的 _warmup_* 绝不动。"""
        os.makedirs(S.OUTDIR, exist_ok=True)
        d = self._mk_old_dir('_warmup_' + _FP, days_old=1.0)
        S.sweep_run_artifacts()
        assert os.path.isdir(d)

    def test_sweep_ignores_non_hex_name(self, tmp_path):
        """非 32 位 hex 的 _warmup_ 前缀目录不属于白名单形态，sweep 仍按孤儿回收（前缀即判定）。"""
        os.makedirs(S.OUTDIR, exist_ok=True)
        d = self._mk_old_dir('_warmup_abc')
        S.sweep_run_artifacts()
        assert not os.path.exists(d)


class TestStoragePanel:
    def test_scan_classifies_warmup_as_residual(self, tmp_path):
        """_warmup_* 归入 run_residual（safe 可删），不再混入 outputs（keep）。"""
        os.makedirs(S.OUTDIR, exist_ok=True)
        os.makedirs(os.path.join(S.OUTDIR, '_warmup_' + _FP), exist_ok=True)
        os.makedirs(os.path.join(S.OUTDIR, 'run-abc'), exist_ok=True)
        os.makedirs(os.path.join(S.OUTDIR, '20260923-120000'), exist_ok=True)
        with open(os.path.join(S.OUTDIR, '_warmup_' + _FP, 'x.txt'), 'w') as f:
            f.write('data')
        sc = S._storage_scan()
        by_key = {g['key']: g for g in sc['groups']}
        residual_names = [i['name'] for i in by_key['run_residual']['items']]
        output_names = [i['name'] for i in by_key['outputs']['items']]
        assert '_warmup_' + _FP in residual_names
        assert '_warmup_' + _FP not in output_names
        assert 'run-abc' in residual_names
        assert '20260923-120000' in output_names

    def test_storage_allow_matches_warmup(self, tmp_path):
        """存储面板白名单放行 webui_output/_warmup_<32hex>，非法形态拒绝。"""
        ok = S._storage_resolve_deletable('webui_output/_warmup_' + _FP)
        assert ok and ok.lower().endswith('_warmup_' + _FP)
        assert S._storage_resolve_deletable('webui_output/_warmup_abc') is None
        assert S._storage_resolve_deletable('webui_output/_warmup_' + _FP + '/../run-x') is None

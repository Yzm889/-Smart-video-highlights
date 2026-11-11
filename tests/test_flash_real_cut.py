# -*- coding: utf-8 -*-
"""T0' 闪窗证据落地后的两处修复回归。

证据：.dsh_tools/flash_report.json（系统解释器重跑，baseline_procs=264，
console_flashes=92）。按父进程归类：Ollama 43 / edge-tts 14 / ffmpeg 9 /
nvidia-smi 2 / 其余噪声。本轮只修 FrameCut 自己可控、零回归风险的部分。
"""
import io
import os

import webui_server as S  # noqa: F401 必须先导入宿主，规避 video_render<->webui_server 循环导入
import movie_narrator as M
import ffmpeg_utils as F
import tts_engines as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    return io.open(os.path.join(ROOT, name), encoding='utf-8').read()


# --- T2：成品没剪辑 ---------------------------------------------------------
def test_force_real_cut_when_coverage_is_low():
    """主人实测：removed=0 但 coverage=17% —— 旧判据放行 no_cut，必须改为真剪。"""
    assert M._should_force_real_cut({'removed': 0, 'coverage': 17.0}) is True
    assert M._should_force_real_cut({'removed': 0, 'coverage': 79.9}) is True


def test_force_real_cut_when_segments_were_removed():
    assert M._should_force_real_cut({'removed': 3, 'coverage': 100.0}) is True


def test_no_force_when_full_coverage_and_nothing_removed():
    """全片模式必须保持原行为（no_cut 返回原片），不能误伤。"""
    assert M._should_force_real_cut({'removed': 0, 'coverage': 100.0}) is False
    assert M._should_force_real_cut({}) is False
    assert M._should_force_real_cut(None) is False


def test_force_real_cut_survives_dirty_values():
    """脏字段不许把合成阶段打挂。"""
    assert M._should_force_real_cut({'removed': 'abc', 'coverage': 'n/a'}) is False
    assert M._should_force_real_cut({'removed': None, 'coverage': 17}) is True


def test_compose_uses_the_new_predicate():
    src = _src('movie_narrator.py')
    assert 'skip_no_cut=_should_force_real_cut(_cp3)' in src
    assert 'skip_no_cut=bool(int(_cp3.get' not in src, '旧判据还在，修复被回退了'


# --- T1：闪窗 ---------------------------------------------------------------
def test_hidden_startupinfo_hides_console_on_windows():
    si = F._hidden_startupinfo()
    if os.name == 'nt':
        assert si is not None
        assert si.dwFlags & F.subprocess.STARTF_USESHOWWINDOW
        assert si.wShowWindow == F.subprocess.SW_HIDE
    else:
        assert si is None


def test_ffmpeg_run_passes_both_no_window_and_hidden_startupinfo():
    src = _src('ffmpeg_utils.py')
    i = src.index('proc = subprocess.Popen(')
    chunk = src[i:i + 400]
    assert 'creationflags=_NO_WIN' in chunk
    assert 'startupinfo=_hidden_startupinfo()' in chunk


def test_edge_tts_probe_has_long_ttl_and_both_checks_use_it():
    """12 分钟内 edge-tts --version 被重探 14 次；TTL 必须够长且两处都用同一常量。"""
    assert T._EDGE_INSTALLED_TTL >= 300
    src = _src('tts_engines.py')
    assert src.count('_EDGE_INSTALLED_TTL') >= 3, '只改了一处检查，另一个 30s 窗口仍会重探'
    assert "(now - _EDGE_INSTALLED_CACHE['ts']) < 30" not in src

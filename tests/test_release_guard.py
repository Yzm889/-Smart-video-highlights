# -*- coding: utf-8 -*-
"""发版护栏：钉住 v1.4.82 修好的两道门禁，防止后来人删回去。

背景见 AI_CHANGELOG v1.4.82：v1.4.81 的 CI 包混入了 UCRT 转发器导致窗口层
CLR 崩溃，而当时的门禁只轮询 8765（服务线程先监听）→ 假阳性放行。
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YML = os.path.join(ROOT, '.github', 'workflows', 'release.yml')


def _yml():
    with open(YML, encoding='utf-8') as f:
        return f.read()


def test_removes_ucrt_forwarders_before_zip():
    """打包后必须移除 CI 混入的转发器，且要早于打 zip。"""
    s = _yml()
    assert 'api-ms-win-*.dll' in s, '缺少 UCRT 转发器清理步骤'
    assert 'ucrtbase.dll' in s, '缺少 ucrtbase.dll 清理'
    assert s.index('移除 CI 混入的 UCRT 转发器') < s.index('打 zip'), \
        '清理必须发生在压缩之前，否则等于没修'


def test_window_gate_checks_clr_crash_not_just_port():
    """门禁必须同时验「失败信号不出现」，并留出窗口层崩溃观察窗。"""
    s = _yml()
    assert '8765' in s, '端口门禁不能丢'
    assert 'Python\\.Runtime\\.Loader' in s, '必须检查 CLR 崩溃日志'
    assert 'Failed to execute script' in s, '必须检查未处理异常'
    assert 'Start-Sleep -Seconds 6' in s, '必须留出窗口层初始化观察窗'


def test_window_gate_runs_from_short_path():
    """长路径会让 CLR 加载失败（环境特异），门禁必须复制到短路径再验。"""
    s = _yml()
    assert 'C:\\FC' in s, '门禁必须在短路径下启动 EXE'
    assert 'Copy-Item' in s, '必须复制打包产物到短路径'

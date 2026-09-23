import os
import sys

import pytest

# 让 pytest 能直接 import 项目根的 webui_server 模块
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _isolate_project_state(tmp_path):
    """测试隔离：AI 配置 / 生成历史 / 输出目录 全部指向 tmp_path，避免测试
    受本机 ai_config.json（真实 key / 开关）影响，也杜绝测试往真实 history.json
    与 webui_output/ 写入垃圾条目（_render_plan_job 等现在会写历史，必须隔离）。"""
    import webui_server as S
    import ai_providers
    import tts_engines as T
    p = tmp_path / 'ai_config.json'
    p.write_text('{}', encoding='utf-8')
    old_cfg, old_hist, old_out = S.AI_CONFIG_PATH, S.HISTORY_PATH, S.OUTDIR
    S.AI_CONFIG_PATH = str(p)
    # [3.2] ai_providers 拆出后持有独立的 AI_CONFIG_PATH 模块级常量；
    # 测试统一以 webui 入口为事实来源，这里同步 patch 保证 load_ai_config 系读到临时配置。
    old_ai_cfg = ai_providers.AI_CONFIG_PATH
    ai_providers.AI_CONFIG_PATH = str(p)
    ai_providers._AI_CFG_CACHE = {'data': None, 'mtime': 0}
    S.HISTORY_PATH = str(tmp_path / 'history.json')
    S.OUTDIR = str(tmp_path / 'webui_output')
    # [CI] ai_status() 有 5s 结果缓存、_edge_tts_installed() 有 30s 探测缓存——
    # 若前序测试真实调用过（如 ai_status() 里探测 edge），mock 会被缓存命中吞掉。
    # 本机慢跑时窗口恰好错过（看似全绿），CI 跑得快则必现。这里统一清缓存，保证
    # 每个测试都在无污染状态下评估 mock。
    old_status = S._ai_status_cache
    S._ai_status_cache = {'data': None, 'time': 0}
    old_inst = T._EDGE_INSTALLED_CACHE
    T._EDGE_INSTALLED_CACHE = {'val': None, 'ts': 0}
    old_edge = T._EDGE_STATE
    T._EDGE_STATE = {'fails': 0, 'dead_until': 0.0, 'reason': ''}
    # 编码器探测缓存同样隔离：本机有 GPU 走 nvenc、CI 无 GPU 走 libx264，
    # 若被前序测试的探测结果污染，编码路径会随执行顺序漂移。
    old_enc = S._ENC_CACHE
    S._ENC_CACHE = {'probe': None}
    try:
        yield
    finally:
        S.AI_CONFIG_PATH = old_cfg
        ai_providers.AI_CONFIG_PATH = old_ai_cfg
        ai_providers._AI_CFG_CACHE = {'data': None, 'mtime': 0}
        S.HISTORY_PATH = old_hist
        S.OUTDIR = old_out
        S._ai_status_cache = old_status
        T._EDGE_INSTALLED_CACHE = old_inst
        T._EDGE_STATE = old_edge
        S._ENC_CACHE = old_enc

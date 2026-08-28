import os
import sys

import pytest

# 让 pytest 能直接 import 项目根的 webui_server 模块
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _isolate_ai_config(tmp_path):
    """测试隔离：把 AI 配置指向空文件，避免测试结果受本机 ai_config.json
    （真实 key / vlm 开关 / whisper 模型等）影响，保证测试可复现。"""
    import webui_server as S
    p = tmp_path / 'ai_config.json'
    p.write_text('{}', encoding='utf-8')
    old = S.AI_CONFIG_PATH
    S.AI_CONFIG_PATH = str(p)
    try:
        yield
    finally:
        S.AI_CONFIG_PATH = old

# -*- coding: utf-8 -*-
"""看图模型「选了又没了」+ 兜底旧名 —— 2026-11-10 真机问题③的回归钉。

主人真机现象（DIAG_5_ISSUES_2026-11-10.md）：
  设置页里从下拉选了已装的 qwen3-vl:8b，看起来生效了；过一会儿再看，
  配置里 vlm.model 又没了，探活报「未拉取 qwen2.5vl:latest」，反复修不好。
  日志原文（16:36:57）：
    [A1] VLM 探活失败（Ollama 可达，但未拉取 qwen2.5vl:latest（请执行 ollama
    pull qwen2.5vl:latest）），画面索引为空；解说配画面将退化为纯位置均匀兜底。

两条代码级原因（都已实测复现，不是用户操作问题）：

  1. 【模型名被删】desktop.js 打开设置页时用 `c.vision.model` 填 `setVisModel`，
     而运行时真正生效的是 `c.vlm.model`。主人真机 vision.model="" →
     输入框空白 → 保存时 `const _vm = setVisModel.value` 取到空 →
     payload.vlm 不带 model 键 → handler.py 的
     `for k in (...) : if inc.get(k) is not None: ... elif k in cur: del cur[k]`
     把这个键**主动删掉**。于是「选好了，下次一保存就没了」。

  2. 【兜底停在旧代名字】ai_providers.vlm_cfg() 的兜底是 'qwen2.5vl:latest'，
     而模型中心/一键配置用的是 qwen3-vl:8b。配置缺 model 时探活查旧名 →
     永远查不到；点「拉取」拉的却是新名 → 永远补不上旧名。

本文件同时钉住「保存不再误删」与「兜底紧跟当前默认」两件事。
"""
import io
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_providers as A  # noqa: E402
import handler as H  # noqa: E402


# ---------------------------------------------------------------------------
# 一、兜底值：缺 model 时必须给「当前默认」，不是 2024 代旧名
# ---------------------------------------------------------------------------
def test_fallback_model_matches_current_default(monkeypatch):
    """配置里没有 vlm.model 时，兜底必须是当前默认（qwen3-vl:8b）。"""
    monkeypatch.setattr(A, 'load_ai_config', lambda: {
        'vlm': {'enabled': True, 'mode': 'ollama', 'base_url': 'http://localhost:11434'}})
    c = A.vlm_cfg()
    assert c['model'] == 'qwen3-vl:8b', (
        'vlm_cfg 的兜底必须与模型中心/一键配置的当前默认一致；'
        '停在 qwen2.5vl:latest 会让探活永远查一个拉不到的名字')


def test_fallback_is_not_the_retired_model_name(monkeypatch):
    """显式钉住「旧名不许再当兜底」——否则这个坑会随重构回来。"""
    monkeypatch.setattr(A, 'load_ai_config', lambda: {})
    assert A.vlm_cfg()['model'] != 'qwen2.5vl:latest'


def test_explicit_model_still_wins(monkeypatch):
    """有显式 model 时当然听配置的（兜底只在缺字段时生效）。"""
    monkeypatch.setattr(A, 'load_ai_config', lambda: {
        'vlm': {'enabled': True, 'base_url': 'http://localhost:11434',
                'model': 'minicpm-v4.5:latest'}})
    assert A.vlm_cfg()['model'] == 'minicpm-v4.5:latest'


def test_example_config_uses_current_default():
    """示例配置（用户照着填的那份）同样不能停在旧名。"""
    p = os.path.join(ROOT, 'ai_config.example.json')
    cfg = json.load(io.open(p, encoding='utf-8'))
    assert cfg['vlm']['model'] != 'qwen2.5vl:latest', (
        'ai_config.example.json 是用户复制的模板，写旧名等于把坑发出去')
    assert cfg['vlm']['model'] == 'qwen3-vl:8b'


# ---------------------------------------------------------------------------
# 二、保存不再误删 vlm.model
# ---------------------------------------------------------------------------
class _Hdrs(object):
    def __init__(self, n):
        self._n = n

    def get(self, k, d=None):
        return str(self._n) if k.lower() == 'content-length' else d


class _RFile(object):
    def __init__(self, b):
        self._b = b

    def read(self, n=None):
        return self._b


class _Resp(object):
    """给 Handler._post_ai_config 提供真实形状的 self.headers / self.rfile。"""

    def __init__(self, payload):
        raw = json.dumps(payload).encode('utf-8')
        self.headers = _Hdrs(len(raw))
        self.rfile = _RFile(raw)
        self.sent = None

    def _send(self, code, content, ctype='text/plain; charset=utf-8', extra=None):
        self.sent = (code, content)

    def _send_err(self, *a, **k):
        self.sent = ('ERR', a, k)


@pytest.fixture
def isolated_cfg(monkeypatch, tmp_path):
    """把配置读写都指到 tmp_path。

    ⚠️ 必须同时 patch ai_providers 与 webui_server 两个名字：
    handler 走的是 `_w.save_ai_config()` → webui_server.AI_CONFIG_PATH，
    只补 ai_providers 那份的话写盘会落到真实 ai_config.json（踩过）。
    """
    import webui_server as S
    fake = str(tmp_path / 'ai_config.json')
    monkeypatch.setattr(A, 'AI_CONFIG_PATH', fake)
    monkeypatch.setattr(S, 'AI_CONFIG_PATH', fake)
    monkeypatch.setattr(A, '_AI_CFG_CACHE', {'data': None, 'mtime': 0})
    monkeypatch.setattr(S, '_AI_CFG_CACHE', {'data': None, 'mtime': 0}, raising=False)
    return fake


def _seed(path, vlm):
    io.open(path, 'w', encoding='utf-8').write(
        json.dumps({'vlm': vlm, 'tts_local': {}, 'local': {}}, ensure_ascii=False))
    A._AI_CFG_CACHE.update({'data': None, 'mtime': 0})


def _post(payload):
    r = _Resp(payload)
    H.Handler._post_ai_config(r)
    return r


def test_saving_settings_keeps_the_chosen_vlm_model(isolated_cfg):
    """★核心回归★ 下拉选好的 model，不能被随后一次「保存设置」删掉。

    复刻 desktop.js:1783-1810 在「用户没动过 setVisModel」时送出的 payload：
    早期版本因 vision.model 为空而带上 `model` 键缺失，后端据此把 vlm.model 删了。
    修复方向在前端（回显实际生效的模型名），后端语义保持「显式传 None 才删」不变。
    """
    _seed(isolated_cfg, {'enabled': True, 'mode': 'ollama',
                         'base_url': 'http://localhost:11434',
                         'model': 'qwen3-vl:8b'})
    assert A.vlm_cfg()['model'] == 'qwen3-vl:8b'

    # 前端修复后：回显拿到实际模型名，保存时 payload 一定带 model
    _post({'vision': {'base_url': '', 'model': 'qwen3-vl:8b'},
           'tts': {}, 'local': {}, 'tts_local': {},
           'vlm': {'enabled': True, 'mode': 'ollama',
                   'base_url': 'http://localhost:11434', 'model': 'qwen3-vl:8b'}})

    A._AI_CFG_CACHE.update({'data': None, 'mtime': 0})
    after = json.load(io.open(isolated_cfg, encoding='utf-8'))['vlm']
    assert after.get('model') == 'qwen3-vl:8b', (
        '保存设置把用户选好的看图模型删掉了 —— 这正是真机「选了又没了」')
    assert A.vlm_cfg()['model'] == 'qwen3-vl:8b'


def test_settings_echo_falls_back_to_the_effective_vlm_model():
    """前端回显：vision.model 为空时必须回退读 vlm.model，否则输入框空白 → 保存丢键。"""
    js = io.open(os.path.join(ROOT, 'static', 'desktop', 'desktop.js'),
                 encoding='utf-8').read()
    assert "(c.vision || {}).model || (c.vlm || {}).model" in js, (
        'setVisModel 的回显必须回退到 vlm.model（运行时真正生效的那个），'
        '否则输入框空白 → 保存时 payload 不带 model → 后端删键')


def test_explicit_null_still_deletes_the_key(isolated_cfg):
    """后端原有语义要保住：显式传 null 才表示「清掉这个键」。"""
    _seed(isolated_cfg, {'enabled': True, 'mode': 'ollama',
                         'base_url': 'http://localhost:11434', 'model': 'qwen3-vl:8b'})
    _post({'vlm': {'enabled': True, 'mode': 'ollama',
                   'base_url': 'http://localhost:11434', 'model': None}})
    A._AI_CFG_CACHE.update({'data': None, 'mtime': 0})
    after = json.load(io.open(isolated_cfg, encoding='utf-8'))['vlm']
    assert 'model' not in after, '显式 null 仍应删除该键（局部更新语义）'

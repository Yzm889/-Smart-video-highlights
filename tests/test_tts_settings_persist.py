# -*- coding: utf-8 -*-
"""配音配置的持久化回归（/api/ai/config 的 tts_local 白名单 + instruct 通路）。

为什么单独钉这个：
同一处坑**踩了两次**（见 handler.py _post_ai_config 里两条日期注释）——
① 2026-11-07：白名单漏了 cosyvoice/chattts、也不存 cosy_voice，界面上永远切不到克隆音色；
② 2026-11-10：白名单只认旧引擎，而设置页下拉是 auto/qwen3tts/indextts2 →
   选 Qwen3-TTS / IndexTTS2 存不进去，刷新后弹回 auto；同时 instruct（音色描述）
   后端整条链都读（tts_engines.py:1099 造音色 / :1111 铸参考音、webui_server 试听），
   却从来没被保存过 → 用户自定义音色描述永远等于 DEFAULT_INSTRUCT。

两次都是「前端能选、后端静默丢弃」，前端又把旧值读回下拉，用户看不出被丢。
仅靠人工核对容易再漏，所以在这里用真实 Handler 把契约钉死：
前端下拉给的每个值都必须能存下来、并原样读回。
"""
import json

import pytest

import handler as H
import webui_server as S


def _make_handler(monkeypatch, body):
    """造一个只走 _post_ai_config 的 Handler 桩（不开 socket）。"""
    h = H.Handler.__new__(H.Handler)
    sent = {}

    class _RFile:
        def read(self, _n):
            return json.dumps(body).encode('utf-8')

    h.rfile = _RFile()
    h.headers = {'Content-Length': str(len(json.dumps(body).encode('utf-8')))}
    monkeypatch.setattr(h, '_send',
                        lambda code, b, ctype=None: sent.update(
                            code=code, body=b.decode('utf-8') if isinstance(b, bytes) else b))
    # _post_ai_config 结尾会探测环境，测试里必须短路（否则真跑网络/GPU 探测）
    monkeypatch.setattr(S, '_vision_available', lambda: False)
    monkeypatch.setattr(S, '_tts_available', lambda: False)
    monkeypatch.setattr(S, 'video_encoder_label', lambda: 'cpu')
    return h, sent


@pytest.fixture
def saved(monkeypatch):
    """把 ai_config 读写换成内存字典，返回 (初始 cfg, 保存后的 cfg)。"""
    store = {}

    def _load():
        return json.loads(json.dumps(store)) if store else {}

    def _save(cfg):
        store.clear()
        store.update(json.loads(json.dumps(cfg)))

    monkeypatch.setattr(S, 'load_ai_config', _load)
    monkeypatch.setattr(S, 'save_ai_config', _save)
    return store


# 与前端 static/desktop/desktop.js 的 TTS_ENGINES 表逐字对应。
# 这里刻意写成字面量而非 import：一旦前端新增引擎却忘了加白名单，
# 本用例会红，而不是跟着前端一起漂移。
FRONTEND_ENGINES = ['auto', 'qwen3tts', 'indextts2']


@pytest.mark.parametrize('engine', FRONTEND_ENGINES)
def test_frontend_engine_values_are_persisted(monkeypatch, saved, engine):
    """设置页下拉能选的每个引擎都必须能存下来（前端与后端白名单不许再漂移）。"""
    h, _sent = _make_handler(monkeypatch, {'tts_local': {'engine': engine}})
    h._post_ai_config()
    assert saved['tts_local']['engine'] == engine, (
        '前端下拉有 %r 但 /api/ai/config 没存下来：用户选了会被静默丢弃、刷新后弹回旧值'
        % engine)


def test_legacy_engines_still_accepted(monkeypatch, saved):
    """旧引擎显式选用仍要可用（代码保留作回滚，别把回滚路堵死）。"""
    for eng in ('edge', 'sherpa', 'sapi', 'cosyvoice', 'chattts'):
        h, _sent = _make_handler(monkeypatch, {'tts_local': {'engine': eng}})
        h._post_ai_config()
        assert saved['tts_local']['engine'] == eng, eng


def test_unknown_engine_is_rejected(monkeypatch, saved):
    """不在白名单里的值不许写进去（保持白名单语义，不是「什么都收」）。"""
    saved['tts_local'] = {'engine': 'auto'}
    h, _sent = _make_handler(monkeypatch, {'tts_local': {'engine': 'not-a-real-engine'}})
    h._post_ai_config()
    assert saved['tts_local']['engine'] == 'auto', '未知引擎不该覆盖已有值'


def test_instruct_round_trips(monkeypatch, saved):
    """音色描述必须能存能读——这是 v1.4.17 加的字段，前端当时无入口。"""
    text = '低沉磁性的中年男声，语速偏慢，有纪录片质感'
    h, _sent = _make_handler(monkeypatch, {'tts_local': {'instruct': text}})
    h._post_ai_config()
    assert saved['tts_local']['instruct'] == text


def test_instruct_can_be_cleared(monkeypatch, saved):
    """空串要能存下来（= 用默认音色）。

    不能用「空值不写」的写法：那样用户清空输入框后，旧描述会永远留在配置里，
    表现为「改了没生效」。这是 voice/cosy_voice 的老逻辑，instruct 必须不同。
    """
    saved['tts_local'] = {'instruct': '旧描述'}
    h, _sent = _make_handler(monkeypatch, {'tts_local': {'engine': 'auto', 'instruct': ''}})
    h._post_ai_config()
    assert saved['tts_local']['instruct'] == '', (
        '清空音色描述后旧值仍残留：用户会认为「改了没生效」')


def test_instruct_is_length_capped(monkeypatch, saved):
    """超长描述要被截断（前端 maxlength=200），避免整段文本进 VoiceDesign。"""
    h, _sent = _make_handler(monkeypatch, {'tts_local': {'instruct': 'x' * 500}})
    h._post_ai_config()
    assert len(saved['tts_local']['instruct']) == 200


def test_instruct_absent_does_not_wipe_existing(monkeypatch, saved):
    """请求里没带 instruct 字段时不许清空已有描述。

    关键区分：`'instruct' not in inc`（字段缺席 = 别的调用方在改其它配置，别动它）
    与 `instruct == ''`（用户明确清空）语义不同，混了就会随机丢配置。
    """
    saved['tts_local'] = {'instruct': '已有的描述'}
    h, _sent = _make_handler(monkeypatch, {'tts_local': {'engine': 'indextts2'}})
    h._post_ai_config()
    assert saved['tts_local']['instruct'] == '已有的描述', '未提及 instruct 时不该清空'
    assert saved['tts_local']['engine'] == 'indextts2'


def test_other_tts_fields_survive_a_partial_update(monkeypatch, saved):
    """只改 instruct 时，engine/voice/rate 必须原样保留（局部更新不许整段覆盖）。"""
    saved['tts_local'] = {'engine': 'indextts2', 'voice': 'zh-CN-XiaoxiaoNeural',
                          'cosy_voice': '中文女', 'rate': '+10%'}
    h, _sent = _make_handler(monkeypatch, {'tts_local': {'instruct': '新描述'}})
    h._post_ai_config()
    got = saved['tts_local']
    assert got['engine'] == 'indextts2' and got['voice'] == 'zh-CN-XiaoxiaoNeural'
    assert got['cosy_voice'] == '中文女' and got['rate'] == '+10%'
    assert got['instruct'] == '新描述'


def test_handler_calls_save(monkeypatch, saved):
    """端到端信号：端点必须真的落盘（曾经出现「回 ok:true 但没存」的静默失败）。"""
    h, sent = _make_handler(monkeypatch, {'tts_local': {'engine': 'qwen3tts'}})
    h._post_ai_config()
    body = json.loads(sent['body'])
    assert body.get('ok') is True, body
    assert saved.get('tts_local', {}).get('engine') == 'qwen3tts'

# -*- coding: utf-8 -*-
"""v1.4.17 回归钉：试听不许静默降级、不许无限期「合成中」。

事故：点「🧪 试听」后按钮永远停在「⏳ 合成中…」。
三层根因：试听走完整兜底链（Qwen3 失败会静默切到首次加载 202s 的 IndexTTS2）、
后端同步 HTTP 长任务无阶段输出、前端 fetch 没有超时/取消/finally。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as S  # noqa: E402
import tts2_engines as T2  # noqa: E402

JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')


def _fake_wav(path):
    with open(path, 'wb') as f:
        f.write(b'RIFF' + b'0' * 2048)
    return True


def test_preview_never_falls_back_to_other_engine(monkeypatch, tmp_path):
    """IndexTTS2 失败时试听必须明确报错，不许偷偷换成 Qwen3 逐句造音色。

    v1.4.25 起 auto 的默认引擎是 IndexTTS2（克隆），所以「不降级」的对象
    也跟着变了；这条钉的仍是同一件事：试听只听一个引擎，失败就说清楚。
    """
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(S, 'tts_local_cfg',
                        lambda: {'engine': 'auto', 'instruct': ''})
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'voice_ref', lambda instruct=None: None)
    monkeypatch.setattr(T2, 'indextts2_speak', lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(T2, 'qwen3tts_speak',
                        lambda *a, **k: calls.append(1) or True)
    ok, msg, eng, rel = S.tts_test_speak('试听一句。')
    assert ok is False and eng is None and rel == ''
    assert not calls, '试听绝不能降级到另一个引擎'
    assert 'IndexTTS2' in msg and 'edge-tts' not in msg


def test_preview_uses_explicitly_selected_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(S, 'tts_local_cfg',
                        lambda: {'engine': 'indextts2', 'instruct': ''})
    monkeypatch.setattr(T2, 'indextts2_speak', lambda text, out, **k: _fake_wav(out))
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 2.0)
    ok, msg, eng, rel = S.tts_test_speak('试听一句。')
    assert ok is True and eng == 'indextts2' and rel.endswith('.wav')
    assert 'IndexTTS2' in msg


def test_preview_defaults_to_clone_with_fixed_ref(monkeypatch, tmp_path):
    """auto → IndexTTS2 克隆，且带同一条参考音（与成片同一把嗓子）。"""
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(S, 'tts_local_cfg',
                        lambda: {'engine': 'auto', 'instruct': '低沉男声', 'rate': '+0%'})
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: True)
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, 'voice_ref', lambda instruct=None: r'C://ref.wav')
    monkeypatch.setattr(T2, 'rate_to_speed', lambda r: 1.0)
    got = {}

    def _speak(text, out, spk=None, diag=None, **k):
        got['spk'] = spk
        return _fake_wav(out)

    monkeypatch.setattr(T2, 'indextts2_speak', _speak)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 2.0)
    ok, msg, eng, rel = S.tts_test_speak('试听一句。')
    assert ok is True and eng == 'indextts2'
    assert got['spk'] == r'C://ref.wav', '试听必须用成片那条参考音'
    assert 'IndexTTS2' in msg


def test_preview_explicit_qwen3_still_passes_instruct(monkeypatch, tmp_path):
    """显式选 Qwen3 时仍要能试听它，自定义音色描述照旧生效。"""
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(S, 'tts_local_cfg',
                        lambda: {'engine': 'qwen3tts', 'instruct': '低沉男声'})
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    got = {}

    def _speak(text, out, instruct=None, diag=None):
        got['instruct'] = instruct
        return _fake_wav(out)

    monkeypatch.setattr(T2, 'qwen3tts_speak', _speak)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 2.0)
    ok, msg, eng, rel = S.tts_test_speak('试听一句。')
    assert ok is True and eng == 'qwen3tts'
    assert got['instruct'] == '低沉男声'
    assert 'VoiceDesign' in msg


def test_frontend_has_timeout_and_finally_restores_button():
    js = open(JS, encoding='utf-8').read()
    i = js.index("$('btnTtsTest').addEventListener")
    block = js[i:i + 1800]
    assert 'AbortController' in block and 'ctrl.abort()' in block
    assert 'signal: ctrl.signal' in block
    assert 'finally {' in block and "b.disabled = false" in block
    assert 'AbortError' in block
    assert 'dataset.busy' in block, '连点不能叠加请求'


def test_worker_is_unbuffered_logged_and_bounded():
    src = open(os.path.join(ROOT, 'tts2_engines.py'), encoding='utf-8').read()
    assert "'-u'" in src, 'worker 必须无缓冲，否则 stdout 阶段输出会被块缓冲吞掉'
    assert ".log'" in src or '.log\"' in src, 'worker stderr 必须落日志'
    assert 'ready_timeout=150' in src and 'ready_timeout=360' in src
    assert 'def ask(self, req, timeout=240)' in src


def test_preview_failure_shows_real_reason(monkeypatch, tmp_path):
    """v1.4.18：失败必须把 worker/显存的真实原因带出来。

    事故现场：用户只看到「未就绪或加载超时」，而真因是 Ollama 的
    qwen3:8b 常驻吃了 9.6GB 显存，模型被驱动换页 → 永远算不完。
    """
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    # 显式选 Qwen3：auto 现在默认走 IndexTTS2，拿 auto 测 Qwen3 的失败路径会
    # 去真启 IndexTTS2（实测把整套测试拖到 5 分钟）。
    monkeypatch.setattr(S, 'tts_local_cfg',
                        lambda: {'engine': 'qwen3tts', 'instruct': ''})
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: False)

    def _speak(text, out, instruct=None, diag=None):
        diag.update({'engine': 'qwen3tts',
                     'reason': '显存不足：11671/12288 MB 已用，配音需要约 5200 MB',
                     'note': '', 'vram': {'used': 11671, 'total': 12288}})
        return False
    monkeypatch.setattr(T2, 'qwen3tts_speak', _speak)
    ok, msg, eng, rel = S.tts_test_speak('试听一句。')
    assert ok is False
    assert '显存不足' in msg and '11671' in msg
    assert '未就绪或加载超时' not in msg, '不能再拿模糊文案糊弄用户'

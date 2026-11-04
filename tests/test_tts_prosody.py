# -*- coding: utf-8 -*-
"""影视解说韵律注入回归：不改稿子原文，但 TTS 必须拿到情绪与停顿。"""
import webui_server as S  # noqa: F401  先导入入口，解循环依赖
import tts_engines as T

LONG = ('瑞克站起来，放轻声音喊她。“小姑娘？别怕，我是警察。”'
        '女孩缓缓转过身。半边脸的皮肉已经烂穿，浑浊的眼珠死死锁在他身上。')


def test_prosody_keeps_original_text():
    """剥离标记后必须逐字等于原稿：字幕和稿子一个字都不能被改。"""
    out = T.prosody_for_tts(LONG)
    assert T._strip_tts_markup(out) == LONG


def test_prosody_adds_emotion_and_pause():
    out = T.prosody_for_tts(LONG)
    assert '{情绪:' in out and '{/情绪}' in out
    assert '{停顿:' in out
    assert out.count('{情绪:') == out.count('{/情绪}')


def test_prosody_emotion_per_clause():
    """整句一刀切会把「放轻声音喊她」和「皮肉烂穿」贴成同一种情绪。"""
    out = T.prosody_for_tts(LONG)
    assert '{情绪:温柔}' in out or '{情绪:紧张}' in out
    assert out.count('{情绪:') >= 3


def test_prosody_dialogue_not_split():
    """引号里的对白不能被停顿标记劈成两半。"""
    out = T.prosody_for_tts(LONG)
    assert '“小姑娘？别怕，我是警察。”' in out
    assert '小姑娘？{停顿' not in out


def test_prosody_skips_marked_and_short():
    marked = '{情绪:紧张}测试{/情绪}'
    assert T.prosody_for_tts(marked) == marked
    assert T.prosody_for_tts('你好') == '你好'
    assert T.prosody_for_tts('') == ''


def test_prosody_segments_pause_seconds():
    segs = T.prosody_segments(T.prosody_for_tts(LONG))
    assert len(segs) >= 4
    assert any(p > 0 for _, p in segs)
    assert segs[-1][1] == 0.0        # 句末不插尾部空白
    assert ''.join(t for t, _ in segs) == LONG


def test_ssml_contains_emotion_and_breaks():
    """SSML 分支以前被 _strip_tts_markup 剥光，这个测试锁住它必须真的被走到。"""
    ssml = T.markup_to_ssml(T.prosody_for_tts(LONG),
                            voice='zh-CN-XiaoxiaoNeural', rate='+0%')
    assert ssml.count('<mstts:express-as') == ssml.count('</mstts:express-as>') >= 3
    assert '<break' in ssml
    assert '小姑娘？别怕，我是警察。' in ssml


def test_edge_speak_receives_markup(monkeypatch):
    """local_tts_speak 的 edge 分支不能再剥标记。"""
    import tts_engines as T
    seen = {}

    def fake_edge(text, path, voice=None, rate=None):
        seen['text'] = text
        return True

    monkeypatch.setattr(T, 'edge_tts_available', lambda: True)
    monkeypatch.setattr(T, 'edge_tts_speak', fake_edge)
    monkeypatch.setattr(T, '_tls_edge_state', lambda: None)
    monkeypatch.setattr(T, '_tts_run_banned', lambda: set())
    monkeypatch.setattr(T, 'tts_local_cfg',
                        lambda: {'engine': 'edge', 'voice': 'zh-CN-XiaoxiaoNeural',
                                 'rate': '+0%', 'cosy_voice': '中文女'})
    ok, eng, _ = T.local_tts_speak(LONG, '_tmp_prosody_check.mp3')
    assert ok and eng == 'edge'
    assert '{情绪:' in seen['text'] and '{停顿:' in seen['text']

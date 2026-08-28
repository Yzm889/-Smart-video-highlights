"""一帧成片 FrameCut · 核心逻辑回归测试

覆盖已验证过的关键纯函数，避免后续改动打回：
- align_script_to_segments：解说事件 ↔ 镜头段 时序保持对齐 + 全覆盖
- collect_partial：任务失败时中间产物分类（video/audio/subtitle/text）
- chat_cfg：LLM 配置解析（优先 chat 段，回退 vision 段，向后兼容）
- web_search：联网源全失败时安全返回 []，不抛异常

说明：webui_server 顶层只 import numpy/PIL（轻量），重依赖（ffmpeg/whisper/librosa）
均在函数内 import，故本测试环境只需 numpy + Pillow 即可 import。
"""

import os

import urllib.request

import pytest


def _boom(*a, **k):
    raise Exception('offline')


# ---------------------------------------------------------------------------
# align_script_to_segments
# ---------------------------------------------------------------------------
def test_align_matches_and_keeps_order():
    import webui_server as S
    segs = [(0, 5), (5, 10), (10, 15)]
    asr = [
        {'text': '火焰山烈焰', 'start': 1, 'end': 3},
        {'text': '水帘洞清泉', 'start': 6, 'end': 8},
        {'text': '风云变莫测', 'start': 11, 'end': 13},
    ]
    events = [
        {'desc': '火焰山的试炼', 'keywords': ['火焰']},
        {'desc': '水帘洞的温柔', 'keywords': ['水帘']},
        {'desc': '风云莫测的传说', 'keywords': ['风云', '火焰']},
    ]
    out = S.align_script_to_segments(events, segs, asr)
    assert [s for s, _ in out] == [0, 5, 10], out
    assert [d for _, d in out] == [
        '火焰山的试炼', '水帘洞的温柔', '风云莫测的传说']
    # 第 3 个事件虽含「火焰」，但段 0 已被占用，不应回跳，必须落在段 2（时序保持）
    assert out[2][0] == 10


def test_align_full_coverage_with_padding():
    import webui_server as S
    segs = [(0, 5), (5, 10), (10, 15), (15, 20)]
    asr = [
        {'text': '甲一', 'start': 1, 'end': 3},
        {'text': '乙二', 'start': 6, 'end': 8},
        {'text': '丙三', 'start': 11, 'end': 13},
        {'text': '丁四', 'start': 16, 'end': 18},
    ]
    events = [
        {'desc': 'e1', 'keywords': ['甲一']},
        {'desc': 'e2', 'keywords': ['乙二']},
        {'desc': 'e3', 'keywords': ['丙三']},
        {'desc': 'e4', 'keywords': ['zz']},  # 无匹配，应补位到空闲段
    ]
    out = S.align_script_to_segments(events, segs, asr)
    assert len(out) == 4, out
    starts = [s for s, _ in out]
    assert starts == sorted(starts), '输出必须严格按时间升序'
    assert len(set(starts)) == len(starts), '不能有重复镜头段'


# ---------------------------------------------------------------------------
# collect_partial
# ---------------------------------------------------------------------------
def test_collect_partial_classifies(tmp_path):
    import webui_server as S
    S.OUTDIR = str(tmp_path)  # 让 url 相对路径基于临时目录计算
    run = tmp_path / 'run-x'
    run.mkdir()
    (run / 'final.mp4').write_bytes(b'vid')
    (run / 'narr0.wav').write_bytes(b'aud')
    (run / 'subs.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\nhi')
    (run / 'script.txt').write_text('解说稿内容')
    res = S.collect_partial(str(run))
    kinds = {f['name']: f['kind'] for f in res['files']}
    assert kinds == {
        'final.mp4': 'video',
        'narr0.wav': 'audio',
        'subs.srt': 'subtitle',
        'script.txt': 'text',
    }
    assert res['best_video'].endswith('final.mp4')
    assert '解说稿内容' in (res['text'] or '')


def test_collect_partial_missing_dir():
    import webui_server as S
    res = S.collect_partial('/nonexistent/xyz/path')
    assert res['files'] == []
    assert res['best_video'] is None
    assert res['text'] is None


# ---------------------------------------------------------------------------
# chat_cfg
# ---------------------------------------------------------------------------
def test_chat_cfg_prefers_chat(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(S, 'load_ai_config', lambda: {
        'chat': {'api_key': 'C', 'base_url': 'U1'},
        'vision': {'api_key': 'V'},
    })
    assert S.chat_cfg().get('api_key') == 'C'


def test_chat_cfg_falls_back_to_vision(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(S, 'load_ai_config', lambda: {
        'vision': {'api_key': 'V', 'base_url': 'U2'},
    })
    assert S.chat_cfg().get('api_key') == 'V'


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------
def test_web_search_returns_empty_on_failure(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(urllib.request, 'urlopen', _boom)
    assert S.web_search('任意查询') == []


# ---------------------------------------------------------------------------
# compute_mode / ai_status（引导：避免静默用免费模板）
# ---------------------------------------------------------------------------
def _cfg(monkeypatch, chat=None, vision=None):
    import webui_server as S
    c = {}
    if chat is not None:
        c['chat'] = chat
    if vision is not None:
        c['vision'] = vision
    monkeypatch.setattr(S, 'load_ai_config', lambda: c)
    return S


def test_compute_mode_free_when_no_key(monkeypatch):
    import webui_server as S
    _cfg(monkeypatch, chat=None)  # 未配置 LLM
    # 即便 economy 显式 False，无 key 也应强制降级为免费
    assert S.compute_mode({'economy': False}, needs_chat=True) == 'free'
    assert S.compute_mode({}, needs_chat=True) == 'free'


def test_compute_mode_ai_when_key_and_explicit(monkeypatch):
    import webui_server as S
    _cfg(monkeypatch, chat={'api_key': 'K', 'base_url': 'U', 'model': 'M'})
    assert S.compute_mode({'economy': False}, needs_chat=True) == 'ai'
    # 默认 economy=None 视为免费
    assert S.compute_mode({}, needs_chat=True) == 'free'


def test_ai_status_reports_readiness(monkeypatch):
    import webui_server as S
    S2 = _cfg(monkeypatch, chat={'api_key': 'K', 'base_url': 'U', 'model': 'M'}, vision=None)
    st = S2.ai_status()
    assert st['chat'] is True
    assert st['vision'] is False
    assert st['any_ai'] is True
    assert st['configured'] is True


# ---------------------------------------------------------------------------
# 省流(本地离线)模式：必须真正用本地 Whisper 识别，而非套固定模板
# ---------------------------------------------------------------------------
def _torch_cuda_available():
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def test_whisper_device_returns_valid_pair():
    import webui_server as S
    dev, ct = S.whisper_device()
    assert dev in ('cpu', 'cuda')
    assert ct in ('int8', 'float16')
    # 无 torch / 无显卡的环境应安全回退 CPU(int8)，省流模式仍能跑（只是慢些）
    if not _torch_cuda_available():
        assert (dev, ct) == ('cpu', 'int8')


def test_generate_narration_economy_uses_real_asr(monkeypatch):
    import webui_server as S
    # 禁用本地模型，确保走真实台词/模板分支，测试快且确定
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: False)
    segs = [(0.0, 5.0), (5.0, 10.0)]
    asr = [{'start': 1.0, 'end': 3.0, 'text': '你好这个世界真的很精彩'}]
    out, used_local = S.generate_narration(segs, asr, {'economy': True})
    assert used_local is False
    # 有台词的镜头段必须用真实转写文本，绝不能是那 8 句循环模板
    assert out[0] == '你好这个世界真的很精彩'[:40]
    assert '镜头缓缓推进' not in out[0]
    # 无台词的镜头段才回退模板
    assert out[1] in [
        '镜头缓缓推进，故事就此展开。', '画面一转，新的转折正在发生。',
        '气氛渐起，关键情节悄然铺开。', '人物登场，冲突拉开了序幕。',
        '悬念浮现，让人忍不住屏息。', '节奏陡然加快，高潮正在靠近。',
        '真相逼近，谜底即将揭晓。', '余波未平，故事仍在继续。',
    ]


def S_module():
    import webui_server as S
    return S


def monkeypatch_local_off(S):
    # 由调用方在 monkeypatch 上下文里用；此处仅占位，真正 patch 在测试中完成
    pass


def test_generate_narration_economy_local_rewrite(monkeypatch):
    """省流 + 本地模型就绪时，用本地模型离线改写解说词，且标记 used_local=True。"""
    import webui_server as S
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: True)
    monkeypatch.setattr(S, 'local_llm_chat',
                        lambda prompt, system=None, timeout=180: '春天来了樱花盛开\n主角漫步在树下\n微风拂过落英缤纷')
    segs = [(0.0, 5.0), (5.0, 10.0), (10.0, 15.0)]
    asr = [{'start': 1.0, 'end': 3.0, 'text': '你好'}]
    out, used_local = S.generate_narration(segs, asr, {'economy': True})
    assert used_local is True
    assert out == ['春天来了樱花盛开', '主角漫步在树下', '微风拂过落英缤纷']


def test_ai_status_includes_local(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: True)
    st = S.ai_status()
    assert 'local' in st
    assert st['local'] is True
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: False)
    assert S.ai_status()['local'] is False


def test_narrate_video_runs_local_asr_in_economy():
    """关键回归：省流模式不得再关掉本地 ASR（旧实现 asr=[] 导致套话解说）。"""
    import webui_server as S, inspect
    src = inspect.getsource(S.narrate_video)
    assert 'asr = asr_segments(video_path) if not params.get' not in src, \
        'narrate_video 仍在省流模式禁用 ASR，已修复应无条件调用 asr_segments'
    assert 'asr = asr_segments(video_path)' in src


# ---------------------------------------------------------------------------
# Whisper 模型可配置 + 本地缓存目录
# ---------------------------------------------------------------------------
def test_whisper_model_name_default_and_sanitize(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(S, 'load_ai_config', lambda: {})
    assert S.whisper_model_name() == 'base'
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'whisper': {'model': 'medium'}})
    assert S.whisper_model_name() == 'medium'
    # 非法值必须回退 base，避免 WhisperModel 抛错
    monkeypatch.setattr(S, 'load_ai_config', lambda: {'whisper': {'model': '诺莫'}})
    assert S.whisper_model_name() == 'base'


def test_whisper_models_dir_points_into_project(monkeypatch):
    import webui_server as S
    # 项目内 models/whisper，便于引导用户查看/管理权重
    assert S.whisper_models_dir().replace('\\', '/').endswith('models/whisper')


def test_whisper_model_ready_detects_cache(monkeypatch, tmp_path):
    import webui_server as S
    monkeypatch.setattr(S, 'whisper_models_dir', lambda: str(tmp_path))
    assert S.whisper_model_ready('base') is False
    d = tmp_path / 'base'
    d.mkdir()
    (d / 'model.bin').write_bytes(b'x')
    assert S.whisper_model_ready('base') is True


# ---------------------------------------------------------------------------
# 本地视觉理解 VLM（看图+台词+梗概 → 真解说）
# ---------------------------------------------------------------------------
def test_vlm_cfg_defaults(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(S, 'load_ai_config', lambda: {})
    c = S.vlm_cfg()
    assert c['enabled'] is False
    assert c['mode'] == 'ollama'
    assert c['base_url'] == 'http://localhost:11434'
    assert c['model'] == 'qwen2.5vl:latest'


def test_vlm_enabled_reflects_config(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(S, 'load_ai_config', lambda: {})
    assert S.vlm_enabled() is False
    monkeypatch.setattr(S, 'load_ai_config', lambda: {
        'vlm': {'enabled': True, 'base_url': 'http://localhost:11434', 'model': 'qwen2.5vl:latest'}})
    assert S.vlm_enabled() is True


def test_generate_narration_vlm_branch_when_enabled(monkeypatch):
    """省流 + 本地 VLM 就绪：必须走 VLM 真解说，返回其文案并标记 used_local。"""
    import webui_server as S
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: False)
    monkeypatch.setattr(S, 'local_vlm_narrate',
                        lambda per_seg, frames, params: (['画面里主角拔剑出鞘', '他转身迎战群敌'], True))
    segs = [(0.0, 5.0), (5.0, 10.0)]
    asr = [{'start': 1.0, 'end': 3.0, 'text': '你好'}]
    out, used_local = S.generate_narration(segs, asr, {'economy': True}, frames={0: 'f.jpg', 1: 'f.jpg'})
    assert used_local is True
    assert out == ['画面里主角拔剑出鞘', '他转身迎战群敌']


def test_generate_narration_vlm_fallback_on_error(monkeypatch):
    """VLM 调用失败（如未拉模型）时不得抛异常，应回退真实台词/模板。"""
    import webui_server as S
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: False)
    monkeypatch.setattr(S, 'local_vlm_narrate', lambda per_seg, frames, params: (_ for _ in ()).throw(RuntimeError('offline')))
    segs = [(0.0, 5.0), (5.0, 10.0)]
    asr = [{'start': 1.0, 'end': 3.0, 'text': '真实台词内容'}]
    out, used_local = S.generate_narration(segs, asr, {'economy': True}, frames={0: 'f.jpg'})
    assert used_local is False
    # 有台词段回退真实台词（非模板）
    assert out[0] == '真实台词内容'[:40]


def test_ai_status_includes_whisper_vlm(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(S, 'vlm_ping', lambda: (True, 'ok'))
    monkeypatch.setattr(S, 'whisper_model_ready', lambda m=None: True)
    st = S.ai_status()
    for k in ('whisper_model', 'whisper_ready', 'vlm_enabled', 'vlm_ready', 'vlm_msg'):
        assert k in st, k
    assert st['whisper_ready'] is True
    assert st['vlm_enabled'] is True
    assert st['vlm_ready'] is True


# ---------------------------------------------------------------------------
# 混音 ducking：解说配音存在时原声压低，杜绝双声重叠
# ---------------------------------------------------------------------------
def test_compose_applies_ducking(monkeypatch, tmp_path):
    import webui_server as S
    calls = []
    def fake_ffmpeg(args, input_data=None):
        calls.append(args)
        return 0, b'', b'Stream #0:1: Audio: aac'  # 任何调用都成功，且含音轨
    monkeypatch.setattr(S, 'ffmpeg_run', fake_ffmpeg)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 5.0)
    final = S._compose_narration_video(
        'x.mp4', [(0.0, 5.0)], ['解说词'], [('fake.wav', 0.0)], str(tmp_path), {}, music_path=None)
    joined = ' '.join(' '.join(c) for c in calls)
    # 必须出现逐帧音量表达式（ducking），并在解说段内压到 0.08
    assert 'eval=frame' in joined, '未生成 ducking 音量表达式'
    assert 'between(t' in joined, '未出现按时间区间压低原声的表达式'
    assert '0.08' in joined, '解说段内原声未被压低到 0.08'
    assert final.endswith('final.mp4')


def test_ai_status_contains_mirror():
    """ai_status 应返回 mirror 配置（国内镜像引导），供前端展示免科学上网提示。"""
    import webui_server as S
    st = S.ai_status()
    assert 'mirror' in st
    assert 'hf_endpoint' in st['mirror']
    assert 'ollama_proxy' in st['mirror']


def test_mirror_cfg_default_uses_hf_mirror(tmp_path):
    """mirror_cfg 默认开启 HF 镜像(hf-mirror.com)，中文用户免科学上网。"""
    import webui_server as S
    old = S.AI_CONFIG_PATH
    p = str(tmp_path / 'empty_ai_config.json')
    open(p, 'w', encoding='utf-8').close()
    S.AI_CONFIG_PATH = p
    try:
        m = S.mirror_cfg()
        assert m['use_hf_mirror'] is True
        assert 'hf-mirror.com' in m['hf_endpoint']
        assert m['ollama_proxy'] == ''
    finally:
        S.AI_CONFIG_PATH = old


def test_probe_ollama_mirror_ok(monkeypatch):
    """探测返回 200 且页面含 OllamaSetup → 判定可用。"""
    import webui_server as S, urllib.request

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def getcode(self): return 200
        def read(self, n=-1): return b'<html>OllamaSetup.exe releases download</html>'
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: _Resp())
    ok, note = S.probe_ollama_mirror('https://ghfast.top')
    assert ok is True
    assert 'OllamaSetup' in note


def test_probe_ollama_mirror_cert_unsafe(monkeypatch):
    """证书不安全（ghproxy.net 类）→ 判为不可用，正好被前端自动过滤。"""
    import webui_server as S, urllib.request, urllib.error

    def _raise(*a, **k):
        raise urllib.error.URLError('certificate verify failed: self-signed')
    monkeypatch.setattr(urllib.request, 'urlopen', _raise)
    ok, note = S.probe_ollama_mirror('https://ghproxy.net')
    assert ok is False
    assert '证书不安全' in note


def test_probe_ollama_mirror_timeout(monkeypatch):
    """超时/连接失败 → 判为不可用。"""
    import webui_server as S, urllib.request, urllib.error

    def _raise(*a, **k):
        raise urllib.error.URLError('timed out')
    monkeypatch.setattr(urllib.request, 'urlopen', _raise)
    ok, note = S.probe_ollama_mirror('https://mirror.ghproxy.com')
    assert ok is False
    assert '超时' in note


def test_scan_ollama_mirrors_picks_best(monkeypatch):
    """scan 并发探测后 best=首个可用的 base；其余失败不影响结果。"""
    import webui_server as S

    def fake(base, timeout=6):
        if base == 'https://ghfast.top':
            return True, '可用：含 OllamaSetup.exe 下载'
        return False, '连接失败'
    monkeypatch.setattr(S, 'probe_ollama_mirror', fake)
    res = S.scan_ollama_mirrors()
    assert res['best'] == 'https://ghfast.top'
    assert any(m['ok'] for m in res['mirrors'])
    assert 'mirrors' in res and 'scanned_at' in res


# ---------------------------------------------------------------------------
# 升级回归：解说混音 ducking 逗号 bug / 配音裁剪 / 字幕清洗 / 百度解析 /
#          /api/cancel 取消接口 / narrate_movie 省流默认 / whisper 误判
# ---------------------------------------------------------------------------
def test_compose_ducking_expression_quoted(monkeypatch, tmp_path):
    """回归：ducking 音量表达式含逗号，必须用单引号包裹，否则 ffmpeg 会把逗号当滤镜链分隔符、
    报 "No option name near 'frame'" 导致解说混音失败。"""
    import webui_server as S
    calls = []
    def fake_ffmpeg(args, input_data=None):
        calls.append(args)
        return 0, b'', b'Stream #0:1: Audio: aac'
    monkeypatch.setattr(S, 'ffmpeg_run', fake_ffmpeg)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 5.0)
    S._compose_narration_video(
        'x.mp4', [(0.0, 5.0)], ['解说词'], [('fake.wav', 0.0)], str(tmp_path), {}, music_path=None)
    joined = ' '.join(' '.join(c) for c in calls)
    assert "volume='" in joined, 'ducking 表达式必须用单引号包裹：%s' % joined
    assert 'eval=frame' in joined and 'between(t' in joined and '0.08' in joined, joined


def test_compose_clips_tts_to_segment(monkeypatch, tmp_path):
    """配音 3 元组 (audio, start, end)：应把配音裁剪在本镜头段内，避免跨段语音重叠。"""
    import webui_server as S
    calls = []
    def fake_ffmpeg(args, input_data=None):
        calls.append(args)
        return 0, b'', b'Stream #0:1: Audio: aac'
    monkeypatch.setattr(S, 'ffmpeg_run', fake_ffmpeg)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 8.0)
    S._compose_narration_video(
        'x.mp4', [(0.0, 5.0)], ['解说词'], [('fake.wav', 0.0, 5.0)], str(tmp_path), {}, music_path=None)
    joined = ' '.join(' '.join(c) for c in calls)
    assert 'atrim=0:5.00' in joined, '应裁剪配音时长到镜头段长度：%s' % joined


def test_compose_supports_two_tuple_tts(monkeypatch, tmp_path):
    """向后兼容：tts_paths 仍支持旧 2 元组 (audio, start)，不裁剪（无段尾信息）。"""
    import webui_server as S
    calls = []
    def fake_ffmpeg(args, input_data=None):
        calls.append(args)
        return 0, b'', b'Stream #0:1: Audio: aac'
    monkeypatch.setattr(S, 'ffmpeg_run', fake_ffmpeg)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 5.0)
    S._compose_narration_video(
        'x.mp4', [(0.0, 5.0)], ['解说词'], [('fake.wav', 0.0)], str(tmp_path), {}, music_path=None)
    joined = ' '.join(' '.join(c) for c in calls)
    assert 'atrim=' not in joined, '2 元组不应裁剪'
    assert 'adelay=0|0' in joined


def test_clean_caption():
    import webui_server as S
    assert S._clean_caption('  你好\n世界  ') == '你好 世界'
    assert S._clean_caption('“他说”') == '他说'
    assert S._clean_caption('') == ''
    assert S._clean_caption('多   空格') == '多 空格'


def test_web_search_parses_baidu_blocks(monkeypatch):
    """回归：百度结果逐条解析——各结果标题/摘要独立，且能提取 URL（旧实现 snippet 全取第一个）。"""
    import webui_server as S, urllib.request
    html = '''
    <html><body>
    <div class="result c-container x">
      <h3 class="t"><a href="https://example.com/1">第一个结果标题</a></h3>
      <span class="content-right_8ZsYK">第一段摘要内容</span>
    </div>
    <div class="result c-container y">
      <h3 class="t"><a href="https://example.com/2">第二个结果标题</a></h3>
      <span class="content-right_8ZsYK">第二段摘要内容</span>
    </div>
    </body></html>
    '''
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return html.encode('utf-8')
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: _Resp())
    res = S.web_search('测试查询')
    assert res, '百度结果应被解析出来'
    assert res[0][0] == '第一个结果标题', res
    assert res[1][0] == '第二个结果标题', res
    assert res[0][2].startswith('https://'), '应提取 URL：%s' % res[0]
    assert res[1][2].startswith('https://'), '应提取 URL：%s' % res[1]


def test_cancel_endpoint_registered():
    """回归：/api/cancel 取消接口必须存在（此前为 mirror/scan 后的死代码，取消合成会 404）。"""
    import webui_server as S, inspect
    src = inspect.getsource(S.Handler.do_POST)
    assert '/api/cancel' in src, '/api/cancel 接口缺失，取消合成会 404'
    assert src.find('/api/cancel') < src.find('/api/history/delete'), '取消接口应在死代码位置之后正常注册'


def test_narrate_movie_defaults_economy():
    """回归：narrate_movie 应省流优先（economy 默认 True），且省流下不调用付费 TTS。"""
    import webui_server as S, inspect
    src = inspect.getsource(S.narrate_movie)
    assert "economy=bool(params.get('economy', True))" in src, 'narrate_movie 默认应省流'
    assert "not bool(params.get('economy', True))) and _tts_available()" in src, '省流模式不应调用付费 TTS'


def test_local_llm_cfg_disabled_by_default(monkeypatch):
    """回归：未配置本地模型时不得默认启用（否则省流解说会无谓连 localhost:11434）。"""
    import webui_server as S
    monkeypatch.setattr(S, 'load_ai_config', lambda: {})
    assert S.local_llm_cfg()['enabled'] is False
    assert S.local_llm_enabled() is False
    # 显式启用才生效
    monkeypatch.setattr(S, 'load_ai_config', lambda: {
        'local': {'enabled': True, 'base_url': 'http://127.0.0.1:11434/v1', 'model': 'qwen2.5:latest'}})
    assert S.local_llm_enabled() is True


def test_whisper_model_ready_ignores_ai_config_json(monkeypatch, tmp_path):
    """回归：models 目录里只有 ai_config.json 等文件时，不得误判模型已就绪（旧实现 endswith('config.json') 误报）。"""
    import webui_server as S
    monkeypatch.setattr(S, 'whisper_models_dir', lambda: str(tmp_path))
    (tmp_path / 'ai_config.json').write_text('{}', encoding='utf-8')
    assert S.whisper_model_ready('base') is False
    (tmp_path / 'model.bin').write_bytes(b'x')
    assert S.whisper_model_ready('base') is True


# ---------------------------------------------------------------------------
# 卡点剪辑升级回归：节拍同步音画时长对齐 / 原声保留 / 免重编码拼接 / xfade 转场
# ---------------------------------------------------------------------------
def _beatcut_env(monkeypatch, tmp_path, keep_audio=False):
    """构造 beat_cut_video 的 mock 环境，返回 (S, calls)。"""
    import webui_server as S
    calls = []

    def fake_ffmpeg(args, input_data=None):
        calls.append(list(args))
        return 0, b'', b'Stream #0:0: Video: h264'

    monkeypatch.setattr(S, 'ffmpeg_run', fake_ffmpeg)
    monkeypatch.setattr(S, 'detect_scene_cuts', lambda v, threshold=0.3: [1.0, 3.0, 5.0])
    # 卡点分析现为「一次抽帧」：mock _analyze_video_frames 与从帧信号提取的内部函数
    monkeypatch.setattr(S, '_analyze_video_frames', lambda v, fps_s=4.0: {})
    monkeypatch.setattr(S, '_detect_motion_from_frames', lambda an, min_gap=0.6, strength='standard': [2.0, 4.0])
    monkeypatch.setattr(S, '_detect_visual_from_frames', lambda an, strength='standard': [])
    monkeypatch.setattr(S, 'detect_strong_beats',
                        lambda m, top_k=None, min_sep=0.25: ([1.0, 2.0, 3.0, 4.0, 5.0], 1.0))
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 6.0)
    monkeypatch.setattr(S, '_has_audio_track', lambda p: keep_audio)

    def fake_make_clip(video_path, seg_dur, seg, w=1280, h=720, fps=30, start=0.0):
        with open(seg, 'wb') as f:
            f.write(b'x')

    monkeypatch.setattr(S, 'make_video_clip', fake_make_clip)
    return S, calls


def test_beatcut_hardcut_uses_concat_demuxer(monkeypatch, tmp_path):
    """默认硬切应走 concat demuxer + copy 免重编码（性能升级）。"""
    S, calls = _beatcut_env(monkeypatch, tmp_path)
    final, diag = S.beat_cut_video('src.mp4', 'music.mp3', str(tmp_path), {}, progress=None)
    assert final.endswith('final.mp4')
    concat_call = calls[0]
    assert '-f' in concat_call and 'concat' in concat_call and '-c' in concat_call and 'copy' in concat_call, \
        '硬切应使用 concat demuxer 免重编码：%s' % concat_call
    assert diag['transition'] == 'none'


def test_beatcut_keep_audio_mixes(monkeypatch, tmp_path):
    """保留原声开启：配乐命令应含 原声+音乐 amix（原声压低 + 音乐铺底）。"""
    S, calls = _beatcut_env(monkeypatch, tmp_path, keep_audio=True)
    final, diag = S.beat_cut_video('src.mp4', 'music.mp3', str(tmp_path), {'keepAudio': True}, progress=None)
    assert diag['keep_audio'] is True
    mix_call = calls[-1]
    joined = ' '.join(mix_call)
    assert 'amix=inputs=2' in joined, '应做 原声+音乐 混音：%s' % joined
    assert 'volume=0.3' in joined and 'volume=0.7' in joined, '原声压低+音乐铺底：%s' % joined
    assert '-map' in mix_call and '[aout]' in joined


def test_beatcut_keep_audio_skips_when_no_track(monkeypatch, tmp_path):
    """原视频无音轨时，即使开启保留原声也应回退纯音乐。"""
    S, calls = _beatcut_env(monkeypatch, tmp_path, keep_audio=False)
    final, diag = S.beat_cut_video('src.mp4', 'music.mp3', str(tmp_path), {'keepAudio': True}, progress=None)
    mix_call = calls[-1]
    joined = ' '.join(mix_call)
    assert 'amix' not in joined, '无原声时不应 amix：%s' % joined


def test_beatcut_xfade_transition(monkeypatch, tmp_path):
    """指定转场时拼接命令应生成 xfade 链，offset 逐段累计。"""
    S, calls = _beatcut_env(monkeypatch, tmp_path)
    final, diag = S.beat_cut_video('src.mp4', 'music.mp3', str(tmp_path),
                                   {'transition': 'fadewhite', 'transDur': 0.2}, progress=None)
    assert diag['transition'] == 'fadewhite'
    first = calls[0]
    joined = ' '.join(first)
    assert 'xfade=transition=fadewhite:duration=0.200' in joined, '应生成 xfade 链：%s' % joined
    assert joined.count('xfade=') >= 1, '至少一次转场'


def test_beat_sync_fills_beat_segments(monkeypatch, tmp_path):
    """回归：节拍同步每段画面时长必须填满拍点间隔（src_end-src_start ≈ seg_dur），
    否则拼接总长 < 音乐时长，-shortest 会把音乐结尾砍掉。"""
    import webui_server as S, os

    captured = {}

    def fake_ffmpeg(args, input_data=None):
        # 第一次调用是 concat 拼接：读取 concat.txt 内容留证
        if 'concat' in args and '-f' in args:
            i = args.index('-i')
            txt_path = args[i + 1]
            captured['concat'] = open(txt_path, encoding='utf8').read()
        return 0, b'', b''

    monkeypatch.setattr(S, 'ffmpeg_run', fake_ffmpeg)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 10.0)
    monkeypatch.setattr(S, 'detect_beats', lambda a, sensitivity=0.5: [1.0, 3.0, 6.0])
    out = os.path.join(str(tmp_path), 'out.mp4')
    res = S.generate_beat_sync_video('src.mp4', 'music.mp3', out,
                                     beat_sensitivity=0.5, min_clip_dur=0.6, progress=None)
    assert res['ok'] is True
    content = captured.get('concat', '')
    assert content, '应生成 concat 列表'
    # 解析每段的 inpoint/outpoint，验证总时长 ≈ 音乐时长(10s)
    total = 0.0
    lines = content.splitlines()
    for i in range(0, len(lines), 3):
        assert lines[i].startswith('file ')
        inp = float(lines[i + 1].split()[1])
        outp = float(lines[i + 2].split()[1])
        total += outp - inp
    assert abs(total - 10.0) < 0.05, '拼接总时长应 ≈ 音乐时长 10s，实际 %.2f（音画错位）' % total


# ---------------------------------------------------------------------------
# 人机协同：分析(plan) / 微调渲染(confirm) 流程
# ---------------------------------------------------------------------------
def test_analyze_plan_beatcut(monkeypatch, tmp_path):
    """分析阶段应生成 beatcut 方案（timeline + 缩略图），并置 plan_ready。"""
    import webui_server as S
    run_dir = os.path.join(str(tmp_path), 'r1')
    os.makedirs(run_dir, exist_ok=True)
    src = os.path.join(run_dir, 'src_video.mp4')
    open(src, 'wb').write(b'v')
    mus = os.path.join(run_dir, 'm.mp3')
    open(mus, 'wb').write(b'm')

    monkeypatch.setattr(S, '_analyze_beatcut',
                        lambda v, m, p, prog=None: ([0.0, 2.0, 4.0, 6.0], {'scene_cuts': [2.0]}, 6.0))
    def _fake_thumbs(v, segs, rd, max_side=220):
        t = os.path.join(rd, 't0.jpg')
        open(t, 'wb').write(b'j')
        return {0: t}
    monkeypatch.setattr(S, '_plan_thumbs', _fake_thumbs)
    monkeypatch.setattr(S, '_resolve_music', lambda m: mus)

    prog = {'runid': 'run-9', 'run_dir': run_dir, 'phase': '', 'pct': 0, 'done': False}
    req = {'type': 'beatcut', 'params': {}, 'video': {'name': 'x.mp4', 'data': 'AA=='}, 'music': None}
    S._analyze_plan_job(req, prog)
    assert prog.get('plan_ready') is True
    plan = S.PLANS.get('run-9')
    assert plan and plan['type'] == 'beatcut' and plan['timeline'] == [0.0, 2.0, 4.0, 6.0]
    assert prog['plan']['segs'][0]['thumb'].endswith('t0.jpg')
    S.PLANS.pop('run-9', None)


def test_render_plan_beatcut_respects_edits(monkeypatch, tmp_path):
    """渲染阶段：取消勾选的段 → 该处切点被移除，时间线按用户编辑重建。"""
    import webui_server as S
    run_dir = os.path.join(str(tmp_path), 'r2')
    os.makedirs(run_dir, exist_ok=True)
    captured = {}

    def fake_render(v, m, timeline, p, rd, prog=None, diag=None):
        captured['timeline'] = list(timeline)
        fp = os.path.join(rd, 'final.mp4')
        open(fp, 'wb').write(b'x')
        return fp

    monkeypatch.setattr(S, '_render_beatcut', fake_render)
    S.PLANS['run-9'] = {'type': 'beatcut', 'video': 'v.mp4', 'music': 'm.mp3', 'vdur': 6.0,
                        'params': {}, 'diag': {}, 'thumbs': {}}
    prog = {'runid': 'run-10', 'run_dir': run_dir, 'phase': '', 'pct': 0, 'done': False}
    # 用户取消第 2 段（end=4.0）：段1 保留则 2.0 切点保留，4.0 处不切（2-6 合并成一段）
    edits = {'segs': [
        {'start': 0.0, 'end': 2.0, 'on': True},
        {'start': 2.0, 'end': 4.0, 'on': False},
        {'start': 4.0, 'end': 6.0, 'on': True},
    ]}
    S._render_plan_job({'runid': 'run-9', 'edits': edits}, prog)
    assert captured['timeline'] == [0.0, 2.0, 6.0], '取消的切点(4.0)应被移除：%s' % captured['timeline']
    assert prog.get('done') is True and prog.get('file', '').endswith('final.mp4')
    S.PLANS.pop('run-9', None)


def test_render_plan_narrate_edits_captions(monkeypatch, tmp_path):
    """解说渲染：按用户编辑的解说词与保留开关重建 segs/narr。"""
    import webui_server as S
    run_dir = os.path.join(str(tmp_path), 'r3')
    os.makedirs(run_dir, exist_ok=True)
    captured = {}

    def fake_render(v, segs, narr, p, rd, prog=None, music_path=None, mode=None):
        captured['segs'] = list(segs)
        captured['narr'] = list(narr)
        fp = os.path.join(rd, 'final.mp4')
        open(fp, 'wb').write(b'x')
        return fp, 1

    monkeypatch.setattr(S, '_render_narrate', fake_render)
    S.PLANS['run-9'] = {'type': 'narrate', 'video': 'v.mp4', 'segs': [(0, 5), (5, 10)],
                        'narr': ['原词A', '原词B'], 'params': {}, 'music': None, 'diag': {}, 'mode': None}
    prog = {'runid': 'run-11', 'run_dir': run_dir, 'phase': '', 'pct': 0, 'done': False}
    edits = {'segs': [
        {'start': 0.0, 'end': 5.0, 'caption': '改成新词A', 'on': True},
        {'start': 5.0, 'end': 10.0, 'caption': '不要这段', 'on': False},
    ]}
    S._render_plan_job({'runid': 'run-9', 'edits': edits}, prog)
    assert captured['segs'] == [(0.0, 5.0)], '被取消的段应移除：%s' % captured['segs']
    assert captured['narr'] == ['改成新词A'], '解说词应为用户编辑后的：%s' % captured['narr']
    assert prog.get('done') is True
    S.PLANS.pop('run-9', None)


def test_plan_confirm_endpoints_registered():
    """人机协同接口 /api/plan 与 /api/confirm 必须注册在 do_POST。"""
    import webui_server as S, inspect
    src = inspect.getsource(S.Handler.do_POST)
    assert '/api/plan' in src and '/api/confirm' in src


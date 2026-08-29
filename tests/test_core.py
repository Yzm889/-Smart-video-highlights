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


def test_offline_caption_uses_original_name():
    """省流文案对用户可见：必须用原始素材名，不得露出 up_N 内部临时名。"""
    import webui_server as S
    assert 'up_' not in S.offline_caption('我的猫.mp4', 0, 3)
    assert '我的猫' in S.offline_caption('我的猫.mp4', 0, 3)
    assert '内置' not in S.offline_caption('img1.png', 0, 3)  # 内置春景图走固定文案
    assert '春日' in S.offline_caption('img1.png', 0, 3)


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


def test_narrate_analysis_runs_local_asr_in_economy():
    """关键回归：省流模式不得关掉本地 ASR（旧实现 asr=[] 导致套话解说）。
    ASR 调用已抽到公共层 _narrate_analysis（/api/plan 分析与直接生成共用），pin 在那里；
    narrate_video 自身不得再出现条件化禁用 ASR 的写法。"""
    import webui_server as S, inspect
    src = inspect.getsource(S._narrate_analysis)
    assert 'asr = asr_segments(video_path)' in src, '公共分析层必须无条件调用本地 ASR'
    src_nv = inspect.getsource(S.narrate_video)
    assert 'asr_segments(video_path) if' not in src_nv, 'narrate_video 不得条件化禁用 ASR'


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


def test_narrate_movie_auto_routing():
    """回归（自动选路）：联网解说默认免费离线；只有配置了云端 key 才用付费 LLM/配音（填 key = 同意付费）。"""
    import webui_server as S, inspect
    src = inspect.getsource(S.narrate_movie)
    assert "economy=not ai_enabled('chat')" in src, '未配置云端 key 时应走免费离线切句'
    assert 'use_mimo = _tts_available()' in src, '未配置云端 TTS 时应走免费 SAPI 配音'


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
    monkeypatch.setattr(S, 'detect_scene_cuts', lambda v, threshold=0.3, progress=None: [1.0, 3.0, 5.0])
    # 卡点分析现为「一次抽帧」：mock _analyze_video_frames 与从帧信号提取的内部函数
    monkeypatch.setattr(S, '_analyze_video_frames', lambda v, fps_s=4.0, **kw: {})
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


# ---------------------------------------------------------------------------
# 锚点测试：plan_beat_cuts / _segment_timeline 行为基线（重构前先上保险）
# ---------------------------------------------------------------------------
def test_plan_beat_cuts_anchors():
    """锚点：时间线首尾固定 0/vdur；切点吸附强拍（容差内）；距开头不足 min_seg 的切点被滤掉。"""
    import webui_server as S
    beats = [1.0, 2.0, 3.0, 4.0, 5.0]
    tl = S.plan_beat_cuts([1.0, 3.0, 5.0], [], beats, 6.0)
    assert tl[0] == 0.0 and tl[-1] == 6.0
    assert tl[1:-1] == [3.0, 5.0], '1.0 距开头 <min_seg(1.2) 应被滤掉，3/5 吸附强拍原值：%s' % tl
    for c in tl[1:-1]:
        assert any(abs(c - b) <= 0.35 for b in beats), '切点 %s 未吸附强拍' % c


def test_plan_beat_cuts_snaps_motion_to_beat():
    """锚点：动作切点应吸附到最近强拍（1.9 → 2.0）。"""
    import webui_server as S
    tl = S.plan_beat_cuts([], [1.9], [1.0, 2.0, 3.0, 4.0, 5.0], 6.0)
    assert tl[0] == 0.0 and tl[-1] == 6.0
    assert 2.0 in tl, '动作切点 1.9 应吸附强拍 2.0：%s' % tl


def test_plan_beat_cuts_thins_over_quota():
    """锚点：段数超限必须均匀抽稀（保留首尾）；max_cuts 限的是内部切点数，段数 ≤ max_cuts+1。"""
    import webui_server as S
    beats = [i * 0.5 for i in range(1, 36)]
    cuts = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0]
    tl = S.plan_beat_cuts(cuts, [], beats, 17.0, max_cuts=3)
    assert len(tl) - 1 <= 4, '超量必须抽稀：%s' % tl
    assert tl[0] == 0.0 and tl[-1] == 17.0
    assert len(tl) - 1 < len(cuts) + 1, '确实发生了抽稀：%s' % tl


def test_segment_timeline_scene_priority_and_margins(monkeypatch):
    """锚点：解说分段场景切点优先；距开头 <4.0s 的切点被滤掉；结尾保留。"""
    import webui_server as S
    monkeypatch.setattr(S, 'detect_scene_cuts', lambda v, threshold=0.3: [1.0, 4.5, 10.0])
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 12.0)
    segs = S._segment_timeline('fake.mp4')
    # 1.0 距开头 <4.0 被滤；10.0 距片尾 2.0 <3.0 被滤；仅 4.5 保留
    assert segs == [(0.0, 4.5), (4.5, 12.0)], '首尾边距应过滤 1.0 与 10.0：%s' % (segs,)


def test_segment_timeline_even_split_without_cuts(monkeypatch):
    """锚点：无场景切点时按 max_seg 均分。"""
    import webui_server as S
    monkeypatch.setattr(S, 'detect_scene_cuts', lambda v, threshold=0.3: [])
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 50.0)
    segs = S._segment_timeline('fake.mp4', max_seg=25.0)
    assert segs == [(0.0, 25.0), (25.0, 50.0)]


# ---------------------------------------------------------------------------
# 长视频抽帧保护：自适应 fps + 分析帧长边降采样
# ---------------------------------------------------------------------------
def test_scaled_dims_downscales_and_evens():
    """分析帧降采样：1080p → 640x360；竖屏等比；奇数宽高取偶；已达标不放大。"""
    import webui_server as S
    assert S._scaled_dims(1920, 1080) == (640, 360)
    assert S._scaled_dims(1080, 1920) == (360, 640)
    assert S._scaled_dims(640, 360) == (640, 360)
    w, h = S._scaled_dims(1919, 1079)
    assert w % 2 == 0 and h % 2 == 0 and max(w, h) <= 640


def test_adaptive_fps_caps_total_frames():
    """自适应 fps：短视频不降（118s→4.0），长视频封顶总帧数（600s→3.0，1h→1.0 下限）。"""
    import webui_server as S
    assert S._adaptive_fps(118.0) == 4.0
    assert S._adaptive_fps(600.0) == 3.0
    assert S._adaptive_fps(1800.0) == 1.0
    assert S._adaptive_fps(3600.0) == 0.5, '1 小时视频应压到 0.5fps = 1800 帧'
    assert S._adaptive_fps(0) == 4.0 and S._adaptive_fps(None) == 4.0
    assert S._adaptive_fps(100.0, fps_s=2.0) == 2.0, '自定义 fps 只降不升'


def test_analyze_video_frames_keeps_adaptive_pipe():
    """源码 pin：抽帧管道必须带 scale 降采样 + fps 自适应（防回退原生分辨率全片解码）。"""
    import webui_server as S, inspect
    src = inspect.getsource(S._analyze_video_frames)
    assert 'scale=' in src and '_adaptive_fps(' in src and '_scaled_dims(' in src


# ---------------------------------------------------------------------------
# 分析缓存：命中 / 参数失效 / 内容失效 / 小文件跳过 / 接线
# ---------------------------------------------------------------------------
def test_cached_scene_cuts_roundtrip_and_invalidate(monkeypatch, tmp_path):
    """缓存命中（第二次不再检测）；换阈值、改文件内容都应重新分析。"""
    import webui_server as S
    monkeypatch.setattr(S, 'ANALYSIS_CACHE_DIR', str(tmp_path / 'cache'))
    big = tmp_path / 'v.mp4'
    big.write_bytes(b'v' * 8192)
    calls = []

    def fake_detect(v, threshold=0.3):
        calls.append(threshold)
        return [1.0, 2.0]

    monkeypatch.setattr(S, 'detect_scene_cuts', fake_detect)
    assert S._cached_scene_cuts(str(big), threshold=0.25) == [1.0, 2.0]
    assert S._cached_scene_cuts(str(big), threshold=0.25) == [1.0, 2.0]
    assert len(calls) == 1, '第二次应命中缓存，不再调用检测'
    assert S._cached_scene_cuts(str(big), threshold=0.3) == [1.0, 2.0]
    assert len(calls) == 2, '阈值不同应重新分析'
    big.write_bytes(b'w' * 9000)
    assert S._cached_scene_cuts(str(big), threshold=0.25) == [1.0, 2.0]
    assert len(calls) == 3, '文件内容变更（指纹变化）应重新分析'


def test_cached_scene_cuts_skips_small_fake_files(monkeypatch, tmp_path):
    """<4KB 假文件不走缓存：两次调用各自实时分析，杜绝 mock 数据落盘。"""
    import webui_server as S
    monkeypatch.setattr(S, 'ANALYSIS_CACHE_DIR', str(tmp_path / 'cache'))
    tiny = tmp_path / 't.mp4'
    tiny.write_bytes(b'v')
    seq = [[1.0], [2.0]]

    def fake_detect(v, threshold=0.3):
        return seq.pop(0)

    monkeypatch.setattr(S, 'detect_scene_cuts', fake_detect)
    assert S._cached_scene_cuts(str(tiny)) == [1.0]
    assert S._cached_scene_cuts(str(tiny)) == [2.0], '小文件每次都应实时分析'


def test_beatcut_and_segment_wire_through_cache():
    """接线检查：卡点分析与解说分段必须走带缓存入口（共享切点）。"""
    import webui_server as S, inspect
    assert '_cached_scene_cuts(' in inspect.getsource(S._analyze_beatcut)
    assert '_cached_frame_signals(' in inspect.getsource(S._analyze_beatcut)
    assert '_cached_scene_cuts(' in inspect.getsource(S._segment_timeline)


# ---------------------------------------------------------------------------
# 解说字幕对齐修复：环节密度自适应 / 字幕窗口跟随配音 / 台词中点归段
# ---------------------------------------------------------------------------
def test_merge_segs_adaptive_density():
    """解说环节密度：长视频自适应约 10s/环节（比旧固定 6 更密），60s 短视频维持 ≤6，显式 max_keep 仍生效。"""
    import webui_server as S
    segs = [(i * 5.0, (i + 1) * 5.0) for i in range(24)]     # 120s
    merged = S._merge_segs(segs)
    assert len(merged) > 4, '长视频环节应比旧固定 ≤6 更密：%d' % len(merged)
    assert merged[0][0] == 0.0 and merged[-1][1] == 120.0, '合并不得丢时间轴首尾'
    segs60 = [(i * 5.0, (i + 1) * 5.0) for i in range(12)]   # 60s → max_keep=6（与旧行为一致）
    assert len(S._merge_segs(segs60)) <= 6
    assert len(S._merge_segs(segs, max_keep=3)) <= 3, '显式指定 max_keep 必须生效'


def test_compose_srt_follows_voice(monkeypatch, tmp_path):
    """字幕窗口应跟随配音（有声才显字、念完即收），而不是挂满整个镜头段。"""
    import webui_server as S
    monkeypatch.setattr(S, 'ffmpeg_run', lambda args, input_data=None: (0, b'', b''))
    monkeypatch.setattr(S, '_has_audio_track', lambda p: False)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 3.0 if str(p).endswith('.wav') else 20.0)
    wav = tmp_path / 'n0.wav'
    wav.write_bytes(b'w')
    segs = [(0.0, 10.0), (10.0, 20.0)]
    narr = ['第一段解说内容', '']
    # 跟随配音：0~3.35s 显示，配音结束后收字；空解说段不写空字幕
    S._compose_narration_video('v.mp4', segs, narr, [(str(wav), 0.0, 10.0)], str(tmp_path), {},
                               voice_spans={0: (0.0, 3.35)})
    srt = (tmp_path / 'narr.srt').read_text(encoding='utf-8')
    assert '00:00:00,000 --> 00:00:03,350' in srt, '字幕应只显示配音时长窗口：%s' % srt
    assert '00:00:10,000 --> 00:00:20,000' not in srt
    assert srt.count('-->') == 1, '空解说段不应写空字幕'
    # 兼容：不传 voice_spans 时回退整段显示（旧行为）
    S._compose_narration_video('v.mp4', segs, narr, [(str(wav), 0.0, 10.0)], str(tmp_path), {})
    srt2 = (tmp_path / 'narr.srt').read_text(encoding='utf-8')
    assert '00:00:00,000 --> 00:00:10,000' in srt2, '无配音窗口时应回退整段显示：%s' % srt2


def test_generate_narration_asr_midpoint_assignment(monkeypatch):
    """骑跨段边界的台词按中点归到唯一段，不再两头都分不到而丢失。"""
    import webui_server as S
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: False)
    segs = [(0.0, 5.0), (5.0, 10.0)]
    asr = [{'start': 3.5, 'end': 6.5, 'text': '跨段的一句台词'}]
    out, used_local = S.generate_narration(segs, asr, {'economy': True})
    assert used_local is False
    assert out[1] == '跨段的一句台词', '台词中点 5.0 应归到第二段：%s' % out
    assert '镜头缓缓推进' in out[0], '无台词的第一段才回退模板：%s' % out[0]


def test_record_history_writes_entry(monkeypatch, tmp_path):
    """统一历史记录：_record_history 应把成片写入 history.json（此前只有一键合成写）。"""
    import webui_server as S
    outdir = tmp_path / 'out'
    (outdir / 'r1').mkdir(parents=True)
    (outdir / 'r1' / 'final.mp4').write_bytes(b'x')
    monkeypatch.setattr(S, 'OUTDIR', str(outdir))
    monkeypatch.setattr(S, 'HISTORY_PATH', str(tmp_path / 'history.json'))
    S._record_history({'music': {'name': 'm.mp3'}}, {'file': 'r1/final.mp4'}, 'narrate')
    items = S.load_history()
    assert len(items) == 1 and items[0]['file'] == 'r1/final.mp4'
    assert items[0]['kind'] == 'narrate' and items[0]['music'] == 'm.mp3'
    assert items[0]['duration'] == 0.0  # 假文件探测不到时长，兜底 0
    # 无成片（如联网解说只出稿）不应写历史
    S._record_history({}, {'file': ''}, 'movie')
    assert len(S.load_history()) == 1


def test_movie_narration_subtitles_follow_voice():
    """源码 pin：联网解说配音也必须生成 voice_spans（字幕跟随配音），与短片解说一致。"""
    import webui_server as S, inspect
    src = inspect.getsource(S.narrate_movie)
    assert 'voice_spans' in src


def test_beat_engines_share_onset_detection():
    """源码 pin：强卡点与节拍同步两引擎必须共用同一 librosa onset 检测层（防各自漂移）。"""
    import webui_server as S, inspect
    assert '_music_onset_peaks(' in inspect.getsource(S.detect_strong_beats)
    assert '_music_onset_peaks(' in inspect.getsource(S.detect_beats)


def test_run_dir_not_reused_after_restart():
    """源码 pin：run 目录名必须带时间戳——服务重启后 RUNSEQ 归零，
    若复用 run-N 目录会用新成片覆盖旧成片，历史条目随之指向错误文件。"""
    import webui_server as S, inspect
    src = inspect.getsource(S.Handler._spawn)
    assert 'strftime' in src, 'run_dir 应包含时间戳：%s' % src[:200]


# ---------------------------------------------------------------------------
# 第六轮：服务端健壮性（路径穿越 / 历史写锁 / 内存修剪）
# ---------------------------------------------------------------------------
def test_safe_join_blocks_traversal(tmp_path):
    """/media 与 /music_lib 的路径安全：穿越路径必须被拒，正常路径可用。"""
    import webui_server as S
    base = tmp_path / 'pub'
    (base / 'sub').mkdir(parents=True)
    (base / 'a.txt').write_text('x')
    (base / 'sub' / 'b.txt').write_text('y')
    (tmp_path / 'secret.txt').write_text('k')
    assert S._safe_join(str(base), 'a.txt') == str(base / 'a.txt')
    assert S._safe_join(str(base), 'sub/b.txt') == str(base / 'sub' / 'b.txt')
    assert S._safe_join(str(base), '../secret.txt') is None, '穿越必须被拒'
    assert S._safe_join(str(base), 'sub/../../secret.txt') is None
    assert S._safe_join(str(base), 'C:/Windows/win.ini') is None
    assert S._safe_join(str(base), 'missing.txt') is None


def test_add_history_concurrent_writes_no_loss(monkeypatch, tmp_path):
    """历史写锁：并发任务同时完成、同时写 history.json 时不得丢条目。"""
    import webui_server as S, threading
    monkeypatch.setattr(S, 'HISTORY_PATH', str(tmp_path / 'history.json'))

    def w(i):
        S.add_history({'time': 't', 'file': 'f%d.mp4' % i, 'duration': 1.0})

    ts = [threading.Thread(target=w, args=(i,)) for i in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(S.load_history()) == 20, '并发写不得丢条目（需持锁）'


def test_progress_and_plans_pruned():
    """源码 pin：_spawn 必须修剪 PROGRESS（只增不减会泄漏），方案表必须限量。"""
    import webui_server as S, inspect
    assert 'PROGRESS.pop' in inspect.getsource(S.Handler._spawn)
    assert 'PLANS.pop' in inspect.getsource(S._analyze_plan_job)


def test_read_json_drains_and_joins():
    """源码 pin：超限请求体必须排空后再返回 None（否则客户端收到 10053 断连
    而不是「请求过大」）；分块读取必须 join 拼接（raw += 是 O(n²) 拷贝）。"""
    import webui_server as S, inspect
    src = inspect.getsource(S.Handler._read_json)
    assert 'max_len' in src and 'return None' in src, '超限应排空后返回 None'
    assert "b''.join(parts)" in src, '应使用 join 拼接'


def test_cooperative_abort(monkeypatch):
    """协作式取消：任务线程在解说稿生成/配音等无 ffmpeg 的长阶段也能感知取消标志。"""
    import webui_server as S
    monkeypatch.setattr(S._TLS, 'runid', 'run-abort', raising=False)
    S.PROGRESS['run-abort'] = {'abort': True}
    assert S._aborted() is True
    S.PROGRESS['run-abort'] = {}
    assert S._aborted() is False
    S.PROGRESS.pop('run-abort', None)
    assert S._aborted() is False
    monkeypatch.setattr(S._TLS, 'runid', None, raising=False)
    assert S._aborted() is False, '无 runid 绑定的线程恒为未取消'


def test_abort_checks_wired():
    """源码 pin：解说稿生成（LLM 调用之间）与逐段渲染必须在阶段间检查协作式取消。"""
    import webui_server as S, inspect
    assert '_aborted()' in inspect.getsource(S.local_vlm_narrate)
    assert '_aborted()' in inspect.getsource(S._render_narrate)
    assert '_aborted()' in inspect.getsource(S._render_beatcut)


def test_parse_instruction_routing():
    """指令路由：解说/卡点/联网片名/默认合成 四类 + 模式与分辨率关键词。"""
    import webui_server as S
    assert S.parse_instruction('帮我解说这段视频')['action'] == 'narrate'
    assert S.parse_instruction('把这段视频剪成强卡点短片')['action'] == 'beatcut'
    r3 = S.parse_instruction('解说《盗梦空间》并配乐')
    assert r3['action'] == 'movie' and r3['movie'] == '盗梦空间', '片名优先走联网解说：%s' % r3
    assert S.parse_instruction('用这些图合成一个短视频')['action'] == 'compose'
    r5 = S.parse_instruction('解说这段视频 省流 竖屏')
    assert r5['params'].get('economy') is True and r5['params'].get('w') == 1080
    assert S.parse_instruction('解说《沙丘》 真ai')['params'].get('economy') is False


def test_narrate_flow_deduplicated():
    """源码 pin：plan 分析与直接生成必须共用同一解说分析层（防两份流程漂移）。"""
    import webui_server as S, inspect
    assert '_narrate_analysis(' in inspect.getsource(S._analyze_narrate)
    assert '_narrate_analysis(' in inspect.getsource(S.narrate_video)


def test_segment_timeline_clamps_max_seg(monkeypatch):
    """maxSeg 服务端钳制 4~600s：API/指令路径可绕过前端 min=8，极端值会切出海量碎段。"""
    import webui_server as S
    monkeypatch.setattr(S, 'detect_scene_cuts', lambda v, threshold=0.3: [])
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 40.0)
    segs = S._segment_timeline('fake.mp4', max_seg=0.5)
    assert len(segs) <= 10, '极端 max_seg 必须被钳制（40s/4s=10 段）：%d' % len(segs)
    segs2 = S._segment_timeline('fake.mp4', max_seg=None)
    assert segs2, 'None 应回落默认 25s 均分'


def test_beatcut_max_cuts_clamped():
    """源码 pin：maxCuts 服务端钳制 3~96（API/指令路径可绕过前端滑条范围）。"""
    import webui_server as S, inspect
    src = inspect.getsource(S._analyze_beatcut)
    assert 'min(96' in src, 'maxCuts 应有服务端钳制：%s' % src[:300]


def test_concurrent_cache_writes_safe(monkeypatch, tmp_path):
    """并发分析同一视频：多线程同时写缓存不得产生半写文件或坏数据（os.replace 原子替换）。"""
    import webui_server as S, threading, time, os
    monkeypatch.setattr(S, 'ANALYSIS_CACHE_DIR', str(tmp_path / 'cache'))
    f = tmp_path / 'v.mp4'
    f.write_bytes(b'v' * 8192)

    def slow_detect(v, threshold=0.3):
        time.sleep(0.2)   # 制造并发窗口：10 个线程几乎同时算完并落盘
        return [1.0, 2.0]

    monkeypatch.setattr(S, 'detect_scene_cuts', slow_detect)
    results = []

    def run():
        results.append(S._cached_scene_cuts(str(f)))

    ts = [threading.Thread(target=run) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert all(r == [1.0, 2.0] for r in results), '并发读到的缓存必须完整一致'
    leftovers = [fn for fn in os.listdir(str(tmp_path / 'cache')) if fn.endswith('.tmp')]
    assert not leftovers, '不得残留 .tmp 半写文件'


def test_no_shell_injection_surface():
    """安全回归：全项目禁止 shell=True；模型拉取的 cmd /c 参数必须来自
    FAST_GGUF_SOURCES 白名单（模型名可来自用户输入框，绝不能拼进 shell 字符串）。"""
    import webui_server as S, inspect
    src = inspect.getsource(S)
    assert 'shell=True' not in src, '不得使用 shell=True（命令注入面）'
    fast_src = inspect.getsource(S._fast_pull_local)
    assert fast_src.index('FAST_GGUF_SOURCES.get(model)') < fast_src.index("'cmd', '/c'"), \
        'cmd /c 必须在白名单校验之后（任意 model 名不得进入命令）'


def test_upload_chunk_and_finalize(monkeypatch, tmp_path):
    """分片上传：乱序写片→按序合并；非法 upload_id/单片超限/缺片必须被拒。"""
    import webui_server as S, os
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    uid = 'up-123-abc'
    assert S._upload_dir_of(uid) == str(tmp_path / 'uploads' / uid)
    assert S._upload_dir_of('../evil') is None, '穿越 id 必须被拒'
    assert S._upload_dir_of('up-x/y') is None
    ok, err = S._upload_chunk_write(uid, 0, b'a')
    assert not ok and '不存在' in err, '会话未初始化时拒写'
    os.makedirs(str(tmp_path / 'uploads' / uid))
    assert S._upload_chunk_write(uid, 1, b'bb')[0] is True   # 乱序：先写 idx=1
    assert S._upload_chunk_write(uid, 0, b'aa')[0] is True
    assert not S._upload_chunk_write(uid, 2, b'x' * (S.UPLOAD_CHUNK_MAX + 1))[0], '单片超限必须拒绝'
    final, err = S._upload_finalize(uid, '我的视频.mp4', 2)
    assert final and os.path.isfile(final) and open(final, 'rb').read() == b'aabb', '乱序分片须按序合并'
    assert not any(fn.startswith('part_') for fn in os.listdir(str(tmp_path / 'uploads' / uid))), '合并后应清理分片'
    assert S._upload_finalize(uid, 'x.mp4', 3)[0] is None, '缺片必须拒绝'
    assert S._upload_finalize(uid, 'x.mp4', 0)[0] is None, '非法分片数必须拒绝'


def test_resolve_upload_video_moves_final(monkeypatch, tmp_path):
    """任务取走分片成品用 move 免二次拷贝；无效会话返回 None。"""
    import webui_server as S, os
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    run_dir = str(tmp_path / 'run')
    os.makedirs(run_dir)
    assert S._resolve_upload_video({'name': 'v.mp4', 'upload_id': 'up-1-a'}, run_dir) is None, '无效会话须为 None'
    d = str(tmp_path / 'uploads' / 'up-1-a')
    os.makedirs(d)
    final = os.path.join(d, 'final__v.mp4')
    open(final, 'wb').write(b'vv')
    fp = S._resolve_upload_video({'name': 'v.mp4', 'upload_id': 'up-1-a'}, run_dir)
    assert fp and open(fp, 'rb').read() == b'vv'
    assert not os.path.exists(final), '成品应被移走（免二次拷贝）'


def test_upload_resume_have_parts(monkeypatch, tmp_path):
    """断点续传：init 能拿到已到齐分片的升序列表；会话过期返回 None；续传补齐后可正常合并。"""
    import webui_server as S, os
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    assert S._upload_have_parts('up-1-a') is None, '会话不存在应返回 None'
    d = str(tmp_path / 'uploads' / 'up-1-a')
    os.makedirs(d)
    assert S._upload_have_parts('up-1-a') == []
    for i in (2, 0, 1):   # 乱序落盘
        open(os.path.join(d, 'part_%04d' % i), 'wb').write(b'x')
    assert S._upload_have_parts('up-1-a') == [0, 1, 2], '乱序分片也按升序返回'
    open(os.path.join(d, 'part_0003'), 'wb').write(b'y')
    final, err = S._upload_finalize('up-1-a', 'v.mp4', 4)
    assert final and err == '', '续传补齐剩余分片后应可正常合并'


def test_build_items_support_chunked_video():
    """源码 pin：一键合成的视频素材支持分片上传（upload_id），大素材不再受 150MB 限制。"""
    import webui_server as S, inspect
    assert '_resolve_upload_video(' in inspect.getsource(S.dispatch_build)


# ---------------------------------------------------------------------------
# B 站素材：BV 号校验 / 文件名消毒 / 搜索结果归一化
# ---------------------------------------------------------------------------
def test_bili_valid_bvid():
    """BV 号校验：BV + 10 位字母数字；拒绝 av 号/路径注入/空值。"""
    import webui_server as S
    assert S._bili_valid_bvid('BV1bB8Y6nEaK') is True
    assert S._bili_valid_bvid('BV1GJ411x7h7') is True
    assert S._bili_valid_bvid('av1706621248') is False
    assert S._bili_valid_bvid('BV123') is False
    assert S._bili_valid_bvid('../../etc') is False
    assert S._bili_valid_bvid('') is False
    assert S._bili_valid_bvid(None) is False


def test_bili_safe_filename():
    """下载文件名消毒：非法字符转下划线，长度受限。"""
    import webui_server as S
    assert S._safe_filename('BV1abc.mp4') == 'BV1abc.mp4'
    assert '/' not in S._safe_filename('a/b\\c:d*e?"<>|')
    assert len(S._safe_filename('x' * 500)) <= 120


def test_bili_search_normalization(monkeypatch):
    """搜索结果归一化：从 yt-dlp 的条目结构提取 bvid/标题/作者/时长/封面。"""
    import webui_server as S

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            assert url.startswith('bilisearch'), '应使用 bilisearch 搜索'
            return {'entries': [
                {'id': 'BV1abc', 'title': '测试视频', 'uploader': 'UP主', 'duration': 61.4,
                 'thumbnails': [{'url': 'http://x/1.jpg'}]},
                None,                                   # 空条目应被跳过
                {'id': 'BV2def'},                        # 缺字段也应尽量归一化
            ]}

    import sys, types
    fake = types.ModuleType('yt_dlp')
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, 'yt_dlp', fake)
    monkeypatch.setattr(S, '_bili_cookiefile', lambda: 'cookies.txt')
    res = S.bili_search('美食', 2)
    assert res[0] == {'bvid': 'BV1abc', 'title': '测试视频', 'author': 'UP主',
                      'duration': 61, 'pic': 'http://x/1.jpg'}
    assert res[1]['bvid'] == 'BV2def' and res[1]['duration'] == 0


def test_bili_cfg_defaults():
    """bili.cookie 缺省为空字符串（自动收割 buvid3）。"""
    import webui_server as S
    assert S.bili_cfg() == {'cookie': ''}


# ---------------------------------------------------------------------------
# 封面生成：选帧打分 / 候选抽帧 / 渲染
# ---------------------------------------------------------------------------
def test_cover_score_prefers_detailed_frame():
    """封面选帧打分：细节丰富的帧得分应高于纯色/全黑帧。"""
    from PIL import Image
    import webui_server as S
    import numpy as np
    flat = Image.new('RGB', (160, 90), (200, 200, 200))
    black = Image.new('RGB', (160, 90), (0, 0, 0))
    noise = Image.fromarray((np.random.rand(90, 160, 3) * 255).astype('uint8'))
    assert S._cover_score(noise) > S._cover_score(flat), '细节帧应优于纯色帧'
    assert S._cover_score(noise) > S._cover_score(black), '细节帧应优于全黑帧'


def test_cover_candidates_and_render(monkeypatch, tmp_path):
    """候选抽帧（mock ffmpeg 写真实小图）+ 按设置渲染出封面文件。"""
    import webui_server as S, os
    from PIL import Image
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'v')
    run_dir = str(tmp_path / 'run')
    os.makedirs(run_dir)
    monkeypatch.setattr(S, 'probe_audio_len', lambda p: 40.0)

    def fake_ff(args, input_data=None):
        out = args[args.index('-an') + 1]   # 两条命令的目标路径都紧跟在 -an 之后
        Image.new('RGB', (320, 180), (120, 60, 30)).save(out, quality=85)
        return 0, b'', b''

    monkeypatch.setattr(S, 'ffmpeg_run', fake_ff)
    cands = S._cover_candidates(str(video), run_dir, n=4)
    assert len(cands) == 4 and all(0 < c['ts'] < 40 for c in cands)
    assert all(os.path.isfile(os.path.join(run_dir, 'cover_cand', os.path.basename(c['thumb']))) for c in cands)
    cover = os.path.join(run_dir, 'cover.jpg')
    S._cover_render(str(video), cands[0]['ts'], '测试标题很长很长很长需要自动换行的超长内容', '副标题', 1, cover)
    assert os.path.isfile(cover) and os.path.getsize(cover) > 1000, '应产出封面图'
    S._cover_render(str(video), cands[1]['ts'], '居中标题', '', 0, cover)
    assert os.path.getsize(cover) > 1000


def test_cover_endpoint_registered():
    """/api/cover 必须注册在 do_POST。"""
    import webui_server as S, inspect
    assert '/api/cover' in inspect.getsource(S.Handler.do_POST)


def test_upload_prune_caps_sessions(monkeypatch, tmp_path):
    """会话数量上限：活跃会话超过 100 个时按最旧清理（防 LAN 暴露下的数量滥用）。"""
    import webui_server as S, os, time
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    os.makedirs(str(tmp_path / 'uploads'))
    for i in range(103):
        d = os.path.join(str(tmp_path / 'uploads'), 'up-%03d' % i)
        os.makedirs(d)
        open(os.path.join(d, 'part_0000'), 'wb').write(b'x')
        old = time.time() - 3600 * (i + 1)   # 递减 mtime：up-000 最新、up-102 最旧
        os.utime(d, (old, old))
    S._upload_prune()
    left = os.listdir(str(tmp_path / 'uploads'))
    assert len(left) <= 100, '应清最旧至 ≤100：%d' % len(left)
    assert 'up-000' in left, '最新会话应保留'
    assert 'up-102' not in left, '最旧会话应被清'

# ---------------------------------------------------------------------------
# 本地素材库：保存/列表/删除/去重 + 任务引用 mlib
# ---------------------------------------------------------------------------
def test_material_roundtrip_and_dedupe(monkeypatch, tmp_path):
    """素材库：字节保存→列表→重名自动加序号→删除。"""
    import webui_server as S, os
    monkeypatch.setattr(S, 'MATERIAL_DIR', str(tmp_path / 'material_library'))
    n1, err = S.material_save_bytes('我的视频.mp4', b'vvv')
    assert not err and n1 == '我的视频.mp4'
    n2, _ = S.material_save_bytes('我的视频.mp4', b'zzz')
    assert n2 == '我的视频(1).mp4', '重名应自动加序号'
    items = S.material_list()
    assert len(items) == 2 and all(i['kind'] == 'video' for i in items)
    ok, _ = S.material_delete('我的视频.mp4')
    assert ok and len(S.material_list()) == 1
    ok, err = S.material_delete('不存在.mp4')
    assert not ok


def test_material_path_blocks_traversal(monkeypatch, tmp_path):
    """素材路径防穿越：../ 与绝对路径必须被拒。"""
    import webui_server as S
    monkeypatch.setattr(S, 'MATERIAL_DIR', str(tmp_path / 'material_library'))
    assert S._material_path('../ai_config.json') is None
    assert S._material_path('C:/Windows/win.ini') is None


def test_resolve_video_mlib_copies_from_library(monkeypatch, tmp_path):
    """任务引用素材库（mlib）：应从库中 copy 进 run_dir（库内文件保留）。"""
    import webui_server as S, os
    mdir = str(tmp_path / 'material_library')
    os.makedirs(mdir)
    monkeypatch.setattr(S, 'MATERIAL_DIR', mdir)
    open(os.path.join(mdir, 'v.mp4'), 'wb').write(b'vv')
    run_dir = str(tmp_path / 'run')
    os.makedirs(run_dir)
    fp = S._resolve_upload_video({'name': 'v.mp4', 'mlib': 'v.mp4'}, run_dir)
    assert fp and open(fp, 'rb').read() == b'vv'
    assert os.path.isfile(os.path.join(mdir, 'v.mp4')), '素材库文件必须保留'


def test_build_supports_mlib_items():
    """源码 pin：一键合成的图片/视频素材都支持素材库引用（mlib）。"""
    import webui_server as S, inspect
    assert 'mlib' in inspect.getsource(S.dispatch_build)

# ---------------------------------------------------------------------------
# 长视频分析预览修复：plan 任务必须支持分片上传/素材库引用的视频
# ---------------------------------------------------------------------------
def test_analyze_plan_accepts_upload_id_video(monkeypatch, tmp_path):
    """回归：分析预览（/api/plan）必须支持分片上传（upload_id）的视频——
    此前只认 base64，长视频（>64MB 自动走分片）分析预览报「请先上传视频」。"""
    import webui_server as S, os
    updir = str(tmp_path / 'uploads' / 'up-1-a')
    os.makedirs(updir)
    open(os.path.join(updir, 'final__v.mp4'), 'wb').write(b'vv')
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(S, '_analyze_beatcut',
                        lambda v, m, p, prog=None: ([0.0, 2.0, 6.0], {'scene_cuts': [2.0]}, 6.0))
    monkeypatch.setattr(S, '_plan_thumbs', lambda v, segs, rd: {})
    monkeypatch.setattr(S, '_resolve_music', lambda m: 'm.mp3')
    run_dir = str(tmp_path / 'run')
    os.makedirs(run_dir)
    prog = {'runid': 'r1', 'run_dir': run_dir, 'phase': '', 'pct': 0, 'done': False}
    S._analyze_plan_job({'type': 'beatcut', 'params': {},
                         'video': {'name': 'v.mp4', 'upload_id': 'up-1-a'}}, prog)
    assert prog.get('plan_ready') is True, '分片上传的视频应能完成分析'
    assert os.path.isfile(os.path.join(run_dir, 'src_video.mp4')), '应把上传成品落盘到 run_dir'
    assert prog.get('plan', {}).get('type') == 'beatcut'


def test_analyze_plan_accepts_mlib_video(monkeypatch, tmp_path):
    """分析预览同样支持素材库引用（mlib）的视频。"""
    import webui_server as S, os
    mdir = str(tmp_path / 'material_library')
    os.makedirs(mdir)
    open(os.path.join(mdir, 'v.mp4'), 'wb').write(b'vv')
    monkeypatch.setattr(S, 'MATERIAL_DIR', mdir)
    monkeypatch.setattr(S, '_analyze_narrate',
                        lambda v, p, rd, prog=None: ([(0.0, 6.0)], ['n'], [], {}, None))
    monkeypatch.setattr(S, '_plan_thumbs', lambda v, segs, rd: {})
    monkeypatch.setattr(S, '_resolve_music', lambda m: None)
    run_dir = str(tmp_path / 'run')
    os.makedirs(run_dir)
    prog = {'runid': 'r2', 'run_dir': run_dir, 'phase': '', 'pct': 0, 'done': False}
    S._analyze_plan_job({'type': 'narrate', 'params': {}, 'video': {'name': 'v.mp4', 'mlib': 'v.mp4'}}, prog)
    assert prog.get('plan_ready') is True, '素材库引用的视频应能完成解说分析'


def test_parse_time_str():
    """ffmpeg time= 统计行解析：HH:MM:SS.cc → 秒。"""
    import webui_server as S
    assert S._parse_time_str('00:01:23.45') == 83.45
    assert S._parse_time_str('01:00:00') == 3600.0
    assert S._parse_time_str('bad') is None


# ---------------------------------------------------------------------------
# 视频编码器选择：GPU 硬编(h264_nvenc) 优先、不可用时回退 CPU 软编(libx264)
# ---------------------------------------------------------------------------
def _write_video_cfg(S, encoder):
    """把编码策略写入被 conftest 隔离到 tmp 的 ai_config.json。"""
    import json
    with open(S.AI_CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump({'video': {'encoder': encoder}}, f)


def test_video_encoder_cfg_defaults_to_auto():
    import webui_server as S
    _write_video_cfg(S, '')
    assert S.video_encoder_cfg() == 'auto'
    _write_video_cfg(S, 'gpu')
    assert S.video_encoder_cfg() == 'gpu'
    _write_video_cfg(S, '不存在的策略')
    assert S.video_encoder_cfg() == 'auto', '非法值必须回退 auto，不能让下游拿到脏值'


def test_video_encode_args_cpu_mode_uses_libx264():
    import webui_server as S
    _write_video_cfg(S, 'cpu')
    args = S.video_encode_args(20)
    assert args[0:2] == ['-c:v', 'libx264']
    assert 'veryfast' in args
    assert args[args.index('-crf') + 1] == '20'


def test_video_encode_args_gpu_when_usable(monkeypatch):
    import webui_server as S
    _write_video_cfg(S, 'auto')
    monkeypatch.setattr(S, '_nvenc_usable', lambda: True)
    args = S.video_encode_args(20)
    assert args[0:2] == ['-c:v', 'h264_nvenc']
    assert args[args.index('-qp') + 1] == '20'


def test_video_encode_args_falls_back_when_gpu_unusable(monkeypatch):
    """强制 GPU 但本机 nvenc 不可用时必须回退 CPU，绝不能让整条流水线崩掉。"""
    import webui_server as S
    _write_video_cfg(S, 'gpu')
    monkeypatch.setattr(S, '_nvenc_usable', lambda: False)
    args = S.video_encode_args()
    assert args[0:2] == ['-c:v', 'libx264']


def test_video_encode_args_auto_without_gpu(monkeypatch):
    import webui_server as S
    _write_video_cfg(S, 'auto')
    monkeypatch.setattr(S, '_nvenc_usable', lambda: False)
    assert S.video_encode_args()[1] == 'libx264'


def test_encoder_preset_unified_source_pin():
    """源码 pin：8 处编码点统一走 video_encode_args，旧的不一致 preset 'fast' 不得复活。"""
    import webui_server as S
    src = open(os.path.join(os.path.dirname(os.path.abspath(S.__file__)),
                            'webui_server.py'), encoding='utf-8').read()
    assert "'fast'" not in src, "发现旧的 'fast' preset，8 处编码应统一为 veryfast/GPU 档位"
    assert src.count('video_encode_args(') >= 9, '应为 1 处定义 + 8 处调用'


# ---------------------------------------------------------------------------
# 解说稿「行 → 镜头」映射：模型把整稿写成一整段时，不得塌缩成「整片只有一句」
# ---------------------------------------------------------------------------
def test_map_lines_one_paragraph_not_collapsed():
    """回归：模型输出单段落（无换行）但句数够时，应拆成各镜头独立的句子，不能全等。"""
    import webui_server as S
    one_para = ('这位年轻人走进便利店，走到一台自动售货机前，想要买一瓶汽水，但是却发现这台机器只收港币。'
                '此时，年轻人身上的钱却都不是港币，他有些焦急地看向旁边。'
                '旁边的大叔看他着急，便热心地帮他换了一些港币。'
                '拿到港币后，年轻人赶紧投币，终于买到了汽水。'
                '他开心地打开汽水喝了起来。')
    mapped = S._map_lines_to_segs(S._split_nar_lines(one_para), 5)
    assert len(mapped) == 5
    assert len(set(mapped)) == 5, '解说塌缩：5 个镜头拿到相同内容（整片只剩一句）'


def test_map_lines_short_paragraph_no_midword_split():
    """句数不足时按小句拆，但只能在标点处断开——不得把「便利店」劈成「便利」+「店想」。"""
    import webui_server as S
    text = '男子走进便利店想买汽水，掏出人民币却被机器拒收。无奈之下他向路人求助，折腾半天才买到。'
    mapped = S._map_lines_to_segs(S._split_nar_lines(text), 5)
    assert len(set(mapped)) > 1, '短段落也不应全部塌缩成一句'
    # 不变量：每个片段都必须以标点结尾。硬切字符会产生「男子走进便利」这种不以标点收尾的半截词。
    for chunk in mapped:
        assert chunk[-1] in '。！？!?，,；;、', '出现从词中间硬切的碎片（不以标点结尾）：%r' % chunk


def test_map_lines_exact_and_overflow():
    import webui_server as S
    lines = ['甲。', '乙。', '丙。']
    assert S._map_lines_to_segs(lines, 3) == lines
    assert len(S._map_lines_to_segs(lines, 5)) == 5
    assert S._map_lines_to_segs([], 4) == ['', '', '', '']


def test_split_nar_clauses_and_into_k():
    import webui_server as S
    cl = S._split_nar_clauses('他走进店里，掏出钱，却发现机器不收。')
    assert len(cl) >= 2 and cl[0].endswith('，')
    k = S._split_into_k('一二三四五六七八九十', 3)
    assert len(k) == 3 and ''.join(k) == '一二三四五六七八九十'


# ---------------------------------------------------------------------------
# 解说词驱动的分镜重匹配（/api/narrate/align）
# ---------------------------------------------------------------------------
def _shots(n, dur=3.0):
    return [(i * dur, i * dur + dur) for i in range(n)]


def test_algo_align_covers_all_and_contiguous():
    import webui_server as S
    segs, src = S._align_shots_to_lines(_shots(12), ['甲。', '乙。', '丙。', '丁。', '戊。'],
                                        None, None, use_model=False)
    assert src == 'algo' and len(segs) == 5
    assert segs[0][0] == 0.0 and abs(segs[-1][1] - 36.0) < 1e-6
    for i in range(len(segs) - 1):
        assert abs(segs[i][1] - segs[i + 1][0]) < 1e-6, '分镜必须连续覆盖，不能有空洞或重叠'


def test_align_more_lines_than_shots_no_crash():
    """回归：解说句数 > 候选镜头数时曾触发 IndexError（每句至少 1 镜头无解）。"""
    import webui_server as S
    segs, src = S._align_shots_to_lines(_shots(3, 4.0), ['甲。', '乙。', '丙。', '丁。', '戊。'],
                                        None, None, use_model=False)
    assert len(segs) == 5, '句数多于镜头时应先细分镜头，保证每句都分到画面'
    assert abs(segs[-1][1] - 12.0) < 1e-6


def test_expand_shots_only_when_needed():
    import webui_server as S
    s = _shots(4)
    assert len(S._expand_shots(s, 2)) == 4, '镜头够用时不细分'
    assert len(S._expand_shots(s, 9)) >= 9, '镜头不够时应细分到至少够分'
    assert S._expand_shots([], 5) == []


def test_model_align_parses_valid_json(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(S, '_llm_text', lambda *a, **k: '[3, 6, 9, 12]')
    assert S._model_align_shots(_shots(12), ['甲。', '乙。', '丙。', '丁。']) == [3, 6, 9, 12]


def test_model_align_rejects_bad_output(monkeypatch):
    """模型输出不合法时必须返回 None，由调用方回退算法，而不是产出错乱分镜。"""
    import webui_server as S
    lines = ['甲。', '乙。', '丙。', '丁。']
    for bad in ['这不是JSON', '[3, 6]', '[]', '[9, 3, 6, 12]', '', None]:
        monkeypatch.setattr(S, '_llm_text', lambda *a, **k: bad)
        assert S._model_align_shots(_shots(12), lines) is None, '应拒绝非法输出: %r' % bad


def test_align_falls_back_to_algo_when_model_unavailable(monkeypatch):
    import webui_server as S
    monkeypatch.setattr(S, '_llm_text', lambda *a, **k: None)
    segs, src = S._align_shots_to_lines(_shots(12), ['甲。', '乙。', '丙。'])
    assert src == 'algo' and len(segs) == 3

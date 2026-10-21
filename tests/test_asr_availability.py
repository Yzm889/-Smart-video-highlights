# -*- coding: utf-8 -*-
"""P0-1 回归：ASR 真实可用性（修复 av/PyAV 不兼容 + 静默失败）。

背景（真机全链路 P0-1 实测发现）：
  1. faster_whisper 1.2.1 内部 decode_audio 调 av.open(..., metadata_errors='ignore')，
     而 PyAV>=15 已移除该关键字（本机 av19.0.1），转写必抛
     `TypeError: open() got an unexpected keyword argument 'metadata_errors'`。
  2. asr_segments 用裸 `except Exception: return []` 把它吞掉，
     上层只看到「ASR 没结果」——用户完全无从判断是环境坏了还是片子没对白。
     31 段的分析结果草稿全空、default_keep 全 False，编辑台直接空转。

这里锁定三条：
  A. _load_wav_as_float32 能读标准 16bit/单声道 wav，且完全不碰 av；
  B. asr_segments 失败时把真实异常写进 PROGRESS['warnings']（不再静默）；
  C. _cuda_available 在「有驱动但无 CUDA 运行时库」时必须返回 False，
     避免 GPU 推理必然崩溃却被误判为可用。
"""
import os
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui_server as S  # noqa: E402  （必须先导入宿主，模块晚绑定其符号）
import ai_providers as P  # noqa: E402


def _make_wav(path, seconds=1.0, rate=16000):
    import struct
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        n = int(rate * seconds)
        # 简单斜坡，够解码用
        wf.writeframes(b''.join(struct.pack('<h', int(3000 * (i / n) - 1500))
                               for i in range(n)))
    return path


def test_load_wav_as_float32_does_not_need_av(tmp_path):
    """A. 读 wav 不依赖 PyAV——这正是原缺陷的根。"""
    p = _make_wav(str(tmp_path / 'a.wav'))
    audio = P._load_wav_as_float32(p)
    assert audio.dtype.name == 'float32'
    assert audio.shape == (16000,)
    # 幅度应在 [-1, 1]
    assert float(abs(audio).max()) <= 1.0


def test_load_wav_rejects_non_pcm16_mono(tmp_path):
    """A2. 非 16bit/单声道输入必须明确报错，而不是悄悄给出错数据。"""
    p = str(tmp_path / 'stereo.wav')
    with wave.open(p, 'wb') as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b'\x00\x01' * 2000)
    with pytest.raises(RuntimeError):
        P._load_wav_as_float32(p)


def test_asr_failure_is_reported_not_silent(monkeypatch, tmp_path):
    """B. 转写抛异常时，真实原因必须进当前任务的 warnings。"""
    from ffmpeg_utils import PROGRESS, _TLS
    rid = 'test-asr-warn'
    PROGRESS[rid] = {}
    monkeypatch.setattr(_TLS, 'runid', rid, raising=False)

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

    monkeypatch.setitem(sys.modules, 'faster_whisper',
                        type(sys)('faster_whisper'))
    sys.modules['faster_whisper'].WhisperModel = _Boom

    # 跳过 ffmpeg 解码这一步（用真wav 省时间），直接测异常路径
    def _fake_run(args, **kw):
        out = args[-1]
        _make_wav(out)
        return 0, '', ''
    monkeypatch.setattr(P, 'ffmpeg_run', _fake_run)
    monkeypatch.setattr(P, '_whisper_env_setup', lambda: None)
    monkeypatch.setattr(P, 'whisper_device', lambda: ('cpu', 'int8'))

    try:
        segs = P.asr_segments(str(tmp_path / 'x.mp4'), progress=PROGRESS[rid])
        warns = PROGRESS[rid].get('warnings') or []
        assert segs == []
        assert warns, 'ASR 失败必须留下 warning，不能静默返回空'
        assert any('metadata_errors' in w for w in warns), \
            'warning 里应能看到真实异常原因，实际: %s' % warns
    finally:
        PROGRESS.pop(rid, None)


def test_cuda_requires_runtime_not_just_driver(monkeypatch):
    """C. 有nvidia-smi 但无 CUDA 库时，必须判为不可用。"""
    monkeypatch.setitem(P._CUDA_CACHE, 'val', None)  # 清缓存
    monkeypatch.setattr(P, '_cuda_runtime_present', lambda: False)
    assert P._cuda_available() is False, \
        '缺 CUDA 运行时库时若仍判可用，GPU 推理必崩（cublas64_12.dll not found）'
    monkeypatch.setitem(P._CUDA_CACHE, 'val', None)


def test_cuda_runtime_present_is_pure_probe():
    """C2. 探测函数本身不抛异常（缺 ctranslate2 / 缺 dll 时返回 False）。"""
    try:
        v = P._cuda_runtime_present()
        assert isinstance(v, bool)
    except Exception as ex:  # pragma: no cover
        pytest.fail('CUDA 运行时探测不应抛异常: %s' % ex)


def test_llm_payload_disables_thinking():
    """D. 请求体必须显式关闭思考。

    Qwen3 默认开 thinking，max_tokens 是思考+正文共享配额。实测 14b-q4_K_M
    一次简单问答就用掉 492 字思考 token，1500 配额下写解说稿正文直接被挤成
    空串 → 阶段①「llm_empty」→ 所有段落在编辑台无草稿可编辑。
    """
    payload = P._llm_payload('qwen3:14b-q4_K_M', [{'role': 'user', 'content': 'x'}])
    assert payload['think'] is False, '混合思考模型必须关思考，否则正文被思考 token 挤空'
    assert payload['model'] == 'qwen3:14b-q4_K_M'
    assert payload['max_tokens'] > 0


def test_drafts_generated_from_asr_text(monkeypatch):
    """D2. 端到端：给一段台词，_drafts_for_segment 必须产出三版草稿。
    锁住「草稿不再全灭」这个 P0-1 目标。"""
    import analyze_pipeline as AP

    def _fake_llm(prompt, system='', timeout=180):
        return '{"简洁":"画面直给。","悬念":"接下来会发生什么？","口语":"你猜怎么着。"}'
    monkeypatch.setattr(AP, '_llm_light', _fake_llm)
    drafts, err = AP._drafts_for_segment('这是一段台词')
    assert err is None, err
    assert len(drafts) == 3, '应产出简洁/悬念/口语三版'
    assert [d['style'] for d in drafts] == ['简洁', '悬念', '口语']
    assert all(d['text'].strip() for d in drafts)


def test_drafts_reports_failure_not_silently(monkeypatch):
    """D3. LLM 返回空时，必须报 llm_empty 而不是返回空列表装成功。"""
    import analyze_pipeline as AP
    monkeypatch.setattr(AP, '_llm_light', lambda *a, **k: '')
    drafts, err = AP._drafts_for_segment('这是一段台词')
    assert drafts == []
    assert err == 'llm_empty', err


def test_try_parse_json_handles_multiple_objects():
    """E. 模型一次输出多个 JSON 对象时，必须救回第一组而不是全丢。

    qwen3 实测会吐 3 组草稿（换行分隔）。旧实现的贪婪正则把它们连成一块
    非法 JSON → 整段 llm_parse_fail，草稿明明生成了却一个不留。"""
    import movie_narrator as MN
    one = '{"简洁":"a","悬念":"b","口语":"c"}'
    three = '\n'.join([one, '{"简洁":"d","悬念":"e","口语":"f"}',
                       '{"简洁":"g","悬念":"h","口语":"i"}'])
    obj = MN._try_parse_json(three)
    assert isinstance(obj, dict), '多个对象输出时必须救回一组'
    assert obj['简洁'] == 'a'


def test_try_parse_json_edge_cases():
    """E2. 边界：markdown 围栏 / 前后废话 / 字符串内花括号 / 无法解析。"""
    import movie_narrator as MN
    good = '{"简洁":"a","悬念":"b","口语":"c"}'
    assert MN._try_parse_json('```json\n%s\n```' % good)['简洁'] == 'a'
    assert MN._try_parse_json('结果如下：\n%s\n以上。' % good)['简洁'] == 'a'
    # 花括号出现在字符串内部时不得误判边界
    nested = '{"简洁":"他说{\\"x\\"}了","悬念":"b","口语":"c"}'
    assert MN._try_parse_json(nested)['简洁'] == '他说{"x"}了'
    # 无法解析时必须返回 None，不能返回半个对象
    assert MN._try_parse_json('抱歉我做不到') is None
    assert MN._try_parse_json('') is None
    assert MN._try_parse_json('{"简洁":"a"') is None


def test_cuda_dll_dirs_returns_list():
    """F. CUDA 库目录探测不抛异常，非 Windows 返回空列表。"""
    v = P._cuda_dll_dirs()
    assert isinstance(v, list)
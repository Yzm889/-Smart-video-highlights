# -*- coding: utf-8 -*-
"""v1.4.75 回归钉：Whisper 预热与正式分析的让位机制（修 P0 优先级倒挂）。

【真问题】上传完成即后台预热 ASR（_warmup_worker），预热持有 _gpu_slot 做长
转写；用户点「生成」后正式分析在 _gpu_slot 外排队干等（实测卡 5+ 分钟）。
修法：① 正式转写进推理段时 _ASR_DEMAND['real'] +1、finally -1；
② priority='warmup' 的调用在模型加载前和每个段落间检查计数器，正式任务一到
立刻 AbortError 让位（预热本来就是可丢弃的，正式任务自己跑出完整缓存）。

运行：pytest tests/test_whisper_priority.py -q
"""
import os
import sys
import types


import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server  # noqa: F401,E402  先导宿主，规避循环导入
import ai_providers as AP  # noqa: E402


class _FakeSeg:
    def __init__(self, i):
        self.start = i * 1.0
        self.end = i * 1.0 + 0.8
        self.text = '第%d句' % (i + 1)


class _FakeInfo:
    duration = 5.0
    language = 'zh'


class _FakeWhisperModule(types.ModuleType):
    """假 faster_whisper：WhisperModel() 返回**模型对象**（其 .transcribe 产出
    5 个假段落）。demand_at_yield 记录每段消费时的 _ASR_DEMAND 快照；
    push_at 非空 = 消费到该段时模拟「正式任务到达」（push 一次）。"""

    def __init__(self, fail_first=None, push_at=None):
        super().__init__('faster_whisper')
        self.instances = []
        self.demand_at_yield = []
        self._fail_first = fail_first
        self.push_at = push_at

    def WhisperModel(self, path, device=None, compute_type=None, download_root=None):
        self.instances.append({'path': path, 'device': device})
        if self._fail_first is not None and len(self.instances) <= self._fail_first:
            raise RuntimeError('模拟加载失败')
        mod = types.SimpleNamespace()
        fw = self

        def _transcribe(audio, **kw):
            def _gen():
                for i in range(5):
                    if fw.push_at is not None and i == fw.push_at:
                        AP._asr_demand_push()
                    fw.demand_at_yield.append((i, AP._asr_demand()))
                    yield _FakeSeg(i)
            return _gen(), _FakeInfo()

        mod.transcribe = _transcribe
        return mod


@pytest.fixture()
def asr_env(monkeypatch, tmp_path):
    """把 asr_segments 的外部依赖全部桩化，让转写可以真跑。"""
    wav_path = tmp_path / 'asr.wav'
    # 假 ffmpeg：直接产出一个真 wav（_load_wav_as_float32 不再桩，读得动）
    import wave as _wave
    monkeypatch.setattr(AP, 'ffmpeg_run', lambda cmd: (0, b'', b''))
    monkeypatch.setattr(AP, '_subtitle_segments', lambda p: None)
    monkeypatch.setattr(AP, '_external_subtitle_segments', lambda p: None)
    monkeypatch.setattr(AP, 'whisper_device', lambda: ('cpu', 'int8'))
    monkeypatch.setattr(AP, '_whisper_env_setup', lambda: None)

    def _fake_load_wav(p):
        with _wave.open(p, 'rb') as w:
            n = w.getnframes()
            return np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32)
    monkeypatch.setattr(AP, '_load_wav_as_float32', _fake_load_wav)

    # 真 wav（16kHz 单声道 2 秒静音）：ffmpeg_run 桩要能「产出」它
    with _wave.open(str(wav_path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b'\x00\x00' * 32000)

    def fake_ffmpeg(cmd):
        # 最后一个参数是输出 wav：把它复制到位
        import shutil
        out = cmd[-1]
        shutil.copyfile(str(wav_path), out)
        return 0, b'', b''
    monkeypatch.setattr(AP, 'ffmpeg_run', fake_ffmpeg)

    # 计数器归零
    AP._ASR_DEMAND['real'] = 0
    import types as _types
    env = _types.SimpleNamespace(wav_path=str(wav_path))
    yield env
    AP._ASR_DEMAND['real'] = 0


def test_warmup_yields_before_model_load(asr_env, monkeypatch):
    """正式转写已排队（demand≥1）→ 预热在**模型加载前**就弃权。"""
    fw = _FakeWhisperModule()
    monkeypatch.setitem(sys.modules, 'faster_whisper', fw)
    AP._asr_demand_push()   # 正式转写已到达

    with pytest.raises(AP.AbortError, match='预热让位'):
        AP.asr_segments(asr_env.wav_path, priority='warmup')
    assert fw.instances == [], '预热不许加载模型（6GB 权重都不许碰）'
    assert AP._asr_demand() == 1, '让位不能动正式任务的计数'


def test_warmup_yields_mid_stream_when_real_arrives(asr_env, monkeypatch):
    """预热跑到一半正式任务到达 → 段落间弃权（最多慢一个段落）。"""
    fw = _FakeWhisperModule(push_at=2)
    monkeypatch.setitem(sys.modules, 'faster_whisper', fw)

    try:
        with pytest.raises(AP.AbortError, match='预热让位'):
            AP.asr_segments(asr_env.wav_path, priority='warmup')
    finally:
        AP._asr_demand_pop()   # 模拟到达的 push 必须配对，否则泄漏毒化后续用例
    yields = fw.demand_at_yield
    assert [d for _, d in yields][:3] == [0, 0, 1], '前两段无正式任务、第三段起计数=1'
    assert yields[-1][0] <= 3, '到达后最多再消费一段就该弃权: %r' % (yields,)
    assert AP._asr_demand() == 0, '测试结束计数必须归零'


def test_real_call_sets_demand_and_completes(asr_env, monkeypatch):
    """正式调用（默认 priority）：消费期间计数 ≥1，结束后归 0，全段跑完不弃权。"""
    fw = _FakeWhisperModule()
    monkeypatch.setitem(sys.modules, 'faster_whisper', fw)
    segs = AP.asr_segments(asr_env.wav_path)
    assert len(segs) == 5, '正式调用必须跑完全部段落'
    assert all(d >= 1 for _, d in fw.demand_at_yield), '消费期间计数必须 ≥1'
    assert AP._asr_demand() == 0, '结束后计数归零（无泄漏）'


def test_warmup_completes_when_no_demand(asr_env, monkeypatch):
    """没有正式任务 → 预热正常跑完（行为与旧版一致）。"""
    fw = _FakeWhisperModule()
    monkeypatch.setitem(sys.modules, 'faster_whisper', fw)
    segs = AP.asr_segments(asr_env.wav_path, priority='warmup')
    assert len(segs) == 5
    assert AP._asr_demand() == 0


def test_demand_never_leaks_on_early_return(asr_env, monkeypatch):
    """wav 抽取失败等早退路径也不许泄漏计数。"""
    monkeypatch.setitem(sys.modules, 'faster_whisper', types.ModuleType('faster_whisper'))
    monkeypatch.setattr(AP, 'ffmpeg_run', lambda cmd: (1, b'', b'boom'))  # wav 抽取失败
    assert AP.asr_segments(asr_env.wav_path) == []
    assert AP._asr_demand() == 0


def test_warmup_worker_passes_priority_and_yields():
    """源码钉：预热必须带 priority='warmup'，并接住让位 AbortError 记日志。"""
    with open(os.path.join(ROOT, 'workflows.py'), encoding='utf-8') as f:
        src = f.read()
    assert "asr_segments(video_path, priority='warmup')" in src
    assert '预热让位' in src, '让位必须有日志（可排查）'

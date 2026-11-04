# -*- coding: utf-8 -*-
"""台词识别质量 + 剧情理解可见性 + 失败任务回收（用户 2026-10-27 反馈三项）。

用户原话：
  1) 「台词识别不准确，识别字幕也不对，识别语音也不对」
  2) 「一键生成得先让 AI 知道这个视频讲的什么，不然剧情都不知道，剪也是乱剪」
  3) 「清理上一个视频产生的缓存和中间物」（但别删已下载的模型）

本文件锁定：
  ① 有内嵌字幕轨 → 直接用字幕（人工校对，比 ASR 准），并告知来源；
     没有字幕轨才走语音识别。
  ② ASR 参数必须关掉 condition_on_previous_text（否则长视频会连锁复读 = 对不上），
     且语言可配（默认 zh，可设 auto）。
  ③ 视觉剧情理解失败 → 必须告警 + 自动改用台词重建剧情；两次都失败也要明确说
     「按通用模板生成，剧情走向可能不准」——绝不允许静默退化（那就是"乱剪"）。
  ④ 失败/取消的任务必须回收 src 副本与中间物，但保留结果文件与缩略图。
"""
import sys
import types

import webui_server as S
import ai_providers as A
import movie_narrator as MN


# ---------------------------------------------------------------------------
# ① 字幕轨优先
# ---------------------------------------------------------------------------
def test_subtitle_segments_parses_srt(tmp_path, monkeypatch):
    """内嵌字幕轨 → 解析成带时间轴的台词段。"""
    srt = ('1\n00:00:01,000 --> 00:00:03,500\n第一句台词\n\n'
           '2\n00:01:02,250 --> 00:01:04,000\n第二句台词\n')
    src = tmp_path / 'with_sub.mp4'
    src.write_bytes(b'0' * 100)

    def _fake_run(cmd):
        # 探测调用：只在 stderr 里回字幕流信息
        if not cmd[-1].endswith('.srt'):
            return 0, '', '  Stream #0:2: Subtitle: subrip\n'
        # 抽取调用：真的把 srt 写到目标文件（实现是从磁盘读文件）
        with open(cmd[-1], 'w', encoding='utf-8') as f:
            f.write(srt)
        return 0, '', ''

    monkeypatch.setattr(A, 'ffmpeg_run', _fake_run)
    monkeypatch.setattr(A, 'WORKDIR', str(tmp_path))
    segs = A._subtitle_segments(str(src))
    assert segs and len(segs) == 2, segs
    assert segs[0]['text'] == '第一句台词'
    assert abs(segs[0]['start'] - 1.0) < 0.01 and abs(segs[0]['end'] - 3.5) < 0.01
    assert abs(segs[1]['start'] - 62.25) < 0.01


def test_subtitle_tolerates_malformed_time_line(tmp_path, monkeypatch):
    """畸形 SRT（时间行后面直接跟文字）不能丢内容——真机实测踩过。"""
    srt = ('1\n00:00:01,000 --> 00:00:04,000\n第一句\n\n'
           '2\n00:00:05,000 --> 00:00:08,500男主角推开吱呀作响的木门\n\n')
    src = tmp_path / 'v.mp4'
    src.write_bytes(b'0' * 100)

    def _fake_run(cmd):
        if cmd[-1].endswith('.srt'):
            with open(cmd[-1], 'w', encoding='utf-8') as f:
                f.write(srt)
            return 0, '', ''
        return 0, '', '  Stream #0:1: Subtitle: subrip\n'

    monkeypatch.setattr(A, 'ffmpeg_run', _fake_run)
    monkeypatch.setattr(A, 'WORKDIR', str(tmp_path))
    segs = A._subtitle_segments(str(src))
    assert segs and len(segs) == 2, segs
    assert segs[1]['text'] == '男主角推开吱呀作响的木门', segs[1]
    assert abs(segs[1]['end'] - 8.5) < 0.01, segs[1]


def test_subtitle_stream_probe_handles_bytes_stderr(tmp_path, monkeypatch):
    """ffmpeg_run 的 stderr 是 **bytes**（项目惯例）——单测用 str mock 看不出来，
    真机第一次跑就 TypeError。必须按 bytes 也能解析。"""
    src = tmp_path / 'v.mp4'
    src.write_bytes(b'0' * 100)
    monkeypatch.setattr(A, 'ffmpeg_run',
                        lambda cmd: (0, b'', b'  Stream #0:2: Subtitle: subrip\n'))
    n, idx = A._subtitle_streams(str(src))
    assert n == 1 and idx == 2, (n, idx)


def test_asr_prefers_subtitle_over_speech(tmp_path, monkeypatch):
    """有字幕轨时**不得**再跑 ASR（ASR 会更差且更慢）。"""
    src = tmp_path / 'v.mp4'
    src.write_bytes(b'0' * 100)
    monkeypatch.setattr(A, '_subtitle_segments', lambda p: [{'start': 0.0, 'end': 1.0, 'text': '字幕内容'}])
    called = {'asr': False}

    def _boom(*a, **k):
        called['asr'] = True
        raise AssertionError('有字幕轨时不该走 ASR')
    monkeypatch.setattr(A, '_load_wav_as_float32', _boom)
    segs = A.asr_segments(str(src))
    assert segs and segs[0]['text'] == '字幕内容'
    assert not called['asr']


def test_asr_falls_back_to_speech_when_no_subtitle(tmp_path, monkeypatch):
    """没有字幕轨 → 照常走语音识别（不能因为加了字幕优先级就完全不识别）。"""
    src = tmp_path / 'v.mp4'
    src.write_bytes(b'0' * 100)
    monkeypatch.setattr(A, '_subtitle_segments', lambda p: None)
    monkeypatch.setattr(A, '_note_progress_warning', lambda m: None)

    class _FakeSeg:
        def __init__(self, s, e, t):
            self.start, self.end, self.text = s, e, t
    class _FakeInfo:
        duration = 2.0

    class _FakeModel:
        def __init__(self, *a, **k):
            pass
        def transcribe(self, audio, **kw):
            _FakeModel.kwargs = kw
            return ([_FakeSeg(0.0, 1.0, '识别到的台词')], _FakeInfo())

    fake_mod = types.ModuleType('faster_whisper')
    fake_mod.WhisperModel = _FakeModel
    monkeypatch.setitem(sys.modules, 'faster_whisper', fake_mod)
    def _fake_run(cmd):
        # ASR 路径：真的产出 wav 文件（实现会检查文件是否存在）
        out = cmd[-1]
        if out.endswith('.wav'):
            with open(out, 'wb') as f:
                f.write(b'RIFF' + b'0' * 100)
        return 0, '', ''
    monkeypatch.setattr(A, 'ffmpeg_run', _fake_run)
    monkeypatch.setattr(A, '_load_wav_as_float32', lambda p: [0.0])
    monkeypatch.setattr(A, 'whisper_device', lambda: ('cpu', 'int8'))
    monkeypatch.setattr(A, '_whisper_env_setup', lambda: None)
    monkeypatch.setattr(A, '_whisper_load_path', lambda n: n)
    monkeypatch.setattr(A, 'whisper_models_dir', lambda: '/tmp')
    segs = A.asr_segments(str(src))
    assert segs and segs[0]['text'] == '识别到的台词'
    kw = _FakeModel.kwargs
    # 关掉上下文条件 → 不再连锁复读（用户反馈"识别语音对不上"的成因）
    assert kw.get('condition_on_previous_text') is False, kw
    assert kw.get('temperature') == 0.0, kw
    assert kw.get('beam_size') == 5, kw


def test_whisper_language_configurable(monkeypatch):
    """语言可配：默认 auto（模型自动判别），显式配置则按配置走。

    旧默认是 'zh'，把英文片硬按中文解码 → 输出成串翻译腔幻觉（实测
    『我跟他们说』×12、『林顿省警察受伤』×4），再被剧情理解层当台词证据。
    详见 tests/test_asr_language.py。
    """
    monkeypatch.setattr(A, 'load_ai_config', lambda: {'whisper': {'language': 'auto'}})
    assert A.whisper_language() == 'auto'
    monkeypatch.setattr(A, 'load_ai_config', lambda: {'whisper': {}})
    assert A.whisper_language() == 'auto', '未配置时应自动判别，而不是假定中文'
    monkeypatch.setattr(A, 'load_ai_config', lambda: {'whisper': {'language': 'zh'}})
    assert A.whisper_language() == 'zh', '显式配置的中文必须被尊重（中文片精度依赖它）'


# ---------------------------------------------------------------------------
# ② 剧情理解失败 → 可见 + 台词补救
# ---------------------------------------------------------------------------
def test_plot_brief_falls_back_to_text_when_vlm_fails(monkeypatch):
    """视觉剧情理解失败 → 自动用台词重建剧情（而不是静默退化）。"""
    warned = []
    monkeypatch.setattr(MN, '_note_progress_warning', lambda m: warned.append(m))
    def _boom(*a, **k):
        raise RuntimeError('VLM 挂了')
    monkeypatch.setattr(MN, 'vlm_chat_multi', _boom)
    monkeypatch.setattr(MN, 'local_llm_chat',
                        lambda p, system=None, timeout=180: '这是一段关于归乡的剧情。')
    per_seg = [(0.0, 5.0, '我回来了'), (5.0, 10.0, '母亲还在门口等我'),
               (10.0, 15.0, '我们一起吃了个饭')]
    out = MN._plot_brief({'0': b'x', '1': b'y'}, per_seg, {})
    assert out and '归乡' in out, out
    assert any('视觉剧情理解失败' in w for w in warned), warned


def test_plot_brief_warns_when_both_paths_fail(monkeypatch):
    """两条路都失败 → 必须明确告知「按通用模板生成，剧情走向可能不准」。"""
    warned = []
    monkeypatch.setattr(MN, '_note_progress_warning', lambda m: warned.append(m))
    def _boom(*a, **k):
        raise RuntimeError('VLM 挂了')
    monkeypatch.setattr(MN, 'vlm_chat_multi', _boom)
    monkeypatch.setattr(MN, 'local_llm_chat', _boom)
    per_seg = [(0.0, 5.0, '台词一'), (5.0, 10.0, '台词二'), (10.0, 15.0, '台词三')]
    out = MN._plot_brief({'0': b'x'}, per_seg, {})
    assert out is None
    assert any('通用模板' in w and '不准' in w for w in warned), warned


def test_plot_brief_written_to_progress(monkeypatch):
    """AI 理解的剧情必须写进进度（前端据此展示，用户能核对剪得对不对）。"""
    prog = {}
    monkeypatch.setattr(MN, 'vlm_chat_multi', lambda *a, **k: '这是一段关于归乡的剧情。')
    monkeypatch.setattr(MN, '_note_progress_warning', lambda m: None)
    per_seg = [(0.0, 5.0, '我回来了'), (5.0, 10.0, '母亲还在门口等我')]
    out = MN._plot_brief({'0': b'x', '1': b'y'}, per_seg, {'_progress': prog})
    assert out and '归乡' in out
    assert prog.get('plot_brief') and '归乡' in prog['plot_brief'], prog
    assert prog.get('plot_source') == '视觉理解', prog


def test_frontend_shows_plot_brief():
    """前端必须有「AI 理解的剧情」展示位，且在进度轮询里被调用。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'app.js'), encoding='utf-8') as f:
        js = f.read()
    with open(os.path.join(root, 'static', 'index.html'), encoding='utf-8') as f:
        html = f.read()
    assert 'moviePlotBrief' in html, '页面缺少「AI 理解的剧情」展示位'
    assert '_showPlotBrief' in js, '前端缺少 _showPlotBrief'
    assert js.count('_showPlotBrief(p)') >= 2, '生成中与完成后都要展示剧情'


def test_external_subtitle_file_is_used(tmp_path, monkeypatch):
    """**外挂字幕文件**（视频同目录同名 .srt）→ 直接用（国内剧集最常见形态）。"""
    vid = tmp_path / 'movie.mp4'
    vid.write_bytes(b'0' * 100)
    (tmp_path / 'movie.srt').write_text(
        '1\n00:00:02,000 --> 00:00:05,000\n你终于回来了\n\n'
        '2\n00:00:06,000 --> 00:00:09,500我等了你很久\n',
        encoding='utf-8')
    monkeypatch.setattr(A, '_subtitle_segments', lambda p: None)   # 容器内没有字幕轨
    monkeypatch.setattr(A, '_note_progress_warning', lambda m: None)
    monkeypatch.setattr(A, '_load_wav_as_float32', lambda p: (_ for _ in ()).throw(
        AssertionError('有外挂字幕时不该走 ASR')))
    segs = A.asr_segments(str(vid))
    assert segs and len(segs) == 2, segs
    assert segs[0]['text'] == '你终于回来了'
    assert abs(segs[1]['start'] - 6.0) < 0.01


def test_external_subtitle_with_language_suffix(tmp_path):
    """同目录 xxx.zh.srt / xxx.chs.srt 这类带语言后缀的也要认。"""
    vid = tmp_path / 'ep01.mp4'
    vid.write_bytes(b'0' * 100)
    (tmp_path / 'ep01.zh.srt').write_text(
        '1\n00:00:01,000 --> 00:00:03,000\n第一句中文\n', encoding='utf-8')
    segs = A._external_subtitle_segments(str(vid))
    assert segs and segs[0]['text'] == '第一句中文', segs


def test_parse_subtitle_file_handles_ass_and_vtt(tmp_path):
    """ASS/SSA 与 VTT 也要能解析（外挂字幕常见格式）。"""
    ass = tmp_path / 'a.ass'
    ass.write_text('[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                   'Dialogue: 0,0:00:02.00,0:00:05.00,Default,,0,0,0,,你好世界\n', encoding='utf-8')
    segs = A.parse_subtitle_file(str(ass))
    assert segs and segs[0]['text'] == '你好世界', segs
    assert abs(segs[0]['start'] - 2.0) < 0.01 and abs(segs[0]['end'] - 5.0) < 0.01
    vtt = tmp_path / 'a.vtt'
    vtt.write_text('WEBVTT\n\n00:00:02.000 --> 00:00:04.000\nVTT 字幕\n', encoding='utf-8')
    segs2 = A.parse_subtitle_file(str(vtt))
    assert segs2 and segs2[0]['text'] == 'VTT 字幕', segs2


def test_vlm_reads_onscreen_subtitles(monkeypatch):
    """硬字幕：VLM 从画面里读出字幕文字（无 OCR 依赖）。"""
    warned = []
    monkeypatch.setattr(MN, '_note_progress_warning', lambda m: warned.append(m))
    monkeypatch.setattr(MN, 'vlm_chat_multi',
                        lambda imgs, p, system=None, timeout=240:
                        '第1段: 你终于回来了\n第2段: (无字幕)\n第3段: 我等了你很久')
    per_seg = [(0.0, 5.0, ''), (5.0, 10.0, ''), (10.0, 15.0, '')]
    got = MN._read_onscreen_subtitles({0: b'a', 1: b'b', 2: b'c'}, per_seg)
    assert got.get(0) == '你终于回来了', got
    assert got.get(2) == '我等了你很久', got
    assert 1 not in got, '「(无字幕)」不该被当成台词'
    assert any('硬字幕' in w for w in warned), warned


# ---------------------------------------------------------------------------
# ④ 画面理解质量（2026-10-28：用户反馈「理解还不够」的根因修复）
# ---------------------------------------------------------------------------

def test_vlm_call_must_disable_thinking(monkeypatch, tmp_path):
    """本机 Ollama 的 VLM 调用必须显式关思考。

    实测 minicpm-v4.5：不关思考时 thinking 141 字、正文只剩 41 字（残缺）且慢 4 倍；
    关掉后正文完整、1.2s。这条不严一下，画面理解会拿到被思考挤占的残缺内容，
    而且症状是「结果很空却没有任何报错」——最难查的那类问题。
    """
    import ai_providers as AP
    captured = {}

    class _Resp:
        def read(self):
            return b'{"message":{"content":"ok"}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):
        import json as _j
        captured['payload'] = _j.loads(req.data.decode('utf-8'))
        captured['url'] = req.full_url
        return _Resp()

    monkeypatch.setattr(AP, 'vlm_cfg', lambda: {
        'model': 'minicpm-v4.5', 'mode': 'ollama', 'base_url': 'http://localhost:11434',
        'api_key': '', 'timeout': 30})
    monkeypatch.setattr('urllib.request.urlopen', _fake_urlopen)
    img = tmp_path / 'f.jpg'
    img.write_bytes(b'\xff\xd8\xff\xe0' + b'0' * 64)   # 最小 JPEG 头，够 base64 编码
    AP.vlm_chat_multi([str(img)], '描述这张图', system='你是画面描述助手', timeout=30)
    payload = captured['payload']
    assert payload.get('think') is False, 'VLM 必须关思考，否则正文被 thinking 挤占：%s' % payload
    assert (payload.get('options') or {}).get('num_predict'), '应显式给出 num_predict'
    assert captured['url'].endswith('/api/chat')


def test_scan_full_visual_covers_every_segment(monkeypatch):
    """剧情理解必须看到**每一段**，不能退化成稀疏抽帧。

    旧实现只从全片取 8 帧：20 分钟视频切成 48 段时等于每 171 秒才看 1 帧，
    画面利用率 17%。而「剧情」恰恰是最高层的理解任务——信息倒挂导致模型只能编。
    """
    import movie_narrator as MN
    n = 30  # 明显多于单批 8 帧
    frames = {i: 'C:/fake/f%02d.jpg' % i for i in range(n)}
    per_seg = [(i * 8.0, (i + 1) * 8.0, '') for i in range(n)]
    seen = []

    def _fake_vlm(imgs, prompt, system=None, timeout=240, max_tokens=None):
        seen.append(list(imgs))
        # 模拟「逐段输出」：返回本次批次每一段的描述
        return '\n'.join('第%d段: 描述%d' % (k + 1, k + 1) for k in range(len(imgs)))

    monkeypatch.setattr(MN, 'vlm_chat_multi', _fake_vlm)
    caps = MN._scan_full_visual(frames, per_seg, {'theme': '', 'name': ''})
    assert len(caps) == n, '应覆盖全部 %d 段，实际 %d 段' % (n, len(caps))
    assert all(k in caps for k in range(n)), '不能有段被跳过'
    # 必须分批（一次塞 30 张会挤爆上下文），且每批不超过上限
    assert len(seen) > 1, '应分批调用，不能一次塞全部帧'
    assert all(len(b) <= MN.VLM_BATCH_FRAMES for b in seen), seen


# ---------------------------------------------------------------------------
# ③ 失败任务回收
# ---------------------------------------------------------------------------
def test_cleanup_failed_run_removes_src_copy_keeps_result(tmp_path, monkeypatch):
    """失败任务：删源片副本与中间物，但保留结果文件与缩略图（结果页要显示错误）。"""
    run = tmp_path / 'run-1-x'
    (run / 'thumbnails').mkdir(parents=True)
    (run / 'src.mp4').write_bytes(b'0' * 5000)
    (run / 'narr01.wav').write_bytes(b'0' * 3000)
    (run / 'frame_00.jpg').write_bytes(b'0' * 100)
    (run / 'cut01.mp4').write_bytes(b'0' * 200)
    (run / 'analyze_result.json').write_text('{"segments":[]}', encoding='utf-8')
    (run / 'thumbnails' / '0.jpg').write_bytes(b'0' * 50)
    freed = S.cleanup_failed_run(str(run))
    assert freed > 0
    assert not (run / 'src.mp4').exists(), '源片副本没回收（20 分钟视频就是 816MB）'
    assert not (run / 'narr01.wav').exists()
    assert not (run / 'frame_00.jpg').exists()
    assert (run / 'analyze_result.json').exists(), '结果文件必须保留（结果页要显示错误）'
    assert (run / 'thumbnails' / '0.jpg').exists(), '缩略图要保留（编辑台要用）'


def test_cleanup_failed_run_ignores_missing_dir(tmp_path):
    assert S.cleanup_failed_run(str(tmp_path / 'nope')) == 0
    assert S.cleanup_failed_run('') == 0

# -*- coding: utf-8 -*-
"""v1.4.18 回归钉：配音前必须确认显存，不够就让 Ollama 让开。

事故（2026-11-09）：用户点试听只看到「未就绪或加载超时」。
真因不是引擎、不是超时 —— 是 Ollama 的 qwen3:8b 以 32K 上下文常驻吃了 9.6GB，
Qwen3-TTS 模型照样能加载，但 WDDM 把权重换页到内存：GPU 占用 11671/12288 MB、
利用率 0%，推理在 PCIe 上反复抖，永远算不完。
清掉 Ollama 后同样的调用 45.3s 出 284KB 音频。
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tts2_engines as T2  # noqa: E402

JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')
HTML = os.path.join(ROOT, 'static', 'desktop', 'index.html')


def _reset(monkeypatch):
    monkeypatch.setattr(T2, '_VRAM_CACHE', {'t': -99.0, 'v': (0, 0)})
    monkeypatch.setattr(T2, '_LLM_GUARD', {'t': -99.0})


def test_gpu_vram_parses_nvidia_smi(monkeypatch):
    class _R:
        stdout = '11671, 12288\n'
    monkeypatch.setattr(T2.subprocess, 'run', lambda *a, **k: _R())
    _reset(monkeypatch)
    assert T2.gpu_vram(0) == (11671, 12288)


def test_gpu_vram_tolerates_missing_nvidia_smi(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError('nvidia-smi')
    monkeypatch.setattr(T2.subprocess, 'run', _boom)
    _reset(monkeypatch)
    assert T2.gpu_vram(0) == (0, 0), '没 N 卡不能抛，只能退化成 0'


def test_release_vram_no_gpu_does_not_block(monkeypatch):
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (0, 0))
    _reset(monkeypatch)
    assert T2.release_vram(want=5200) == (True, '')


def test_release_vram_leaves_ollama_alone_when_enough(monkeypatch):
    called = []
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (900, 12288))
    monkeypatch.setattr(T2, 'ollama_loaded', lambda *a, **k: called.append(1) or ['qwen3:8b'])
    _reset(monkeypatch)
    assert T2.release_vram(want=5200) == (True, '')
    assert not called, '显存够用时不许动用户的 Ollama'


def test_release_vram_unloads_ollama_and_reports(monkeypatch):
    """缺显存 → 给 Ollama 每个在驻模型发卸载请求，腾够就放行并说明。

    【2026-11-11 升级】卸载请求必须带最小化生成参数（prompt 空 + num_predict=1 +
    think=False）并等完成（timeout 90s）：旧行为 6s 就断开，碰上「ps 之后被别人
    抢先卸载」的竞态会触发一次重载、keep_alive=0 没落地、模型继续驻留。
    对照实验见 tts2_engines._ollama_unload 文档。"""
    seen = []

    def _http(base=None, path=None, payload=None, timeout=4):
        seen.append((path, payload))
        return b'{}'

    vram = iter([(11671, 12288), (1500, 12288)])
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: next(vram))
    monkeypatch.setattr(T2, 'ollama_loaded', lambda *a, **k: ['qwen3:8b'])
    monkeypatch.setattr(T2, '_http', _http)
    _reset(monkeypatch)
    ok, note = T2.release_vram(want=5200)
    assert ok is True
    assert ('/api/generate', {'model': 'qwen3:8b', 'prompt': '', 'stream': False,
                              'think': False, 'keep_alive': 0,
                              'options': {'num_predict': 1}}) in seen
    assert 'qwen3:8b' in note and '已临时腾出显存' in note


def test_release_vram_explains_when_still_short(monkeypatch):
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (11671, 12288))
    monkeypatch.setattr(T2, 'ollama_loaded', lambda *a, **k: [])
    _reset(monkeypatch)
    ok, note = T2.release_vram(want=5200)
    assert ok is False
    assert '显存不足' in note and '11671' in note and '关闭' in note


def test_serve_does_not_start_worker_when_vram_short(monkeypatch):
    """显存不够时不要拉起 worker（拉起来也是白等 240s）。"""
    monkeypatch.setattr(T2, 'release_vram', lambda **k: (False, '显存不足：11671/12288 MB 已用'))
    srv = T2._Serve('qwen3tts', ['nope'], need_mb=5200)
    assert srv._start() is False
    assert srv.proc is None
    assert '显存不足' in srv.error


def test_speak_returns_worker_reason_via_diag(monkeypatch):
    class _S:
        note = ''
        def ask(self, req, timeout=240):
            return False, 'CUDA out of memory'
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, '_get_qwen', lambda: _S())
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (11000, 12288))
    diag = {}
    ok = T2.qwen3tts_speak('测试', os.path.join(os.environ.get('TEMP', '.'), 'x.wav'),
                           diag=diag)
    assert ok is False
    assert diag['reason'] == 'CUDA out of memory'
    assert diag['vram'] == {'used': 11000, 'total': 12288}


def test_free_vram_for_llm_only_when_short(monkeypatch):
    """解说开工前：够就什么都不做，不够才停配音 worker。"""
    stopped = []
    monkeypatch.setattr(T2, '_ollama_model_size', lambda m: 0)
    monkeypatch.setattr(T2, 'release_engines', lambda: stopped.append(1) or ['qwen3tts'])

    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (500, 12288))
    _reset(monkeypatch)
    assert T2.free_vram_for_llm('qwen3:8b') == []
    assert not stopped

    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (6000, 12288))
    _reset(monkeypatch)
    assert T2.free_vram_for_llm('qwen3:8b') == ['qwen3tts']
    assert stopped == [1]


def test_free_vram_for_llm_sizes_by_model(monkeypatch):
    """按 Ollama 里该模型体积估算，别对小模型也一顿猛停。"""
    monkeypatch.setattr(T2, '_ollama_model_size', lambda m: 3 * 1024 ** 3)  # 3GB 小模型
    monkeypatch.setattr(T2, 'release_engines', lambda: ['qwen3tts'])
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (4400, 12288))
    _reset(monkeypatch)
    # 3GB*1.25+800 ≈ 4640MB，空闲 7888MB 足够 → 不动
    assert T2.free_vram_for_llm('qwen3:4b') == []


def test_engine_status_exposes_vram_and_ollama(monkeypatch):
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (11671, 12288))
    monkeypatch.setattr(T2, 'ollama_loaded', lambda *a, **k: ['qwen3:8b'])
    st = T2.engine_status()
    assert st['vram'] == {'used': 11671, 'total': 12288, 'free': 617}
    assert st['ollama'] == ['qwen3:8b']


def test_frontend_shows_vram_and_longer_timeout():
    js = open(JS, encoding='utf-8').read()
    assert '_vramRow' in js and 'Ollama 正占用' in js, '设置页要摊开显存占用'
    i = js.index("$('btnTtsTest').addEventListener")
    block = js[i:i + 2400]
    assert 'ctrl.abort(), 360000' in block, '前端超时必须长于后端(加载 360s+合成 240s)'
    html = open(HTML, encoding='utf-8').read()
    # 缓存戳（cache-buster）：改过 desktop.js / desktop.css 就必须同时抬 index.html 里的
    # ?v=N，否则 WebView2 会继续用旧文件，用户看到「改了没生效」。
    # 这里不钉死具体数字（上一次钉死 ?v=12，下一次合法升级就必然变红），
    # 只钉真正的不变量：两个资源都带版本号，且**两个数字必须相等**——
    # 只抬其中一个才是真的 bug（两个资源版本漂移）。
    js_v = re.search(r'desktop\.js\?v=(\d+)', html)
    css_v = re.search(r'desktop\.css\?v=(\d+)', html)
    assert js_v and css_v, 'index.html 里 desktop.js / desktop.css 必须带 ?v=N 缓存戳'
    assert js_v.group(1) == css_v.group(1), (
        'desktop.js?v=%s 与 desktop.css?v=%s 版本号不一致：两个资源必须同步抬版本'
        % (js_v.group(1), css_v.group(1)))
    assert int(js_v.group(1)) >= 13, 'v1.4.21 已抬到 13，不许回退（否则旧缓存继续生效）'


def test_hw_onclick_never_takes_over_a_chosen_engine():
    """已拍板第⑦条：硬件一键配置不得接管「在用的自然配音引擎」。

    btnHwApply 里那句 `if([...].indexOf(curEng) >= 0 ...)` 是**可覆盖白名单**——
    命中就把 tts_local 改成 sherpa。这里钉住：名单里只能有「用户没做过显式选择」的值
    （auto / sapi / 空），绝不能出现 qwen3tts / indextts2 / cosyvoice / chattts / edge。

    背景：v1.4.21 修好 engine 白名单后 qwen3tts/indextts2 才第一次真的存得住，
    以前它们存不进去、curEng 永远是 auto，所以旧名单「看起来」没问题；
    一旦把它们加进这个列表，用户显式选的 Qwen3-TTS/IndexTTS2 会被静默改成 sherpa。
    """
    js = open(JS, encoding='utf-8').read()
    i = js.index('indexOf(curEng) >= 0')
    line = js[js.rindex('\n', 0, i) + 1:js.index('\n', i)]
    for eng in ('qwen3tts', 'indextts2', 'cosyvoice', 'chattts', 'edge'):
        assert "'%s'" % eng not in line, (
            '硬件一键配置的可覆盖名单里出现了 %s —— 这会接管用户显式选择的引擎'
            '（已拍板第⑦条）。该名单只应含 auto / sapi / 空串。实际：%s' % (eng, line.strip()))
    for eng in ('auto', 'sapi'):
        assert "'%s'" % eng in line, '可覆盖名单应仍含 %s。实际：%s' % (eng, line.strip())


def test_ask_timeout_covers_generation_after_load():
    src = open(os.path.join(ROOT, 'tts2_engines.py'), encoding='utf-8').read()
    assert 'def ask(self, req, timeout=240)' in src
    assert '合成超时' in src, '超时也要说人话，别只说 worker 无响应'


# ---------------------------------------------------------------------------
# v1.4.19：worker 不许在打包目录里跑
# 事故：冻结后 PyInstaller 把 _internal 塞进 PYTHONPATH，3.11 的 worker 拣到
# 打包进来的 3.13 `_ctypes.pyd` → ImportError: python313.dll conflicts，
# 界面只显示「试听失败，worker 启动失败」。
# ---------------------------------------------------------------------------
def test_sync_worker_copies_then_refreshes(monkeypatch, tmp_path):
    src_dir = tmp_path / 'bundle'
    src_dir.mkdir()
    src = src_dir / 'qwen3tts_worker.py'
    src.write_text('v1', encoding='utf-8')
    monkeypatch.setattr(T2, '_HERE', str(src_dir))
    monkeypatch.setattr(T2, 'BIN_DIR', str(tmp_path / 'bin'))

    dst = T2.sync_worker('qwen3tts_worker.py')
    assert dst == str(tmp_path / 'bin' / 'qwen3tts_worker.py')
    assert open(dst, encoding='utf-8').read() == 'v1'

    src.write_text('v2', encoding='utf-8')          # 升级成品后副本要跟着变
    assert open(T2.sync_worker('qwen3tts_worker.py'), encoding='utf-8').read() == 'v2'


def test_sync_worker_keeps_copy_when_source_missing(monkeypatch, tmp_path):
    """源码态/异常态：源没了就用已有副本，别把可用状态弄坏。"""
    monkeypatch.setattr(T2, '_HERE', str(tmp_path / 'empty'))
    binstub = tmp_path / 'bin'
    binstub.mkdir()
    (binstub / 'indextts2_worker.py').write_text('old', encoding='utf-8')
    monkeypatch.setattr(T2, 'BIN_DIR', str(binstub))
    assert T2.sync_worker('indextts2_worker.py') == str(binstub / 'indextts2_worker.py')


@pytest.mark.skipif(os.name != 'nt',
                    reason=r'断言的是 Windows 路径拼接（C:\assets\bin\...）：'
                           r'Linux 上 os.path.join 不把反斜杠当分隔符，必然拼错')
def test_start_runs_assets_copy_with_isolated_python(monkeypatch, tmp_path):
    """启动命令必须是 assets 里的副本 + -E，且环境是白名单（无 PYTHONPATH）。"""
    captured = {}

    class _P:
        def __init__(self, cmd, **kw):
            captured['cmd'] = list(cmd)
            captured['env'] = dict(kw.get('env') or {})
            raise RuntimeError('stop-here')

    monkeypatch.setattr(T2, 'release_vram', lambda **k: (True, ''))
    monkeypatch.setattr(T2, 'sync_worker', lambda n: r'C:\assets\bin\%s' % n)
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path))
    monkeypatch.setattr(T2.subprocess, 'Popen', _P)
    monkeypatch.setenv('PYTHONPATH', r'C:\app\_internal')

    srv = T2._Serve('qwen3tts', ['C:\\venv\\python.exe', '-u', r'C:\app\_internal\qwen3tts_worker.py',
                                 'serve', r'C:\models'], need_mb=1)
    assert srv._start() is False          # 被 _P 打断，但命令已经拿到
    cmd = captured['cmd']
    assert cmd[0].endswith('python.exe')
    assert '-E' in cmd and '-X' in cmd and 'utf8' in cmd, '必须强制 UTF-8 标准流'
    assert cmd[cmd.index('-X') + 1] == 'utf8'
    assert cmd[cmd.index('-E') + 1] == '-X'      # 两个开关连在一起，-u 在后面
    assert cmd[-3] == r'C:\assets\bin\qwen3tts_worker.py', '必须跑 assets 里的副本'
    assert 'PYTHONPATH' not in captured['env']



# ---------------------------------------------------------------------------
# v1.4.19：启动链路的三处硬伤
# ① 上一轮的 _eos 会留在队列里，让下一次启动「秒判失败」并把刚起的 worker 杀掉；
# ② 失败只留一句「worker 启动失败」，退出码和日志尾巴全丢；
# ③ 宿主进程的环境变量整包继承，打包目录会顺着 PATH/PYTHONPATH 漏进子进程。
# ---------------------------------------------------------------------------
def test_drain_drops_stale_answers(monkeypatch):
    srv = T2._Serve('probe', ['x'])
    srv.q.put({'_eos': True})
    srv.q.put({'ok': True})
    srv._drain()
    assert srv.q.empty()


def test_restart_survives_stale_eos(monkeypatch, tmp_path):
    """worker 死过一次后（队列里躺着 _eos），下次启动仍必须能成功。"""
    worker = tmp_path / 'fake_echo.py'
    worker.write_text('\n'.join([
        'import sys, json',
        'print(json.dumps({"event": "ready"}), flush=True)',
        'for line in sys.stdin:',
        '    print(json.dumps({"ok": True}), flush=True)',
    ]) + '\n', encoding='utf-8')
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path))
    monkeypatch.setattr(T2, 'release_vram', lambda **k: (True, ''))
    srv = T2._Serve('probe', [sys.executable, '-u', str(worker)],
                    ready_timeout=30, need_mb=1)
    assert srv._start() is True
    srv.proc.kill()                      # 模拟崩溃
    srv.proc = None
    srv.q.put({'_eos': True})            # 上一轮 reader 留下的墓碑
    assert srv._start() is True, '残留 _eos 不该让新进程秒死'
    srv.proc.kill()


def test_child_env_is_whitelisted(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', r'C:/app/_internal')
    monkeypatch.setenv('PYTHONHOME', r'C:/app/_internal')
    monkeypatch.setenv('PATH', r'C:/app/_internal;C:/Windows/system32')
    env = T2.child_env()
    assert 'PYTHONPATH' not in env and 'PYTHONHOME' not in env
    assert '_internal' not in env['PATH']
    assert 'system32' in env['PATH']
    assert env['HF_ENDPOINT'].startswith('https://')


def test_start_failure_reports_exit_code(monkeypatch, tmp_path):
    """失败必须带上退出码（不然只会看到一句「worker 启动失败」）。"""
    class _Dead:
        def __init__(self):
            self.stdout = []
            self.returncode = 3

        def poll(self):
            return 3

        def kill(self):
            pass

    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path))
    monkeypatch.setattr(T2, 'release_vram', lambda **k: (True, ''))
    monkeypatch.setattr(T2.subprocess, 'Popen', lambda *a, **k: _Dead())
    srv = T2._Serve('qwen3tts', ['x'], ready_timeout=5, need_mb=1)
    assert srv._start() is False
    assert '退出码 3' in srv.error


def test_chinese_text_survives_the_pipe(monkeypatch, tmp_path):
    """中文必须原样送到 worker：子进程 stdin 是 GBK 时，裸 UTF-8 会变乱码
    （表现是 tokenizer 抛 TextEncodeInput must be Union[...]）。"""
    worker = tmp_path / 'echo_to_file.py'
    got = tmp_path / 'got.txt'
    worker.write_text('\n'.join([
        'import sys, json',
        'print(json.dumps({"event": "ready"}), flush=True)',
        'for line in sys.stdin:',
        '    req = json.loads(line)',
        '    open(sys.argv[1], "w", encoding="utf-8").write(req.get("text", ""))',
        '    print(json.dumps({"ok": True}), flush=True)',
    ]) + '\n', encoding='utf-8')
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path))
    monkeypatch.setattr(T2, 'release_vram', lambda **k: (True, ''))
    srv = T2._Serve('echo', [sys.executable, '-u', str(worker), str(got)],
                    ready_timeout=30, need_mb=1)
    assert srv._start() is True
    text = '画面还没有开始，声音先到了——试听。'
    assert srv.ask({'text': text, 'out': 'x'})[0] is True
    assert got.read_text(encoding='utf-8') == text, '中文走到 worker 不能变形'
    srv.proc.kill()


# ---------------------------------------------------------------------------
# v1.4.28：显存拉锯战三连（2026-11-11 主人实测「配音卡死+炸内存+频繁黑窗」）
# qwen3:8b（默认 32K ctx 驻留 9.65GB）+ IndexTTS2 worker（6.6GB）= 15.6GB > 12GB
# → WDDM 静默换页进内存 → 每句 5s 变 60~100s → ask 超时 → worker 反复重启闪黑窗。
# ---------------------------------------------------------------------------

def test_ollama_unload_sends_minimal_generate(monkeypatch):
    """卸载请求必须是「最小代价生成」：空 prompt + num_predict=1 + think=False +
    等 90s。6s 就断开的旧写法，竞态下会让 keep_alive=0 没落地、模型继续驻留。"""
    seen = []

    def _http(base=None, path=None, payload=None, timeout=4):
        seen.append({'path': path, 'payload': payload, 'timeout': timeout})
        return b'{}'

    monkeypatch.setattr(T2, '_http', _http)
    freed = T2._ollama_unload(['qwen3:8b', '', None, 'nomic-embed-text:latest'])
    assert freed == ['qwen3:8b', 'nomic-embed-text:latest'], '空名必须被跳过'
    assert len(seen) == 2
    p = seen[0]
    assert p['path'] == '/api/generate'
    assert p['payload']['keep_alive'] == 0
    assert p['payload']['prompt'] == ''
    assert p['payload']['think'] is False
    assert p['payload']['options'] == {'num_predict': 1}
    assert p['timeout'] >= 60, '竞态下要冷加载 40~60s，客户端必须等它完成'


def test_ollama_unload_swallows_http_errors(monkeypatch):
    """Ollama 没开/请求失败：只跳过该模型，绝不抛——腾位失败不该阻断配音。"""

    def _boom(**k):
        raise RuntimeError('connection refused')

    monkeypatch.setattr(T2, '_http', _boom)
    assert T2._ollama_unload(['qwen3:8b']) == []


def test_ensure_tts_vram_noop_when_headroom_ok(monkeypatch):
    """余量够 → 纯只读检查，不许动 Ollama（每 10 句调用一次，必须是零开销）。"""
    called = []
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (7500, 12288))
    monkeypatch.setattr(T2, 'ollama_loaded', lambda *a, **k: called.append(1) or ['qwen3:8b'])
    assert T2.ensure_tts_vram() == []
    assert not called, '余量 4.7GB 足够时动 Ollama 就是白折腾'


def test_ensure_tts_vram_unloads_when_short(monkeypatch):
    """余量不足（比如 qwen3:8b 被谁灌回来了）→ 卸 Ollama，返回卸掉的名字。"""
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (12100, 12288))
    monkeypatch.setattr(T2, 'ollama_loaded', lambda *a, **k: ['qwen3:8b'])
    monkeypatch.setattr(T2, '_ollama_unload', lambda names: list(names))
    assert T2.ensure_tts_vram(wait_sec=0) == ['qwen3:8b']


def test_ensure_tts_vram_no_gpu_info_does_not_block(monkeypatch):
    monkeypatch.setattr(T2, 'gpu_vram', lambda *a, **k: (0, 0))
    assert T2.ensure_tts_vram() == []


def test_ask_reports_worker_death_not_generic_timeout(monkeypatch, tmp_path):
    """worker 死了必须报真实原因（退出码+日志尾巴），不能再谎报「合成超时」。
    2026-11-11：worker 被显存换页拖死后，旧文案把排障引向「显存被抢占」歧途。"""
    worker = tmp_path / 'die_on_request.py'
    worker.write_text('\n'.join([
        'import sys, os, json',
        'print(json.dumps({"event": "ready"}), flush=True)',
        'for line in sys.stdin:',
        '    os._exit(3)',                # 收到请求直接死，一行答复都不留
    ]) + '\n', encoding='utf-8')
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path))
    monkeypatch.setattr(T2, 'release_vram', lambda **k: (True, ''))
    srv = T2._Serve('die', [sys.executable, '-u', str(worker)],
                    ready_timeout=30, need_mb=1)
    assert srv._start() is True
    ok, err = srv.ask({'text': 'x', 'out': 'y'}, timeout=20)
    assert ok is False
    assert '合成超时' not in err, 'worker 都死了还报超时就是误导'
    assert '配音进程已退出' in err and '退出码 3' in err
    assert srv.proc is None, '死掉的进程不许留在 self.proc 上'


def test_ask_still_reports_plain_timeout_when_worker_alive():
    """worker 活着但没算完 → 仍报「合成超时」（这是真的算得慢/被抢显存）。"""
    src = open(os.path.join(ROOT, 'tts2_engines.py'), encoding='utf-8').read()
    assert '合成超时' in src, '超时也要说人话，别只说 worker 无响应'
    assert '配音进程已退出' in src, 'worker 死亡必须单独成句，不许混进超时文案'


def test_text_model_num_ctx_is_pinned_and_shared():
    """文本模型必须显式给 num_ctx=16384，且真实调用与推理探针共用同一处。

    不给 num_ctx 时 Ollama 按模型默认 32768 加载（KV 缓存吃掉 ~4.8GB，本机
    qwen3:8b 驻留 9.65GB），12GB 卡上和配音 worker 必然对撞。ctx 一变 Ollama
    就重载——探针和正式调用若不同值会互相踢（主人拍板降到 16K，2026-11-11）。"""
    import io
    src = io.open(os.path.join(ROOT, 'ai_providers.py'), encoding='utf-8').read()
    assert '_LLM_NUM_CTX = 16384' in src, '文本模型 ctx 必须显式钉在 16384'
    assert "'num_ctx': _LLM_NUM_CTX" in src, '必须用共享常量，不许写死数字'
    i = src.index('def _ollama_native_chat')
    assert "'num_ctx': _LLM_NUM_CTX" in src[i:], 'options 里没有 num_ctx 会按 32K 重载'


def test_infer_probe_is_gated_on_running_tasks():
    """任务/预热运行期间，真实推理探针必须停手（源码级钉子）。

    实测机理：桌面 UI 每 15s 轮询 /api/local/status → 探针每 60s force 真打一次
    生成 → 先 free_vram_for_llm 杀掉配音 worker、再把 qwen3:8b 灌回显存 5 分钟
    →「卸了又弹回」+ worker 重启闪黑窗 + 换页卡死。探针只在无任务时跑。"""
    import io
    hs = io.open(os.path.join(ROOT, 'handler.py'), encoding='utf-8').read()
    i = hs.index('def _local_infer_probe_async')
    assert '_w.is_task_running()' in hs[i:i + 2000], (
        '探针必须先问 is_task_running()，任务跑着时不许真打生成请求')
    ws = io.open(os.path.join(ROOT, 'webui_server.py'), encoding='utf-8').read()
    assert 'def is_task_running' in ws and 'def _bump_task_running' in ws
    # 任务与预热的进出必须配对增减（漏一处就是探针永久停手或永久误伤）
    for fn_src in ('handler', 'workflows'):
        s = io.open(os.path.join(ROOT, fn_src + '.py'), encoding='utf-8').read()
        assert s.count('_bump_task_running(1)') >= 1, fn_src + ' 没有登记任务开始'
        assert s.count('_bump_task_running(-1)') >= 1, fn_src + ' 没有登记任务结束'

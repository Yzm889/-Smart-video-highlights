# -*- coding: utf-8 -*-
"""v1.4.18 回归钉：配音前必须确认显存，不够就让 Ollama 让开。

事故（2026-11-09）：用户点试听只看到「未就绪或加载超时」。
真因不是引擎、不是超时 —— 是 Ollama 的 qwen3:8b 以 32K 上下文常驻吃了 9.6GB，
Qwen3-TTS 模型照样能加载，但 WDDM 把权重换页到内存：GPU 占用 11671/12288 MB、
利用率 0%，推理在 PCIe 上反复抖，永远算不完。
清掉 Ollama 后同样的调用 45.3s 出 284KB 音频。
"""
import os
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
    """缺显存 → 给 Ollama 每个在驻模型发 keep_alive=0，腾够就放行并说明。"""
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
    assert ('/api/generate', {'model': 'qwen3:8b', 'keep_alive': 0}) in seen
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
    assert 'desktop.js?v=10' in html and 'desktop.css?v=10' in html


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

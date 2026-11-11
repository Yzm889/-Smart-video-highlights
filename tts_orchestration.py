# -*- coding: utf-8 -*-
"""TTS 编排（逐段配音、并发/熔断/诊断日志）——由 movie_narrator.py 拆出（v1.4.40）。

搬运范围（原 6103-6447 行，逐字搬运、零逻辑改动）：
    _free_sys_ram_mb / _tts_concurrency / _generate_all_tts /
    _warn_tts_engine_degraded / _edge_probe_recover

约定与 movie_narrator 一致：宿主符号经 _w.<name> 晚绑定（本文件照抄 _host() 模式，
拿到的是同一个 webui_server 模块对象）；公共符号由 movie_narrator 尾部 re-export
并回注宿主——workflows.py:715 的 _w._generate_all_tts、render_pipeline.py:29 的
from movie_narrator import _generate_all_tts 等调用方零改动。

【依赖解析规则——拆分成败点，勿改】凡定义在 movie_narrator 命名空间的符号
（含它 from ai_providers / tts_engines / text_utils 按值 import 的名字），一律经
函数内惰性 `import movie_narrator as _MN` 后以 _MN.X(...) 调用期属性查找：
测试普遍 monkeypatch.setattr(M, 'X', ...) 打在主模块命名空间上，函数搬家后
__globals__ 换成了本模块的 dict，裸名会让全部 patch 静默打空
（test_core.py:1810 的 test_model_align_rejects_bad_output 记录过同款事故）。
只有 stdlib、本文件自有定义、_w 宿主符号、跨模块共享的单例对象（_TLS）用裸名。
"""
import logging, os, sys, threading

from ffmpeg_utils import _TLS

_log = logging.getLogger('framecut.narrator')   # 与主模块同名 logger（同一单例）

# ---- 宿主晚绑定（照抄 movie_narrator._host）----
_HERE = os.path.dirname(os.path.abspath(__file__))
_HOST_FILE = os.path.join(_HERE, 'webui_server.py')


def _host():
    m = sys.modules.get('webui_server')
    if m is not None:
        return m
    m = sys.modules.get('__main__')
    if m is not None and os.path.abspath(getattr(m, '__file__', '')) == _HOST_FILE:
        return m
    import webui_server
    return webui_server


_w = _host()


def _free_sys_ram_mb():
    """系统可用物理内存 MB（Windows GlobalMemoryStatusEx，无第三方依赖）；
    拿不到返回 0，调用方回退默认路数。"""
    try:
        if os.name == 'nt':
            import ctypes

            class _MEMSTAT(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong),
                            ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong),
                            ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong),
                            ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong),
                            ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

            m = _MEMSTAT()
            m.dwLength = ctypes.sizeof(_MEMSTAT)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return int(m.ullAvailPhys // (1024 * 1024))
    except Exception:
        pass
    return 0


def _tts_concurrency():
    """TTS 并发路数：环境变量 TTS_CONCURRENCY 显式配置优先（钳制 1~5）；
    未配置时按系统可用内存算——每路并发是独立的 edge-tts CLI 子进程
    （Python 解释器 + 探测/合成缓冲，每路 100~300MB），显存换页把内存吃满的
    16GB 机器再开 3 路就是雪上加霜（2026-11-11 主人实测「并发跑每次都炸内存」）。
    可用 <4GB → 1 路；<8GB → 2 路；其余 3 路。
    仅「本地引擎 + edge-tts 可用」时生效（网络等待可重叠）；mimo/本地 CPU 引擎保持串行。"""
    import movie_narrator as _MN
    try:
        v = int(os.environ.get('TTS_CONCURRENCY', '') or 0)
    except ValueError:
        v = 0
    if v > 0:
        return max(1, min(5, v))
    free_mb = _MN._free_sys_ram_mb()
    if free_mb and free_mb < 4000:
        return 1
    if free_mb and free_mb < 8000:
        return 2
    return 3


def _generate_all_tts(narr, run_dir, progress=None, skip_idx=None):
    """逐段生成所有配音，返回 [(index, clip_path), ...]。不做视频裁剪。
    断点续跑：已存在且有效的 narr%d.mp3 直接跳过，只生成缺失的。
    【并发（S6）】默认路径（本地引擎 + edge-tts 可用）用 TTS_CONCURRENCY 路并发：
      - 每段是独立 edge-tts CLI 子进程，线程安全；
      - 并发下首选引擎全部锁定为 edge（local_tts_speak 的锁定语义），音色一致；
      - 并发数可用环境变量 TTS_CONCURRENCY 覆盖（=1 即完全回退原串行行为）；
      - mimo（云端）/ 本地 CPU 引擎（cosyvoice/chattts/sherpa/sapi）保持串行，避免抢资源/限流。
    【熔断探测恢复】并发下探测文件（.edge_probe.mp3）用任务级锁互斥；
    各 worker 按自己处理的段数每 3 段检查一次，edge-tts 成功后内部自动复位熔断。
    【诊断日志】逐段记录 {idx, engine, ok, error, duration_ms} 到 tts_log.json。
    summary 口径：[2.5] 起 `failed`＝最终仍缺失、`first_failures`＝首轮失败段数、
    `retried`＝被重试兜回的段数；正常（非取消）路径下 `failed + retried == first_failures`。"""
    import time as _time
    import concurrent.futures as _cf
    import movie_narrator as _MN
    # 路径修复：progress_state 中的 run_dir 可能是相对路径（如 "run-1-..."），
    # 转成绝对路径（基于 _w.OUTDIR）避免 TTS 文件写到项目根目录导致"有日志无文件"。
    if run_dir and not os.path.isabs(run_dir):
        run_dir = os.path.join(_w.OUTDIR, run_dir)
    results = []
    _tcfg = _MN.load_ai_config().get('tts') or {}
    use_mimo = bool(_tcfg.get('api_key')) and bool(_tcfg.get('model'))
    skipped = 0
    skip_idx = set(skip_idx or ())   # 原声让位段：本来就不配音
    _tts_diag = []   # 诊断日志：[{idx, engine, ok, error, duration_ms, source}]
    _n_workers = _tts_concurrency()
    _parallel = (not use_mimo) and bool(_MN.edge_tts_available()) and _n_workers > 1
    _prog_lock = threading.Lock()
    _probe_lock = threading.Lock()   # 并发下 .edge_probe.mp3 是共享路径，探测必须互斥
    _probe_state = {'n': 0}
    # 首段串行预热选定的引擎：并发 worker 线程各自第一次调用都要重新探测一遍
    # （引擎锁在 _TLS 上是线程本地的），每次约 11s。这里把首段结果当提示，
    # 让后续线程直接沿用 —— 既省探测，也天然满足「一个任务内音色一致」的设计意图。
    _eng_hint = {'name': None}

    # === 断点续跑：主线程统一检查，只提交缺失段 ===
    pending = []   # [(idx, txt)]
    for i, txt in enumerate(narr):
        if not (txt or '').strip():
            continue
        if i in skip_idx:
            skipped += 1
            _tts_diag.append({'idx': i, 'engine': 'quote', 'ok': True, 'error': '',
                              'duration_ms': 0, 'source': 'quote'})
            continue
        if _MN.is_white_line(txt):
            # 「（留白）」是功能标记不是台词：这条不配音，让原片声音飞。
            # 以前只跳过空串，留白段会被念成「括号留白括号」，功能直接失效。
            skipped += 1
            _tts_diag.append({'idx': i, 'engine': 'white', 'ok': True, 'error': '',
                              'duration_ms': 0, 'source': 'white'})
            continue
        existing = os.path.join(run_dir, 'narr%d.mp3' % i)
        if os.path.exists(existing) and os.path.getsize(existing) > 100:
            results.append((i, existing))
            skipped += 1
            _tts_diag.append({'idx': i, 'engine': 'cache', 'ok': True, 'error': '',
                              'duration_ms': 0, 'source': 'cache'})
        else:
            pending.append((i, txt))
    total = len(pending)
    done = 0
    _log.info('[DIAG] TTS开始: 共%d段, 需生成%d, 跳过%d, 并发%d路' % (
        len(narr), total, skipped, _n_workers if _parallel else 1))
    # 【显存保活·开工】配音 worker（6.6GB）常驻后，谁再把 qwen3:8b（32K ctx 时
    # 9.65GB）灌进显存，12GB 卡立刻装不下 → WDDM 静默换页 → 每句 5s 变 60~100s。
    # 写稿/画面理解到这里都结束了，Ollama 里的模型纯属抢地盘的——余量不足就请出去。
    try:
        import tts2_engines as _T2g
        _freed_tts = _T2g.ensure_tts_vram()
        if _freed_tts:
            _log.info('[DIAG] 配音开工前腾显存: %s（写稿/画面理解已结束，下次用到会自动重载）'
                      % '、'.join(_freed_tts))
    except Exception as _e:
        _log.info('[DIAG] 配音开工显存检查失败（不阻断）: %s' % _e)

    def _update_progress(phase, pct):
        if progress:
            with _prog_lock:
                progress['phase'] = phase
                progress['pct'] = pct

    def _synth_one(item):
        """合成单段；返回 (idx, clip, eng, err, dur_ms, is_abort)。"""
        i, txt = item
        if _MN._aborted():
            return (i, None, None, '', 0, True)
        # 沿用首段预热选定的引擎（线程本地锁，post-pool 线程里默认是空的）
        if _eng_hint['name'] and not getattr(_TLS, 'tts_engine', None):
            try:
                _TLS.tts_engine = _eng_hint['name']
            except Exception:
                pass
        # 熔断探测恢复（根因 1 改良）：每 3 段检查，被熔断则探测
        _probe_state['n'] += 1
        if _probe_state['n'] % 3 == 0 and _MN.edge_tts_dead_reason():
            with _probe_lock:
                if _MN.edge_tts_dead_reason():
                    _MN._edge_probe_recover(run_dir)
        _t0 = _time.time()
        clip = None
        _eng = None
        _err = ''
        if use_mimo:
            np_ = os.path.join(run_dir, 'narr%d.mp3' % i)
            if _w.ai_tts(txt, np_):
                clip = np_
                _eng = 'mimo'
            else:
                _err = 'mimo_failed'
        if clip is None:
            ok, _eng, lp = _MN.local_tts_speak(txt, os.path.join(run_dir, 'narr%d.mp3' % i))
            if ok:
                clip = lp
            else:
                _err = _eng or 'all_engines_failed'
                _eng = _eng or 'none'
        _dur = int((_time.time() - _t0) * 1000)
        return (i, clip, _eng, _err, _dur, False)

    def _apply(result, source):
        """汇总一段结果并推进进度。返回 True 表示收到取消信号。"""
        nonlocal done
        i, clip, _eng, _err, _dur, is_abort = result
        if is_abort:
            return True
        if clip is not None:
            results.append((i, clip))
            _tts_diag.append({'idx': i, 'engine': _eng, 'ok': True, 'error': '',
                              'duration_ms': _dur, 'source': source})
        else:
            _tts_diag.append({'idx': i, 'engine': _eng, 'ok': False, 'error': _err,
                              'duration_ms': _dur, 'source': source})
        done += 1
        processed = skipped + done
        if done % 10 == 0:
            _log.info('[DIAG] 配音 %d/%d 句（最近一句 %.1fs）'
                      % (processed, len(narr), _dur / 1000.0))
            # 【显存保活·每 10 句】防配音中途有谁把大模型灌回显存（探针/预热/手滑），
            # 撞上换页就整批拖死。余量足时是零开销的只读检查。
            try:
                import tts2_engines as _T2g
                _freed_mid = _T2g.ensure_tts_vram()
                if _freed_mid:
                    _log.info('[DIAG] 配音中途显存告急，已请出: %s' % '、'.join(_freed_mid))
            except Exception:
                pass
        if _parallel:
            _update_progress('逐段配音 %d/%d（并发%d路）' % (processed, len(narr), _n_workers),
                             55 + int(25 * processed / max(1, len(narr))))
        else:
            _update_progress('逐段配音 %d/%d' % (processed, len(narr))
                             + ('（断点续跑，已跳过%d段）' % skipped if skipped else ''),
                             55 + int(25 * processed / max(1, len(narr))))
        return False

    # === 首段串行预热（v1.4.11）===
    # 实测真数据（整集 79 段那次）：并发池里前 3 段各花了 80736 / 80739 / 80742 ms，
    # 第 4 段起回到 1852ms。原因：三段同时在 worker 里触发「edge → sapi 降级探测 +
    # SAPI COM 初始化」，探测是全局互斥的，于是排队 × 每段约 27s ≈ 80s。
    # 处理：先串行跑第一段把引擎烧热（单线程只付一次 11s），再进并发池。
    # 附带好处：这 80 秒期间 UI 只有一个不动的 55%，现在会显示「正在加载配音引擎」。
    if _parallel and total > 0:
        _update_progress('正在加载配音引擎（首次较慢）…', 55)
        _first = pending.pop(0)
        _r0 = _synth_one(_first)
        if _r0[2]:
            _eng_hint['name'] = _r0[2]
        if _apply(_r0, 'fresh'):
            return results
        total = len(pending)

    # === 第一轮：并发或串行生成 ===
    if _parallel and total > 0:
        with _cf.ThreadPoolExecutor(max_workers=_n_workers) as _ex:
            _futs = [_ex.submit(_synth_one, it) for it in pending]
            for _f in _cf.as_completed(_futs):
                try:
                    _r = _f.result()
                except Exception as _e:
                    _log.info('[DIAG] TTS并发worker异常: %s' % _e)
                    continue
                if _apply(_r, 'fresh'):
                    break   # 协作式取消：收到信号即停止汇总（worker 内自行快速返回）
    else:
        for _item in pending:
            if _MN._aborted():
                break
            _r = _synth_one(_item)
            if _apply(_r, 'fresh'):
                break

    if skipped:
        _log.info('[DIAG] TTS断点续跑: 跳过%d段已生成配音' % skipped)
    _log.info('[DIAG] TTS第一轮完成: %d/%d段' % (len(results), len(narr)))
    # 失败重试：第一轮没成功的段落再试一次（可能是瞬时网络抖动或熔断恢复）
    success_idx = set(i for i, _ in results)
    # 留白段本来就不该有配音，绝不能算「失败」再补念一遍——
    # 否则第一轮跳过了，第二轮又把它念出来（实测踩中，功能等于没做）。
    failed = [i for i in range(len(narr))
              if i not in success_idx and i not in skip_idx
              and (narr[i] or '').strip()
              and not _MN.is_white_line(narr[i])]
    if failed:
        _log.info('[DIAG] TTS重试 %d 个失败段落' % len(failed))
        _time.sleep(1.0)  # 等熔断恢复
        for i in failed:
            if _MN._aborted(): break
            _r = _synth_one((i, narr[i]))
            if _r[5]:
                break
            _apply(_r, 'retry')
            if progress:
                with _prog_lock:
                    progress['phase'] = '配音重试 %d/%d' % (len([r for r in results if r[0] in failed]), len(failed))
    results.sort(key=lambda x: x[0])
    # 写入诊断日志
    _log_path = os.path.join(run_dir, 'tts_log.json')
    try:
        _bad = [e for e in _tts_diag if not e.get('ok')]
        # [2.5] 首轮口径：`failed` 统计的是「最终仍缺失」= len(narr)-len(results)-skipped，
        # 只要重试兜回，它恒为 0 —— 于是「第一轮曾经失败过」这件事在日志里彻底不可见。
        # 实测真机产物 run-1-20261109-185905：summary 里 failed=0，但 failed_segments 明明有
        # 一条 {idx:0, engine:"none", error:"all_engines_failed"}（首段冷启动失败 3273ms，
        # 1 秒后重试成功 8004ms）——同一份 summary 两个字段自相矛盾，排障时看不到真因。
        # 这里把首轮失败与重试兜回各自单独计数，互不干扰。不变量：failed + retried == first_failures。
        _first_bad_idx = set(e.get('idx') for e in _tts_diag
                             if not e.get('ok') and (e.get('source') or '') == 'fresh')
        _retried_idx = set(e.get('idx') for e in _tts_diag
                           if e.get('ok') and (e.get('source') or '') == 'retry')
        _jd = {'segments': _tts_diag,
               'summary': {'total': len(narr), 'success': len(results),
                           'failed': len(narr) - len(results) - skipped,   # [2.2] 计数（兼容旧字段）
                           'skipped': skipped,
                           'first_failures': len(_first_bad_idx),   # [2.5] 首轮失败段数（曾经失败过，可能已被重试救回）
                           'retried': len(_retried_idx),            # [2.5] 重试兜回段数
                           'engines_used': list(set(e.get('engine', '') for e in _tts_diag if e.get('engine'))),
                           'broken_until': _MN._edge_internal().get('dead_until', 0.0),  # [2.4] 线程级熔断状态
                           'failed_segments': [{'idx': e.get('idx'), 'engine': e.get('engine'),
                                                'error': e.get('error', '')} for e in _bad]}}  # [2.2] 结构化失败原因
        with open(_log_path, 'w', encoding='utf8') as _f:
            import json as _json
            _json.dump(_jd, _f, ensure_ascii=False, indent=1)
        if progress:
            # [2.2] 失败段对前端可见（手动调整/进度页可展示「第X段配音失败，已用Y替换/该段静音」）
            progress['tts_failures'] = [{'idx': e.get('idx'), 'engine': e.get('engine'),
                                         'error': e.get('error', '')} for e in _bad]
    except Exception as _e:
        _log.info(f'[DIAG] TTS日志写入失败: {_e}')
    _warn_tts_engine_degraded(_tts_diag, progress)
    _log.info('[DIAG] TTS最终完成: %d/%d段, 日志%s' % (len(results), len(narr), _log_path))
    return results


def _warn_tts_engine_degraded(diag, progress):
    """配音引擎被降级时，必须让用户知道——否则「不静默」就是一句空话。

    实测场景：edge-tts（晓晓/云希，自然）连不上微软服务时，管线静悄悄全部
    落到 SAPI（系统自带，机械感重），tts_log 里 success=4、无一条错误，
    用户拿到的成片音色断崖下降却毫不知情，只以为「配音就这样」。
    这里在进度里写明实际用的引擎，前端进度条旁边直接可见。
    """
    # 注意用 `is None`：progress 早期常是空字典，`not {}` 为真会把告警整个吞掉
    if progress is None or not diag:
        return
    used = [e.get('engine', '') for e in diag if e.get('ok') and e.get('engine')
            and e.get('engine') not in ('cache',)]
    if not used:
        return
    fallback_engines = ('sapi', 'sapi_fallback')
    if any(e in fallback_engines for e in used):
        progress['warn'] = (
            (progress.get('warn') + '；' if progress.get('warn') else '')
            + '配音引擎已降级到系统自带 SAPI（机械感较重）：edge-tts 当前不可用，'
              '多为网络连不上微软朗读服务。检查网络/代理后可恢复。')
        progress['tts_engine_degraded'] = True
    progress['tts_engine'] = max(set(used), key=used.count)


def _edge_probe_recover(run_dir):
    """edge-tts 熔断探测恢复。用极短文本做一次轻量合成测试。
    edge_tts_speak 内部成功即复位熔断（update fails=0, dead_until=0）；
    失败则不影响熔断状态。探测文件自动清理。"""
    import movie_narrator as _MN
    probe_text = '你好'
    probe_out = os.path.join(run_dir, '.edge_probe.mp3')
    try:
        _MN.edge_tts_speak(probe_text, probe_out)
    except Exception:
        pass
    finally:
        # 清理探测产物（mp3 和可能的 .ssml）
        for _p in (probe_out, probe_out + '.ssml'):
            if _p and os.path.exists(_p):
                try:
                    os.unlink(_p)
                except Exception:
                    pass

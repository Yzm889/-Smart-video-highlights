# ---- 第4批架构拆分：re-export 引擎层（符号等价于拆分前 webui 内定义）----
# 逐名别名 re-export：与原先 from tts_engines import * 完全等价（同样的对象、
# 一次性绑定；tts_engines.__all__ 白名单覆盖全部引擎函数与常量，含下划线内部状态），
# 保证旧命名空间 API 完整。改用显式别名是因为 star import 会让 pyflakes 报
# "unable to detect undefined names"，CI 门禁直接红掉。
import tts_engines  # noqa: F401  (仅用于下方别名，保证 pyflakes 视为 used)
tts_local_cfg = tts_engines.tts_local_cfg
local_tts_speak = tts_engines.local_tts_speak
local_tts_label = tts_engines.local_tts_label
tts_models_dir = tts_engines.tts_models_dir
sapi_tts = tts_engines.sapi_tts
_tts_available = tts_engines._tts_available
edge_tts_dead_reason = tts_engines.edge_tts_dead_reason
edge_tts_available = tts_engines.edge_tts_available
edge_tts_reset = tts_engines.edge_tts_reset
edge_tts_speak = tts_engines.edge_tts_speak
_edge_internal = tts_engines._edge_internal
_edge_note_failure = tts_engines._edge_note_failure
_tls_edge_state = tts_engines._tls_edge_state
_EDGE_RETRY = tts_engines._EDGE_RETRY
_EDGE_RETRY_SLEEP = tts_engines._EDGE_RETRY_SLEEP
_EDGE_MAX_FAILS = tts_engines._EDGE_MAX_FAILS
_EDGE_DEAD_SECONDS = tts_engines._EDGE_DEAD_SECONDS
_EDGE_RUN_DOWNGRADE = tts_engines._EDGE_RUN_DOWNGRADE
_EDGE_STATE = tts_engines._EDGE_STATE
_chattts_venv_python = tts_engines._chattts_venv_python
chattts_available = tts_engines.chattts_available
chattts_load = tts_engines.chattts_load
chattts_speak = tts_engines.chattts_speak
_cosyvoice_venv_python = tts_engines._cosyvoice_venv_python
_find_cosyvoice_python = tts_engines._find_cosyvoice_python
cosyvoice_available = tts_engines.cosyvoice_available
cosyvoice_speak = tts_engines.cosyvoice_speak
_COSYVOICE = tts_engines._COSYVOICE
COSYVOICE_REPO_DIR = tts_engines.COSYVOICE_REPO_DIR
COSYVOICE_MODEL_DIR = tts_engines.COSYVOICE_MODEL_DIR
COSYVOICE_VENV_PY = tts_engines.COSYVOICE_VENV_PY
_sherpa_ready = tts_engines._sherpa_ready
sherpa_model_key = tts_engines.sherpa_model_key
sherpa_tts_ready = tts_engines.sherpa_tts_ready
sherpa_tts_available = tts_engines.sherpa_tts_available
_sherpa_load = tts_engines._sherpa_load
sherpa_tts_speak = tts_engines.sherpa_tts_speak
_SHERPA_TTS = tts_engines._SHERPA_TTS
SHERPA_TTS_MODELS = tts_engines.SHERPA_TTS_MODELS
SHERPA_DEFAULT_MODEL = tts_engines.SHERPA_DEFAULT_MODEL
_speak_short_clause = tts_engines._speak_short_clause
_tts_run_banned = tts_engines._tts_run_banned
_tts_note_fail = tts_engines._tts_note_fail
_tts_note_ok = tts_engines._tts_note_ok
_tts_force_downgrade = tts_engines._tts_force_downgrade
_ping_preferred_engine = tts_engines._ping_preferred_engine
parse_tts_markup = tts_engines.parse_tts_markup
markup_to_ssml = tts_engines.markup_to_ssml
markup_to_chattts_text = tts_engines.markup_to_chattts_text
has_tts_markup = tts_engines.has_tts_markup
_enhance_tts_markup = tts_engines._enhance_tts_markup
strip_tts_markup = tts_engines.strip_tts_markup
_fix_unclosed_tags = tts_engines._fix_unclosed_tags
_EMOTION_MAP = tts_engines._EMOTION_MAP
_EMOTION_VOICES = tts_engines._EMOTION_VOICES
_CHATTS = tts_engines._CHATTS

from ai_providers import AI_CONFIG_PATH, _aborted, _gpu_slot, _is_local_url, _strip_think, _whisper_env_setup, _whisper_load_path, asr_segments, load_ai_config, local_llm_cfg, local_llm_chat, mirror_cfg, vlm_cfg, vlm_chat_multi, whisper_device, whisper_model_name, whisper_models_dir, refresh_whisper_models  # noqa: F401  # [3.2] 引擎层 re-export

from text_utils import _clamp_line, _clean_caption, _strip_tts_markup  # noqa: F401
from cache_utils import ANALYSIS_VERSION, WORKDIR, _analysis_cache_load, \
    _analysis_cache_save, _cache_load, _cache_save, _file_fp, \
    _sample_frame_cache_dir, _sample_frame_cache_mark, _sample_frame_cache_ready, \
    _sample_frame_cache_trim, _video_cache_key  # noqa: F401
from ffmpeg_utils import AbortError, PROGRESS, RUN_PROCS, _PROC_LOCK, _TLS, \
    _has_audio_track, ffmpeg_exe, ffmpeg_run, probe_audio_len, \
    probe_duration  # noqa: F401
from video_render import _NAR_CPS, _NAR_MIN_CHARS, _NAR_MAX_CHARS, _NAR_MAX_SPEED, _NAR_MIN_SPEED, \
    _target_chars, _fit_voice, _render_narrate, \
    _merge_spans, _cut_video_by_spans, \
    _build_subtitle_style, _compose_narration_video  # noqa: F401
# 兼容旧命名空间：拆分前下列符号定义在 webui 模块级，保持模块属性可见（长期建议调用方迁到归属模块）
import cache_utils, ffmpeg_utils  # noqa: F401  (仅用于下方别名，保证 pyflakes 视为 used)
ANALYSIS_CACHE_DIR = cache_utils.ANALYSIS_CACHE_DIR
ANALYSIS_CACHE_KEEP = cache_utils.ANALYSIS_CACHE_KEEP
SAMPLE_FRAME_CACHE_DIR = cache_utils.SAMPLE_FRAME_CACHE_DIR
SAMPLE_FRAME_CACHE_KEEP = cache_utils.SAMPLE_FRAME_CACHE_KEEP
_analysis_cache_path = cache_utils._analysis_cache_path
_analysis_cache_trim = cache_utils._analysis_cache_trim
FFMPEG_MAX_SECONDS = ffmpeg_utils.FFMPEG_MAX_SECONDS
_DUR_CACHE = ffmpeg_utils._DUR_CACHE
_DUR_CACHE_LOCK = ffmpeg_utils._DUR_CACHE_LOCK
_DUR_CACHE_MAX = ffmpeg_utils._DUR_CACHE_MAX
_ERR_CHUNK_MAX = ffmpeg_utils._ERR_CHUNK_MAX
_dur_cache_key = ffmpeg_utils._dur_cache_key
_probe_duration_cached = ffmpeg_utils._probe_duration_cached
_parse_time_str = ffmpeg_utils._parse_time_str
_parse_duration = ffmpeg_utils._parse_duration
import ai_providers  # noqa: F401  (兼容别名，供测试等旧命名空间引用)
_cuda_available = ai_providers._cuda_available
_WHISPER_MODELS = ai_providers._WHISPER_MODELS  # handler.py 经 _w 引用（whisper 状态页）
_fmt_hms = ai_providers._fmt_hms

import os, sys, json, math, random, re, shutil, subprocess, threading, time, base64, itertools
# urlparse/parse_qs/unquote 仅 handler.py 经 _w.<名> 引用：改别名绑定，pyflakes 视为 used
import urllib.parse
urlparse = urllib.parse.urlparse
parse_qs = urllib.parse.parse_qs
unquote = urllib.parse.unquote

import logging
_log = logging.getLogger('framecut')
if not logging.getLogger('framecut').handlers:
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(name)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S')

# ===========================================================================
# 【模块级共享状态清单】（batch4-3.4 显式化；改动此处先读这段）
#
# 本文件是拆分后各层（workflows/handler 经 _w.<名>、tts_engines/ai_providers
# 经自身模块）共享状态的「定义点」。按簇归类，标注并发约束与测试 patch 面：
#
#   任务/进度（跨层共享，最热）
#     PROGRESS          进度字典（定义在 ffmpeg_utils，这里 re-export）：
#                       后台任务线程写 / HTTP 轮询读 / _save_progress 快照线程读。
#                       单键读写 GIL 原子；整表遍历容忍读到中间态（前端刷新即可）。
#     PLANS             runid -> /api/plan 的分析方案，_analyze_plan_job 写、
#                       _post_confirm 读后删。与 PROGRESS 同生命周期。
#     _TASK_SEM/_TASK_QUEUE/_TASK_QUEUE_LOCK/_RUN_CTR
#                       任务并发闸门与排队（_max_concurrent_tasks() 决定），
#                       全部经 _TASK_QUEUE_LOCK 或信号量本体保护，勿裸改。
#   目录常量（conftest 测试隔离的改写面，改名=测试写真实目录）
#     OUTDIR/PROGRESS_FILE/HISTORY_PATH/UPLOAD_DIR/MATERIAL_DIR/STATIC_DIR/
#     MUSIC_DIR/BILI_DIR/WORKDIR(cache_utils)
#   后台下载/安装状态（后台线程写、HTTP 状态页读；单键原子，无锁）
#     WHISPER_DL/VLM_PULL/LOCAL_PULL/BILI_PULL/TTS_SETUP
#   字体（_FONT_LOCK 保护 _FONT_CACHE / _font_cache_by_size）
#   引擎层自有状态（定义在各自模块，这里只 re-export 同一对象）
#     tts_engines._EDGE_STATE/_CHATTS/_COSYVOICE/_SHERPA_TTS、
#     ai_providers._WHISPER_MODELS
#
# 约束：新增模块级可变状态时，必须归入上述某一簇并在本清单登记；
# 禁止在模块 import 期做重活（字体扫描/目录遍历等一律惰性）。
# ===========================================================================
HERE = os.path.dirname(os.path.abspath(__file__))
PLANS = {}             # runid -> 人机协同「规划方案」（分析结果，等待用户确认/微调后再渲染）
import threading as _threading
OUTDIR = os.path.join(HERE, 'webui_output')
import bili_downloader  # noqa: E402  (B站搜索/下载；公共符号已回注宿主)
import beat_analysis  # noqa: E402  (分镜/卡点分析引擎；公共符号已回注宿主)
import movie_narrator  # noqa: E402  (电影解说模块；公共符号已回注宿主)
# 下列符号定义于 movie_narrator（拆分时迁出）并经其末尾宿主注入同名绑定；
# 此处显式别名绑定等价于注入结果，同时让 pyflakes 静态可见（消除 F821 误报）。
_atomic_json_dump = movie_narrator._atomic_json_dump
_load_json_file = movie_narrator._load_json_file
_model_narr_guide = movie_narrator._model_narr_guide
# 引擎层 re-export 锚点：下列符号仅经 _w.<name> 晚绑定使用（movie_narrator / workflows /
# handler 经宿主对象属性访问），pyflakes 静态跨模块不可见 → 显式引用消除 F401 误报。
_REEXPORT_ANCHOR = (_aborted, asr_segments, local_llm_chat, vlm_chat_multi, refresh_whisper_models,
                   _clamp_line, ANALYSIS_VERSION, _analysis_cache_load, _analysis_cache_save,
                   _cache_load, _cache_save, _file_fp, _sample_frame_cache_dir,
                   _sample_frame_cache_mark, _sample_frame_cache_ready, _sample_frame_cache_trim,
                   _video_cache_key, AbortError, RUN_PROCS, _PROC_LOCK, _TLS, _has_audio_track,
                   ffmpeg_exe, _NAR_CPS, _NAR_MIN_CHARS, _NAR_MAX_CHARS, _NAR_MAX_SPEED,
                   _NAR_MIN_SPEED, _target_chars, _fit_voice, _render_narrate, _merge_spans,
                   _cut_video_by_spans, _build_subtitle_style, _compose_narration_video,
                   bili_downloader, beat_analysis, movie_narrator)
_ACTIVE_TASK_FILE = os.path.join(OUTDIR, '_active_task.json')

def _save_active_task(runid, phase='', pct=0, run_dir='', task_type=''):
    """保存当前活动任务状态，崩溃后可恢复提示。"""
    try:
        os.makedirs(OUTDIR, exist_ok=True)
        import json as _j
        with open(_ACTIVE_TASK_FILE + '.tmp', 'w', encoding='utf-8') as f:
            _j.dump({'runid': runid, 'phase': phase, 'pct': pct,
                     'run_dir': run_dir, 'task_type': task_type,
                     'updated_at': time.time()}, f, ensure_ascii=False)
        os.replace(_ACTIVE_TASK_FILE + '.tmp', _ACTIVE_TASK_FILE)
    except Exception:
        pass

def _clear_active_task():
    """任务完成/取消时清除活动任务状态。"""
    try:
        if os.path.exists(_ACTIVE_TASK_FILE):
            os.remove(_ACTIVE_TASK_FILE)
    except Exception:
        pass

def _get_active_task():
    """读取活动任务状态，不存在返回None。"""
    try:
        if os.path.exists(_ACTIVE_TASK_FILE):
            import json as _j
            with open(_ACTIVE_TASK_FILE, 'r', encoding='utf-8') as f:
                return _j.load(f)
    except Exception:
        pass
    return None
PROGRESS_FILE = os.path.join(OUTDIR, 'progress_state.json')  # 进度持久化（服务重启不丢失）


def _save_progress():
    """把 PROGRESS 快照写入磁盘（每5秒由后台线程调用，服务重启后可恢复）。

    [P0-1+] 非原子写：写到一半被打断（断电/kill/磁盘满）会留半截 JSON，
    下次 _load_progress() 读不到 → 用户服务重启后看似全清，实际是文件坏掉。
    已有 _atomic_json_dump 帮手，迁过去。

    [P0-2+] 迭代时未快照：PROGRESS 在 runner 线程的 finally 被并发改；
    list() 强制快照后构造 snap，避免 RuntimeError。
    """
    try:
        snap = {}
        for rid, p in list(PROGRESS.items()):    # 加 list() 快照
            if isinstance(p, dict):
                snap[rid] = {k: v for k, v in p.items() if k != '_thread'}
        _atomic_json_dump(PROGRESS_FILE, snap)    # 原子写
    except Exception:
        pass


def _load_progress():
    """启动时从磁盘恢复 PROGRESS（只恢复已完成/失败的，运行中的标记为中断）。"""
    try:
        if os.path.isfile(PROGRESS_FILE):
            with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
                snap = json.load(f)
            for rid, p in snap.items():
                if isinstance(p, dict) and rid not in PROGRESS:
                    if not p.get('done') and not p.get('error'):
                        p['error'] = '服务重启，任务已中断（成片文件仍在磁盘上，可在记录中下载）'
                        p['done'] = True
                    PROGRESS[rid] = p
    except Exception:
        pass


def _start_progress_saver():
    """后台线程：每5秒持久化一次 PROGRESS；顺带做 run_dir 磁盘清扫（启动即一次，之后每6小时）。"""
    def _loop():
        next_sweep = 0.0   # 首轮立即清扫
        while True:
            try:
                if time.time() >= next_sweep:
                    next_sweep = time.time() + 6 * 3600
                    sweep_run_artifacts()
            except Exception:
                pass
            try:
                _save_progress()
            except Exception:
                pass
            time.sleep(5)
    t = _threading.Thread(target=_loop, daemon=True, name='progress-saver')
    t.start()
_RUN_CTR = itertools.count(1)   # 原子自增的 run id 计数器（原来的 RUNSEQ[0] += 1 非原子，多线程下可能撞号）


def _max_concurrent_tasks():
    """任务并发上限：默认 2，可用环境变量 MAX_CONCURRENT_TASKS 覆盖；非法值退回默认。"""
    try:
        v = int(os.environ.get('MAX_CONCURRENT_TASKS', '') or 2)
    except ValueError:
        v = 2
    return max(1, v)


# ffmpeg 是 CPU / 内存大户，放任并发会把机器打满、多个任务互相拖慢到不可用。
# 超限采用「直接拒绝并提示」而不是无限排队——排队没有任何可见反馈，用户只会以为卡死。
_MAX_CONCURRENT_TASKS = _max_concurrent_tasks()
_TASK_SEM = threading.Semaphore(_MAX_CONCURRENT_TASKS)
_TASK_QUEUE = []  # 排队中的任务 [(fn, req, runid, run_dir, prog), ...]
_TASK_QUEUE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 任务队列持久化（Task 3）：
#
# 设计动机：排队中的任务在被运行前一直只在内存，前端刷新 / 服务进程被强杀 / 误
# 关 cmd 窗口后用户再打开 URL，旧的 PROGRESS 条目残留「排队中（前面还有2个任务）」
# 永远不变、永远跑不到，且没有任何 UI 反馈「这个任务其实已经丢了」。一次重连串
# 下来能堆十几次，赶工时尤其崩溃。
#
# 写入时机：每次 _TASK_QUEUE 变更（_spawn 排队、_start_next_queued 取走、取消
# 队列项）都触发一次落盘；`_persist_queue_unlocked` 必须在已持锁状态调用。
# 读取时机：模块 import 末尾进行一次，把磁盘上的残留队列标 done + error，让前端
# 任务卡片正确显示「❌ 已取消（队列丢失，需重新提交）」。
#
# 不持久化 fn / req：函数闭包与 req 字典可能持有大块数据（视频 base64 / 模型
# 配置等），跨进程重建没意义，反正这些列队项已无法重启——标记丢失让用户重提即可。
#
# 注意：OUTDIR 是测试时被 conftest 改写的对象，因此持久化文件路径**每次调用都
# 重新算**，不能用模块级常量；常量 TASK_QUEUE_FILE 仅作外部可见性 / 调试用，
# 内部不依赖。
# ---------------------------------------------------------------------------
TASK_QUEUE_BASENAME = 'task_queue.json'


def _task_queue_path():
    """当前 OUTDIR 下的 task_queue.json 绝对路径。

    每次调用现算，是因为 conftest（以及未来可能的运行时切换输出目录）会改
    OUTDIR，模块 import 阶段的常量绑定就会指错地方。"""
    return os.path.join(OUTDIR, TASK_QUEUE_BASENAME)


# 让「外部看一眼」仍能拿到当前的路径，等价于旧版 TASK_QUEUE_FILE
TASK_QUEUE_FILE = _task_queue_path()


def _summarize_req(req):
    """把 req 字典压成可读的最小摘要（用于持久化 + 兜底 UI 提示，不重建执行）。

    只保留可序列化字段；视频二进制 / 大模型配置天然就在 req 里，但落盘后不会
    被消费——写盘只是「让用户看到队里原来有什么」。"""
    if not isinstance(req, dict):
        return {}
    out = {'action': str(req.get('action') or req.get('mode') or '')}
    # 一些可读字段，前端展示用
    for k in ('plot', 'title', 'video_file', 'videoUrl', 'music_query',
              'preset', 'tts_engine', 'voice_id', 'style'):
        v = req.get(k)
        if isinstance(v, str) and len(v) <= 200:
            out[k] = v
    return out


def _persist_queue_unlocked():
    """【必须持锁调用】把当前队列写到 OUTDIR/task_queue.json。

    写盘失败（磁盘满 / 权限不够）一律静默——队列本身还能跑，丢了只是无法跨
    进程恢复，最坏用户重启后看见「已取消」，可以重提。"""
    items = []
    for i, (_fn, req, runid, run_dir, prog) in enumerate(_TASK_QUEUE):
        items.append({
            'runid': runid,
            'run_dir': run_dir,
            'queue_index': i,
            'phase': prog.get('phase', ''),
            'queued_at': prog.get('queued_at', ''),
            'req_summary': _summarize_req(req),
        })
    payload = {
        'version': 1,
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'count': len(items),
        'queue': items,
    }
    return _atomic_json_dump(_task_queue_path(), payload)


def _load_queue_from_disk():
    """模块启动时把磁盘上残留的队列项标记为「已取消（队列丢失）」。

    实现要点：
    - 不重建 fn / req，所以这些任务只能视为丢失（详见上方注释）。
    - 同时清空队列文件，避免下次启动再吃一遍。
    - 若磁盘文件不存在或损坏，静默返回，不影响主流程启动。
    """
    data = _load_json_file(_task_queue_path())
    if not isinstance(data, dict) or not data.get('queue'):
        return 0
    evicted = 0
    for item in data['queue']:
        runid = item.get('runid')
        if not runid:
            continue
        # PROGRESS 里可能已经有了（比如服务刚启动还没刷掉）——不要覆盖更新的
        prog = PROGRESS.get(runid)
        if prog is None:
            PROGRESS[runid] = {
                'phase': '已取消（服务重启，队列丢失）',
                'pct': 0, 'done': True, 'aborted': True,
                'error': '服务重启时队列已清空，请重新提交',
                'runid': runid,
                'run_dir': item.get('run_dir', ''),
            }
            _evict_finished_progress(keep=100)
            evicted += 1
        elif not prog.get('done') and not prog.get('error'):
            # 排队中漂过来的，给它一个明确终态
            prog['done'] = True
            prog['aborted'] = True
            prog['error'] = '服务重启时队列已清空，请重新提交'
            prog['phase'] = '已取消（队列丢失）'
            evicted += 1
    try:
        os.remove(_task_queue_path())
    except OSError:
        pass
    if evicted:
        _log.info(f'[DIAG] 启动恢复: 把 {evicted} 个残留排队任务标记为取消')
    return evicted


# ---------------------------------------------------------------------------
# 中文字体解析（跨平台探测 · 缺字形校验 · 不静默降级）
#
# 历史硬伤：这里曾是 FONT_PATH = "C:/Windows/Fonts/msyh.ttc"，且加载失败时
# `except: ImageFont.load_default()` —— 该默认字体**不含中文字形**且**不报错**，
# 于是 macOS/Linux/Docker 上烧字幕与封面标题会静默变成「豆腐块」，用户只看到
# 一堆方框却无从得知原因（README 当时还宣称 Docker 镜像已装好中文字体）。
# 现策略：显式探测 → 逐字校验是否真含中文字形 → 找不到就抛 FontMissingError
# 并给出可执行的修复指引。宁可明确失败，也不产出不可读的视频。
#
# 待办（合规）：微软雅黑版权归方正/微软，渲染进公开发布的商业视频存在授权风险，
# 后续拟换成 SIL OFL 协议字体（思源黑体 Noto Sans SC）随仓库分发。探测顺序已
# 为「仓库自带 assets/fonts/」预留优先级，换字体时只需丢文件进去，不用改代码。
# ---------------------------------------------------------------------------
FONT_ENV = 'SPRING_VIDEO_FONT'                      # 环境变量：显式指定字体文件
FONT_DIR = os.path.join(HERE, 'assets', 'fonts')    # 仓库自带字体目录（建议放 OFL 字体）
_CJK_SAMPLE = '中文字幕测试'                          # 字形校验采样
_PUA_PROBE = '\ue000'        # 私用区码位：常规字体必然缺失，用作「缺字形」基准位图
_FONT_LOCK = threading.Lock()
_FONT_CACHE = {'checked': False, 'path': '', 'reason': ''}
_font_cache_by_size = {}


def _font_has_cjk(path, size=48):
    """判断字体文件是否**真的**含中文字形（只看文件存在与否是不够的）。

    原理：把每个采样汉字渲染为位图，与「必然缺失」的私用区码位位图逐一比对 ——
    完全相同说明该字落到了 .notdef（缺字形）。实测可正确区分
    微软雅黑/宋体（含中文）与 Arial / Segoe UI / Times / Pillow 默认字体（不含中文）。
    个别字体（如 simsun）连 .notdef 都不绘制，此时退化为「是否画出任何笔画」判定。
    """
    try:
        f = ImageFont.truetype(path, size)
    except Exception:
        return False

    def _bmp(ch):
        img = Image.new('L', (size * 3, size * 3), 0)
        ImageDraw.Draw(img).text((8, 8), ch, font=f, fill=255)
        return img.tobytes()

    try:
        miss = _bmp(_PUA_PROBE)
        if not any(miss):                       # 该字体不绘制 .notdef（空白）
            return all(any(_bmp(ch)) for ch in _CJK_SAMPLE)
        return all(_bmp(ch) != miss for ch in _CJK_SAMPLE)
    except Exception:
        return False


# 各平台常见中文字体（按优先级）
_FONT_CANDIDATES = {
    'win32': ['C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyhl.ttc',
              'C:/Windows/Fonts/msyhbd.ttc', 'C:/Windows/Fonts/simhei.ttf',
              'C:/Windows/Fonts/simsun.ttc', 'C:/Windows/Fonts/Deng.ttf'],
    'darwin': ['/System/Library/Fonts/PingFang.ttc',
               '/System/Library/Fonts/STHeiti Medium.ttc',
               '/System/Library/Fonts/STHeiti Light.ttc',
               '/System/Library/Fonts/Hiragino Sans GB.ttc',
               '/Library/Fonts/Arial Unicode.ttf',
               '/Library/Fonts/Noto Sans CJK SC Regular.otf',
               os.path.expanduser('~/Library/Fonts/Noto Sans SC Regular.otf')],
}
_FONT_CANDIDATES['linux'] = [
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf',   # Debian 12+ / Ubuntu 22.04+
    '/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf',      # 上游 Noto Sans SC（OFL）
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf',
    '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
    '/usr/share/fonts/truetype/arphic/uming.ttc',
    os.path.expanduser('~/.fonts/NotoSansSC-Regular.otf'),
    os.path.expanduser('~/.local/share/fonts/NotoSansSC-Regular.otf'),
]
# 候选都没命中时的兜底：扫描系统字体目录（限流，避免启动拖慢）
_FONT_SCAN_DIRS = {
    'win32': ['C:/Windows/Fonts'],
    'darwin': ['/System/Library/Fonts', '/Library/Fonts',
               os.path.expanduser('~/Library/Fonts')],
    'linux': ['/usr/share/fonts', '/usr/local/share/fonts',
              os.path.expanduser('~/.fonts'), os.path.expanduser('~/.local/share/fonts')],
}
_FONT_SCAN_MAX_FILES = 400      # 最多校验这么多字体文件
_FONT_SCAN_DEADLINE = 8.0       # 或最多花这么多秒


def _platform_key():
    p = (sys.platform or '').lower()
    if p.startswith('win'):
        return 'win32'
    if p.startswith('darwin'):
        return 'darwin'
    return 'linux'


def _iter_bundled_fonts():
    try:
        for fn in sorted(os.listdir(FONT_DIR)):
            if fn.lower().endswith(('.ttf', '.otf', '.ttc')):
                yield os.path.join(FONT_DIR, fn)
    except OSError:
        return


def _scan_system_fonts(key, deadline_ts):
    """遍历系统字体目录找第一个含中文字形的字体（限文件数与时间）。"""
    n = 0
    for root in _FONT_SCAN_DIRS.get(key, []):
        if not os.path.isdir(root):
            continue
        for dirpath, _dn, filenames in os.walk(root):
            for fn in sorted(filenames):
                if not fn.lower().endswith(('.ttf', '.otf', '.ttc')):
                    continue
                n += 1
                if n > _FONT_SCAN_MAX_FILES or time.time() > deadline_ts:
                    return ''
                p = os.path.join(dirpath, fn)
                if _font_has_cjk(p):
                    return p
    return ''


def _resolve_cjk_font(force=False):
    """惰性解析中文字体路径，结果进程内缓存。返回 '' 表示没找到（附 reason）。"""
    with _FONT_LOCK:
        if _FONT_CACHE['checked'] and not force:
            return _FONT_CACHE['path']
        key = _platform_key()
        found, notes = '', []
        deadline = time.time() + _FONT_SCAN_DEADLINE

        env = (os.environ.get(FONT_ENV) or '').strip()
        if env:
            if os.path.isfile(env) and _font_has_cjk(env):
                found = os.path.abspath(env)
            else:
                notes.append(f'环境变量 {FONT_ENV} 指定的字体不可用或不含中文字形：{env}')
        if not found:
            for p in _iter_bundled_fonts():     # 仓库自带（OFL 字体放这里即可，无需改代码）
                if _font_has_cjk(p):
                    found = p
                    break
        if not found:
            for p in _FONT_CANDIDATES.get(key, []):
                if os.path.isfile(p) and _font_has_cjk(p):
                    found = p
                    break
        if not found:
            found = _scan_system_fonts(key, deadline)
        if not found and not notes:
            notes.append('已检查 环境变量 / assets/fonts / 系统常见中文字体 / 系统字体目录，均未找到含中文字形的字体')
        _FONT_CACHE.update(checked=True, path=found, reason='；'.join(notes))
        global FONT_PATH
        FONT_PATH = found or ''
        return _FONT_CACHE['path']


class FontMissingError(RuntimeError):
    """找不到含中文字形的字体。绝不静默降级为不含中文的字体（那会产出豆腐块）。"""


def font_missing_help():
    """缺字体时的可执行修复指引（各平台安装命令 + 两种免安装兜底）。"""
    return (
        '未找到含中文字形的字体，已中止渲染 —— 不会生成「豆腐块」字幕/封面标题。'
        f'（{_FONT_CACHE.get("reason") or "系统无可用中文字体"}）\n'
        '任选一种方式修复：\n'
        '  1) 安装开源中文字体（推荐思源黑体 Noto Sans SC，SIL OFL 协议，可商用）：\n'
        '     · Debian/Ubuntu : sudo apt-get install -y fonts-noto-cjk\n'
        '     · Alpine(Docker): apk add --no-cache font-noto-cjk\n'
        '     · CentOS/RHEL   : sudo yum install -y google-noto-sans-cjk-fonts\n'
        '  2) 把字体文件（.ttf/.otf/.ttc）放进项目的 assets/fonts/ 目录后重启\n'
        f'  3) 用环境变量指定：{FONT_ENV}=/path/to/NotoSansSC-Regular.otf'
    )


def cjk_font(size):
    """加载中文字体。找不到含中文字形的字体时抛 FontMissingError（而非静默降级）。"""
    p = _resolve_cjk_font()
    if not p:
        raise FontMissingError(font_missing_help())
    with _FONT_LOCK:
        f = _font_cache_by_size.get(size)
    if f is None:
        try:
            f = ImageFont.truetype(p, size)
        except Exception as e:
            raise FontMissingError(f'字体加载失败：{p}（{e}）。{font_missing_help()}')
        with _FONT_LOCK:
            _font_cache_by_size[size] = f
    return f


def font_selfcheck():
    """启动自检：返回 (ok, 说明文本)。缺字体时给出警告而非让用户在成片里踩坑。"""
    p = _resolve_cjk_font()
    if p:
        return True, f'中文字体：{p}'
    return False, '[警告] ' + font_missing_help().replace('\n', '\n        ')


FONT_PATH = ''   # 由 _resolve_cjk_font() 惰性填充；保留名字仅为向后兼容
HISTORY_PATH = os.path.join(HERE, 'history.json')
STATIC_DIR = os.path.join(HERE, 'static')
_LAST_TTS_ERR = ''

def _atomic_write_json(path, obj, indent=2):
    """原子写 JSON：先写同目录 .tmp，再 os.replace 覆盖。

    旧实现 open(path,'w') 会先截断原文件：写入过程中进程被杀 / 磁盘满 / 被占用，
    文件就变成半截 JSON。下一次 load 静默解析失败返回空，随后任何一次写都以空为基底
    覆盖回去 —— 用户表现为「历史记录全没了 / 配置全空了」，且无法恢复。
    os.replace 在同一卷上是原子的：要么读到旧内容，要么读到新内容，不存在中间态。"""
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, '.' + os.path.basename(path) + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return obj


def save_ai_config(cfg):
    return _atomic_write_json(AI_CONFIG_PATH, cfg)



# Ollama 安装包（OllamaSetup.exe）GitHub 加速镜像候选池：网页会自动探测其中可用者，免去人工替换失效链接。
# 前缀规则：base + '/https://github.com/ollama/ollama/releases/latest'
OLLAMA_INSTALL_MIRRORS = [
    'https://ghfast.top',
    'https://gh.llkk.cc',
    'https://gh-proxy.com',
    'https://ghproxy.com',
    'https://gh.ddlc.top',
    'https://gh.idayer.com',
    'https://gh.widyun.com',
    'https://gh.con.sh',
]


def probe_ollama_mirror(base, timeout=10):
    """探测单个 GitHub 加速镜像是否可用（能正常打开 Ollama releases 页、且证书安全）。
    返回 (ok, note)。证书不安全/超时/连接失败都会被判为不可用，正好过滤掉 ghproxy.net 这类坑。"""
    import ssl, urllib.request, urllib.error
    base = (base or '').strip().rstrip('/')
    if not base:
        return False, '空地址'
    target = base + '/https://github.com/ollama/ollama/releases/latest'
    try:
        req = urllib.request.Request(target, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            if code != 200:
                return False, 'HTTP %d' % code
            data = resp.read(300000).decode('utf-8', 'ignore')
        if 'Ollama' in data and ('OllamaSetup' in data or 'releases' in data or 'Setup' in data):
            return True, '可用：含 OllamaSetup.exe 下载'
        if 'Ollama' in data:
            return True, '可用（页面含 Ollama，建议点开确认）'
        return False, '未返回有效版本页'
    except urllib.error.HTTPError as e:
        return False, 'HTTP %d' % e.code
    except urllib.error.URLError as e:
        rs = str(getattr(e, 'reason', e))
        if 'CERT' in rs.upper() or 'certificate' in rs.lower():
            return False, '证书不安全'
        if 'timed out' in rs.lower() or 'timeout' in rs.lower():
            return False, '超时'
        return False, '连接失败'
    except ssl.SSLError:
        return False, '证书不安全'
    except Exception as e:
        return False, '失败：%s' % str(e)[:40]


def scan_ollama_mirrors(timeout=10):
    """并发探测全部候选镜像，返回 {mirrors:[{base,url,ok,note}], best(首个可用的 base 或 None), scanned_at}。"""
    import concurrent.futures, time
    mirrors = OLLAMA_INSTALL_MIRRORS

    def _probe(m):
        ok, note = probe_ollama_mirror(m, timeout)
        return {'base': m, 'url': m + '/https://github.com/ollama/ollama/releases/latest',
                'ok': ok, 'note': note}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(mirrors))) as ex:
        results = list(ex.map(_probe, mirrors))
    best = next((r['base'] for r in results if r['ok']), None)
    return {'mirrors': results, 'best': best, 'scanned_at': int(time.time())}


def ai_enabled(section='chat'):
    """判断某个 AI 段是否可用（base_url + api_key + model 齐备）。
    section='chat' 时兼容旧配置：没有 chat 段则回退看 vision 段（deepseek 等共用端点）。"""
    cfg = load_ai_config().get(section) or {}
    if section == 'chat' and not cfg:
        cfg = load_ai_config().get('vision') or {}
    return bool(cfg.get('base_url') and cfg.get('api_key') and cfg.get('model'))


def chat_cfg():
    """解说/剧情类纯文本 LLM 配置：优先读 ai_config.chat，回退到 vision（兼容旧配置，
    因 deepseek 等 chat 与 vision 共用 /chat/completions 端点）。画面描述仍走 vision 段。"""
    cfg = load_ai_config()
    return cfg.get('chat') or cfg.get('vision') or {}



def local_llm_enabled():
    """是否已启用本地模型（配置层面：enabled 且填了 base_url）。"""
    cfg = local_llm_cfg()
    return bool(cfg['enabled'] and cfg['base_url'])


def local_llm_ping():
    """轻量探活：GET {base_url}/models。返回 (ok, message)。不加载模型、不阻塞。"""
    cfg = local_llm_cfg()
    if not cfg['base_url']:
        return False, '未配置 base_url'
    import urllib.request
    try:
        req = urllib.request.Request(cfg['base_url'] + '/models', method='GET')
        if cfg['api_key']:
            req.add_header('Authorization', 'Bearer ' + cfg['api_key'])
        with urllib.request.urlopen(req, timeout=8) as r:
            return (r.status == 200), ('本地模型服务可达' if r.status == 200 else '服务返回 %s' % r.status)
    except Exception as e:
        return False, str(e)[:200]




# ---------------------------------------------------------------------------
# Whisper (本地 ASR) 模型配置：可切换 tiny/base/small/medium/large，权重缓存进项目目录
# ---------------------------------------------------------------------------
# 原值：['tiny','base','small','medium','large-v3','distil-large-v3']
# [B] 追加 large-v3-turbo：ASR 走 GPU 的推荐模型（已下载到 models/whisper/large-v3-turbo）

def whisper_model_ready(model=None):
    """检测指定(或当前配置)模型权重是否已在本机缓存，可直接加载。
    兼容两种布局：① 手动 aria2c 放置到 models/whisper/<m>/ 子目录；② faster-whisper 默认平铺到 models/whisper/。
    判据以 model.bin（核心权重）为准，避免 ai_config.json 等文件被 endswith('config.json') 误判为模型已就绪。"""
    m = model or whisper_model_name()

    def _check(dirpath):
        if not os.path.isdir(dirpath):
            return False
        try:
            names = os.listdir(dirpath)
        except Exception:
            return False
        if 'model.bin' in names:
            return True
        # 兼容某些发行版只放 .safetensors / vocab 的情况：需同时存在配置文件才视为就绪
        return (('config.json' in names or 'vocabulary.txt' in names)
                and any(n.endswith(('.bin', '.safetensors')) for n in names))

    # ① 子目录形式（手动放置）
    if _check(os.path.join(whisper_models_dir(), m)):
        return True
    # ② 平铺形式（faster-whisper 直接下载到 download_root）
    if _check(whisper_models_dir()):
        return True
    return False

_WHISPER_REPO = {
    'tiny': 'Systran/faster-whisper-tiny',
    'base': 'Systran/faster-whisper-base',
    'small': 'Systran/faster-whisper-small',
    'medium': 'Systran/faster-whisper-medium',
    'large-v3': 'Systran/faster-whisper-large-v3',
    'distil-large-v3': 'Systran/faster-distil-whisper-large-v3',
}

def whisper_prepare(model=None):
    """预下载/加载指定 whisper 模型（触发 faster-whisper 自动下载到项目目录）。返回 (ok,msg)。"""
    m = model or whisper_model_name()
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        return False, '未安装 faster-whisper：' + str(e)[:160]
    try:
        device, ctype = whisper_device()
        _whisper_env_setup()
        local = _whisper_load_path(m)
        if local != m:
            # 本地目录已就绪（手动 aria2c 放置 / 上次已复制真实文件）→ 直接加载，不再联网
            WhisperModel(local, device=device, compute_type=ctype)
            return True, '模型 %s 已就绪' % m
        # 本地未就绪 → 显式下载为「真实文件」(禁用符号链接)：Windows 下 huggingface_hub 默认用符号链接，
        # 在 bat 启动的标准 Python 中常解析失败 → WhisperModel 秒报错、状态回退「未下载」。强制复制可根除该坑。
        try:
            import huggingface_hub
            repo = _WHISPER_REPO.get(m, 'Systran/faster-whisper-' + m)
            huggingface_hub.snapshot_download(
                repo,
                local_dir=os.path.join(whisper_models_dir(), m),
                local_dir_use_symlinks=False,
            )
            # 下载成功 → 加载扁平目录（真实文件，无符号链接）
            WhisperModel(os.path.join(whisper_models_dir(), m), device=device, compute_type=ctype)
        except Exception:
            # 镜像 snapshot 拉取失败 → 退回 faster-whisper 自带下载（走同一镜像与代理环境）
            WhisperModel(m, device=device, compute_type=ctype, download_root=whisper_models_dir())
        return True, '模型 %s 已就绪' % m
    except Exception as e:
        e2 = str(e)
        hint = ''
        if '10061' in e2 or '拒绝' in e2 or 'ConnectError' in e2:
            hint = '（多为代理未开/镜像连不上：请在「🌐 国内下载镜像」检查代理是否运行，或改用直连；改完点「下载/预载」重试）'
        return False, '加载/下载失败：' + e2[:160] + hint

def whisper_download_async(model=None):
    """后台异步预下载 whisper 模型（避免阻塞 HTTP 请求）。通过 /api/whisper/status 轮询。"""
    m = model or whisper_model_name()
    if WHISPER_DL['running']:
        return False, '已有下载任务进行中（%s）' % WHISPER_DL['model']
    WHISPER_DL['model'] = m
    threading.Thread(target=_whisper_download_thread, args=(m,), daemon=True).start()
    return True, '已开始下载 %s（可在状态中查看进度）' % m


WHISPER_DL = {'model': None, 'running': False, 'ok': None, 'msg': ''}

def _whisper_download_thread(model):
    WHISPER_DL['running'] = True
    WHISPER_DL['ok'] = None
    WHISPER_DL['msg'] = '下载中…'
    # 统一环境：走 HF 镜像 + 清理失效代理（代理没开时 huggingface_hub 会撞 127.0.0.1 被拒 → 10061）
    _whisper_env_setup()
    ok, msg = whisper_prepare(model)
    WHISPER_DL['running'] = False
    WHISPER_DL['ok'] = ok
    WHISPER_DL['msg'] = msg


def vlm_enabled():
    return bool(vlm_cfg()['enabled'] and vlm_cfg()['base_url'])

def vlm_ping():
    """探活 + 检测目标模型是否已拉取。返回 (ok, msg)。"""
    c = vlm_cfg()
    if not c['base_url']:
        return False, '未配置 VLM 地址'
    import urllib.request, json as _json
    try:
        if c['mode'] == 'ollama':
            req = urllib.request.Request(c['base_url'] + '/api/tags', method='GET')
            with urllib.request.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read().decode('utf-8'))
            models = [m.get('name', '') for m in (data.get('models') or [])]
            if c['model'] in models:
                return True, 'Ollama 可达，模型 %s 已就绪' % c['model']
            return False, 'Ollama 可达，但未拉取 %s（请执行 ollama pull %s）' % (c['model'], c['model'])
        else:
            req = urllib.request.Request(c['base_url'] + '/models', method='GET')
            if c['api_key']:
                req.add_header('Authorization', 'Bearer ' + c['api_key'])
            with urllib.request.urlopen(req, timeout=8) as r:
                return (r.status == 200), 'VLM 服务可达'
    except Exception as e:
        return False, str(e)[:200]

def vlm_chat(image_path, text, system=None, timeout=180):
    """调用本地/云端 VLM：传入一张图 + 文本，返回模型文本回复。失败抛异常。"""
    c = vlm_cfg()
    if not c['base_url']:
        raise RuntimeError('VLM 未配置')
    import urllib.request, base64 as _b64, json as _json
    with open(image_path, 'rb') as f:
        b64 = _b64.b64encode(f.read()).decode('ascii')
    headers = {'Content-Type': 'application/json'}
    if c['api_key']:
        headers['Authorization'] = 'Bearer ' + c['api_key']
    if c['mode'] == 'ollama':
        msg = {'role': 'user', 'content': text, 'images': [b64]}
        messages = ([{'role': 'system', 'content': system}] if system else []) + [msg]
        payload = {'model': c['model'], 'messages': messages, 'stream': False}
        url = c['base_url'] + '/api/chat'
    else:
        content = [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
                   {'type': 'text', 'text': text}]
        messages = ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': content}]
        payload = {'model': c['model'], 'messages': messages, 'max_tokens': 600, 'temperature': 0.7}
        url = c['base_url'] + '/v1/chat/completions'
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'), headers=headers)

    def _call():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        if c['mode'] == 'ollama':
            return (data.get('message') or {}).get('content', '')
        return (data.get('choices') or [{}])[0].get('message', {}).get('content', '')

    # S7: 本地 ollama 推理独占 GPU 槽；openai 模式（可能云端）与远程不加锁
    if c['mode'] == 'ollama' and _is_local_url(c['base_url']):
        with _gpu_slot():
            return _call()
    return _call()



def vlm_text(text, system=None, timeout=180):
    """调用本地/云端 VLM 的纯文本能力（不带图）。用于在已有多帧剧情理解后，纯文本生成连贯解说稿，
    避免逐段重复看图导致的复读。失败抛异常。"""
    c = vlm_cfg()
    if not c['base_url']:
        raise RuntimeError('VLM 未配置')
    import urllib.request, json as _json
    headers = {'Content-Type': 'application/json'}
    if c['api_key']:
        headers['Authorization'] = 'Bearer ' + c['api_key']
    if c['mode'] == 'ollama':
        messages = ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': text}]
        payload = {'model': c['model'], 'messages': messages, 'stream': False}
        url = c['base_url'] + '/api/chat'
    else:
        messages = ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': text}]
        payload = {'model': c['model'], 'messages': messages, 'max_tokens': 1200, 'temperature': 0.8}
        url = c['base_url'] + '/v1/chat/completions'
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'), headers=headers)

    def _call():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        if c['mode'] == 'ollama':
            return _strip_think((data.get('message') or {}).get('content', ''))
        return _strip_think((data.get('choices') or [{}])[0].get('message', {}).get('content', ''))

    # S7: 本地 ollama 推理独占 GPU 槽；openai 模式（可能云端）与远程不加锁
    if c['mode'] == 'ollama' and _is_local_url(c['base_url']):
        with _gpu_slot():
            return _call()
    return _call()


VLM_PULL = {'model': None, 'running': False, 'ok': None, 'msg': '', 'pct': 0}
VLM_FAST_GGUF_SOURCES = {

    'qwen3-vl:8b': (

        'https://hf-mirror.com/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3VL-8B-Instruct-Q4_K_M.gguf',

        'Qwen3VL-8B-Instruct-Q4_K_M.gguf',

        'https://hf-mirror.com/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf',

        'mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf'),

}




def vlm_pull_async(model=None):
    """后台异步执行 `ollama pull <model>`（避免阻塞请求）。通过 /api/vlm/status 轮询。"""
    m = model or vlm_cfg()['model']
    running = _model_pull_running()
    if running:
        return False, '已有模型在后台下载（%s），请等它完成后再试' % running
    VLM_PULL['model'] = m
    threading.Thread(target=_vlm_pull_thread, args=(m,), daemon=True).start()
    return True, '已开始拉取 %s' % m

def _vlm_pull_thread(model):
    VLM_PULL['running'] = True
    VLM_PULL['ok'] = None
    VLM_PULL['pct'] = 0
    VLM_PULL['msg'] = '拉取中…'
    c = vlm_cfg()
    try:
        import subprocess as _sp, re as _re, urllib.request, json as _json
        if c['mode'] != 'ollama':
            VLM_PULL['ok'] = False
            VLM_PULL['msg'] = '当前为 openai 模式，请在你部署的服务端手动拉取模型'
            return
        base = c['base_url'].replace('/v1', '').rstrip('/')
        if not base:
            VLM_PULL['ok'] = False
            VLM_PULL['msg'] = 'VLM base_url 为空，请填写 Ollama 地址（如 http://localhost:11434）'
            return
        # 启动前先探测 Ollama 服务是否可用，避免拉取命令静默卡死 / 无报错
        try:
            _req = urllib.request.Request(base + '/api/version', method='GET')
            with urllib.request.urlopen(_req, timeout=8) as _r:
                _json.loads(_r.read().decode('utf-8'))
        except Exception as _e:
            VLM_PULL['ok'] = False
            VLM_PULL['msg'] = ('Ollama 服务未响应（%s）。请确认已安装并启动 Ollama（托盘应出现图标），'
                               '再点拉取；若端口不是 11434 请在上方修改 base_url。' % str(_e)[:120])
            return
        # 国内镜像：若配置了代理，让 ollama pull 走代理拉取，免科学上网
        # 加速通道：白名单模型走 hf-mirror GGUF + aria2c + ollama create（含视觉投影 mmproj）

        spec = VLM_FAST_GGUF_SOURCES.get(model)

        if spec:

            _fast_pull_local(model, spec=spec, slot=VLM_PULL)

            return

        mc = mirror_cfg()
        if mc['ollama_proxy']:
            os.environ['HTTP_PROXY'] = mc['ollama_proxy']
            os.environ['HTTPS_PROXY'] = mc['ollama_proxy']
        # 流式读取 ollama pull 输出，实时解析进度百分比（ollama 用 \r 原地刷新进度，
        # Python 文本模式会按行切分），避免全过程无进度、卡死也无报错。
        try:
            p = _sp.Popen(['ollama', 'pull', model], stdout=_sp.PIPE, stderr=_sp.STDOUT,
                          text=True, bufsize=1, encoding='utf-8', errors='ignore')
        except FileNotFoundError:
            VLM_PULL['ok'] = False
            VLM_PULL['msg'] = '未找到 ollama 命令：Ollama 未安装或未加入 PATH。请先安装 Ollama 并重启本工具。'
            return
        def _reader(stream):
            for _raw in stream:
                _line = _raw.strip()
                if not _line:
                    continue
                VLM_PULL['msg'] = _line[:400]
                _m = _re.search(r'(\d{1,3}(?:\.\d+)?)\s*%', _line)
                if _m:
                    try:
                        VLM_PULL['pct'] = min(100, max(0, float(_m.group(1))))
                    except Exception:
                        pass
        _t = threading.Thread(target=_reader, args=(p.stdout,), daemon=True)
        _t.start()
        try:
            rc = p.wait(timeout=1800)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
            rc = -1
        try:
            _t.join(timeout=5)
        except Exception:
            pass
        VLM_PULL['ok'] = (rc == 0)
        if rc != 0 and not VLM_PULL['msg']:
            VLM_PULL['msg'] = 'ollama pull 退出码 %d（可能网络不通 / 模型名错误 / 磁盘空间不足）' % rc
    except Exception as e:
        VLM_PULL['ok'] = False
        VLM_PULL['msg'] = str(e)[:300]
    finally:
        VLM_PULL['running'] = False

LOCAL_PULL = {'model': None, 'running': False, 'ok': None, 'msg': '', 'pct': 0}

def local_model_exists(model):
    """查询 Ollama 是否已存在该模型（/api/tags），避免对已装模型触发整包重复拉取。返回 True/False/None(未知)。"""
    cfg = local_llm_cfg()
    root = cfg['base_url'].replace('/v1', '').rstrip('/')
    if not root:
        return None
    import urllib.request, json as _json
    try:
        req = urllib.request.Request(root + '/api/tags', method='GET')
        if cfg['api_key']:
            req.add_header('Authorization', 'Bearer ' + cfg['api_key'])
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read().decode('utf-8'))
        names = [m.get('name', '') for m in data.get('models', [])]
        base = model.split(':')[0]
        for n in names:
            if n == model or n.startswith(model + ':') or n == base:
                return True
        return False
    except Exception:
        return None

def _model_pull_running():
    """任一模型（写稿/看图）正在后台下载时返回其名称；否则 None。
    同时下载两个大模型会互相抢带宽、进度条也互相干扰——统一串行。"""
    if LOCAL_PULL.get('running'):
        return LOCAL_PULL.get('model') or '本地模型'
    if VLM_PULL.get('running'):
        return VLM_PULL.get('model') or '视觉模型'
    return None


def local_pull_async(model=None, force=False):
    """后台异步执行 `ollama pull <model>`（文字解说模型）。通过 /api/local/status 轮询。
    若模型已存在则跳过重复拉取（手动 ollama create 导入的模型与官方打包版 manifest 不同，
    直接 ollama pull 会误判为需整包重下，浪费带宽）。"""
    m = model or local_llm_cfg()['model']
    running = _model_pull_running()
    if running:
        return False, '已有模型在后台下载（%s），请等它完成后再试' % running
    if not force:
        exists = local_model_exists(m)
        if exists is True:
            LOCAL_PULL['running'] = False
            LOCAL_PULL['ok'] = True
            LOCAL_PULL['model'] = m
            LOCAL_PULL['msg'] = '模型已存在，无需重复拉取（可直接点🧪测试连接）'
            return True, '模型已存在，无需重复拉取'
    LOCAL_PULL['model'] = m
    threading.Thread(target=_local_pull_thread, args=(m,), daemon=True).start()
    return True, '已开始拉取 %s' % m

# 国内加速源表：优先用「非 split 单文件 GGUF（bartowski, Q4_K_M）+ aria2c 多线程下载 + ollama create 本地导入」，
# 远快于官方 registry.ollama.ai。键为 ollama 模型名，值为 (GGUF 直链, 本地文件名)。
FAST_GGUF_SOURCES = {
    'qwen2.5:14b': ('https://hf-mirror.com/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf', 'qwen2.5-14b-instruct-q4_k_m.gguf'),
    'qwen2.5:7b': ('https://hf-mirror.com/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf', 'qwen2.5-7b-instruct-q4_k_m.gguf'),
    'qwen3:14b-q4_K_M': ('https://hf-mirror.com/bartowski/Qwen_Qwen3-14B-GGUF/resolve/main/Qwen_Qwen3-14B-Q4_K_M.gguf', 'Qwen_Qwen3-14B-Q4_K_M.gguf'),

    'qwen2.5:latest': ('https://hf-mirror.com/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf', 'qwen2.5-7b-instruct-q4_k_m.gguf'),
}


def _fast_pull_local(model, spec=None, slot=None):
    """加速通道：下载单文件 GGUF（可含 mmproj 视觉投影）+ aria2c 多线程 + ollama create 导入。
    spec: (url, 文件名) 或 (url, 文件名, mmproj_url, mmproj文件名)；缺省查 FAST_GGUF_SOURCES。
    slot: 进度槽（LOCAL_PULL / VLM_PULL）。返回 (ok, msg)。失败返回 (False, 原因)，由调用方回退官方源。"""
    import subprocess as _sp, urllib.request, shutil, time as _t, re as _re
    src = spec or FAST_GGUF_SOURCES.get(model)
    if not src:
        return False, '该模型没有内置加速源'
    slot = slot or LOCAL_PULL
    m_url, m_fname = src[0], src[1]
    p_url = src[2] if len(src) > 2 else None
    p_fname = src[3] if len(src) > 3 else None
    aria = shutil.which('aria2c')
    if not aria:
        return False, '未找到 aria2c（多线程下载器），可安装 aria2 后重试，或改用官方源'
    dl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_dl')
    os.makedirs(dl_dir, exist_ok=True)
    def _pull_one(u, fn, lo, hi):
        # 单文件下载（-c 断点续传；已有完整文件则跳过），进度按 lo~hi 区间映射
        total = 0
        try:
            _req = urllib.request.Request(u)
            _req.add_header('Range', 'bytes=0-0')
            with urllib.request.urlopen(_req, timeout=30) as _r:
                _cr = _r.headers.get('Content-Range', '') or ''
                if '/' in _cr:
                    total = int(_cr.split('/')[-1])
        except Exception:
            pass
        tgt = os.path.join(dl_dir, fn)
        if os.path.exists(tgt) and (not total or os.path.getsize(tgt) >= total):
            return True
        pp = _sp.Popen([aria, '-c', '-x', '8', '-s', '8', '-k', '1M', '--max-tries=0',
                        '--retry-wait=3', '--timeout=60', '--console-log-level=warn',
                        '-o', fn, u], cwd=dl_dir,
                       stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        while pp.poll() is None:
            if total and os.path.exists(tgt):
                slot['pct'] = min(hi, lo + int(os.path.getsize(tgt) * (hi - lo) // max(1, total)))
            _t.sleep(1)
        return pp.returncode == 0

    if not _pull_one(m_url, m_fname, 0, 85):
        return False, 'aria2c 下载失败，已回退官方源'
    if p_url:
        if not _pull_one(p_url, p_fname, 85, 92):
            return False, '视觉投影（mmproj）下载失败'
    # ollama create 导入（cmd /c 规避 PowerShell 对 stderr 进度条的误判；Modelfile 用绝对路径）
    mf = os.path.join(dl_dir, 'Modelfile_' + _re.sub(r'[^0-9A-Za-z]', '_', model))
    with open(mf, 'w', encoding='utf-8') as f:
        f.write('FROM ' + os.path.join(dl_dir, m_fname).replace('\\', '/') + '\n')
        if p_fname:

            f.write('FROM ' + os.path.join(dl_dir, p_fname).replace('\\', '/') + '\n')

    try:
        r = _sp.run(['cmd', '/c', 'ollama', 'create', model, '-f', mf],
                    capture_output=True, text=True, timeout=1200)
    except Exception as e:
        return False, 'ollama create 失败：%s' % str(e)[:120]
    finally:
        for _f in (os.path.join(dl_dir, m_fname), mf):
            try:
                os.remove(_f)
            except Exception:
                pass
    if r.returncode != 0:
        return False, 'ollama create 失败：%s' % ((r.stdout or r.stderr or '')[-200:])
    return True, '加速通道完成：%s' % m_fname


def _local_pull_thread(model):
    LOCAL_PULL['running'] = True
    LOCAL_PULL['ok'] = None
    LOCAL_PULL['pct'] = 0
    LOCAL_PULL['msg'] = '拉取中…'
    try:
        import subprocess as _sp, re as _re, urllib.request, json as _json
        cfg = local_llm_cfg()
        root = (cfg.get('base_url') or '').replace('/v1', '').rstrip('/')
        if not root:
            LOCAL_PULL['ok'] = False
            LOCAL_PULL['msg'] = '本地模型 base_url 为空，请填写 Ollama 地址（如 http://localhost:11434）'
            return
        # 启动前先探测 Ollama 服务是否可用，避免拉取命令静默卡死 / 无报错
        try:
            _req = urllib.request.Request(root + '/api/version', method='GET')
            with urllib.request.urlopen(_req, timeout=8) as _r:
                _json.loads(_r.read().decode('utf-8'))
        except Exception as _e:
            LOCAL_PULL['ok'] = False
            LOCAL_PULL['msg'] = ('Ollama 服务未响应（%s）。请确认已安装并启动 Ollama（托盘应出现图标），'
                                 '再点拉取；若端口不是 11434 请在上方修改 base_url。' % str(_e)[:120])
            return
        mc = mirror_cfg()
        if mc['ollama_proxy']:
            os.environ['HTTP_PROXY'] = mc['ollama_proxy']
            os.environ['HTTPS_PROXY'] = mc['ollama_proxy']
        # ---- 优先走国内加速通道（bartowski 单文件 + aria2c + ollama create），失败再回退官方源 ----
        if model in FAST_GGUF_SOURCES:
            LOCAL_PULL['msg'] = '使用国内加速通道（多线程下载单文件 GGUF + 本地导入）…'
            _fok, _fmsg = _fast_pull_local(model)
            if _fok:
                LOCAL_PULL['ok'] = True
                LOCAL_PULL['pct'] = 100
                LOCAL_PULL['msg'] = '✅ ' + _fmsg
                return
            LOCAL_PULL['msg'] = '加速通道未成功（%s），回退官方源拉取…' % _fmsg
        # 流式读取 ollama pull 输出，实时解析进度百分比（ollama 用 \r 原地刷新，Python 文本模式按行切分）
        try:
            p = _sp.Popen(['ollama', 'pull', model], stdout=_sp.PIPE, stderr=_sp.STDOUT,
                          text=True, bufsize=1, encoding='utf-8', errors='ignore')
        except FileNotFoundError:
            LOCAL_PULL['ok'] = False
            LOCAL_PULL['msg'] = '未找到 ollama 命令：Ollama 未安装或未加入 PATH。请先安装 Ollama 并重启本工具。'
            return
        def _reader(stream):
            for _raw in stream:
                _line = _raw.strip()
                if not _line:
                    continue
                LOCAL_PULL['msg'] = _line[:400]
                _m = _re.search(r'(\d{1,3}(?:\.\d+)?)\s*%', _line)
                if _m:
                    try:
                        LOCAL_PULL['pct'] = min(100, max(0, float(_m.group(1))))
                    except Exception:
                        pass
        _t = threading.Thread(target=_reader, args=(p.stdout,), daemon=True)
        _t.start()
        try:
            rc = p.wait(timeout=1800)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
            rc = -1
        try:
            _t.join(timeout=5)
        except Exception:
            pass
        LOCAL_PULL['ok'] = (rc == 0)
        if rc != 0 and not LOCAL_PULL['msg']:
            LOCAL_PULL['msg'] = 'ollama pull 退出码 %d（可能网络不通 / 模型名错误 / 磁盘空间不足）' % rc
    except Exception as e:
        LOCAL_PULL['ok'] = False
        LOCAL_PULL['msg'] = str(e)[:300]
    finally:
        LOCAL_PULL['running'] = False

def extract_segment_frames(video_path, segs, out_dir, max_side=640):
    """为每个镜头段抽 1 张中间帧(jpg)，用于 VLM 视觉理解。返回 {seg_idx: frame_path}。"""
    os.makedirs(out_dir, exist_ok=True)
    frames = {}
    vdur = probe_audio_len(video_path) or 0.0
    for i, (s0, s1) in enumerate(segs):
        mid = max(0.0, (s0 + s1) / 2.0)
        if vdur and mid > vdur:
            mid = max(0.0, vdur - 0.1)
        fp = os.path.join(out_dir, f'frame_{i}.jpg')
        rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.3f' % mid, '-i', video_path,
                                '-frames:v', '1', '-vf', 'scale=min(iw\\,%d):-2' % max_side,
                                '-q:v', '4', '-an', fp])
        if rc == 0 and os.path.exists(fp):
            frames[i] = fp
    return frames

# ---------------------------------------------------------------------------
# 硬件检测 + 模型推荐（根据用户显卡/内存给出最优模型配置）
# ---------------------------------------------------------------------------
HARDWARE_MODEL_RECS = [
    # (最低显存GB, 档位名, VLM推荐, Whisper推荐, TTS推荐, 文本模型推荐, 说明)
    (16, '旗舰独显(≥16GB)', 'qwen3-vl:30b', 'large-v3', 'melo-zh', 'qwen3:30b-q4_K_M',
     '16GB显存+32GB内存可跑30B MoE视觉模型（激活3B，速度快理解强），或Qwen3.8-27B原生多模态；MiniCPM-V4.5(8B)是速度优先的替代，视频token压缩96x适合长视频'),
    (12, '高端独显(12-16GB)', 'minicpm-v4.5', 'large-v3', 'melo-zh', 'qwen3:14b-q4_K_M',
     '推荐MiniCPM-V4.5（8B，视频理解专项优化，96x token压缩，同显存多看10倍帧）；或qwen3-vl:8b（综合强）；内存≥32GB可尝试qwen3-vl:30b MoE'),
    (8, '中高端独显(8-12GB)', 'minicpm-v4.5', 'large-v3', 'melo-zh', 'qwen3:14b-q4_K_M',
     'MiniCPM-V4.5（8B，视频理解强，显存占用小）或qwen3-vl:8b，Whisper GPU加速，MeloTTS配音'),
    (6, '中端独显(6-8GB)', 'qwen3-vl:8b', 'medium', 'melo-zh', 'qwen3:8b',
     '8B视觉模型可用，Whisper用medium平衡速度精度，MiniCPM-V4.5更省显存'),
    (4, '入门独显(4-6GB)', 'qwen3-vl:4b', 'small', 'piper-huayan', 'qwen3:8b',
     '4B视觉模型，Whisper用small，TTS用轻量piper'),
    (0, '纯CPU/无独显', 'qwen3-vl:4b', 'base', 'piper-huayan', 'qwen3:8b',
     '无显卡，全部走CPU，选最小模型保证速度'),
]


def detect_hardware():
    """检测本机 GPU/内存/Ollama 状态，返回硬件信息 + 推荐模型 + 当前配置对比。
    供前端 AI 设置页展示「你的硬件适合用什么模型」，避免用户盲目选小模型浪费显卡。"""
    info = {'gpu': None, 'gpu_vram_gb': 0, 'ram_gb': 0, 'ollama': False, 'ollama_models': [],
            'tier': None, 'recommendations': {}, 'current': {}, 'upgrades': []}
    # GPU
    try:
        import subprocess
        r = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split(',')
            info['gpu'] = parts[0].strip()
            info['gpu_vram_gb'] = round(int(parts[1].strip()) / 1024.0, 1)
    except Exception:
        pass
    # 内存
    try:
        import ctypes
        class _MS(ctypes.Structure):
            _fields_ = [('len', ctypes.c_ulong), ('load', ctypes.c_ulong),
                        ('totalPhys', ctypes.c_ulonglong), ('availPhys', ctypes.c_ulonglong),
                        ('totalPage', ctypes.c_ulonglong), ('availPage', ctypes.c_ulonglong),
                        ('totalVirtual', ctypes.c_ulonglong), ('availVirtual', ctypes.c_ulonglong),
                        ('availExtended', ctypes.c_ulonglong)]
        ms = _MS()
        ms.len = ctypes.sizeof(ms)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            info['ram_gb'] = round(ms.totalPhys / (1024**3), 1)
    except Exception:
        try:
            import psutil
            info['ram_gb'] = round(psutil.virtual_memory().total / (1024**3), 1)
        except Exception:
            pass
    # Ollama
    try:
        import urllib.request
        req = urllib.request.Request('http://localhost:11434/api/tags')
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read().decode('utf-8'))
        info['ollama'] = True
        info['ollama_models'] = [m.get('name', '') for m in data.get('models', [])]
    except Exception:
        info['ollama'] = False
    # 匹配档位
    vram = info['gpu_vram_gb']
    for min_vram, tier, vlm, whisper, tts, text, note in HARDWARE_MODEL_RECS:
        if vram >= min_vram:
            info['tier'] = tier
            info['recommendations'] = {'vlm': vlm, 'whisper': whisper, 'tts': tts, 'text': text, 'note': note}
            break
    # 当前配置
    cfg = load_ai_config()
    info['current'] = {
        'vlm': (cfg.get('vlm') or {}).get('model', ''),
        'whisper': (cfg.get('whisper') or {}).get('model', 'base'),
        'tts_engine': (cfg.get('tts_local') or {}).get('engine', ''),
        'tts_sherpa': (cfg.get('tts_local') or {}).get('sherpa_model', ''),
        'text': (cfg.get('local') or {}).get('model', ''),
    }
    # 可升级项对比
    rec = info['recommendations']
    cur = info['current']
    if rec.get('vlm') and cur.get('vlm') and rec['vlm'] != cur['vlm']:
        info['upgrades'].append({'slot': 'VLM 视觉模型', 'current': cur['vlm'], 'recommend': rec['vlm'],
                                 'reason': '你的显存放得下更大的视觉模型，剧情理解会更强'})
    if rec.get('whisper') and cur.get('whisper') and rec['whisper'] != cur['whisper']:
        info['upgrades'].append({'slot': 'Whisper 转写模型', 'current': cur['whisper'], 'recommend': rec['whisper'],
                                 'reason': '大模型转写更准，GPU 加速后速度也快'})
    if rec.get('tts') == 'melo-zh' and not _sherpa_ready('melo-zh'):
        info['upgrades'].append({'slot': 'TTS 配音模型', 'current': 'piper(机械感)', 'recommend': 'melo-zh(自然)',
                                 'reason': 'MeloTTS 中文自然度明显更好，项目已支持只需下载'})
    return info


def _voice_status():
    """汇总配音链路就绪状态，供生成前置引导（ai_status().voice）。

    就绪定义（防「静默掉到 SAPI 机械音」）：
    - 显式选 sapi → 用户已知情接受机械音，视为就绪；
    - 其余情况：云端 TTS Key 或本地任一「自然语音引擎」可用即视为就绪
      （edge / CosyVoice / ChatTTS / 离线 sherpa 任一可用，配音会自动按序兜底，
       不会静默落 SAPI）。
    全部探测均为本地导入/文件系统检查，不联网、不下载；单点探测异常按不可用处理。
    """
    e = {
        'cloud': {'ready': False},
        'edge': {'ready': False},
        'cosyvoice': {'ready': False},
        'chattts': {'ready': False},
        'sherpa': {'ready': False},
        'sapi': {'ready': False},
    }

    def _probe(fn):
        try:
            return bool(fn())
        except Exception:
            return False

    try:
        cfg = tts_local_cfg() or {}
        engine = str(cfg.get('engine') or 'auto').lower()
    except Exception:
        engine = 'auto'
    try:
        label = local_tts_label() or ''
    except Exception:
        label = ''
    try:
        cloud_cfg = (load_ai_config().get('tts') or {})
        cloud = bool(cloud_cfg.get('api_key') and cloud_cfg.get('model')) and (
            (str(cloud_cfg.get('provider') or 'openai').lower() in ('dashscope', 'mimo'))
            or bool(cloud_cfg.get('base_url')))
    except Exception:
        cloud = False
    e['cloud']['ready'] = cloud
    e['edge']['ready'] = _probe(edge_tts_available) and not _probe(edge_tts_dead_reason)
    e['cosyvoice']['ready'] = _probe(cosyvoice_available)
    e['chattts']['ready'] = _probe(chattts_available)
    e['sherpa']['ready'] = _probe(sherpa_tts_ready)
    e['sapi']['ready'] = True  # Windows 系统兜底（pyttsx3 存在即视为可用）

    chain = cloud or e['edge']['ready'] or e['cosyvoice']['ready'] \
        or e['chattts']['ready'] or e['sherpa']['ready']
    # selected_ready：所选引擎自身是否就绪（auto → 看整条自然链）
    selected_ready = chain if engine == 'auto' else (
        True if engine == 'sapi' else bool(e.get(engine, {}).get('ready')))
    # ready：最终会不会静默落 SAPI。显式 sapi 视为用户已接受；其余只要自然链非空即可。
    ready = True if engine == 'sapi' else chain
    if ready:
        reason = '配音就绪：' + (label or '自然语音引擎可用')
    else:
        missing = []
        if not cloud:
            missing.append('云端 TTS Key')
        if not e['edge']['ready']:
            missing.append('Edge-tts(需能连微软朗读)')
        if not e['cosyvoice']['ready']:
            missing.append('CosyVoice(需安装)')
        if not e['chattts']['ready']:
            missing.append('ChatTTS(需安装)')
        if not e['sherpa']['ready']:
            missing.append('离线 sherpa 模型(需下载)')
        reason = '当前无可用的自然配音引擎（' + '、'.join(missing) + '均未就绪），只剩系统 SAPI 机械音。'
    return {
        'engine': engine,
        'label': label,
        'ready': ready,
        'selected_ready': selected_ready,
        'reason': reason,
        'engines': e,
    }


_ai_status_cache = {'time': 0, 'data': None}
def ai_status():
    """返回各 AI 能力的就绪状态，供前端做生成前置引导。
    5秒缓存：vlm_ping() 会阻塞，前端轮询时避免重复检查导致超时。"""
    import time as _time
    now = _time.time()
    if _ai_status_cache['data'] and now - _ai_status_cache['time'] < 5.0:
        return _ai_status_cache['data']
    vok, vmsg = (vlm_ping() if vlm_enabled() else (False, 'VLM 未启用'))
    _result = {
        'chat': ai_enabled('chat'),
        'vision': ai_enabled('vision'),
        'tts': _tts_available(),
        'tts_local': {
            'cfg': tts_local_cfg(),
            'edge_installed': edge_tts_available(),
            'edge_dead': edge_tts_dead_reason(),
            'sherpa_installed': sherpa_tts_available(),
            'sherpa_model_ready': sherpa_tts_ready(),
            'sherpa_model': sherpa_model_key(),
            'sherpa_models': [{'key': k, 'label': m['label'], 'ready': _sherpa_ready(k)}
                              for k, m in SHERPA_TTS_MODELS.items()],
            'voices': EDGE_TTS_VOICES,
            'label': local_tts_label(),
        },
        'local': local_llm_enabled(),
        'whisper_model': whisper_model_name(),
        'whisper_ready': whisper_model_ready(),
        'vlm_enabled': vlm_enabled(),
        'vlm_ready': bool(vok) if vlm_enabled() else False,
        'vlm_msg': vmsg,
        'narr_guide': _model_narr_guide(),
        'any_ai': ai_enabled('chat') or ai_enabled('vision'),
        'configured': bool(load_ai_config()),
        'voice': _voice_status(),
        'mirror': mirror_cfg(),
    }
    _ai_status_cache['time'] = now
    _ai_status_cache['data'] = _result
    return _result


def compute_mode(params, needs_chat=True):
    """判定本次任务实际会以哪种模式运行，用于结果打标与前置引导。
    返回 'free'（免费模板/离线）或 'ai'（真 AI）。
    - economy 显式 False 且 chat 已配置 → 'ai'
    - 其余（默认免费 / 缺 key 强制降级）→ 'free'"""
    econ = params.get('economy')
    if econ is None:
        econ = True  # 默认省流免费
    if needs_chat and not ai_enabled('chat'):
        econ = True  # 缺 LLM key 强制降级为免费
    return 'free' if econ else 'ai'

def _safe_filename(name):
    import re as _re
    return _re.sub(r'[\\/:*?"<>|]', '_', name or '')[:120] or 'video'


# ---------------------------------------------------------------------------
# 🖼 封面生成：从成片智能选帧（对比度+细节打分）+ 大字标题合成封面图。
# 发抖音/B站都需要封面——出片后一键生成，可换帧/改标题/换版式。
# ---------------------------------------------------------------------------
COVER_CANDIDATES = 8


def _cover_score(im):
    """封面帧打分：对比度(灰度std) + 细节(边缘能量) - 过曝过暗惩罚，分越高越"有内容"。"""
    import numpy as np
    g = np.asarray(im.convert('L'), dtype=np.float32)
    contrast = float(g.std())
    edges = np.asarray(im.convert('L').filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    detail = float(edges.mean())
    ext = float((g < 8).mean() + (g > 247).mean())
    return round(contrast * 0.6 + detail * 2.0 - ext * 40.0, 1)


def _cover_candidates_sequential(video_path, cand_dir, ts_list, max_side=640, only=None):
    """逐帧抽帧（保底实现）：每个候选单独起一次 ffmpeg，-ss 精确 seek + 单帧输出。

    批量抽帧不可用时（老版本 ffmpeg 不认 -fps_mode / fps:start_time，或落盘帧数不足）
    退回本函数。only 为下标列表时只补抽这些下标（批量已抽到的不重复劳动）。
    共 ts_list 与 cand_dir 两个参数：时间与目录由调用方统一算好，避免两条路径漂移。"""
    os.makedirs(cand_dir, exist_ok=True)
    idxs = range(len(ts_list)) if only is None else only
    for k in idxs:
        fp = os.path.join(cand_dir, 'cand_%02d.jpg' % k)
        rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.2f' % ts_list[k], '-i', video_path,
                                 '-frames:v', '1', '-vf', 'scale=min(iw\\,%d):-2' % max_side,
                                 '-q:v', '4', '-an', fp])
        if rc != 0 and os.path.isfile(fp):
            # 失败残留的半张图会污染候选列表，直接清掉交给上层判缺失
            try:
                os.remove(fp)
            except OSError:
                pass


def _cover_candidates_batch(video_path, cand_dir, n, max_side, vdur):
    """一次 ffmpeg 调用抽 n 帧：fps 滤镜按 n/vdur 分桶 + scale + 模板输出 cand_%02d.jpg。
    返回 True 表示 n 张 jpg 全部落盘。

    【为什么要这么写】fps 滤镜默认的 round=near 会把时间轴按 vdur/n 分桶，桶边界正好
    落在 (k+0.5)*vdur/n —— 也就是逐帧路径的 ts_k，所以「不碰时间轴」反而天然对齐。
    实测（1080p/30fps 样片，n=4/6/8/12）抽到的帧与逐帧 -ss 抽到的相差 <8/255 灰度，
    肉眼无差别。以下三种「看起来更对」的写法实测都是错的，别再改回去：
      · fps=...:start_time=ts0 —— start_time 会重置锚点，桶边界整体后移 vdur/2n
        （实测 0.625s），缩略图与最终封面完全不是同一画面（差异 35~120）。
      · -ss ts0（输入侧 seek）—— 带滤镜链时输出时间戳不因 -ss 归零，同样整组后移。
      · -ss ts0 + setpts=PTS-ts0/TB —— 锚点被挪到首个候选时刻之后，同样错位。
    另注：fps 取的是「桶内最后一帧」，逐帧 -ss 取的是「ts 之后第一帧」，两者最多差
    一帧（33ms@30fps）；这条差异无法通过挪网格消除，只能接受。
    三级回退：-fps_mode vfr（ffmpeg 6+）→ -vsync 0（旧版）→ 全失败交给调用方逐帧抽。"""
    out_tpl = os.path.join(cand_dir, 'cand_%02d.jpg')
    rate = n / vdur if vdur > 0 and n > 0 else 1.0
    vf = 'fps=fps=%.6f,scale=min(iw\\,%d):-2' % (rate, max_side)
    for extra in (['-fps_mode', 'vfr'], ['-vsync', '0']):
        # -start_number 0 必须显式给：image2 默认从 1 开始编号，与逐帧路径的
        # cand_%02d.jpg（0 起）对不上，会让「帧已抽到」的判定全部落空而白白回退。
        # 输出选项一律排在 -an 之前：保持「输出路径紧跟 -an」的位置约定。
        rc, _o, _e = ffmpeg_run(['-y', '-i', video_path, '-vf', vf, '-q:v', '4',
                                 '-start_number', '0'] + extra + ['-an', out_tpl])
        if rc == 0 and all(os.path.isfile(out_tpl % k) for k in range(n)):
            # fps 滤镜会向上取整多吐一帧（时刻已超出片长，是尾帧的重复），删掉免得
            # 留在 run_dir 里被当成有效候选 / 中间产物
            for name in sorted(os.listdir(cand_dir)):
                m = re.match(r'cand_(\d+)\.jpg$', name)
                if m and int(m.group(1)) >= n:
                    try:
                        os.remove(os.path.join(cand_dir, name))
                    except OSError:
                        pass
            return True
    return False


def _cover_candidates(video_path, run_dir, n=COVER_CANDIDATES, max_side=640):
    """均匀抽 n 帧做候选（预览小图存 run_dir/cover_cand，带打分）。返回按时间序的候选列表。

    默认走一次 ffmpeg 批量抽 n 帧（原来 n 次进程 → 1 次，省掉 n-1 次 seek+解码开销）；
    批量不可用或落盘不足时按缺失下标逐帧补抽，失败则退回全量逐帧。"""
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        raise RuntimeError('无法读取视频时长')
    cand_dir = os.path.join(run_dir, 'cover_cand')
    os.makedirs(cand_dir, exist_ok=True)
    ts_list = [round(vdur * (k + 0.5) / n, 2) for k in range(n)]
    if _cover_candidates_batch(video_path, cand_dir, n, max_side, vdur):
        missing = [k for k in range(n) if not os.path.isfile(os.path.join(cand_dir, 'cand_%02d.jpg' % k))]
        if missing:
            # 批量抽够了但个别帧缺（尾部帧可能落在时长之外）→ 只补这几帧
            _cover_candidates_sequential(video_path, cand_dir, ts_list, max_side, only=missing)
    else:
        _cover_candidates_sequential(video_path, cand_dir, ts_list, max_side)
    out = []
    for k, ts in enumerate(ts_list):
        fp = os.path.join(cand_dir, 'cand_%02d.jpg' % k)
        if not os.path.isfile(fp):
            continue
        try:
            score = _cover_score(Image.open(fp))
        except Exception:
            score = 0.0
        out.append({'ts': ts, 'thumb': os.path.relpath(fp, OUTDIR).replace('\\', '/'),
                    'score': score})
    return out


def _cover_render(video_path, ts, title, sub, style, out_path, w_cap=1920):
    """抽全分辨率帧 + 叠加标题/副标题 → 封面图。style: 0 居中大字 / 1 底部条幅 / 2 左上角。"""
    tmp = out_path + '.frame.jpg'
    rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.2f' % ts, '-i', video_path,
                             '-frames:v', '1', '-q:v', '2', '-an', tmp])
    if rc != 0 or not os.path.isfile(tmp):
        raise RuntimeError('抽帧失败（该时间点可能超出视频范围）')
    im = Image.open(tmp).convert('RGB')
    try:
        os.remove(tmp)
    except OSError:
        pass
    if im.width > w_cap:
        im = im.resize((w_cap, int(im.height * w_cap / im.width)), Image.LANCZOS)
    draw = ImageDraw.Draw(im, 'RGBA')
    W, H = im.size
    fs = max(28, W // 16)

    def font(size):
        # 找不到含中文字形的字体时显式报错（cjk_font 抛 FontMissingError），
        # 绝不回退到 ImageFont.load_default() —— 那个字体不含中文，只会画出豆腐块。
        return cjk_font(size)

    def wrap(text, fnt, maxw, maxlines=3):
        lines, cur = [], ''
        for ch in text:
            if draw.textlength(cur + ch, font=fnt) <= maxw or not cur:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        return lines[:maxlines]

    t = _clean_caption(title)
    lines = wrap(t, font(fs), int(W * 0.86)) if t else []
    lh = int(fs * 1.3)
    if style == 1 and lines:   # 底部条幅：半透明黑条 + 白字
        bh = int(H * 0.08) * len(lines) + int(H * 0.04)
        overlay = Image.new('RGBA', im.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle([0, H - bh, W, H], fill=(0, 0, 0, 150))
        im = Image.alpha_composite(im.convert('RGBA'), overlay).convert('RGB')
        draw = ImageDraw.Draw(im, 'RGBA')
        y = H - bh + int(H * 0.02)
        for ln in lines:
            draw.text((int(W * 0.04), y), ln, font=font(fs), fill=(255, 255, 255, 255))
            y += lh
    else:                       # 居中大字 / 左上角：白字黑描边
        y = (H - len(lines) * lh) // 2 if style == 0 else int(H * 0.06)
        for ln in lines:
            x = int((W - draw.textlength(ln, font=font(fs))) // 2) if style == 0 else int(W * 0.05)
            draw.text((x, y), ln, font=font(fs), fill=(255, 255, 255, 255),
                      stroke_width=max(2, fs // 14), stroke_fill=(0, 0, 0, 220))
            y += lh
    if sub:
        f_sub = font(max(20, fs // 2))
        sw = draw.textlength(sub, font=f_sub)
        sx = int((W - sw) // 2) if style != 2 else int(W * 0.05)
        sy = min(int(H * 0.9), y + int(fs * 0.2))
        draw.text((max(4, sx), sy), sub, font=f_sub, fill=(255, 255, 255, 230),
                  stroke_width=2, stroke_fill=(0, 0, 0, 200))
    im.save(out_path, quality=90)
    return out_path


# ---------------------------------------------------------------------------
# 🗂 本地素材库：独立文件夹 material_library/，素材持久保存、刷新/重启不丢。
# 上传（小文件 base64 / 大文件复用分片协议）、从成片目录存入（如 B 站下载的视频）、
# 删除；任务请求里 video/item 传 {name, mlib} 即可直接使用库内素材（copy 进 run_dir）。
# ---------------------------------------------------------------------------
MATERIAL_DIR = os.path.join(HERE, 'material_library')
_MATERIAL_META_FILE = os.path.join(MATERIAL_DIR, '.metadata.json')


def _material_meta_load():
    """读取素材元数据（标签/收藏）。文件不存在或损坏返回空 dict。"""
    try:
        with open(_MATERIAL_META_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _material_meta_save(meta):
    """持久化素材元数据到 sidecar JSON。"""
    os.makedirs(MATERIAL_DIR, exist_ok=True)
    with open(_MATERIAL_META_FILE, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)


def material_set_meta(name, tags=None, favorite=None):
    """更新单个素材的标签和/或收藏状态。返回 (ok, error)。"""
    fp = _material_path(name)
    if not fp:
        return False, '素材不存在'
    meta = _material_meta_load()
    entry = meta.get(name, {})
    if tags is not None:
        entry['tags'] = [t.strip() for t in tags if t and t.strip()][:10]
    if favorite is not None:
        entry['favorite'] = bool(favorite)
    if entry:
        meta[name] = entry
    else:
        meta.pop(name, None)
    try:
        _material_meta_save(meta)
    except Exception as e:
        return False, str(e)[:80]
    return True, ''


def material_all_tags():
    """返回素材库中所有已使用的标签（去重、排序），供前端标签筛选器使用。"""
    meta = _material_meta_load()
    tags = set()
    for entry in meta.values():
        for t in entry.get('tags', []):
            tags.add(t)
    return sorted(tags)


def _material_path(name):
    """素材库内文件的安全路径（防穿越 + 必须存在）；非法返回 None。"""
    return _safe_join(MATERIAL_DIR, name)


def material_list():
    """列出素材库中的视频/图片素材（按名称排序），含标签与收藏信息。"""
    if not os.path.isdir(MATERIAL_DIR):
        os.makedirs(MATERIAL_DIR, exist_ok=True)
    meta = _material_meta_load()
    out = []
    for fn in sorted(os.listdir(MATERIAL_DIR)):
        p = os.path.join(MATERIAL_DIR, fn)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext in ('.mp4', '.mov', '.webm', '.avi', '.mkv', '.m4v'):
            kind = 'video'
        elif ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'):
            kind = 'image'
        else:
            continue
        try:
            entry = meta.get(fn, {})
            out.append({'name': fn, 'kind': kind, 'size': os.path.getsize(p),
                        'mtime': int(os.path.getmtime(p)),
                        'tags': entry.get('tags', []),
                        'favorite': entry.get('favorite', False)})
        except OSError:
            pass
    return out


def material_save_file(src_path):
    """把一个已存在的文件复制进素材库（重名自动加 (1)(2) 序号）。返回最终文件名。"""
    os.makedirs(MATERIAL_DIR, exist_ok=True)
    base = os.path.basename(src_path)
    stem, ext = os.path.splitext(base)
    candidate, i = base, 1
    while os.path.exists(os.path.join(MATERIAL_DIR, candidate)):
        candidate = '%s(%d)%s' % (stem, i, ext)
        i += 1
    shutil.copy2(src_path, os.path.join(MATERIAL_DIR, candidate))
    return candidate


# 可执行/可渲染扩展名：这类文件放进素材库并被同源访问即为存储型 XSS
# （配合 /material_lib/ 的 attachment 响应与 nosniff，构成纵深防御）
_MATERIAL_BLOCKED_EXT = {'.html', '.htm', '.xhtml', '.svg', '.js', '.mjs',
                         '.xml', '.xsl', '.swf', '.php', '.jsp'}


def material_save_bytes(name, data):
    """上传的字节存入素材库（保留原始文件名，重名自动加序号）。返回 (最终文件名|None, error)。"""
    base = os.path.basename(_safe_filename(name or ''))
    if not base or not data:
        return None, '文件名或内容为空'
    if os.path.splitext(base)[1].lower() in _MATERIAL_BLOCKED_EXT:
        return None, '不支持该文件类型（网页/脚本类文件不能作为素材）'
    os.makedirs(MATERIAL_DIR, exist_ok=True)
    stem, ext = os.path.splitext(base)
    candidate, i = base, 1
    while os.path.exists(os.path.join(MATERIAL_DIR, candidate)):
        candidate = '%s(%d)%s' % (stem, i, ext)
        i += 1
    with open(os.path.join(MATERIAL_DIR, candidate), 'wb') as f:
        f.write(data)
    return candidate, ''


def material_delete(name):
    fp = _material_path(name)
    if not fp:
        return False, '素材不存在'
    try:
        os.remove(fp)
    except OSError as e:
        return False, str(e)[:80]
    meta = _material_meta_load()
    if name in meta:
        del meta[name]
        try:
            _material_meta_save(meta)
        except Exception:
            pass
    return True, ''


_HIST_LOCK = threading.RLock()   # history.json 读写锁（可重入：add_history 持锁时会再调 load_history）；防丢条目、防读到半写文件


def load_history(limit=50):
    try:
        with _HIST_LOCK:
            with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
                items = json.load(f)
        if not isinstance(items, list):
            items = []
    except FileNotFoundError:
        items = []
    except Exception:
        # 文件存在但解析失败（半截 JSON / 被写坏）：必须**保留现场**再返回空。
        # 旧实现直接返回 []，调用方随后以 [] 为基底整体覆盖写回，损坏文件被彻底抹掉 ——
        # 用户看到「历史全空」且无从恢复。这里改名留档，至少还能人工捞回来。
        items = []
        try:
            if os.path.exists(HISTORY_PATH):
                bak = HISTORY_PATH + '.corrupt.' + time.strftime('%Y%m%d-%H%M%S')
                os.replace(HISTORY_PATH, bak)
                print('[警告] history.json 解析失败，已留档为 %s，本次以空历史启动' % bak)
        except Exception:
            pass
    return items[:limit]


def _record_history(req, prog, kind=''):
    """统一写生成历史（⑨记录）。此前只有一键合成写历史，强卡点/解说/联网解说/
    按方案渲染的成片都不会出现在记录里。失败静默，不影响出片主流程。
    容量淘汰遵循 add_history 的规则（超 100 条丢弃最旧并清理其成片文件）。"""
    try:
        if not prog.get('file'):
            return
        f = os.path.join(OUTDIR, prog['file'])
        add_history({
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'file': prog['file'],
            'duration': round(float(probe_audio_len(f) or 0.0), 2),
            'music': (req.get('music') or {}).get('name') if isinstance(req.get('music'), dict) else None,
            'voice': False, 'captions': [],
            'kind': kind,
        })
    except Exception:
        pass


def _safe_join(base, name):
    """拼接文件路径并校验结果仍落在 base 目录内。防 /media/../ 等目录穿越
    读到 ai_config.json 等敏感文件（默认只绑 127.0.0.1 风险低，但 Docker 部署
    HOST=0.0.0.0 时必须兜住）。返回可读文件的绝对路径；穿越/不存在返回 None。"""
    try:
        if not name or name.startswith(('/', '\\')) or ':' in name:
            return None
        base_abs = os.path.abspath(base)
        full = os.path.abspath(os.path.join(base_abs, name))
        if os.path.commonpath([base_abs, full]) != base_abs:
            return None
        return full if os.path.isfile(full) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 🧹 存储管理：扫描各类磁盘占用 + 安全删除（路径白名单防穿越）
# ---------------------------------------------------------------------------
def _storage_dir_size(p):
    """递归统计目录体积（字节）。"""
    tot = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    tot += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return tot


# 可被存储面板安全删除的路径白名单（相对项目根，正则匹配；禁止任何穿越/越界子路径）
_STORAGE_ALLOW = [
    r'^webui_output/run-[^/]+$',
    r'^webui_output/_warmup_[0-9a-f]{32}$',
    r'^webui_workspace/uploads/up-[0-9A-Za-z-]+$',
    r'^webui_workspace/asr_[0-9]+\.wav$',
    r'^webui_workspace/music_[0-9]+\.(mp3|wav)$',
    # 素材落盘名带 runid（见 dispatch_build），故第一段允许字母/数字/横线下划线
    r'^webui_workspace/up_[0-9A-Za-z_-]+_[a-z]+\.(jpg|png|webp|mp4)$',
    r'^webui_workspace/analysis_cache$',
    r'^models(/whisper)?$',
]


def _storage_resolve_deletable(rel):
    """校验 rel 是否为可清理路径，返回绝对路径；否则 None（拒绝穿越/越权删除）。"""
    import re as _re
    if not rel:
        return None
    rel = rel.replace('\\', '/')
    if rel.startswith('/') or '..' in rel.split('/'):
        return None
    for pat in _STORAGE_ALLOW:
        if _re.match(pat, rel):
            full = os.path.normpath(os.path.join(HERE, rel))
            base_abs = os.path.abspath(HERE)
            if os.path.commonpath([base_abs, full]) == base_abs:
                return full
            return None
    return None


def _storage_scan():
    """扫描项目内各类磁盘占用，分组返回，供前端存储管理面板展示与清理。

    档位：keep=保留不可删 / safe=临时可回收 / review=删除需重新下载。
    """
    import re as _re
    groups = []

    out_names = sorted(os.listdir(OUTDIR)) if os.path.isdir(OUTDIR) else []
    out_items, out_total = [], 0
    run_items, run_total = [], 0
    for name in out_names:
        p = os.path.join(OUTDIR, name)
        if not os.path.isdir(p):
            continue
        s = _storage_dir_size(p)
        mtime = int(os.path.getmtime(p))
        if name.startswith(('run-', '_warmup_')):
            run_total += s
            run_items.append({'name': name, 'rel': 'webui_output/' + name,
                              'size': s, 'mtime': mtime})
        else:
            out_total += s
            out_items.append({'name': name, 'rel': 'webui_output/' + name,
                              'size': s, 'mtime': mtime})
    groups.append({'key': 'outputs', 'label': '成片（webui_output 下日期目录）',
                   'tier': 'keep', 'deletable': False, 'total': out_total, 'items': out_items})
    groups.append({'key': 'run_residual', 'label': '任务残留（run-* / _warmup_* 临时产物）',
                   'tier': 'safe', 'deletable': True, 'total': run_total, 'items': run_items})

    up_items, up_total = [], 0
    if os.path.isdir(UPLOAD_DIR):
        for name in sorted(os.listdir(UPLOAD_DIR)):
            p = os.path.join(UPLOAD_DIR, name)
            if os.path.isdir(p):
                s = _storage_dir_size(p)
                up_total += s
                up_items.append({'name': name, 'rel': 'webui_workspace/uploads/' + name,
                                 'size': s, 'mtime': int(os.path.getmtime(p))})
    groups.append({'key': 'uploads', 'label': '上传会话成品（webui_workspace/uploads）',
                   'tier': 'safe', 'deletable': True, 'total': up_total, 'items': up_items})

    def temp_group(pattern, key, label):
        items, total = [], 0
        if os.path.isdir(WORKDIR):
            for fn in sorted(os.listdir(WORKDIR)):
                fp = os.path.join(WORKDIR, fn)
                if _re.match(pattern, fn) and os.path.isfile(fp):
                    s = os.path.getsize(fp)
                    total += s
                    items.append({'name': fn, 'rel': 'webui_workspace/' + fn,
                                  'size': s, 'mtime': int(os.path.getmtime(fp))})
        groups.append({'key': key, 'label': label, 'tier': 'safe',
                       'deletable': True, 'total': total, 'items': items})

    temp_group(r'^asr_[0-9]+\.wav$', 'asr_temp', 'ASR 临时音频（asr_*.wav）')
    temp_group(r'^music_[0-9]+\.(mp3|wav)$', 'music_temp', '音乐临时文件（music_*.mp3/wav）')
    temp_group(r'^up_[0-9A-Za-z_-]+_[a-z]+\.(jpg|png|webp|mp4)$', 'upload_leftover', '上传素材残留（up_*_*）')

    ac = os.path.join(WORKDIR, 'analysis_cache')
    ac_size = _storage_dir_size(ac) if os.path.isdir(ac) else 0
    groups.append({'key': 'analysis_cache', 'label': '分析缓存（webui_workspace/analysis_cache）',
                   'tier': 'safe', 'deletable': True, 'total': ac_size,
                   'items': [{'name': 'analysis_cache', 'rel': 'webui_workspace/analysis_cache',
                              'size': ac_size, 'mtime': int(os.path.getmtime(ac))}] if ac_size else []})

    models_dir = os.path.join(HERE, 'models')
    if os.path.isdir(models_dir):
        msize = _storage_dir_size(models_dir)
        groups.append({'key': 'models', 'label': '模型权重（models/，删除需重新下载）',
                       'tier': 'review', 'deletable': True, 'total': msize,
                       'items': [{'name': 'models', 'rel': 'models',
                                  'size': msize, 'mtime': int(os.path.getmtime(models_dir))}]})

    git_dir = os.path.join(HERE, '.git')
    if os.path.isdir(git_dir):
        gsize = _storage_dir_size(git_dir)
        groups.append({'key': 'git', 'label': '.git 版本历史（不建议删）',
                       'tier': 'keep', 'deletable': False, 'total': gsize, 'items': []})

    reclaimable = sum(g['total'] for g in groups if g['tier'] in ('safe', 'review') and g['deletable'])
    total_all = sum(g['total'] for g in groups)
    try:
        free = shutil.disk_usage(HERE).free
    except Exception:
        free = 0
    return {'ok': True, 'groups': groups, 'total_bytes': total_all,
            'reclaimable_bytes': reclaimable, 'free_bytes': free}


# ---------------------------------------------------------------------------
# 📤 大文件分片上传：>64MB 的视频走「分片 base64」而非一次性 base64 JSON——
# 旧路径会把整个文件膨胀 1.37 倍塞进一个 JSON（334MB 视频 ≈ 446MB 请求体 + GB 级内存峰值）。
# 三接口：init(开会话) → chunk(乱序写分片) → done(按序合并)。小文件仍走 base64 旧路径。
# ---------------------------------------------------------------------------
UPLOAD_DIR = os.path.join(WORKDIR, 'uploads')
UPLOAD_CHUNK_MAX = 8 * 1024 * 1024          # 单分片解码后上限
UPLOAD_TOTAL_MAX = 2 * 1024 * 1024 * 1024   # 单文件总量上限 2GB


def _upload_dir_of(upload_id):
    """校验 upload_id（仅字母数字与连字符，≤64 位）并返回其暂存目录；非法返回 None。"""
    import re as _re
    if not upload_id or len(upload_id) > 64:
        return None
    if not _re.match(r'^up-[0-9A-Za-z-]+$', upload_id):
        return None
    return os.path.join(UPLOAD_DIR, upload_id)


def _upload_prune():
    """清理超 24 小时未动的上传会话（用户中途放弃的残片）；
    活跃会话数超过 100 时再清最旧的——防 HOST=0.0.0.0 部署下被会话数量滥用撑爆磁盘。"""
    try:
        if not os.path.isdir(UPLOAD_DIR):
            return
        now = time.time()
        entries = []
        for fn in os.listdir(UPLOAD_DIR):
            p = os.path.join(UPLOAD_DIR, fn)
            try:
                entries.append((os.path.getmtime(p), p))
            except OSError:
                pass
        entries.sort()
        keep = []
        for mt, p in entries:
            if now - mt > 86400:
                shutil.rmtree(p, ignore_errors=True)
            else:
                keep.append(p)
        excess = len(keep) - 100
        for p in (keep[:excess] if excess > 0 else []):
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def _parse_multipart(raw, boundary):
    """轻量 multipart/form-data 解析器：返回 {字段名: 字符串值 或 字节}。
    只处理本项目上传用到的字段（upload_id/idx/chunk），不做完整 RFC 2046 解析。"""
    result = {}
    delim = b'--' + boundary
    for part in raw.split(delim):
        if not part or part in (b'--', b'--\r\n', b'\r\n'):
            continue
        if b'\r\n\r\n' not in part:
            continue
        header, body = part.split(b'\r\n\r\n', 1)
        body = body[:-2] if body.endswith(b'\r\n') else body
        try:
            hdr = header.decode('utf-8', errors='replace')
        except Exception:
            continue
        name_m = re.search(r'name="([^"]+)"', hdr)
        if not name_m:
            continue
        field = name_m.group(1)
        if 'filename=' in hdr:
            result[field] = body
        else:
            result[field] = body.decode('utf-8', errors='replace')
    return result


def _upload_chunk_write(upload_id, idx, data_bytes):
    """写一个分片（每片一个 part 文件，乱序到达也安全）。返回 (ok, error)。"""
    d = _upload_dir_of(upload_id)
    if d is None:
        return False, '非法 upload_id'
    if not os.path.isdir(d):
        return False, '上传会话不存在或已过期，请重新开始上传'
    try:
        idx = int(idx)
    except Exception:
        return False, '非法分片序号'
    if idx < 0 or idx > 4096:
        return False, '非法分片序号'
    if len(data_bytes) > UPLOAD_CHUNK_MAX:
        return False, '分片过大'
    try:
        used = 0
        for fn in os.listdir(d):
            try:
                used += os.path.getsize(os.path.join(d, fn))
            except OSError:
                pass
        if used + len(data_bytes) > UPLOAD_TOTAL_MAX:
            return False, '文件超过 2GB 上限'
        with open(os.path.join(d, 'part_%04d' % idx), 'wb') as f:
            f.write(data_bytes)
    except OSError as e:
        return False, '分片写入失败：%s' % str(e)[:80]
    return True, ''


def _upload_have_parts(upload_id):
    """断点续传：返回会话中已到齐的分片序号（升序）。会话非法/不存在返回 None。"""
    d = _upload_dir_of(upload_id)
    if d is None or not os.path.isdir(d):
        return None
    have = []
    for fn in os.listdir(d):
        if fn.startswith('part_'):
            try:
                have.append(int(fn.split('_')[1]))
            except (IndexError, ValueError):
                pass
    return sorted(have)


def _upload_finalize(upload_id, name, chunks):
    """按序合并分片为成品文件 final__<name>。返回 (final_path|None, error)。"""
    d = _upload_dir_of(upload_id)
    if d is None:
        return None, '非法 upload_id'
    try:
        chunks = int(chunks)
    except Exception:
        return None, '非法分片数'
    if chunks < 1 or chunks > 4096:
        return None, '非法分片数'
    parts = []
    for i in range(chunks):
        p = os.path.join(d, 'part_%04d' % i)
        if not os.path.isfile(p):
            return None, '缺少分片 %d/%d，请重新上传该分片' % (i + 1, chunks)
        parts.append(p)
    base = os.path.basename(name or 'video.mp4') or 'video.mp4'
    final = os.path.join(d, 'final__' + base)
    total = 0
    try:
        with open(final, 'wb') as out:
            for p in parts:
                with open(p, 'rb') as f:
                    while True:
                        b = f.read(4 * 1024 * 1024)
                        if not b:
                            break
                        out.write(b)
                        total += len(b)
                        if total > UPLOAD_TOTAL_MAX:
                            raise RuntimeError('文件超过 2GB 上限')
    except Exception as e:
        try:
            os.remove(final)
        except OSError:
            pass
        return None, str(e)[:120]
    for p in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    return final, ''


def _upload_final_path(upload_id, name):
    """分片合并后的成品文件路径；校验归属与存在，非法/不存在返回 None。"""
    d = _upload_dir_of(upload_id)
    if d is None:
        return None
    base = os.path.basename(name or 'video.mp4') or 'video.mp4'
    fp = os.path.join(d, 'final__' + base)
    return fp if os.path.isfile(fp) else None


def _resolve_upload_video(vobj, run_dir, prefix='src'):
    """视频对象统一落盘到 run_dir，两种形态并存：
    {name, data(base64)} 旧路径（小文件）；{name, upload_id} 分片上传（大文件——
    直接移动合并成品，省一次整文件拷贝）。返回本地文件路径；无效返回 None。"""
    if not vobj:
        return None
    ext = os.path.splitext(vobj.get('name') or 'x.mp4')[1] or '.mp4'
    if vobj.get('mlib'):
        src = _material_path(vobj.get('mlib'))
        if not src:
            return None
        fp = os.path.join(run_dir, prefix + ext)
        shutil.copy2(src, fp)   # 素材库持久保留，任务用拷贝
        return fp
    if vobj.get('data'):
        fp = os.path.join(run_dir, prefix + ext)
        open(fp, 'wb').write(base64.b64decode(vobj.get('data', '')))
        return fp
    src = _upload_final_path(vobj.get('upload_id'), vobj.get('name'))
    if src:
        fp = os.path.join(run_dir, prefix + ext)
        shutil.move(src, fp)
        try:
            d = _upload_dir_of(vobj.get('upload_id'))
            if d and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
        return fp
    return None


def add_history(entry):
    try:
        with _HIST_LOCK:
            items = load_history(500)
            items.insert(0, entry)
            # 容量上限：超过 MAX_HISTORY_KEEP 条时，丢弃最旧记录并清理其磁盘文件，防止 webui_output 无限涨盘
            MAX_HISTORY_KEEP = 100
            if len(items) > MAX_HISTORY_KEEP:
                dropped = items[MAX_HISTORY_KEEP:]
                items = items[:MAX_HISTORY_KEEP]
                for old in dropped:
                    _remove_history_file(old.get('file'))
            _atomic_write_json(HISTORY_PATH, items)
        # 任务成功后自动清理中间产物（配置开启时，默认开）
        try:
            if load_ai_config().get('cleanup_mid', True):
                rel = entry.get('file')
                if rel:
                    fp = os.path.join(OUTDIR, rel) if not os.path.isabs(rel) else rel
                    if os.path.isfile(fp) and os.path.getsize(fp) > 1024:
                        cleanup_run_mid(os.path.dirname(fp))
        except Exception:
            pass
    except Exception:
        pass


def _remove_history_file(rel):
    """删除一条历史对应的成片文件及其所在 run_dir（若存在）。失败静默。
    history 中的 file 路径相对 OUTDIR（如 'run-X/final.mp4'），故以 OUTDIR 为基准解析。"""
    if not rel:
        return
    try:
        fp = os.path.join(OUTDIR, rel) if not os.path.isabs(rel) else rel
        if os.path.isfile(fp):
            os.remove(fp)
            # 一并清理同目录（独立 run_dir）下的中间产物。
            # 两道边界：① parent 正好等于 OUTDIR 时绝不能 rmtree，否则清空全部成片；
            # ② 该目录下若还有其他记录仍在使用，只删当前文件，不动目录。
            parent = os.path.dirname(fp)
            parent_abs = os.path.abspath(parent) if parent else ''
            out_abs = os.path.abspath(OUTDIR)
            if (parent_abs and os.path.isdir(parent) and parent_abs != out_abs
                    and parent_abs.startswith(out_abs)
                    and not _dir_still_referenced(parent_abs)):
                shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def cleanup_run_mid(run_dir):
    """任务成功后清理 run_dir 下可再生成的中间产物，保留 final.mp4 / src 源视频 / cover 封面 / 音乐 mp3。
    避免 webui_output 无限膨胀（之前手动清过 3GB）。失败静默。"""
    if not run_dir or not os.path.isdir(run_dir):
        return 0
    keep_prefix = ('final', 'src', 'cover', 'poster')
    mid_patterns = [
        re.compile(r'^narr\d+.*\.(wav|mp3)$'),
        re.compile(r'^frame_\d+\.jpg$'),
        re.compile(r'^cut.*\.mp4$'),
        re.compile(r'^bc\d+\.mp4$'),
        re.compile(r'^bc_silent\.mp4$'),
        re.compile(r'^vsub\.mp4$'),
        re.compile(r'^.*\.srt$'),
        re.compile(r'^.*\.concat\.txt$'),
    ]
    freed = 0
    for root, dirs, files in os.walk(run_dir):
        for f in files:
            fp = os.path.join(root, f)
            low = f.lower()
            if low.startswith(keep_prefix) or low.endswith('.mp3'):
                continue
            if any(p.match(f) for p in mid_patterns):
                try:
                    freed += os.path.getsize(fp)
                    os.remove(fp)
                except Exception:
                    pass
    for root, dirs, files in os.walk(run_dir, topdown=False):
        for d in list(dirs):
            dp = os.path.join(root, d)
            try:
                if not os.listdir(dp):
                    os.rmdir(dp)
            except Exception:
                pass
    return freed


def _dir_still_referenced(parent_abs):
    """history 里是否还有别的记录指向该目录——有则不能整目录删除。"""
    try:
        for it in load_history(500):
            f = (it or {}).get('file')
            if not f:
                continue
            p = os.path.abspath(os.path.join(OUTDIR, f) if not os.path.isabs(f) else f)
            if os.path.dirname(p).lower() == parent_abs.lower():
                return True
    except Exception:
        return True      # 判断失败时保守处理：宁可不删
    return False


def delete_history(file):
    """按 file 删除一条历史记录及其磁盘文件。返回是否删除成功。"""
    try:
        with _HIST_LOCK:   # 与 add_history 互斥：否则后台任务收尾写入的记录会被这里读到的旧快照覆盖掉
            items = load_history(500)
            new = [it for it in items if it.get('file') != file]
            if len(new) == len(items):
                return False
            _atomic_write_json(HISTORY_PATH, new)
        _remove_history_file(file)
        return True
    except Exception:
        return False


def _active_run_dirs():
    """当前仍有任务在使用的 run_dir 集合（绝对路径，小写）。

    供 clear_history 跳过——旧实现无差别 rmtree 掉 OUTDIR 下所有目录，
    会把正在渲染的任务目录一并删掉：ffmpeg 写入中途目录消失，几十分钟白跑，
    且报错对用户毫无意义。"""
    out = set()
    try:
        for p in list(PROGRESS.values()):
            rd = (p or {}).get('run_dir')
            if rd and not (p or {}).get('done'):
                out.add(os.path.abspath(rd).lower())
    except Exception:
        pass
    return out


def sweep_run_artifacts():
    """run_dir 磁盘清扫（batch5-5.1）：webui_output 无限上涨的主因是每个 run 里
    835MB 级的 src 源片副本和无人认领的孤儿目录。清两类：
    ① 孤儿目录：历史已删/从未入史、无任务在用、超过 N 天未改动 → 整目录删除；
    ② 过期源片副本：历史引用仍在（成片必须保留）但目录超过 N 天未动 → 只删
       src.* 副本，final/cover/tts 状态全保留。两步走「重新合成」依赖源片，
       超过 N 天未使用即视为放弃重合成——此时合成会得到明确报错而非静默失败。
    N = ai_config['cleanup_src_days']，默认 3 天，0 = 关闭。
    N 天内的新目录绝不动（两步走 Phase1 完成后正在等用户确认/手动调整）。
    历史/进度读不出来时直接放弃本轮清扫（宁可占盘不可误删）；全程静默容错。"""
    try:
        days = int((load_ai_config() or {}).get('cleanup_src_days', 3) or 0)
    except Exception:
        days = 3
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    try:
        active = _active_run_dirs()
        referenced = set()
        for it in (load_history(500) or []):
            rel = (it or {}).get('file')
            if rel:
                fp = rel if os.path.isabs(rel) else os.path.join(OUTDIR, rel)
                referenced.add(os.path.abspath(os.path.dirname(fp)).lower())
    except Exception:
        return 0
    freed = 0
    try:
        entries = list(os.scandir(OUTDIR))
    except OSError:
        return 0
    for e in entries:
        try:
            if not e.is_dir() or not e.name.startswith(('run-', '_warmup_')):
                continue
            dp = e.path
            if os.path.getmtime(dp) > cutoff:
                continue
            dp_low = os.path.abspath(dp).lower()
            if dp_low in active:
                continue
            if dp_low in referenced:
                # 历史引用：成片所在，只回收源片副本
                for f in os.listdir(dp):
                    p = os.path.join(dp, f)
                    if f.startswith('src.') and os.path.isfile(p):
                        freed += os.path.getsize(p)
                        os.remove(p)
            else:
                # 孤儿目录：整个回收
                for root, _dirs, files in os.walk(dp):
                    for f in files:
                        try:
                            freed += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                shutil.rmtree(dp, ignore_errors=True)
        except Exception:
            pass
    if freed > 0:
        try:
            print('[cleanup] run_dir 清扫释放 %.1f MB（孤儿目录+过期源片副本）' % (freed / 1048576.0))
        except Exception:
            pass
    return freed


def clear_history():
    """清空全部历史记录并删除成片（保留目录本身）。

    两点关键：
    ① 先写空索引再删文件——反过来的话，中途异常会留下「文件已删、索引还在」的状态，
       所有记录点开都是 404。
    ② 跳过正在运行任务的 run_dir，避免把进行中的渲染删掉。"""
    try:
        active = _active_run_dirs()
        with _HIST_LOCK:
            _atomic_write_json(HISTORY_PATH, [])
        if os.path.isdir(OUTDIR):
            for name in os.listdir(OUTDIR):
                p = os.path.join(OUTDIR, name)
                if os.path.isdir(p):
                    if os.path.abspath(p).lower() in active:
                        continue       # 正在生成，留着
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.isfile(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
        return True
    except Exception:
        return False


# Offline caption fallback: turn a plain filename into a short spring-y caption if the
# user has not configured any AI. Keeps the pipeline functional without a key.
def offline_caption(name, idx, n_total):
    base = os.path.splitext(os.path.basename(name))[0]
    import re as _re
    # 落盘名为 up_<runid>_<idx>_img；runid 可能是 run-12 / t1234 等非纯数字
    m = _re.search(r'up_[0-9A-Za-z_-]+?_(\d+)_img', base)
    if m:
        i = int(m.group(1))
        phrases = ['春意初醒', '花开时节', '绿野青青', '溪水潺潺', '山色葱茏', '暖阳正好']
        return f'{phrases[(i - 1) % len(phrases)]} · 第{i}帧'
    if 'spring' in base or 'img' in base:
        return f'第 {idx} 帧 · 春日风景'
    return f'第 {idx} 帧 · {base}'

W, H = 1920, 1080

# ---------------------------------------------------------------------------
# 依赖安装（首次）
# ---------------------------------------------------------------------------
def ensure_deps():
    def has(mod):
        try:
            __import__(mod); return True
        except Exception:
            return False
    missing = []
    if not has('PIL'): missing.append('Pillow')
    if not has('numpy'): missing.append('numpy')
    if not has('imageio_ffmpeg'): missing.append('imageio-ffmpeg')
    if not has('yt_dlp'): missing.append('yt-dlp')
    if missing:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                               '--no-input'] + missing)


import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

def hx(h):
    h = h.lstrip('#'); return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def gradient(w, h, top, bot):
    top = hx(top); bot = hx(bot)
    img = Image.new('RGB', (w, h)); px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bot[0]-top[0])*t); g = int(top[1] + (bot[1]-top[1])*t); b = int(top[2] + (bot[2]-top[2])*t)
        for x in range(0, w, 3):
            px[x, y] = (r, g, b)
            if x+1 < w: px[x+1, y] = (r, g, b)
            if x+2 < w: px[x+2, y] = (r, g, b)
    return img.resize((w, h)).convert('RGB')

def blend(base, layer):
    if base.mode != 'RGBA': base = base.convert('RGBA')
    return Image.alpha_composite(base, layer).convert('RGB')

def sun_layer(cx, cy, r, color, blur=20, st=20):
    s = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(s)
    for i in range(8, 0, -1):
        d.ellipse([cx-r*i, cy-r*i, cx+r*i, cy+r*i], fill=color + (max(0, int(st*i)),))
    s = s.filter(ImageFilter.GaussianBlur(blur))
    ImageDraw.Draw(s).ellipse([cx-r, cy-r, cx+r, cy+r], fill=color + (255,))
    return s

def clouds(d, x, y, s, shade):
    r = int(60*s)
    for dx, dy, rr in [(0,0,r),(int(r*.8),-int(r*.3),int(r*.75)),(-int(r*.9),int(r*.15),int(r*.7)),(int(r*.9),int(r*.1),int(r*.6))]:
        d.ellipse([x+dx-rr, y+dy-int(rr*.6), x+dx+rr, y+dy+int(rr*.7)], fill=shade)

def tree(d, x, y, gh, tc, l1, l2):
    tw = max(4, int(gh*.05)); d.rectangle([x-tw//2, y-gh, x+tw//2, y], fill=tc)
    for i in range(5):
        ox = (i-2)*gh*.28; oy = -gh*(.35+.12*((i % 3)-1)); rr = gh*(.28+.1*(i % 2))
        d.ellipse([x+ox-rr, y+oy-rr, x+ox+rr, y+oy+rr], fill=l1)
        d.ellipse([x+ox-int(rr*.5), y+oy-int(rr*.4), x+ox+int(rr*.5), y+oy+int(rr*.6)], fill=l2)

def scene1():
    img = gradient(W, H, '#cfe8ff', '#eef7ff')
    img = blend(img, sun_layer(int(W*.78), int(H*.18), 60, (255, 244, 200)))
    c = ImageDraw.Draw(img, 'RGBA')
    clouds(c, int(W*.2), int(H*.12), 1.1, (255,255,255,220))
    clouds(c, int(W*.6), int(H*.08), .8, (255,255,255,200))
    clouds(c, int(W*.92), int(H*.2), .6, (255,255,255,190))
    rnd = random.Random(11); b1 = int(H*.42); p = []
    for i in range(25): p.append((i/24*W, b1 - int(rnd.random()*180)))
    p += [(W, H), (0, H)]; c.polygon(p, fill=hx('#9fc5e0'))
    rnd2 = random.Random(12); b2 = int(H*.5); p = []
    for i in range(19): p.append((i/18*W, b2 - int(rnd2.random()*220)))
    p += [(W, H), (0, H)]; c.polygon(p, fill=hx('#8fb3d6'))
    c.polygon([(0, int(H*.55)), (W, int(H*.5)), (W, H), (0, H)], fill=hx('#9ed66a'))
    c.polygon([(0, int(H*.62)), (W, int(H*.56)), (W, H), (0, H)], fill=hx('#7fbe57'))
    sd = random.Random(7)
    for _ in range(400):
        x = sd.randint(0, W); y = sd.randint(int(H*.62), H)
        c.ellipse([x-2, y-6, x+2, y], fill=hx(sd.choice(['#5da73f','#79c04f','#8ed060'])))
    bloom = Image.new('RGBA', (W, H), (0, 0, 0, 0)); bd = ImageDraw.Draw(bloom)
    for bx, gh, fl in [(int(W*.12), 360, 1), (int(W*.88), 420, -1)]:
        tw = int(gh*.03); bd.line([(bx, 0), (bx, int(gh*.4))], fill=(120,70,50,255), width=tw)
        for i in range(4):
            hy = int(gh*(.2+.16*i)); bx2 = bx + fl*int(gh*(.2+.1*i))
            bd.line([(bx, int(gh*.4)), (bx2, hy)], fill=(130,80,55,255), width=int(tw*.6))
            rd = random.Random(20+i)
            for _ in range(14):
                ox = bx2 + rd.randint(-int(gh*.28), int(gh*.28)); oy = hy + rd.randint(-int(gh*.15), int(gh*.06)); rr = rd.randint(6, 14)
                bd.ellipse([ox-rr, oy-rr, ox+rr, oy+rr], fill=(247,180,197,200))
                bd.ellipse([ox-int(rr*.5), oy-int(rr*.5), ox+int(rr*.5), oy+int(rr*.5)], fill=(255,230,240,230))
    img = blend(img, bloom)
    pet = Image.new('RGBA', (W, H), (0, 0, 0, 0)); pd = ImageDraw.Draw(pet); rd = random.Random(55)
    for _ in range(60):
        x = rd.randint(0, W); y = rd.randint(int(H*.3), H); rr = rd.randint(3, 7)
        pd.ellipse([x-rr, y-rr//2, x+rr, y+rr//2], fill=(255,200,214,200))
    return blend(img, pet)

def scene2():
    img = gradient(W, H, '#8fd0f5', '#e6f6ff')
    img = blend(img, sun_layer(int(W*.25), int(H*.18), 55, (255, 246, 200), blur=18))
    c = ImageDraw.Draw(img, 'RGBA')
    for (a, b, s) in [(.1, .14, 1.2), (.45, .1, .9), (.8, .22, .8), (.95, .12, .5)]:
        clouds(c, int(W*a), int(H*b), s, (255,255,255,225))
    c.ellipse([-W*.4, int(H*.28), W*.6, int(H*.8)], fill=hx('#a6d87a'))
    c.ellipse([W*.3, int(H*.26), W*1.3, int(H*.82)], fill=hx('#8fd06b'))
    c.polygon([(0, int(H*.5)), (W, int(H*.46)), (W, H), (0, H)], fill=hx('#f2d94e'))
    c.polygon([(0, int(H*.6)), (W, int(H*.56)), (W, H), (0, H)], fill=hx('#e6c93c'))
    rd = random.Random(3)
    for by, cnt in [(int(H*.66), 3), (int(H*.78), 2), (int(H*.9), 2)]:
        for i in range(cnt):
            y = by + i*int(H*.05) + rd.randint(-8, 8); c.line([(0, y), (W, y)], fill=hx('#cfad2e'), width=4)
    rd = random.Random(4)
    for _ in range(9):
        x = rd.randint(200, W-200); y = int(H*.47)+rd.randint(-14, 10); w = rd.randint(50, 90); gh = rd.randint(30, 50)
        c.polygon([(x, y), (x+w, y), (x+w//2, y-gh)], fill=hx('#b0442f'))
        c.rectangle([x+w//4, y, x+w*3//4, y+gh], fill=hx('#efe0c8'))
        c.rectangle([x+w//2-3, y+4, x+w//2+3, y+14], fill=hx('#6b4a2a'))
    rd = random.Random(5)
    for _ in range(6):
        x = rd.randint(100, W-100); y = int(H*.47)+rd.randint(0, 20); gh = rd.randint(140, 240)
        tree(c, x, y, gh, hx('#5a4632'), hx('#4e7a38'), hx('#6f9e46'))
    rd = random.Random(8)
    for _ in range(500):
        x = rd.randint(0, W); y = rd.randint(int(H*.6), H)
        c.ellipse([x-2, y-2, x+2, y+2], fill=hx(rd.choice(['#fff3a0','#f7d94b','#ffd93d'])))
    return img

def scene3():
    img = gradient(W, H, '#bde5ff', '#f2faff')
    img = blend(img, sun_layer(int(W*.7), int(H*.15), 50, (255, 242, 190), blur=16))
    c = ImageDraw.Draw(img, 'RGBA')
    clouds(c, int(W*.15), int(H*.12), 1.0, (255,255,255,220)); clouds(c, int(W*.85), int(H*.2), .7, (255,255,255,200))
    c.ellipse([-W*.5, int(H*.3), W*.7, int(H*.85)], fill=hx('#b7e2a0'))
    c.ellipse([W*.4, int(H*.3), W*1.4, int(H*.85)], fill=hx('#a7d98f'))
    c.polygon([(int(W*.34), 0), (int(W*.58), 0), (int(W*.5), H), (int(W*.3), H)], fill=hx('#bfe8f5'))
    c.polygon([(0, int(H*.42)), (int(W*.34), int(H*.4)), (int(W*.3), H), (0, H)], fill=hx('#7ec850'))
    c.polygon([(int(W*.58), int(H*.4)), (W, int(H*.44)), (W, H), (int(W*.5), H)], fill=hx('#7ec850'))
    for _ in range(40):
        x = random.Random(200+_).uniform(int(W*.31), int(W*.55)); y = random.Random(60+_).uniform(int(H*.45), H-20)
        ln = random.Random(80+_).uniform(30, 120); c.line([(x, y), (x+ln, y)], fill=hx('#e2f6ff'), width=2)
    def pt(c, _x, _y, gh):
        tw = int(gh*.05); c.line([(_x, _y), (_x, int(_y-gh*.5))], fill=(120,66,44,255), width=tw)
        rd = random.Random(int(_x))
        for i in range(6):
            hy = _y-int(gh*(.3+.12*i)); bx = _x+int(gh*(.2+.08*i))*(-1 if i % 2 else 1)
            c.line([(_x, int(_y-gh*.5)), (bx, hy)], fill=(130,72,46,255), width=int(tw*.6))
            for j in range(9):
                ox = bx+rd.randint(-int(gh*.26), int(gh*.26)); oy = hy+rd.randint(-int(gh*.14), int(gh*.05)); rr = rd.randint(6, 12)
                c.ellipse([ox-rr, oy-rr, ox+rr, oy+rr], fill=(245,168,185,210))
                c.ellipse([ox-int(rr*.5), oy-int(rr*.5), ox+int(rr*.5), oy+int(rr*.5)], fill=(255,222,232,235))
    pt(c, int(W*.16), int(H*.66), 250); pt(c, int(W*.84), int(H*.7), 280)
    for wx in [int(W*.62), int(W*.75)]:
        c.line([(wx, int(H*.4)), (wx, int(H*.55))], fill=(130,80,50,255), width=6); rd = random.Random(wx)
        for k in range(18):
            ox = wx+rd.randint(-20, 20); oy = int(H*.55)+k*4
            c.line([(ox, int(H*.55)), (ox+rd.randint(-8, 8), oy)], fill=(140,190,120,200), width=3)
    for _ in range(40):
        x = random.Random(300+_).uniform(int(W*.32), int(W*.54)); y = random.Random(90+_).uniform(int(H*.45), H-20)
        c.ellipse([x-4, y-2, x+4, y+2], fill=(255,190,205,210))
    return img

def scene4():
    img = gradient(W, H, '#aee0f7', '#f0faff')
    img = blend(img, sun_layer(int(W*.5), int(H*.12), 70, (255, 246, 190), blur=30, st=26))
    beam = Image.new('RGBA', (W, H), (0, 0, 0, 0)); bd = ImageDraw.Draw(beam); rd = random.Random(40)
    for i in range(8):
        a0 = rd.uniform(-.5, .5); a1 = rd.uniform(.6, 1.4)
        bd.polygon([(int(W*.5), int(H*.12)), (int(W*.5)+math.sin(a0)*1500, H+200), (int(W*.5)+math.sin(a1)*1500, H+200)], fill=(255,255,210, int(14+i*3)))
    beam = beam.filter(ImageFilter.GaussianBlur(6)); img = blend(img, beam)
    c = ImageDraw.Draw(img, 'RGBA')
    c.rectangle([0, int(H*.55), W, H], fill=hx('#9ed76e'))
    c.polygon([(0, int(H*.5)), (W, int(H*.46)), (W, int(H*.6)), (0, int(H*.62))], fill=hx('#8ccb60'))
    rd = random.Random(33)
    for i in range(9):
        x = int(W*(.05+.11*i)); tw = rd.randint(18, 30)
        c.rectangle([x-tw//2, int(H*.35), x+tw//2, int(H*.62)], fill=(120,84,50,255))
        c.line([(x, int(H*.4)), (x-tw*2, int(H*.34))], fill=(120,84,50,255), width=int(tw*.4))
        c.line([(x, int(H*.42)), (x+tw*2, int(H*.36))], fill=(120,84,50,255), width=int(tw*.4))
    tn = Image.new('RGBA', (W, H), (0, 0, 0, 0)); td = ImageDraw.Draw(tn); rd = random.Random(31)
    for i in range(9):
        x = int(W*(.05+.11*i)); hy = int(H*(.34+rd.uniform(0, .06)))
        for j in range(7):
            ox = x+rd.randint(-70, 70); oy = hy+rd.randint(-50, 30); rr = rd.randint(40, 80)
            td.ellipse([ox-rr, oy-rr//2, ox+rr, oy+rr//2], fill=(120,180,110,200))
    tn = tn.filter(ImageFilter.GaussianBlur(2)); img = blend(img, tn)
    c2 = ImageDraw.Draw(img, 'RGBA'); rd = random.Random(77)
    for _ in range(400):
        x = rd.randint(0, W); y = rd.randint(int(H*.55), H)
        c2.ellipse([x-2, y-8, x+2, y], fill=hx(rd.choice(['#6cb94a','#82cc5c','#5da83f'])))
    rd = random.Random(120)
    for _ in range(90):
        x = rd.randint(0, W); y = rd.randint(int(H*.6), H); rr = rd.randint(6, 14)
        colc = rd.choice(['#ffffff','#ffe77a','#ff9bd2','#ffffff','#ffd9a0'])
        for a in range(8):
            ax = x+int(rr*.8*math.cos(a*math.pi/4)); ay = y+int(rr*.5*math.sin(a*math.pi/4))
            c2.ellipse([ax-3, ay-3, ax+3, ay+3], fill=hx(colc))
        c2.ellipse([x-3, y-3, x+3, y+3], fill=hx('#f5c241'))
    return img

SCENES = [scene1, scene2, scene3, scene4]
SCENE_TITLES = ['花开似锦 · 樱花漫山', '金色田野 · 油菜花开', '桃花流水 · 春水盈盈', '春林新绿 · 阳光正好']

def stamp_title(img, text):
    try:
        font = cjk_font(54); font_small = cjk_font(30)
    except FontMissingError:
        # 默认示例图上的标题只是装饰文字：宁可不加字，也不画一堆豆腐块。
        # 真正的成片渲染路径（字幕/封面）会显式抛错，不会走到这种静默分支。
        if not getattr(stamp_title, '_warned', False):
            stamp_title._warned = True
            print('[警告] ' + font_missing_help(), flush=True)
        return img
    d = ImageDraw.Draw(img, 'RGBA'); sub = '· 春日 ·'
    d.text((34, H-120), text, font=font, fill=(255,255,255,120)); d.text((36, H-116), sub, font=font_small, fill=(255,255,255,120))
    d.text((30, H-120), text, font=font, fill=(40,70,40,255)); d.text((32, H-118), sub, font=font_small, fill=(60,90,50,255))
    return img

def ensure_default_images():
    """Write the 4 built-in spring images if not present."""
    out = []
    for i, fn in enumerate(SCENES):
        path = os.path.join(HERE, f'img{i+1}.png')
        if not os.path.exists(path):
            img = stamp_title(fn(), SCENE_TITLES[i]).convert('RGB').resize((W, H), Image.LANCZOS)
            img.save(path, 'PNG')
        out.append(path)
    return out

# ---------------------------------------------------------------------------
# 合成引擎
# ---------------------------------------------------------------------------





















def make_image_clip(img_path, dur, motion, out_path, w, h, fps):
    """Render one Ken Burns image clip (dur seconds) as an mp4 segment.
    batch5-5.3 逐帧流式写入 ffmpeg stdin：旧实现把全部 N 帧 float32 堆在内存，
    再 np.stack + astype(uint8) + tobytes 三份全量拷贝——1080p/30fps/5s 单张
    峰值约 3.7GB×3；现在每帧生成即写、峰值只有一帧 uint8（约 6MB），并去掉
    整图 float32 往返（PIL 直接缩放，字节输出与旧实现完全一致）。"""
    N = max(1, int(round(dur * fps)))
    base_w = int(w * 1.2); base_h = int(h * 1.2)
    src = Image.open(img_path).convert('RGB')
    iw, ih = src.size
    # downscale once to a small working canvas (output x ~1.2) for speed
    scale = max(base_w / iw, base_h / ih)
    nw = int(round(iw * scale)); nh = int(round(ih * scale))
    pil = src.resize((nw, nh), Image.LANCZOS)
    ox = (nw - base_w) // 2; oy = (nh - base_h) // 2
    canvas_pil = pil.crop((ox, oy, ox + base_w, oy + base_h))
    canvas_w, canvas_h = canvas_pil.size

    def move(which, t):
        iw2, ih2 = canvas_w, canvas_h
        if which == 0:
            z = 1 - 0.26*t; cw = iw2*z; ch = ih2*z; cx = iw2/2; cy = ih2/2
        elif which == 1:
            cw = iw2*0.85; ch = ih2; cx = iw2/2 + (iw2*0.15/2)*t; cy = ih2/2
        elif which == 2:
            z = 0.74 + 0.26*t; cw = iw2*z; ch = ih2*z; cx = iw2/2; cy = ih2/2
        else:
            cw = iw2*0.8; ch = ih2*0.8; cx = iw2*(0.5+0.5*t); cy = ih2*(0.5+0.5*t)
        cw = min(cw, iw2); ch = min(ch, ih2)
        cx = max(cw/2, min(iw2-cw/2, cx)); cy = max(ch/2, min(ih2-ch/2, cy))
        return cx, cy, cw, ch

    # crop is cheap on PIL; resize per frame with BILINEAR (fast, good enough).
    def _frames():
        for k in range(N):
            t = k / max(1, N - 1)
            cx, cy, cw, ch = move(motion % 4, t)
            x0 = max(0, int(round(cx - cw / 2))); y0 = max(0, int(round(cy - ch / 2)))
            x1 = min(canvas_w, int(x0 + cw)); y1 = min(canvas_h, int(y0 + ch))
            win = canvas_pil.crop((x0, y0, x1, y1))
            yield win.resize((w, h), Image.BILINEAR).tobytes()
    rc, o, e = ffmpeg_run(['-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}',
                            '-r', str(fps), '-i', '-'] + video_encode_args(20) + ['-threads', '0', out_path], input_data=_frames())
    return out_path

def make_video_clip(src, dur, out_path, w, h, fps, start=0.0):
    """Trim a source video from `start` to dur seconds and scale/pad to w x h; returns its real duration.
    start 为源视频内的起始时间（默认 0）。调用方按时间线切片时务必传入，否则每段都会从 0 秒截取，
    导致片头画面（如商标/Logo）被反复重复、后面内容完全缺失。"""
    if not os.path.exists(src):
        raise RuntimeError(f'源视频不存在: {src}')
    real = probe_duration(src) or 0.0
    if real <= 0:
        raise RuntimeError(f'无法读取源视频时长: {src}')
    use = min(dur, max(0.0, real - start))
    if use < 0.5:
        use = min(dur, real)  # 降级为全取剩余
    rc, o, e = ffmpeg_run(['-y', '-ss', f'{start:.3f}', '-i', src, '-t', f'{use:.3f}',
                            '-vf', f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1',
                            '-r', str(fps)] + video_encode_args(20) + ['-threads', '0', '-an', out_path])
    if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 100:
        err = (e.decode('utf-8', 'ignore') if isinstance(e, bytes) else str(e))[-300:]
        raise RuntimeError(f'视频片段生成失败: {os.path.basename(out_path)} 源={os.path.basename(src)} dur={dur:.1f}s start={start:.1f}s 报错={err}')
    return out_path, use


# ---------------------------------------------------------------------------
# 视频编码器选择：GPU 硬编(h264_nvenc) 优先，不可用时回退 CPU 软编(libx264)
# 渲染是长视频全流程的瓶颈（8 分钟实测约 360s），Whisper 早已走 CUDA，编码仍纯 CPU。
# ---------------------------------------------------------------------------
_ENC_CACHE = {'probe': None}


def video_encoder_cfg():
    """读取用户选择的编码策略：auto(默认·GPU 可用则用) / cpu / gpu。"""
    v = (load_ai_config().get('video') or {})
    mode = str(v.get('encoder') or 'auto').strip().lower()
    return mode if mode in ('auto', 'cpu', 'gpu') else 'auto'


def _probe_nvenc():
    """实际跑一个极短测试编码，确认 h264_nvenc 真能出片。
    仅查 `ffmpeg -encoders` 不够——驱动/格式不匹配时会"列表里有、运行期失败"。"""
    import tempfile
    out = os.path.join(tempfile.gettempdir(), '_framecut_nvenc_probe.mp4')
    ok = False
    try:
        rc, _o, _e = ffmpeg_run(['-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=10:duration=1',
                                 '-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p',
                                 '-preset', 'p4', '-rc', 'constqp', '-qp', '26', out])
        ok = (rc == 0) and os.path.exists(out) and os.path.getsize(out) > 0
    except Exception:
        ok = False
    try:
        if os.path.exists(out):
            os.remove(out)
    except Exception:
        pass
    return ok


def _nvenc_usable():
    """缓存探测结果（进程内只探测一次，避免每条命令都跑测试编码）。"""
    if _ENC_CACHE['probe'] is None:
        _ENC_CACHE['probe'] = bool(_probe_nvenc())
    return _ENC_CACHE['probe']


def reset_encoder_probe():
    """清空编码探测缓存（切换策略后 / 测试用）。"""
    _ENC_CACHE['probe'] = None


# 质量档位（P2-4）：速度优先 / 均衡(默认) / 画质优先
# 档位同时接管 preset / cq(crf) / 码率上限；语义与 video_encode_args 的 quality 对齐：
#   cq/crf 越小越清晰；nvenc preset 数字越大越清晰，x264 preset 越慢越清晰。
# 速度优先：最快 preset + 略高 cq + 较低码率上限 → 编码快、体积省。
# 画质优先：最慢 preset + 更低 cq + 更高码率上限 → 画质最高，单遍管线(默认)下只编码一次。
_QUALITY_TIERS = {
    'speed': {
        'nvenc': dict(preset='p1', cq=22, bv='6M',  maxrate='8M',  bufsize='12M', lookahead=0),
        'x264':  dict(preset='veryfast', crf=21),
    },
    'balanced': {
        'nvenc': dict(preset='p5', cq=20, bv='10M', maxrate='14M', bufsize='16M', lookahead=32),
        'x264':  dict(preset='medium', crf=19),
    },
    'quality': {
        'nvenc': dict(preset='p7', cq=18, bv='14M', maxrate='20M', bufsize='24M', lookahead=32),
        'x264':  dict(preset='slow',  crf=17),
    },
}


def video_encode_args(quality=23, bitrate=None, tier=None):
    """返回**最终成片**的视频编码参数片段（list）。GPU 硬编可用且用户未禁用时用 h264_nvenc，否则回退 libx264。
    quality：质量档，越小越清晰（libx264 的 crf / nvenc 的 cq，语义对齐）。
    bitrate：指定码率（如 '4M'）时用码率控制替代CRF/CQ。
    tier：质量档位 'speed'/'balanced'/'quality'，优先级高于 quality —— 完整接管 preset/cq/crf/码率上限。
          未指定时退回原 behavior（quality 控制 crf/cq），保持完全兼容。

    2026-09-08 质量升级（本机 RTX 3060 实测，同一 3s 片段对比无损参考）：
      · nvenc: p4/constqp → **p5 + tune hq + vbr + rc-lookahead 32**，PSNR 48.60 → 54.40 dB。
        - constqp 恒定 QP 码率完全不受控，暗场/爆炸等复杂场景码率不足产生色块，而简单场景又浪费；
          vbr+cq 在保持质量目标的同时给出码率上限。实测低复杂度内容实际码率远低于上限值。
        - tune hq 开启 NVENC 的心理视觉率失真与加权预测；rc-lookahead 提升运动场景质量。
      · x264: veryfast/no-tune → **medium + tune film**，PSNR 52.29 dB。
        - tune film 是真人实拍/影视素材专用参数组（保护颗粒感、抑制 banding），本项目素材 100% 适用。
    """
    mode = video_encoder_cfg()
    use_nvenc = mode in ('auto', 'gpu') and _nvenc_usable()
    t = _QUALITY_TIERS.get(tier) if tier else None

    if bitrate:
        # 指定码率时用 -b:v 控制（保持原有行为，preset/tune 优先取档位）
        if use_nvenc:
            preset = (t['nvenc']['preset'] if t else 'p5')
            return ['-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p', '-preset', preset, '-tune', 'hq',
                    '-b:v', str(bitrate), '-maxrate', str(bitrate)]
        preset = (t['x264']['preset'] if t else 'medium')
        return ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', preset, '-tune', 'film',
                '-b:v', str(bitrate), '-maxrate', str(bitrate)]

    if t:
        # 档位优先：完整接管 preset / cq / crf / 码率上限
        if use_nvenc:
            spec = t['nvenc']
            args = ['-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p',
                    '-preset', spec['preset'], '-tune', 'hq',
                    '-rc', 'vbr', '-cq', str(spec['cq']),
                    '-b:v', spec['bv'], '-maxrate', spec['maxrate'], '-bufsize', spec['bufsize']]
            if spec.get('lookahead'):
                args += ['-rc-lookahead', str(spec['lookahead'])]
            return args
        spec = t['x264']
        return ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', spec['preset'], '-tune', 'film',
                '-crf', str(spec['crf'])]

    # 无档位（兼容旧调用）：quality 控制 crf/cq
    if use_nvenc:
        return ['-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p',
                '-preset', 'p5', '-tune', 'hq',
                '-rc', 'vbr', '-cq', str(int(quality)),
                '-b:v', '10M', '-maxrate', '14M', '-bufsize', '16M',
                '-rc-lookahead', '32']
    return ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'medium', '-tune', 'film',
            '-crf', str(int(quality))]


def video_encode_args_interim():
    """返回**中间产物**（剪切片段 / concat 兜底）的编码参数。

    为什么需要单独一档：成片链路里视频会被编码两次（①剪切 ②烧字幕）。
    若两遍都用 crf 23，两次有损累积后等效于单遍 crf 26-27 —— 前面做得再好，最后一刀全赔进去。

    这里改用「最快 preset + 接近无损的 crf」：
      · ultrafast 比原来的 veryfast 还快，所以提速不降速；
      · crf 16 实测 PSNR 54.96 dB（高于最终成片档），视觉无损。
    中间产物体积会变大（实测约 6 倍），但都在 run_dir 临时目录内，成片后即可清理。
    """
    mode = video_encoder_cfg()
    if mode in ('auto', 'gpu') and _nvenc_usable():
        return ['-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p',
                '-preset', 'p1', '-tune', 'hq', '-rc', 'constqp', '-qp', '16']
    return ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast', '-crf', '16']


def build_video_filter(params):
    """根据导出参数构建视频滤镜（分辨率缩放）。返回filter字符串或None。"""
    if not params:
        return None
    res = params.get('resolution') or ''
    if not res or res == 'original':
        return None
    # res格式: '1920x1080' 或 '1280x720'
    parts = str(res).lower().split('x')
    if len(parts) == 2:
        w, h = parts[0].strip(), parts[1].strip()
        if w.isdigit() and h.isdigit():
            # flags=lanczos：默认 bicubic 在降采样（如 1080p→720p）时会明显发糊，
            # lanczos 保留更多细节，是缩放质量最好的通用算法（代价仅为略慢）
            return (f'scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,'
                    f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2')
    return None


def video_encoder_label():
    """给前端展示当前实际生效的编码器。"""
    mode = video_encoder_cfg()
    if mode == 'cpu':
        return 'CPU 软编 libx264（已手动指定）'
    if _nvenc_usable():
        return 'GPU 硬编 h264_nvenc'
    return 'CPU 软编 libx264（未检测到可用 GPU 编码器）'




def analyze_beats(path):
    """Analyze audio with librosa: return (bpm, beat_times_in_seconds).
    beat_times are precise floats (ms precision). Returns None if analysis fails."""
    try:
        import librosa
    except Exception:
        return None
    try:
        y, sr = librosa.load(path, sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units='frames')
        if beat_frames is None or len(beat_frames) == 0:
            tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beats = [float(v) for v in librosa.frames_to_time(beat_frames, sr=sr)]
        # filter out beats beyond audio length
        T = float(len(y)) / sr
        beats = [b for b in beats if b < T - 0.05]
        bpm_val = None
        try:
            arr = np.asarray(tempo)
            bpm_val = float(arr.ravel()[0] if arr.ndim else arr)
        except Exception:
            bpm_val = None
        return bpm_val, beats
    except Exception:
        return None


def plan_beat_durations(item_durs, beats, fade, step=1):
    """Return per-item display durations (list) such that:
       - total video length = sum(item_durs)  (driven by photo/video count & durations)
       - the N-1 interior cuts land on music beats, spaced `step` beats apart.
       step: 0.5 = every half-beat, 1 = every beat, 2 = every other beat, 4 = every 4th.
       Falls back to equal item_durs when beats are unusable."""
    N = len(item_durs)
    total = float(sum(item_durs))
    if N <= 1:
        return list(item_durs)
    try:
        step = float(step or 1.0)
    except (TypeError, ValueError):
        step = 1.0
    cum = []
    acc = 0.0
    for i in range(N - 1):
        acc += item_durs[i]
        cum.append(acc)

    # build a grid of allowed cut instants from the beats (incl. half-beat midpoints if step<1)
    grid = []
    if beats and len(beats) >= 2:
        for i in range(len(beats) - 1):
            b0, b1 = beats[i], beats[i + 1]
            if step < 1:
                grid.append((b0 + b1) / 2.0)
            grid.append(b1)
        grid = sorted(set(round(g, 4) for g in grid if g > 0))

    def nearest_allowed(cp):
        if not grid:
            return cp
        return min(grid, key=lambda g: abs(g - cp))

    snaps = [nearest_allowed(cp) for cp in cum]
    for i in range(1, len(snaps)):
        if snaps[i] <= snaps[i - 1]:
            snaps[i] = snaps[i - 1] + (0.3 * step if step >= 1 else 0.15)
    disp = []
    prev = 0.0
    for i in range(N - 1):
        disp.append(max(0.5, snaps[i] - prev))
        prev = snaps[i]
    last = max(0.6, total - prev)
    disp.append(last)
    return disp



# ---------------------------------------------------------------------------
# 内置免费踩点音乐曲库（Incompetech / CC.BY，可商用，署名即可）
# bpm/duration 为估算值，音频来自逐轨真实下载。
# ---------------------------------------------------------------------------
MUSIC_DIR = os.path.join(HERE, 'music_library')

MUSIC_CATALOG = [
    {'id': 'rising-game',    'title': 'Rising Game',      'genre': '电子/律动', 'bpm': 128, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Rising%20Game.mp3'},
    {'id': 'electro-cabello','title': 'Electro Cabello',  'genre': '电子/流行', 'bpm': 120, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Electro%20Cabello.mp3'},
    {'id': 'glitter-blast',  'title': 'Glitter Blast',    'genre': '电子/活力', 'bpm': 132, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Glitter%20Blast.mp3'},
    {'id': 'long-stroll',    'title': 'Long Stroll',      'genre': '轻快/行走', 'bpm': 110, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Long%20Stroll.mp3'},
    {'id': 'carefree',       'title': 'Carefree',         'genre': '轻快/乐观', 'bpm': 96,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3'},
    {'id': 'cambodian-odyssey','title': 'Cambodian Odyssey','genre': '世界/律动','bpm': 100, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Cambodian%20Odyssey.mp3'},
    {'id': 'wholesome',      'title': 'Wholesome',        'genre': '温暖/治愈', 'bpm': 90,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Wholesome.mp3'},
    {'id': 'wallpaper',      'title': 'Wallpaper',        'genre': '氛围/环境', 'bpm': 80,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Wallpaper.mp3'},
    {'id': 'monkeys-spinning','title': 'Monkeys Spinning Monkeys','genre':'幽默/欢乐','bpm':156, 'license':'CC.BY 4.0','attri':'Kevin MacLeod (incompetech.com)','licenseUrl':'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Monkeys%20Spinning%20Monkeys.mp3'},
    {'id': 'fluffing-a-duck','title': 'Fluffing a Duck',  'genre': '轻快/趣味', 'bpm': 105, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Fluffing%20a%20Duck.mp3'},
    {'id': 'airport-lounge', 'title': 'Airport Lounge',   'genre': '爵士/氛围', 'bpm': 92,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Airport%20Lounge.mp3'},
    {'id': 'prelude-and-action','title': 'Prelude and Action','genre':'电影/磅礴','bpm':118, 'license':'CC.BY 4.0','attri':'Kevin MacLeod (incompetech.com)','licenseUrl':'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Prelude%20and%20Action.mp3'},
    {'id': 'lightless-dawn', 'title': 'Lightless Dawn',   'genre': '氛围/缓拍', 'bpm': 84,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Lightless%20Dawn.mp3'},
]

def catalog_cached_size(mid):
    p = os.path.join(MUSIC_DIR, mid + '.mp3')
    if os.path.exists(p) and os.path.getsize(p) > 5000:
        try:
            return round(probe_audio_len(p), 2)
        except Exception:
            return None
    return None

def search_catalog(q=''):
    q = (q or '').strip().lower()
    os.makedirs(MUSIC_DIR, exist_ok=True)
    out = []
    for t in MUSIC_CATALOG:
        hay = (t['title'] + ' ' + t['genre'] + ' ' + t['id']).lower()
        if q and q not in hay:
            continue
        d = catalog_cached_size(t['id'])
        out.append({'id': t['id'], 'title': t['title'], 'genre': t['genre'],
                    'bpm': t['bpm'], 'license': t['license'], 'attri': t['attri'],
                    'licenseUrl': t['licenseUrl'], 'cached': d is not None,
                    'length': d})
    return out

def catalog_path(mid):
    return os.path.join(MUSIC_DIR, mid + '.mp3')

def download_catalog(mid):
    """Download a catalog track if not cached; return path or raise."""
    path = catalog_path(mid)
    if os.path.exists(path) and os.path.getsize(path) > 5000:
        return path
    track = next((t for t in MUSIC_CATALOG if t['id'] == mid), None)
    if not track:
        raise RuntimeError('未知曲目')
    os.makedirs(MUSIC_DIR, exist_ok=True)
    import urllib.request
    req = urllib.request.Request(track['licenseUrl'], headers={'User-Agent': 'Mozilla/5.0 SpringStudio'})
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, 'wb') as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(path) < 5000:
        os.remove(path)
        raise RuntimeError('下载失败')
    return path


# ---------------------------------------------------------------------------
# AI 能力：两套独立接口 —— 视觉(看图写文案) 与 TTS(中文配音) 可分别配 base_url/key/model
# 未配置该通道的 key 时，自动退回对应离线兜底，能力不中断。
# 配置结构（ai_config.json）：
#   vision:  {base_url, api_key, model}
#   tts:     {base_url, api_key, model, voice}
# ---------------------------------------------------------------------------
def _vision_available():
    v = load_ai_config().get('vision') or {}
    return bool(v.get('base_url') and v.get('api_key') and v.get('model'))



def ai_describe_image(img_path, name=''):
    """Use a vision (OpenAI-compatible) model to write a short Chinese caption for
    one image. Returns a short Chinese sentence. Falls back to offline template."""
    cfg = (load_ai_config().get('vision') or {})
    if not (cfg.get('base_url') and cfg.get('api_key') and cfg.get('model')):
        return offline_caption(name or img_path, 1, 1)
    try:
        import urllib.request, base64 as _b64, json as _json
        im = fromPIL(img_path, max_side=512)
        b64 = _b64.b64encode(im).decode('ascii')
        payload = {
            'model': cfg.get('model'),
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': '请用一句不超过20字的中文，描写这张春天风景图片的内容与氛围，直接输出这一句话，不要引号。'},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
                ],
            }],
            'max_tokens': 500,
            'temperature': 0.7,
        }
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        # content may be a string or a list; extract text, fall back if empty
        content = data['choices'][0]['message'].get('content')
        txt = ''
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            parts = [p.get('text', '') for p in content if isinstance(p, dict) and p.get('text')]
            txt = ''.join(parts)
        txt = (txt or '').strip()
        if txt:
            return txt[:40]
        return offline_caption(name or img_path, 1, 1)
    except Exception:
        return offline_caption(name or img_path, 1, 1)


def fromPIL(path, max_side=512):
    from PIL import Image as _I
    im = _I.open(path).convert('RGB')
    w, h = im.size
    scale = max_side / max(w, h)
    if scale < 1:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), _I.BILINEAR)
    import io
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=70)
    return buf.getvalue()


def ai_tts(text, out_path, voice=None):
    """Synthesize Chinese narration via configured TTS provider (openai-openai兼容 / dashscope通义千问). """
    cfg = (load_ai_config().get('tts') or {})
    api_key = cfg.get('api_key')
    model = cfg.get('model')
    if not (api_key and model):
        return False
    provider = (cfg.get('provider') or 'openai').lower()
    # 云端TTS不支持{停顿:0.3}{情绪:xx}等自定义标记，先剥离避免念出"停顿"
    text = _strip_tts_markup(text)
    try:
        import urllib.request, json as _json
        if provider == 'dashscope':
            # 通义千问 / DashScope 非实时语音合成 (Model Studio) — 用配置的主机，缺省公共端点
            return _tts_dashscope(text, out_path, api_key, model, voice or cfg.get('voice', 'allmina'),
                                  cfg.get('base_url'))
        if provider == 'mimo':
            # 小米 MiMo 语音合成 (mimo.mi.com) — OpenAI 兼容 chat/completions + audio 参数
            return _tts_mimo(text, out_path, api_key, model, voice or cfg.get('voice', 'mimo_default'),
                             cfg.get('base_url'))
        # OpenAI-compatible
        url = (cfg.get('base_url', '').rstrip('/')) + '/audio/speech'
        payload = {'model': model, 'input': text,
                   'voice': voice or cfg.get('voice', 'alloy')}
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + api_key})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
        with open(out_path, 'wb') as f:
            f.write(data)
        return os.path.getsize(out_path) > 500
    except Exception:
        return False


def _tts_dashscope(text, out_path, api_key, model, voice, base_url=None):
    """通义千问 DashScope 非实时语音合成 HTTP API.
    Uses the configured base_url (defaults to the public DashScope endpoint).
    model 例: qwen-audio-turbo / qwen2.5-audio-turbo / cosyvoice-v1
    voice 例: allmina / longxiaochun / cherry ...
    Sets _LAST_TTS_ERR on failure for the test panel. Returns True on success."""
    global _LAST_TTS_ERR
    import urllib.request, json as _json
    base = (base_url or 'https://dashscope.aliyuncs.com/api/v1').rstrip('/')
    # DashScope non-realtime TTS path is appended to the /api/v1 root
    url = base + '/services/aigc/text2audio/tts'
    payload = {
        'model': model,
        'input': {'text': text},
        'voice': voice,
        'parameters': {'format': 'mp3', 'sample_rate': 48000},
    }
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + api_key})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            ctype = resp.headers.get('Content-Type', '')
            data = resp.read()
    except Exception as e:
        code = getattr(e, 'code', None)
        body = ''
        try:
            raw = getattr(e, 'read', lambda: b'')() if hasattr(e, 'read') else b''
            body = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        _LAST_TTS_ERR = f'HTTP{code}: {body[:200]}' if code else str(e)[:200]
        return False
    # If JSON came back, it's an error/status payload
    if 'json' in ctype.lower():
        try:
            obj = _json.loads(data.decode('utf-8', 'ignore'))
            _LAST_TTS_ERR = '服务返回: ' + json.dumps(obj, ensure_ascii=False)[:200]
            return False
        except Exception:
            pass
    with open(out_path, 'wb') as f:
        f.write(data)
    return os.path.getsize(out_path) > 500


def _tts_mimo(text, out_path, api_key, model, voice, base_url=None):
    """小米 MiMo 语音合成（mimo.mi.com）。走 OpenAI 兼容 chat/completions + audio 参数。
    model 例: mimo-v2.5-tts / mimo-v2.5-tts-voicedesign / mimo-v2.5-tts-voiceclone
    voice 例: mimo_default / Mia / Chloe / Milo / Dean
    文本放在 assistant 消息；返回 audio.data(base64)。失败时设置 _LAST_TTS_ERR。"""
    global _LAST_TTS_ERR
    import urllib.request, json as _json, base64 as _b64
    base = (base_url or 'https://api.xiaomimimo.com/v1').rstrip('/')
    url = base + '/chat/completions'
    payload = {
        'model': model,
        'messages': [{'role': 'assistant', 'content': text}],
        'audio': {'voice': voice, 'format': 'mp3'},
    }
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + api_key})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode('utf-8', 'ignore'))
    except Exception as e:
        code = getattr(e, 'code', None)
        body = ''
        try:
            raw = getattr(e, 'read', lambda: b'')() if hasattr(e, 'read') else b''
            body = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        _LAST_TTS_ERR = (f'HTTP{code}: {body[:200]}' if code else str(e)[:200])
        return False
    try:
        audio = data['choices'][0]['message']['audio']
        b64 = audio['data']
        raw = _b64.b64decode(b64)
        with open(out_path, 'wb') as f:
            f.write(raw)
        return os.path.getsize(out_path) > 500
    except Exception:
        _LAST_TTS_ERR = '返回格式异常: ' + _json.dumps(data, ensure_ascii=False)[:200]
        return False


# ---------------------------------------------------------------------------
# 逐通道测试：用当前填写的配置实调一次接口，反馈是否有效
# ---------------------------------------------------------------------------
def _test_vision():
    """Test the configured vision channel with a bundled spring image. Returns (ok, msg)."""
    cfg = (load_ai_config().get('vision') or {})
    if not (cfg.get('base_url') and cfg.get('api_key') and cfg.get('model')):
        return False, '未配置：请填 ① 视觉 的 base_url + api_key + model'
    try:
        import urllib.request, base64 as _b64, json as _json
        test_img = None
        for i in (1, 2, 3, 4):
            p = os.path.join(HERE, f'img{i}.png')
            if os.path.exists(p):
                test_img = p
                break
        if not test_img:
            test_img = os.path.join(HERE, 'img1.png')
        im = fromPIL(test_img, max_side=256)
        b64 = _b64.b64encode(im).decode('ascii')
        payload = {
            'model': cfg.get('model'),
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': '只回复“OK”，表示你能看到图片。'},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
            ]}],
            'max_tokens': 20,
        }
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        txt = data['choices'][0]['message']['content'].strip()
        return True, f'有效（模型回复：{txt[:20]}）'
    except Exception as e:
        body = ''
        try:
            raw = getattr(e, 'read', lambda: b'')() if hasattr(e, 'read') else b''
            body = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        code = getattr(e, 'code', None)
        return False, f'失败{(" HTTP"+str(code)) if code else ""}：{str(e)[:120]} {body[:200]}'


def _test_tts():
    """Test the configured TTS channel by synthesizing a short phrase. Returns (ok, msg)."""
    global _LAST_TTS_ERR
    cfg = (load_ai_config().get('tts') or {})
    if not (cfg.get('api_key') and cfg.get('model')):
        return False, '未配置：请填 ② TTS 的 api_key + 模型'
    os.makedirs(WORKDIR, exist_ok=True)
    out = os.path.join(WORKDIR, f'_tts_test_{int(time.time()*1000)}.mp3')
    _LAST_TTS_ERR = ''
    ok = ai_tts('春天来了', out)
    if ok and os.path.exists(out):
        size = os.path.getsize(out)
        try:
            os.remove(out)
        except Exception:
            pass
        return True, f'有效（生成 {round(size/1024,1)}KB 音频）'
    try:
        if os.path.exists(out):
            os.remove(out)
    except Exception:
        pass
    if _LAST_TTS_ERR:
        return False, '失败：' + _LAST_TTS_ERR
    provider = (cfg.get('provider') or 'openai').lower()
    if provider == 'dashscope':
        return False, '失败：请确认 DashScope Key 有效、模型已开通（如 qwen-audio-turbo）'
    return False, '失败：请确认 base_url/Key/model 正确，接口位于 {base_url}/audio/speech'


def build_srt(captions, starts, durs, out_path):
    """captions[i] with start/end seconds -> SRT subtitle file."""
    def ts(sec):
        hh = int(sec // 3600); mm = int((sec % 3600) // 60); ss = int(sec % 60); ms = int((sec % 1) * 1000)
        return f'{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}'
    lines = []
    for i, cap in enumerate(captions, 1):
        s, d = starts[i - 1], durs[i - 1]
        lines.append(str(i))
        lines.append(f'{ts(s)} --> {ts(s + d)}')
        lines.append(cap)
        lines.append('')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return out_path


# ---------------------------------------------------------------------------
# 本地配音（免费）：edge-tts（微软朗读，免 Key、音质好）+ Windows SAPI（系统内置、完全离线）
#
# 历史缺口：界面上只有「云端 TTS」要 Key 的选项，本地配音只能吃系统 SAPI ——
# 而多数 Windows 只装了一个 zh-CN 音色（Microsoft Huihui），机械味重且无法选择，
# 用户既看不到配置入口也无从下载更好的引擎。这里补上本地配音引擎的选择与安装。
# ---------------------------------------------------------------------------
EDGE_TTS_VOICES = [
    ('zh-CN-XiaoxiaoNeural', '晓晓 · 女声（温柔自然·推荐）'),
    ('zh-CN-YunxiNeural', '云希 · 男声（清朗）'),
    ('zh-CN-YunyangNeural', '云扬 · 男声（播报/解说腔）'),
    ('zh-CN-XiaoyiNeural', '晓伊 · 女声（活泼）'),
    ('zh-CN-YunjianNeural', '云健 · 男声（沉稳叙事）'),
    ('zh-CN-YunxiaNeural', '云夏 · 男声（少年感）'),
    ('zh-CN-liaoning-XiaobeiNeural', '晓北 · 女声（东北话）'),
    ('zh-CN-shaanxi-XiaoniNeural', '晓妮 · 女声（陕西话）'),
    ('zh-HK-WanLungNeural', '粤语 · 云龙（男）'),
    ('zh-TW-HsiaoChenNeural', '台湾腔 · 晓臻（女）'),
]



# edge-tts 需要访问微软朗读服务。本机实测**单次成功率只有约 2/3**（连接被会随时重置），
# 但同一次内容重试一两次基本都能成——所以这里分两层：
#   ① 单次合成内重试（_EDGE_RETRY 次，间隔递增）：把 67% 拉到接近 100%，
#      这是「用得上 edge-tts 好音质」的关键，否则动不动就掉到机械感重的离线模型。
#   ② 整条链路熔断：连续 _EDGE_MAX_FAILS 次「重试后仍失败」才判死一段时间，
#      避免网络真断了以后每段都白等一轮重试，把几十段的解说拖成假死。
                             #      替代原「连续 4 段整链熔断 120s」逻辑——任何段落绝不整链静默，
                             #      至少落到 sherpa / SAPI 保证有声。







# ChatTTS 本地引擎（更自然的对话式语音，需 torch + ChatTTS，延迟加载）






# ============ CosyVoice 离线配音（阿里开源·质量最高·需GPU） ============






def _run_pip(pip, args, timeout=600, cwd=None, label=''):
    """运行pip命令，检查返回码，失败时返回错误信息。成功返回(0,'')。
    自动加--timeout 30 --retries 5避免网络卡死。"""
    # 只在install命令时加网络参数
    if args and args[0] == 'install':
        extra = ['--timeout', '30', '--retries', '5', '--progress-bar', 'off']
        # 插入到install之后、包名之前
        args = [args[0]] + extra + args[1:]
    try:
        r = subprocess.run([pip] + args, capture_output=True, timeout=timeout, cwd=cwd)
        if r.returncode != 0:
            err = (r.stderr or b'').decode('utf-8', 'ignore')[-500:]
            return r.returncode, (label + '失败：' + err if label else err)
        return 0, ''
    except subprocess.TimeoutExpired:
        return -1, (label or 'pip') + '超时（%ds）' % timeout
    except Exception as e:
        return -2, (label or 'pip') + '异常：' + str(e)[:200]


def cosyvoice_install_async():
    """后台安装 CosyVoice：创建venv+装PyTorch+clone仓库+下载模型。返回 (ok, msg)。"""
    with _SETUP_LOCK:
        if TTS_SETUP['running']:
            return False, '已有安装任务在进行中'
        TTS_SETUP.update(running=True, op='cosyvoice', pct=0, msg='正在准备 CosyVoice 安装环境…', ok=None)

    def _run():
        try:
            import threading as _th
            MIRROR = ['-i', 'https://mirrors.aliyun.com/pypi/simple/', '--trusted-host', 'mirrors.aliyun.com']
            # 1. 创建venv（必须用Python 3.11/3.12，PyTorch不支持3.14）
            TTS_SETUP.update(pct=3, msg='查找 Python 3.11/3.12（PyTorch不支持3.14）…')
            base_py = _find_cosyvoice_python()
            if not base_py:
                TTS_SETUP.update(ok=False, pct=3, msg='❌ 未找到Python 3.11/3.12。请先安装Python 3.11（推荐uv或官网）', running=False)
                return
            TTS_SETUP.update(pct=5, msg='创建 Python 虚拟环境（基于 %s）…' % os.path.basename(os.path.dirname(base_py)))
            venv_dir = os.path.join(HERE, '.venv_cosyvoice')
            # 检查已有venv的Python版本，如果是3.13+（不兼容PyTorch）则删除重建
            if os.path.exists(COSYVOICE_VENV_PY):
                try:
                    vr = subprocess.run([COSYVOICE_VENV_PY, '--version'], capture_output=True, text=True, timeout=10)
                    ver = vr.stdout.strip()
                    if '3.13' in ver or '3.14' in ver or '3.15' in ver:
                        TTS_SETUP.update(pct=5, msg='检测到旧venv为%s（不兼容PyTorch），删除重建…' % ver)
                        import shutil as _sh
                        _sh.rmtree(venv_dir, ignore_errors=True)
                except Exception:
                    pass
            if not os.path.exists(COSYVOICE_VENV_PY):
                # 用找到的Python 3.11创建venv，而不是当前的3.14
                r = subprocess.run([base_py, '-m', 'venv', venv_dir], capture_output=True, timeout=120)
                if r.returncode != 0:
                    TTS_SETUP.update(ok=False, pct=5, msg='❌ venv创建失败：' + (r.stderr or b'').decode('utf-8','ignore')[:200], running=False)
                    return
            if not os.path.exists(COSYVOICE_VENV_PY):
                TTS_SETUP.update(ok=False, pct=5, msg='❌ venv创建失败，python.exe不存在', running=False)
                return
            pip = os.path.join(venv_dir, 'Scripts', 'pip.exe')
            py = COSYVOICE_VENV_PY
            # 升级pip
            TTS_SETUP.update(pct=8, msg='升级 pip…')
            _run_pip(pip, ['install', '--upgrade', 'pip'] + MIRROR, timeout=120, label='pip升级')
            # 先验证venv的pip可用
            TTS_SETUP.update(pct=10, msg='验证 pip 环境…')
            rc, err = _run_pip(pip, ['--version'], timeout=30, label='pip验证')
            if rc != 0:
                TTS_SETUP.update(ok=False, pct=10, msg='❌ venv的pip不可用：' + err[:200] + '。请删除.venv_cosyvoice后重试', running=False)
                return
            # 2. 安装PyTorch CUDA版（先检查是否已装，避免重复下载卡住）
            torch_ok = False
            try:
                _chk = subprocess.run([py, '-c', 'import torch; print(torch.__version__)'],
                                      capture_output=True, text=True, timeout=60)
                if _chk.returncode == 0 and _chk.stdout.strip():
                    torch_ok = True
                    TTS_SETUP.update(pct=25, msg='✅ PyTorch %s 已安装，跳过下载' % _chk.stdout.strip())
            except Exception:
                pass
            if not torch_ok:
                # 心跳：下载中每隔一段时间更新提示
                def _torch_heartbeat(stop_event, start_pct):
                    mins = 0
                    while not stop_event.is_set():
                        time.sleep(20)
                        mins += 1
                        if TTS_SETUP['running']:
                            TTS_SETUP.update(pct=min(start_pct + mins * 0.5, 45),
                                             msg='正在下载 PyTorch CUDA版（约2.4GB，已等%d分钟，请耐心等待）…' % mins)
                _stop_hb = _th.Event()
                _hb = _th.Thread(target=_torch_heartbeat, args=(_stop_hb, 15), daemon=True)
                _hb.start()
                torch_err = ''
                try:
                    TTS_SETUP.update(pct=15, msg='正在下载 PyTorch CUDA版（约2.4GB，根据网速需5-30分钟）…')
                    rc, torch_err = _run_pip(pip, ['install', 'torch', 'torchaudio', '--index-url',
                                                    'https://download.pytorch.org/whl/cu121'],
                                             timeout=3600, label='PyTorch CUDA')
                    torch_ok = (rc == 0)
                finally:
                    _stop_hb.set()
                # CUDA版失败，回退CPU版
                if not torch_ok:
                    TTS_SETUP.update(pct=18, msg='⚠️ CUDA版下载失败，尝试CPU版…')
                    rc2, err2 = _run_pip(pip, ['install', 'torch', 'torchaudio'] + MIRROR, timeout=600, label='PyTorch CPU')
                    if rc2 == 0:
                        torch_ok = True
                        TTS_SETUP.update(pct=20, msg='✅ PyTorch CPU版安装成功（无GPU加速，推理较慢）')
                    else:
                        torch_err = torch_err + ' | CPU版也失败: ' + err2
                if not torch_ok:
                    TTS_SETUP.update(ok=False, pct=15, msg='❌ PyTorch安装失败：' + torch_err[:300], running=False)
                    return
            # 3. 克隆CosyVoice仓库
            TTS_SETUP.update(pct=35, msg='克隆 CosyVoice 仓库…')
            if not os.path.isdir(COSYVOICE_REPO_DIR):
                try:
                    r = subprocess.run(['git', 'clone', '--depth', '1',
                                        'https://github.com/FunAudioLLM/CosyVoice.git', COSYVOICE_REPO_DIR],
                                       capture_output=True, timeout=300, cwd=HERE)
                    if r.returncode != 0:
                        # git失败，尝试用gitee镜像
                        r2 = subprocess.run(['git', 'clone', '--depth', '1',
                                             'https://gitee.com/mirrors/CosyVoice.git', COSYVOICE_REPO_DIR],
                                            capture_output=True, timeout=300, cwd=HERE)
                        if r2.returncode != 0:
                            TTS_SETUP.update(ok=False, pct=35,
                                             msg='❌ git clone失败：' + (r.stderr or r2.stderr or b'').decode('utf-8','ignore')[:200],
                                             running=False)
                            return
                except Exception as e:
                    TTS_SETUP.update(ok=False, pct=35, msg='❌ git clone异常：' + str(e)[:200], running=False)
                    return
            # 4. 安装依赖（国内镜像）
            TTS_SETUP.update(pct=50, msg='安装 CosyVoice 依赖（国内镜像，约2-3分钟）…')
            req = os.path.join(COSYVOICE_REPO_DIR, 'requirements.txt')
            if os.path.exists(req):
                rc, err = _run_pip(pip, ['install', '-r', req] + MIRROR, timeout=900,
                                   cwd=COSYVOICE_REPO_DIR, label='依赖安装')
                if rc != 0:
                    # requirements.txt失败，尝试最小依赖
                    TTS_SETUP.update(pct=52, msg='⚠️ 完整依赖安装失败，尝试最小依赖…')
                    # 分包安装，避免一个大包卡死整个流程
                    dep_packages = ['numpy', 'scipy', 'librosa', 'soundfile',
                                    'transformers', 'onnxruntime', 'modelscope',
                                    'einops', 'rotary-embedding-torch', 'tqdm', 'pillow',
                                    'hyperpyyaml', 'conformer', 'omegaconf', 'hydra-core',
                                    'pyworld', 'wetext', 'inflect', 'openai-whisper']
                    dep_ok = True
                    dep_err = ''
                    for i, pkg in enumerate(dep_packages):
                        TTS_SETUP.update(pct=52 + i * 0.9, msg='正在装依赖 %d/%d：%s…' % (i+1, len(dep_packages), pkg))
                        rc_p, err_p = _run_pip(pip, ['install', pkg] + MIRROR, timeout=300, label=pkg)
                        if rc_p != 0:
                            # modelscope是必须的（下载模型用），其他包失败可以继续
                            if pkg == 'modelscope':
                                dep_ok = False
                                dep_err = err_p
                                break
                            else:
                                TTS_SETUP.update(pct=52 + i * 0.9, msg='⚠️ %s装失败，跳过继续…' % pkg)
                                continue
                    if not dep_ok:
                        TTS_SETUP.update(ok=False, pct=55, msg='❌ 关键依赖modelscope安装失败：' + dep_err[:300], running=False)
                        return
            # 确保modelscope已装（下载模型需要）
            rc, err = _run_pip(pip, ['install', 'modelscope'] + MIRROR, timeout=300, label='modelscope')
            # 5. 下载模型（ModelScope国内镜像）
            # 检查是否已有模型文件，有则跳过下载
            os.makedirs(os.path.dirname(COSYVOICE_MODEL_DIR), exist_ok=True)
            existing_files = []
            try:
                existing_files = os.listdir(COSYVOICE_MODEL_DIR)
            except Exception:
                pass
            has_model = any(f.endswith('.pt') or f.endswith('.onnx') or f == 'config.yaml' for f in existing_files)
            if has_model:
                TTS_SETUP.update(pct=90, msg='✅ 模型已存在，跳过下载')
            else:
                # 下载心跳
                _stop_dl = _th.Event()
                def _dl_heartbeat(stop_evt, base_pct):
                    m = 0
                    while not stop_evt.is_set():
                        time.sleep(20)
                        m += 1
                        if TTS_SETUP['running']:
                            # 检查模型目录大小，估算进度
                            try:
                                total = 0
                                for root, dirs, files in os.walk(COSYVOICE_MODEL_DIR):
                                    for f in files:
                                        total += os.path.getsize(os.path.join(root, f))
                                gb = total / (1024**3)
                                est = min(95, base_pct + gb * 3.0)  # 9GB约占30%进度
                                TTS_SETUP.update(pct=est, msg='正在下载模型（已等%d分钟，已下%.1fGB/约9GB）…' % (m, gb))
                            except Exception:
                                TTS_SETUP.update(pct=min(base_pct + m, 90), msg='正在下载模型（已等%d分钟）…' % m)
                _dl_hb = _th.Thread(target=_dl_heartbeat, args=(_stop_dl, 65), daemon=True)
                _dl_hb.start()
                TTS_SETUP.update(pct=65, msg='下载 CosyVoice2-0.5B 模型（约9GB，国内镜像，需10-20分钟）…')
                dl_script = os.path.join(HERE, '_cosyvoice_dl.py')
                with open(dl_script, 'w', encoding='utf-8') as f:
                    f.write('''from modelscope import snapshot_download
snapshot_download("iic/CosyVoice2-0.5B", local_dir=r"%s")
''' % COSYVOICE_MODEL_DIR.replace('\\', '\\\\'))
                try:
                    r = subprocess.run([py, dl_script], capture_output=True, timeout=3600, cwd=HERE)
                    if r.returncode != 0:
                        dl_err = (r.stderr or b'').decode('utf-8', 'ignore')[-400:]
                        TTS_SETUP.update(ok=False, pct=65, msg='❌ 模型下载失败：' + dl_err, running=False)
                        return
                finally:
                    _stop_dl.set()
                    try: os.unlink(dl_script)
                    except Exception: pass
            # 6. 创建worker脚本
            TTS_SETUP.update(pct=95, msg='配置推理脚本…')
            _ensure_cosyvoice_worker()
            # 验证模型目录有关键文件
            model_files = []
            try:
                model_files = os.listdir(COSYVOICE_MODEL_DIR)
            except Exception:
                pass
            has_model = any(f.endswith('.pt') or f.endswith('.onnx') or f == 'config.yaml' for f in model_files)
            ok = has_model and os.path.exists(COSYVOICE_VENV_PY)
            if ok:
                # 最终验证：尝试import CosyVoice2，确保依赖完整
                TTS_SETUP.update(pct=98, msg='验证推理环境…')
                try:
                    _vfy = subprocess.run(
                        [py, '-c',
                         "import sys; sys.path.insert(0, r'%s\\third_party\\Matcha-TTS'); sys.path.insert(0, r'%s'); from cosyvoice.cli.cosyvoice import CosyVoice2; print('OK')"
                         % (COSYVOICE_REPO_DIR, COSYVOICE_REPO_DIR)],
                        capture_output=True, text=True, timeout=120, cwd=HERE)
                    if _vfy.returncode == 0 and 'OK' in _vfy.stdout:
                        TTS_SETUP.update(ok=True, pct=100, msg='✅ CosyVoice 安装完成！引擎选「CosyVoice」即可使用', running=False)
                    else:
                        _verr = (_vfy.stderr or '')[-300:]
                        TTS_SETUP.update(ok=True, pct=100, msg='✅ CosyVoice 安装完成（验证跳过：%s）。引擎选「CosyVoice」即可使用' % _verr.split(chr(10))[-1][:80], running=False)
                except Exception:
                    TTS_SETUP.update(ok=True, pct=100, msg='✅ CosyVoice 安装完成（验证超时，不影响使用）。引擎选「CosyVoice」即可使用', running=False)
            else:
                TTS_SETUP.update(ok=False, pct=95, msg='❌ 模型文件不完整，请检查网络后重试', running=False)
        except Exception as e:
            import traceback
            TTS_SETUP.update(ok=False, pct=0, msg='❌ 安装异常：' + str(e)[:200] + ' | ' + traceback.format_exc()[-200:], running=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True, '开始安装 CosyVoice（约10-20分钟，含9GB模型下载）'


def _ensure_cosyvoice_worker():
    """确保 cosyvoice_worker.py 存在（venv子进程推理脚本）。"""
    worker = os.path.join(HERE, 'cosyvoice_worker.py')
    if os.path.exists(worker):
        return
    with open(worker, 'w', encoding='utf-8') as f:
        f.write('''# -*- coding: utf-8 -*-
import sys, os
# CosyVoice 需要 third_party/Matcha-TTS 在 path 中
repo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CosyVoice')
matcha = os.path.join(repo_dir, 'third_party', 'Matcha-TTS')
if os.path.isdir(matcha):
    sys.path.insert(0, matcha)
if os.path.isdir(repo_dir):
    sys.path.insert(0, repo_dir)

def main():
    if len(sys.argv) < 5:
        print('Usage: cosyvoice_worker.py <txt_file> <out_wav> <model_dir> <voice>')
        sys.exit(1)
    txt_file, out_path, model_dir, voice = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    with open(txt_file, encoding='utf-8') as f:
        text = f.read().strip()
    if not text:
        print('empty text')
        sys.exit(1)
    from cosyvoice.cli.cosyvoice import CosyVoice2
    import torchaudio
    model = CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=False)
    for i, j in enumerate(model.inference_sft(text, voice, stream=False)):
        torchaudio.save(out_path, j['tts_speech'], model.sample_rate)
        break
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        print('OK')
    else:
        print('FAIL: output too small')
        sys.exit(1)

if __name__ == '__main__':
    main()
''')



# ---------------------------------------------------------------------------
# 🎙️ TTS 情感化标记系统
# 文案标记格式（LLM 生成解说词时使用）：
#   {情绪:欢快|悲伤|激动|严肃|紧张|温柔|深情|播报|不满} ... {/情绪}
#   {停顿:0.5}          → 0.5秒停顿
#   {慢}...{/慢}        → 语速减慢
#   {快}...{/快}        → 语速加快
#   {高音}...{/高音}    → 音调升高
#   {低音}...{/低音}    → 音调降低
#   {大声}...{/大声}    → 音量增大
#   {小声}...{/小声}    → 音量减小
# ---------------------------------------------------------------------------


# edge-tts 支持情感的中文声线
























# 离线配音模型（sherpa-onnx）。可选多个，音质差别明显：
#   melo-zh      = MeloTTS 中文：自然度明显好于 piper，接近在线神经 TTS，约 180MB
#   piper-huayan = piper 中文小模型：体积最小、最快，但机械感重、几乎没有语调起伏
# 实测用户反馈「拟真和感情不行」时用的正是 piper-huayan，故默认优先 melo。







def tts_model_uninstall(key):
    """删除已下载的离线配音模型目录（models/tts/<name>/），释放磁盘空间。
    返回 (ok, msg)。"""
    m = SHERPA_TTS_MODELS.get(key)
    if not m:
        return False, '未知模型：%s' % key
    d = os.path.join(tts_models_dir(), m['name'])
    if not os.path.isdir(d):
        return False, '模型未下载，无需卸载'
    import shutil
    try:
        shutil.rmtree(d)
        return True, '已卸载 %s（%s）' % (m['label'], m['name'])
    except Exception as e:
        return False, '卸载失败：%s' % str(e)






TTS_SETUP = {'running': False, 'op': '', 'pct': 0, 'msg': '', 'ok': None}
_SETUP_LOCK = threading.Lock()      # 保护「检查 running → 置位 → 启动线程」的原子性


def tts_install_async(pkg='edge-tts'):
    """后台 pip 安装本地配音引擎。pkg ∈ edge-tts|sherpa-onnx。"""
    pkg = 'sherpa-onnx' if 'sherpa' in str(pkg).lower() else 'edge-tts'
    # 检查与置位必须在同一把锁内：多线程 HTTP 服务下，两次点击可能同时通过检查，
    # 起两个线程共写同一个进度槽，进度条乱跳、模型目录被 os.replace 两次而报错。
    with _SETUP_LOCK:
        if TTS_SETUP['running']:
            return False, '已有一个安装/下载任务在进行中'
        TTS_SETUP.update(running=True, op='pip:' + pkg, pct=0,
                         msg='正在安装 %s…' % pkg, ok=None)

    def _run():
        try:
            r = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                 '--no-input', pkg],
                capture_output=True, timeout=600)
            if r.returncode == 0:
                TTS_SETUP.update(ok=True, pct=100, msg='✅ %s 安装完成（点「🧪 试听」验证）' % pkg)
            else:
                TTS_SETUP.update(ok=False, pct=100,
                                 msg='❌ 安装失败：' + (r.stderr or b'').decode('utf-8', 'ignore')[-300:])
        except Exception as e:
            TTS_SETUP.update(ok=False, pct=100, msg='❌ 安装异常：' + str(e)[-300:])
        finally:
            TTS_SETUP['running'] = False

    _threading.Thread(target=_run, daemon=True).start()
    return True, '开始安装 %s（约 1 分钟，可在下方看进度）' % pkg


def tts_install_chattts_async():
    """后台安装 ChatTTS：uv Python 3.11 venv + CUDA torch + ChatTTS。"""
    with _SETUP_LOCK:
        if TTS_SETUP['running']:
            return False, '已有一个安装/下载任务在进行中'
        TTS_SETUP.update(running=True, op='pip:chattts', pct=5,
                         msg='正在安装 ChatTTS（torch约2.5GB，首次较慢）…', ok=None)

    def _run():
        try:
            venv_py = os.path.join(HERE, '.venv_tts', 'Scripts', 'python.exe')
            if not os.path.exists(venv_py):
                TTS_SETUP.update(pct=10, msg='创建 Python 3.11 虚拟环境…')
                subprocess.run(['uv', 'venv', '--python', '3.11', '.venv_tts'],
                               cwd=HERE, capture_output=True, timeout=120)
            TTS_SETUP.update(pct=20, msg='安装 CUDA torch（约2.5GB，较慢）…')
            subprocess.run(['uv', 'pip', 'install', '--python', venv_py,
                            'torch', 'torchaudio', '--index-url',
                            'https://download.pytorch.org/whl/cu121'],
                           cwd=HERE, capture_output=True, timeout=1800)
            TTS_SETUP.update(pct=80, msg='安装 ChatTTS…')
            subprocess.run(['uv', 'pip', 'install', '--python', venv_py,
                            'ChatTTS', 'soundfile', 'numpy'],
                           cwd=HERE, capture_output=True, timeout=600)
            TTS_SETUP.update(ok=True, pct=100, msg='✅ ChatTTS 安装完成（点「🧪 试听」验证）')
        except Exception as e:
            TTS_SETUP.update(ok=False, pct=100, msg='❌ 安装异常：' + str(e)[-300:])
        finally:
            TTS_SETUP['running'] = False

    _threading.Thread(target=_run, daemon=True).start()
    return True, '已开始后台安装 ChatTTS，可在下方看进度'


def tts_model_download_async(model_key=None):
    """后台下载离线中文配音模型（tar.bz2）到 models/tts/<name>/ 并解压。

    model_key：SHERPA_TTS_MODELS 的键；不传则下载当前选中的（或默认推荐）模型。"""
    key = model_key or sherpa_model_key()
    m = SHERPA_TTS_MODELS.get(key)
    if not m:
        return False, '未知模型：%s' % key
    name = m['name']
    dest = os.path.join(tts_models_dir(), name)
    if _sherpa_ready(key):
        return True, '模型已就绪，无需重复下载'
    with _SETUP_LOCK:      # 与 tts_install_async 互斥，防双点起两个下载线程
        if TTS_SETUP['running']:
            return False, '已有一个安装/下载任务在进行中'
        TTS_SETUP.update(running=True, op='model:' + key, pct=0,
                         msg='正在下载离线配音模型（%s）…' % m['label'], ok=None)

    def _run():
        import tarfile
        import urllib.request as _u
        # 文件名带模型 key：两个模型可以同时/先后下载，互不覆盖
        arch = os.path.join(tts_models_dir(), '_dl_%s.tar.bz2' % key)
        try:
            os.makedirs(tts_models_dir(), exist_ok=True)
            tmp = arch + '.part'
            _u.urlretrieve(m['url'], tmp)
            TTS_SETUP.update(pct=70, msg='下载完成，正在解压…')
            os.replace(tmp, arch)
            with tarfile.open(arch, 'r:bz2') as tf:
                tf.extractall(tts_models_dir())
            os.unlink(arch)
            # 发行包内目录名可能与模型名不同 → 归一到 <name>
            if not os.path.isdir(dest):
                for n in os.listdir(tts_models_dir()):
                    p = os.path.join(tts_models_dir(), n)
                    if os.path.isdir(p) and any(f.endswith('.onnx') for f in os.listdir(p)):
                        os.replace(p, dest)
                        break
            ok = _sherpa_ready(key)
            TTS_SETUP.update(ok=ok, pct=100,
                             msg='✅ %s 就绪' % m['label'] if ok
                             else '❌ 解压后未找到模型文件，请重试')
        except Exception as e:
            TTS_SETUP.update(ok=False, pct=100, msg='❌ 下载失败：' + str(e)[-300:])
        finally:
            # 【必须】缺这一句时 running 永远为 True：前端轮询定时器不停（永久转圈），
            # 且此后所有安装/下载请求都被「已有一个任务在进行中」挡掉，只能重启服务。
            TTS_SETUP['running'] = False

    _threading.Thread(target=_run, daemon=True).start()
    return True, '开始下载离线配音模型（约 130MB，取决于网速）'


def tts_test_speak(text='这是一段中文配音试听。'):
    """试听一句：返回 (ok, msg, engine, rel_path)。rel_path 相对 OUTDIR，供 /media 播放。"""
    text = (text or '').strip() or '这是一段中文配音试听。'
    out_dir = os.path.join(OUTDIR, '_tts_test')
    os.makedirs(out_dir, exist_ok=True)
    # 文件名带时间戳：连点两次「试听」时，旧实现的固定名 sample.mp3 会被第二次
    # 先删后写，导致第一次正在播放的请求读到 404 或半截文件
    out = os.path.join(out_dir, 'sample_%d.mp3' % int(time.time() * 1000))
    ok, eng, path = local_tts_speak(text, out)
    if not ok or not os.path.exists(path):
        hint = ''
        if not edge_tts_available() and edge_tts_dead_reason():
            hint = '（edge-tts 暂时不可用：%s）' % edge_tts_dead_reason()
        return False, '配音失败：没有可用的配音引擎，先装 edge-tts 或下载离线模型' + hint, None, ''
    rel = os.path.relpath(path, OUTDIR).replace('\\', '/')
    label = {'edge': 'edge-tts', 'sherpa': '离线模型', 'sapi': '系统 SAPI'}.get(eng, eng or '未知')
    return True, '✅ 试听已生成（%s，%.1f 秒）' % (label, probe_audio_len(path) or 0), eng, rel




# ---------------------------------------------------------------------------
# [3.3] 工作流调度层已拆至 workflows.py：其加载完成时会把下列符号注入回本命名空间。
# 此处静态兜底绑定让 pyflakes/IDE 可解析（standalone 导入 workflows 的罕见顺序下
# 先为 None，随后被其末尾注入覆盖为真值；正常顺序下直接拿到真函数）。
# ---------------------------------------------------------------------------
import workflows as _workflows_mod  # noqa: E402
parse_instruction = getattr(_workflows_mod, 'parse_instruction', None)
_resolve_music = getattr(_workflows_mod, '_resolve_music', None)
fail_task = getattr(_workflows_mod, 'fail_task', None)
_music_catalog_entry = getattr(_workflows_mod, '_music_catalog_entry', None)
_task_credits = getattr(_workflows_mod, '_task_credits', None)
_finish_task_credits = getattr(_workflows_mod, '_finish_task_credits', None)
dispatch_build = getattr(_workflows_mod, 'dispatch_build', None)
_plan_thumbs = getattr(_workflows_mod, '_plan_thumbs', None)
_plan_to_ui = getattr(_workflows_mod, '_plan_to_ui', None)
_analyze_plan_job = getattr(_workflows_mod, '_analyze_plan_job', None)
_render_plan_job = getattr(_workflows_mod, '_render_plan_job', None)
dispatch_beatcut = getattr(_workflows_mod, 'dispatch_beatcut', None)
dispatch_narrate = getattr(_workflows_mod, 'dispatch_narrate', None)
dispatch_movie = getattr(_workflows_mod, 'dispatch_movie', None)
dispatch_movie_tts = getattr(_workflows_mod, 'dispatch_movie_tts', None)
dispatch_movie_compose = getattr(_workflows_mod, 'dispatch_movie_compose', None)
dispatch_tts_single = getattr(_workflows_mod, 'dispatch_tts_single', None)
dispatch_tts_regen_all = getattr(_workflows_mod, 'dispatch_tts_regen_all', None)
dispatch_instruct = getattr(_workflows_mod, 'dispatch_instruct', None)
collect_partial = getattr(_workflows_mod, 'collect_partial', None)
assemble = getattr(_workflows_mod, 'assemble', None)
finalize = getattr(_workflows_mod, 'finalize', None)
_start_next_queued = getattr(_workflows_mod, '_start_next_queued', None)


# ---------------------------------------------------------------------------
# [3.3] HTTP 层已拆至 handler.py：其加载完成时会把下列符号注入回本命名空间。
# 静态兜底绑定同上（pyflakes/IDE 可解析；standalone 导入顺序下先 None 后被覆盖）。
# ---------------------------------------------------------------------------
import handler as _handler_mod  # noqa: E402
Handler = getattr(_handler_mod, 'Handler', None)
start_server = getattr(_handler_mod, 'start_server', None)
_content_disposition = getattr(_handler_mod, '_content_disposition', None)
_evict_finished_progress = getattr(_handler_mod, '_evict_finished_progress', None)
_kill_all_child_processes = getattr(_handler_mod, '_kill_all_child_processes', None)
webbrowser_open = getattr(_handler_mod, 'webbrowser_open', None)

if __name__ == '__main__':
    _load_progress()
    _load_queue_from_disk()
    _start_progress_saver()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ensure_deps()
    port = int(os.environ.get('PORT', '8765'))
    start_server(port)
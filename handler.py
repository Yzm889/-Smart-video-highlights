# -*- coding: utf-8 -*-
"""HTTP 服务层（batch4-3.3 由 webui_server.py 拆出，符号与拆分前等价）。

约定（与 workflows.py 相同）：
- 宿主（webui_server）的模块级符号（dispatch_* 调度 / PROGRESS / 目录常量 /
  上传与历史等）一律经 `_w.<符号>` 在调用时解析——测试对 `webui_server.<符号>`
  的 monkeypatch、conftest 的 OUTDIR/HISTORY_PATH 隔离改写因此继续生效；
- 本层公共符号在文件末尾注入回宿主命名空间，`webui_server.Handler` /
  `webui_server.start_server` 等旧入口不变。
"""
import threading as _threading
import atexit
import base64
import json
import os
import random
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_HOST_FILE = os.path.join(_HERE, 'webui_server.py')


def _host():
    """宿主模块引用：正常导入时宿主已在 sys.modules（本层只在调用时取属性，
    部分初始化也安全）；`python webui_server.py` 直启时宿主注册名是 __main__，
    按文件路径匹配；standalone 导入本模块时完整导入宿主。"""
    m = sys.modules.get('webui_server')
    if m is not None:
        return m
    m = sys.modules.get('__main__')
    if m is not None and os.path.abspath(getattr(m, '__file__', '')) == _HOST_FILE:
        return m
    import webui_server
    return webui_server


_w = _host()




# ---------------------------------------------------------------------------
# 错误体验 (P0-first) — 统一分类 + 翻译：[P0 toast 升级]
# ---------------------------------------------------------------------------
# 后端所有 self._send(5xx, {'ok':False,'error':str(e)}) 这类调用，全部改走 _send_err(...)
# 前端 ERR_RULES 在收到 error_kind 后渲染「人话 + 修复指引」toast，不再 alert 乱码。
_ERROR_KIND_RULES = [
    # (regex, kind) — 命中第一个匹配的；kind 必须与前端 ERR_RULES 一致
    (re.compile(r'\berrno 2\b|no such file|找不到|系统找不到指定的路径|file not found', re.I), 'path'),
    (re.compile(r'\berrno 28\b|no space|disk full|磁盘|空间不足', re.I), 'disk'),
    (re.compile(r'timed out|timeout|超时', re.I), 'timeout'),
    (re.compile(r'connection refused|connectionerror|unauthorized|api[_-]?key|401|403|429|ssl|proxy|网络连接|certificate|远程拒绝|网络断开', re.I), 'net'),
    (re.compile(r'permission|access is denied|\berrno 13\b|拒绝访问', re.I), 'perm'),
    (re.compile(r'显存|cuda out of memory|oom|cublas', re.I), 'oom'),
    (re.compile(r'model not found|no such model|whisper|ollama|qwen|未下载|未部署|未就绪', re.I), 'model'),
    (re.compile(r'ffmpeg|exit code|invalid data|no such filter|moov atom', re.I), 'frame'),
    (re.compile(r'字体|font|cjk|glyph|豆腐', re.I), 'font'),
    (re.compile(r'并发上限|已达上限|queue is full|too many|已有任务', re.I), 'busy'),
]


def _classify_exception(e, fallback='other'):
    """把任意异常字符串化后归到 ERR_RULES 的 kind。

    用法：
        kind, message = _classify_exception(e)
        self._send_err(500, kind, message, stage='配音', detail=str(e))
    """
    raw = str(e) if e is not None else ''
    msg = raw[:300] if raw else '未知错误'
    for r, k in _ERROR_KIND_RULES:
        if r.search(raw):
            return k, msg
    return fallback, msg


def _http_status_for_kind(kind):
    """根据错误种类映射 HTTP 状态码（前端已分类，不需要翻译的 200 也行，
    但 5xx 让反向代理 / 监控更清晰）。"""
    return 400 if kind in ('path', 'disk', 'perm', 'model', 'busy') else 500


# ---------------------------------------------------------------------------
# 本地 HTTP 服务 + 图形化前端
# ---------------------------------------------------------------------------
MIME = {
    '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.mp4': 'video/mp4',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    # 音频：配音试听（/media/_tts_test/sample.mp3）与配音片段走 /media 时
    # 缺 MIME 会退化成 application/octet-stream，<audio> 在部分浏览器上拒绝播放
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
    '.mp4': 'video/mp4', '.srt': 'text/plain; charset=utf-8', '.webm': 'video/webm',
}




class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, content, ctype='text/plain; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        # 页面与静态资源禁止缓存：前端更新后浏览器必须拉最新版，
        # 否则旧缓存的 index.html/app.js 会与新后端接口错位（本机单用户场景无性能顾虑）
        self.send_header('Cache-Control', 'no-cache')
        # 禁止 MIME 嗅探：否则上传的 .html 素材被当成 text/html 在同源下执行（存储型 XSS）
        self.send_header('X-Content-Type-Options', 'nosniff')
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def _send_err(self, code, kind, msg, *, stage=None, detail=None, jump=None, hint=None):
        """统一错误响应（前端 ERR_RULES 按 kind 命中并渲染「人话 + 修复建议」toast）。

        同时保留顶级 `error` 键以兼容尚未升级的前端调用方。
        """
        body = {'ok': False, 'error': msg, 'error_kind': kind}
        if stage:  body['error_stage']  = stage
        if detail: body['error_detail'] = detail
        if jump:   body['jump']         = jump
        if hint:   body['hint']         = hint
        self._send(code, json.dumps(body, ensure_ascii=False).encode('utf-8'), 'application/json')

    def _send_file(self, full, ctype, attachment=False):
        """流式发送文件，支持 HTTP Range（视频跳转/拖拽进度必须）。

        旧实现不支持 Range：浏览器 video.currentTime 跳转时发 Range 请求，
        服务器返回完整 200，浏览器无法 seek，视频只能从头播放。"""
        size = os.path.getsize(full)
        range_header = self.headers.get('Range')
        if range_header and range_header.startswith('bytes='):
            # 解析 Range: bytes=start-end
            try:
                rng = range_header[6:].split('-')
                start = int(rng[0]) if rng[0] else 0
                end = int(rng[1]) if len(rng) > 1 and rng[1] else size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', 'bytes */%d' % size)
                    self.end_headers()
                    return
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(length))
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Content-Type-Options', 'nosniff')
                if attachment:
                    self.send_header('Content-Disposition',
                                     _content_disposition(os.path.basename(full)))
                self.end_headers()
                with open(full, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except Exception:
                pass  # Range解析失败，回退完整发送
        # 无 Range 或解析失败：完整发送
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(size))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if attachment:
            self.send_header('Content-Disposition',
                             _content_disposition(os.path.basename(full)))
        self.end_headers()
        with open(full, 'rb') as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def _read_json(self, length, max_len=300 * 1024 * 1024):
        """按 Content-Length 分块读取并解析 JSON 请求体。
        超过 max_len 时也必须把请求体读完（排空）再返回 None——提前关连接的话，
        客户端还在发送会收到 WinError 10053 连接中断，看不到友好的「请求过大」。
        用 list 收集 + b''.join 一次拼接：旧写法 raw += chunk 是 O(n²) 拷贝，大文件上传显著变慢。"""
        if length <= 0:
            return {}
        if length > max_len:
            remaining = length
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except Exception:
                pass
            return None
        parts = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            parts.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(parts)
        return json.loads(raw.decode('utf-8'))



    def _spawn(self, fn, req):
        """登记一个后台任务并启动线程，返回 runid 供前端轮询。
        每个任务拥有独立的 run_dir（OUTDIR/runid-时间戳），产物互不干扰；并把 runid 绑定到
        任务线程的 TLS，使 ffmpeg_run 能注册进程并响应「取消」。
        并发上限见 _TASK_SEM：拿不到名额直接拒绝（do_POST 会把这句原样显示给用户）。"""
        # 非阻塞取名额：满了就加入排队队列，前面的跑完自动开始下一个
        if not _w._TASK_SEM.acquire(blocking=False):
            # 加入排队队列
            runid = 'run-%d' % next(_w._RUN_CTR)
            run_dir = os.path.join(_w.OUTDIR, '%s-%s' % (runid, time.strftime('%Y%m%d-%H%M%S')))
            os.makedirs(run_dir, exist_ok=True)
            queue_pos = len(_w._TASK_QUEUE) + 1
            prog = {'phase': '排队中（前面还有%d个任务）' % queue_pos, 'pct': 0, 'done': False,
                    'runid': runid, 'run_dir': run_dir, 'queued': True,
                    'queued_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
            _w.PROGRESS[runid] = prog
            _evict_finished_progress(keep=100)
            with _w._TASK_QUEUE_LOCK:
                _w._TASK_QUEUE.append((fn, req, runid, run_dir, prog))
                _w._persist_queue_unlocked()
            print(f'[DIAG] 任务加入排队: {runid}，队列长度={len(_w._TASK_QUEUE)}')
            return runid
        try:
            runid = 'run-%d' % next(_w._RUN_CTR)
            # 目录名带时间戳：服务重启后 runid 从 1 重新计数会复用 run-N 名字，
            # 否则新任务会写进旧目录覆盖成片，历史记录（⑨记录）也随之指向错误文件
            run_dir = os.path.join(_w.OUTDIR, '%s-%s' % (runid, time.strftime('%Y%m%d-%H%M%S')))
            os.makedirs(run_dir, exist_ok=True)
            prog = {'phase': '排队', 'pct': 0, 'done': False, 'runid': runid, 'run_dir': run_dir}
            _w.PROGRESS[runid] = prog
            _evict_finished_progress(keep=100)

            def _runner():
                _w._TLS.runid = runid
                # 清掉上一次任务锁定的配音引擎：每个任务重新选，避免沿用旧音色
                try:
                    del _w._TLS.tts_engine
                except Exception:
                    pass
                # 保存活动任务状态（崩溃恢复用）
                _w._save_active_task(runid, phase=prog.get('phase',''), pct=prog.get('pct',0),
                                     run_dir=run_dir, task_type=getattr(fn, '__name__', 'unknown'))
                # 监听prog变化，实时更新活动任务状态
                _orig_phase = prog.get('phase', '')
                class _ProgProxy(dict):
                    def __setitem__(self, k, v):
                        super().__setitem__(k, v)
                        if k in ('phase', 'pct'):
                            _w._save_active_task(runid, phase=self.get('phase',''), pct=self.get('pct',0),
                                                 run_dir=run_dir, task_type=getattr(fn, '__name__', 'unknown'))
                prog = _ProgProxy(prog)
                _w.PROGRESS[runid] = prog  # PROGRESS也指向proxy，前端轮询能看到更新
                try:
                    fn(req, prog)
                    # 成功收尾才署名：失败的视频不会流出去，也就没有署名义务
                    # （各 dispatch_* 自己吞异常，失败态只能靠 prog['error'] 判断）
                    if not prog.get('error'):
                        _w._finish_task_credits(req, prog)
                except _w.AbortError:
                    prog['done'] = True
                    prog['aborted'] = True
                    prog['error'] = '已取消（用户中断）'
                except Exception as e:
                    _w.fail_task(prog, e)
                finally:
                    _w._clear_active_task()   # 任务结束，清除活动状态
                    _w._TASK_SEM.release()   # 无论成功/失败/取消都必须归还名额
                    # 启动排队中的下一个任务
                    _w._start_next_queued()

            _threading.Thread(target=_runner, daemon=True).start()
        except Exception:
            _w._TASK_SEM.release()   # 建目录/登记失败时别把名额漏掉
            raise
        return runid

    # ==================== HTTP 路由处理器 ====================
# 每个方法体与拆分前 do_GET/do_POST 内对应分支逐字一致（仅缩进变化），
# 新增端点时：加一个 _get_/_post_ 方法 + 在下方路由表登记一行即可。

    def _get_index(self):
        idx = os.path.join(_w.STATIC_DIR, 'index.html')
        if os.path.exists(idx):
            self._send_file(idx, 'text/html; charset=utf-8')
        else:
            self._send(500, '前端文件缺失：请确保 static/ 目录存在'.encode('utf-8'), 'text/html; charset=utf-8')

    def _get_static_files(self):
        path = _w.urlparse(self.path).path
        name = path[len('/static/'):].split('?')[0]
        full = os.path.join(_w.STATIC_DIR, os.path.basename(name))
        if os.path.isfile(full):
            ext = os.path.splitext(full)[1].lower()
            self._send_file(full, MIME.get(ext, 'application/octet-stream'))
            return
        self._send(404, b'not found')

    def _get_media_files(self):
        path = _w.urlparse(self.path).path
        name = path[len('/media/'):].split('?')[0]
        # 只服务 OUTDIR（成片/中间产物）。
        # 【安全修复】旧实现会回退到项目根 HERE —— /media/ai_config.json 可无鉴权
        # 读出明文 API Key，/media/webui_server.py 与 /media/.git/config 同样可读。
        # 实测确认可泄露，且 HOST=0.0.0.0（Docker）时局域网内任何人可拿。
        # 内置图片（img1~4.png）由后端按本地路径直接交给 ffmpeg，不经 /media/，删除回退无影响。
        full = _w._safe_join(_w.OUTDIR, name)
        if full:
            ext = os.path.splitext(full)[1].lower()
            self._send_file(full, MIME.get(ext, 'application/octet-stream'))
            return
        self._send(404, b'not found')

    def _get_music_lib_files(self):
        path = _w.urlparse(self.path).path
        name = path[len('/music_lib/'):].split('?')[0]
        full = _w._safe_join(_w.MUSIC_DIR, name)
        if full:
            self._send_file(full, MIME.get('.mp3', 'audio/mpeg'))
            return
        self._send(404, b'not found')

    def _get_music_search(self):
        q = _w.parse_qs(_w.urlparse(self.path).query).get('q', [''])[0]
        self._send(200, json.dumps({'ok': True, 'results': _w.search_catalog(q)}).encode('utf-8'),
                   'application/json')

    def _get_music_use(self):
        q = _w.parse_qs(_w.urlparse(self.path).query).get('id', [None])[0]
        if not q:
            self._send(200, json.dumps({'ok': False, 'error': '缺少 id'}).encode('utf-8'), 'application/json')
            return
        try:
            p = _w.download_catalog(q)
            self._send(200, json.dumps({'ok': True, 'file': os.path.basename(p),
                                        'url': '/music_lib/' + os.path.basename(p)}).encode('utf-8'),
                       'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _get_material_list(self):
        self._send(200, json.dumps({'ok': True, 'items': _w.material_list()}).encode('utf-8'),
                   'application/json')

    def _get_material_lib_files(self):
        path = _w.urlparse(self.path).path
        name = _w.unquote(path[len('/material_lib/'):].split('?')[0])
        full = _w._safe_join(_w.MATERIAL_DIR, name)
        if full:
            # 【安全修复】素材库接受任意扩展名上传，若按 MIME 返回 text/html，
            # 上传的 .html 会在 http://localhost:8765 同源下执行 —— 后端无鉴权，
            # 脚本可直接调 /api/ai/config、/api/history/clear（存储型 XSS）。
            # 统一以附件下载方式返回，浏览器不会把它当页面渲染。
            self._send_file(full, 'application/octet-stream', attachment=True)
            return
        self._send(404, b'not found')

    def _get_bili_search(self):
        kw = _w.parse_qs(_w.urlparse(self.path).query).get('kw', [''])[0].strip()
        if not kw:
            self._send(200, json.dumps({'ok': False, 'error': '缺少关键词'}).encode('utf-8'), 'application/json')
            return
        try:
            res = _w.bili_search(kw, 8)
            self._send(200, json.dumps({'ok': True, 'results': res}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')

    def _get_bili_status(self):
        self._send(200, json.dumps({'ok': True, **_w.BILI_PULL}).encode('utf-8'), 'application/json')

    def _get_tts_reset(self):
        try:
            _w._EDGE_STATE.update(fails=0, dead_until=0.0, reason='')
            # 同时清除TLS引擎锁定，下次配音重新选择最优引擎
            try:
                if hasattr(_w._TLS, 'tts_engine'):
                    delattr(_w._TLS, 'tts_engine')
            except Exception:
                pass
            self._send(200, json.dumps({'ok': True, 'msg': '配音引擎已重置，edge-tts熔断已解除'}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _get_tts_recent(self):
        try:
            import glob as _glob
            dirs = sorted(_glob.glob(os.path.join(_w.OUTDIR, '*')), key=os.path.getmtime, reverse=True)
            recent = []
            for d in dirs[:20]:
                if os.path.isdir(d) and os.path.exists(os.path.join(d, 'tts_state.json')):
                    import json as _j
                    try:
                        st = _j.load(open(os.path.join(d, 'tts_state.json'), encoding='utf-8'))
                        recent.append({
                            'run_dir': os.path.basename(d),
                            'movie': st.get('movie_name', ''),
                            'narr_count': len(st.get('narr', [])),
                            'tts_count': len(st.get('tts_results', [])),
                            'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(d)))
                        })
                    except Exception:
                        pass
            self._send(200, json.dumps({'ok': True, 'list': recent}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _get_tts_state(self):
        try:
            qs = _w.parse_qs(_w.urlparse(self.path).query)
            run_dir_name = (qs.get('run_dir') or [''])[0]
            if not run_dir_name:
                self._send(200, json.dumps({'ok': False, 'error': '缺少run_dir'}).encode('utf-8'), 'application/json')
                return
            run_dir = os.path.join(_w.OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
            state_path = os.path.join(run_dir, 'tts_state.json')
            if not os.path.exists(state_path):
                self._send(200, json.dumps({'ok': False, 'error': 'tts_state.json不存在'}).encode('utf-8'), 'application/json')
                return
            import json as _j
            state = _j.load(open(state_path, encoding='utf-8'))
            tts_list = []
            # 计算每段解说词对应的视频时间范围（聚合narr_map+segs）
            segs_state = [tuple(s) for s in state.get('segs', [])]
            narr_map_state = state.get('narr_map') or []
            video_spans = {}
            n_narr = len(state.get('narr', []))
            if narr_map_state and len(narr_map_state) == len(segs_state):
                for bi in range(n_narr):
                    bsegs = [segs_state[k] for k in range(len(segs_state)) if narr_map_state[k] == bi]
                    if bsegs:
                        video_spans[bi] = {'start': round(bsegs[0][0], 2), 'end': round(bsegs[-1][1], 2)}
            video_dur = round(_w.probe_audio_len(state['video_path']) or 0, 1)
            # 回退：narr_map不对（全0或不匹配）时，按时长均匀分配默认位置
            _missing = [i for i in range(n_narr) if i not in video_spans]
            if _missing and video_dur > 0 and n_narr > 0:
                _step = video_dur / n_narr
                for i in _missing:
                    _s = round(i * _step, 2)
                    _e = round(min((i + 1) * _step, video_dur), 2)
                    video_spans[i] = {'start': _s, 'end': _e}
                print('[DIAG] narr_map不完整，%d段默认时间按均匀分配(每段%.0f秒)' % (len(_missing), _step))
            for i, p in state.get('tts_results', []):
                span = video_spans.get(i, {'start': 0, 'end': min(5.0, video_dur) if video_dur else 5.0})
                tts_list.append({
                    'index': i,
                    'text': state['narr'][i] if i < len(state.get('narr', [])) else '',
                    'audio': os.path.relpath(p, _w.OUTDIR).replace('\\', '/'),
                    'duration': round(_w.probe_audio_len(p) or 0, 1),
                    'video_start': span['start'],
                    'video_end': span['end']
                })
            self._send(200, json.dumps({'ok': True, 'run_dir': run_dir_name, 'tts_list': tts_list, 'video_duration': video_dur, 'video_path': os.path.basename(state['video_path'])}).encode('utf-8'), 'application/json')
        except Exception as e:
            kind, msg = _classify_exception(e)
            self._send_err(500, kind, msg, stage='读取 tts_state.json', detail=str(e))

    def _get_video_frame(self):
        try:
            qs = _w.parse_qs(_w.urlparse(self.path).query)
            run_dir_name = (qs.get('run_dir') or [''])[0]
            t = float((qs.get('time') or ['0'])[0])
            if not run_dir_name:
                self._send(400, b'missing run_dir', 'text/plain')
                return
            run_dir = os.path.join(_w.OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
            state_path = os.path.join(run_dir, 'tts_state.json')
            if not os.path.exists(state_path):
                self._send(404, b'state not found', 'text/plain')
                return
            import json as _j
            state = _j.load(open(state_path, encoding='utf-8'))
            video_path = state['video_path']
            if not os.path.exists(video_path):
                self._send(404, b'video not found', 'text/plain')
                return
            # 用ffmpeg提取帧
            import imageio_ffmpeg as _iff
            ff = _iff.get_ffmpeg_exe()
            frame_path = os.path.join(run_dir, 'preview_%d.jpg' % int(t * 1000))
            import subprocess as _sp
            cmd = [ff, '-y', '-ss', str(max(0, t)), '-i', video_path, '-frames:v', '1', '-q:v', '3', frame_path]
            _sp.run(cmd, capture_output=True, timeout=30)
            if os.path.exists(frame_path):
                with open(frame_path, 'rb') as f:
                    data = f.read()
                self._send(200, data, 'image/jpeg')
            else:
                self._send(500, b'frame extract failed', 'text/plain')
        except Exception as e:
            self._send(500, str(e).encode('utf-8'), 'text/plain')

    def _get_progress(self):
        runid = _w.parse_qs(_w.urlparse(self.path).query).get('run', [None])[0]
        if not runid or runid not in _w.PROGRESS:
            self._send(404, json.dumps({'error': '未知 run'}).encode('utf-8'), 'application/json')
            return
        self._send(200, json.dumps(_w.PROGRESS[runid]).encode('utf-8'), 'application/json')

    def _get_model_remove(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except Exception:
            body = {}
        model = str(body.get('model', '')).strip()
        if not model:
            self._send_err(400, 'other', '缺少model参数', stage='参数校验',
                           hint='要删哪个本地模型？例如 qwen2.5:7b / llama3.1:8b。先到「🤖 AI 配置」确认名字。')
            return
        try:
            import subprocess as _sp
            r = _sp.run(['ollama', 'rm', model], capture_output=True, text=True, timeout=60)
            ok = (r.returncode == 0)
            msg = (r.stdout or r.stderr or '').strip()[:200]
            self._send(200, json.dumps({'ok': ok, 'msg': msg}).encode('utf-8'), 'application/json')
        except Exception as e:
            kind, m = _classify_exception(e)
            self._send_err(500, kind, m, stage='ollama rm 模型',
                           detail=str(e), hint='请确认：1) ollama 服务还在跑；2) 模型名拼写正确。')

    def _get_active_task(self):
        """检查是否有崩溃后未完成的活动任务。"""
        task = _w._get_active_task()
        if task:
            self._send(200, json.dumps({'ok': True, 'task': task}).encode('utf-8'), 'application/json')
        else:
            self._send(200, json.dumps({'ok': True, 'task': None}).encode('utf-8'), 'application/json')

    def _get_tasks(self):
        tasks = []
        # [P0-2] PROGRESS 在其他线程（runner 结束 + _evict_finished_progress）会被并发改。
        # list() 强制快照后迭代，避免「dictionary changed size during iteration」。
        # CPython 3.13/3.14 下 GIL 提供部分保护，但仍按 Python 规范处理。
        for rid, p in list(_w.PROGRESS.items()):
            if not isinstance(p, dict):
                continue
            tasks.append({
                'runid': rid,
                'phase': p.get('phase', ''),
                'pct': p.get('pct', 0),
                'done': p.get('done', False),
                'error': p.get('error', ''),
                'file': p.get('file', ''),
                'mode': p.get('mode', ''),
                'start_time': p.get('start_time', ''),
                'queued_at': p.get('queued_at', ''),
                'queued': bool(p.get('queued')),
                'tts_failures': p.get('tts_failures', []),   # [2.2] 失败段对前端可见
            })
        # 运行中的排前面，然后按时间倒序
        tasks.sort(key=lambda t: (t['done'], t['runid']), reverse=False)
        self._send(200, json.dumps({'ok': True, 'tasks': tasks, 'running': sum(1 for t in tasks if not t['done'])})
                   .encode('utf-8'), 'application/json')

    def _post_regen_segment(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except Exception:
            body = {}
        run_id = str(body.get('run_id', ''))
        seg_idx = int(body.get('seg_idx', -1))
        new_text = str(body.get('text', '')).strip()
        if not run_id or seg_idx < 0 or not new_text:
            self._send_err(400, 'other', '缺少run_id/seg_idx/text', stage='参数校验',
                           hint='请确保三个参数都填了（run_id、seg_idx 从 0 起、text 非空）。')
            return
        run_dir = os.path.join(_w.OUTDIR, run_id)
        state_path = os.path.join(run_dir, 'state.json')
        if not os.path.exists(state_path):
            self._send_err(404, 'path', '该任务没有保存中间状态（可能是旧版本生成的）',
                           stage='定位 state.json',
                           hint='试着用 ▶ 重新触发一次解说（自动保存 state.json），或到「OUTDIR/run_id/」手动检查文件。')
            return
        try:
            st = json.load(open(state_path, encoding='utf-8'))
        except Exception as e:
            kind, msg = _classify_exception(e)
            self._send_err(500, kind, f'状态读取失败', stage='解析 state.json',
                           detail=str(e), hint='state.json 可能写坏了。可以备份后删除让下次重跑重新生成。')
            return
        narr = st.get('narr', [])
        if seg_idx >= len(narr):
            self._send_err(400, 'other', f'段索引越界: {seg_idx}/{len(narr)}', stage='参数校验',
                           hint=f'该任务只有 {len(narr)} 段解说，可填 0~{max(0, len(narr)-1)}。')
            return
        # 更新解说词
        narr[seg_idx] = new_text
        st['narr'] = narr
        # 重生成该段TTS
        tts_paths = st.get('tts_paths', [])
        seg_span = st['segs'][seg_idx] if seg_idx < len(st['segs']) else [0.0, 10.0]
        _tcfg = _w.load_ai_config().get('tts') or {}
        use_mimo = bool(_tcfg.get('api_key')) and bool(_tcfg.get('model'))
        clip = None
        if use_mimo:
            np_ = os.path.join(run_dir, f'narr{seg_idx}_regen.mp3')
            if _w.ai_tts(new_text, np_):
                clip = np_
        if clip is None:
            ok, _eng, lp = _w.local_tts_speak(new_text, os.path.join(run_dir, f'narr{seg_idx}_regen.mp3'))
            if ok:
                clip = lp
        if clip is None:
            self._send_err(500, 'model', 'TTS生成失败', stage='配音',
                           hint='请检查：(1) 是否已选配音声音？(2) edge-tts 是否熔断？(3) 本地模型（sherpa-onnx/CosyVoice/ChatTTS）是否已部署？(4) API Key 是否有效？',
                           detail='所有 TTS 路径都返回了失败标记。')
            return
        # 更新tts_paths和voice_spans
        tts_paths = [list(t) for t in tts_paths]
        found = False
        for i, t in enumerate(tts_paths):
            # 按起始时间匹配同一段
            if abs(t[1] - seg_span[0]) < 0.5:
                tts_paths[i] = [clip, float(seg_span[0]), float(seg_span[1])]
                found = True
                break
        if not found:
            tts_paths.append([clip, float(seg_span[0]), float(seg_span[1])])
        st['tts_paths'] = tts_paths
        v_len = _w.probe_audio_len(clip) or max(0.5, seg_span[1] - seg_span[0])
        voice_spans = st.get('voice_spans', {})
        voice_spans[str(seg_idx)] = [float(seg_span[0]), min(float(seg_span[1]), float(seg_span[0]) + v_len + 0.35)]
        st['voice_spans'] = voice_spans
        # 重新合成
        segs = [tuple(s) for s in st['segs']]
        narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)') else t for t in narr]
        tps = [(t[0], t[1], t[2]) for t in tts_paths]
        vs = {int(k): tuple(v) for k, v in voice_spans.items()}
        music_path = st.get('music_path')
        params = st.get('params', {})
        final = _w._compose_narration_video(st['src_video'], segs, narr_srt, tps, run_dir, params,
                                         music_path=music_path, voice_spans=vs)
        st['final'] = os.path.abspath(final) if final else None
        # [P0-1] 状态原子写：与 webui_server._save_tts_state 同步
        _w._atomic_json_dump(state_path, st)
        # 更新历史记录
        if final:
            try:
                import time as _time
                _w.add_history({
                    'time': _time.strftime('%Y-%m-%d %H:%M:%S'),
                    'file': os.path.relpath(final, _w.OUTDIR).replace('\\', '/'),
                    'duration': round(_w.probe_audio_len(final) or 0, 1),
                    'music': None, 'voice': True, 'captions': narr,
                    'mode': 'regen', 'w': 0, 'h': 0, 'fps': 0,
                })
            except Exception:
                pass
        self._send(200, json.dumps({'ok': True, 'file': os.path.relpath(final, _w.OUTDIR).replace('\\', '/') if final else '',
                                    'narr': narr}).encode('utf-8'), 'application/json')

    def _post_recommend_segments(self):
        """AI候选片段推荐：根据解说词关键词在ASR台词中匹配，返回3个最佳画面片段。"""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except Exception:
            body = {}
        run_id = str(body.get('run_id', ''))
        text = str(body.get('text', '')).strip()
        if not run_id or not text:
            self._send_err(400, 'other', '缺少run_id或text', stage='参数校验',
                           hint='run_id 在「⑨记录 → 已完成任务」有；text 是当前编辑框的解说词。')
            return
        run_dir = os.path.join(_w.OUTDIR, run_id)
        state_path = os.path.join(run_dir, 'tts_state.json')
        if not os.path.exists(state_path):
            self._send_err(404, 'path', '未找到任务状态', stage='定位 tts_state.json',
                           hint='该 run_id 可能没跑过，或产物已被清理。在「⑨记录」选一个已完成的 run 重试。')
            return
        try:
            st = json.load(open(state_path, encoding='utf-8'))
            video_path = st.get('video_path', '')
        except Exception as e:
            kind, msg = _classify_exception(e)
            self._send_err(500, kind, '状态读取失败', stage='解析 tts_state.json',
                           detail=str(e), hint='tts_state.json 可能写坏了，建议删掉该 run 重新跑。')
            return
        if not video_path or not os.path.exists(video_path):
            self._send_err(404, 'path', '视频文件不存在', stage='检查源视频',
                           hint='视频路径记录在 tts_state.json，可能已被清理或改名。建议：1) 重新上传该视频；2) 从「素材库」把它加入；3) 重新发起剧情驱动剪辑。')
            return
        # 优先从tts_state.json读ASR（最可靠），fallback到全局缓存
        asr = st.get('asr') or []
        if not asr:
            whisper_model = _w.whisper_model_name()
            asr_cache_key = _w._video_cache_key(video_path, 'asr_%s' % whisper_model)
            asr = _w._cache_load(asr_cache_key) or []
        if not asr:
            self._send(200, json.dumps({'ok': True, 'candidates': [], 'note': '无ASR数据（视频未做台词识别，或任务太旧）'}).encode('utf-8'), 'application/json')
            return
        import re as _re
        keywords = set(_re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', text))
        if not keywords:
            self._send(200, json.dumps({'ok': True, 'candidates': [], 'note': '无有效关键词'}).encode('utf-8'), 'application/json')
            return
        scored = []
        for seg in asr:
            try:
                t0 = float(seg.get('start', 0))
                t1 = float(seg.get('end', t0 + 1))
                seg_text = str(seg.get('text', ''))
                seg_words = set(_re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', seg_text))
                overlap = len(keywords & seg_words)
                if overlap > 0:
                    scored.append({'start': round(t0, 1), 'end': round(t1, 1),
                                   'score': overlap, 'dialogue': seg_text[:60],
                                   'matched': list(keywords & seg_words)[:5]})
            except (ValueError, TypeError):
                continue
        # === 增强：加入VLM画面理解匹配（scene_descs）===
        scene_file = os.path.join(run_dir, 'scene_descs.json')
        scene_candidates = []
        if os.path.exists(scene_file):
            try:
                scenes = json.load(open(scene_file, encoding='utf-8'))
                vdur = scenes[-1].get('t', scenes[-1].get('time', 600)) or 600 if scenes else 600
                seg_idx = int(body.get('seg_idx', 0))
                for sc in scenes:
                    desc = (sc.get('event') or '') + ' ' + (sc.get('location') or '') + ' ' + (sc.get('people') or '') + ' ' + (sc.get('caption') or '')
                    sc_words = set(_re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', desc))
                    overlap = len(keywords & sc_words)
                    if overlap > 0:
                        t = sc.get('t', sc.get('time', 0))
                        # 位置加权：解说词第seg_idx段倾向匹配视频中对应比例位置
                        expected_pos = (seg_idx / max(1, 12)) * vdur
                        pos_weight = max(0, 1 - abs(t - expected_pos) / (vdur * 0.5))
                        scene_candidates.append({
                            'start': round(max(0, t - 1), 1),
                            'end': round(t + 5, 1),
                            'score': overlap + pos_weight,
                            'dialogue': (sc.get('event') or sc.get('caption') or '')[:60],
                            'matched': list(keywords & sc_words)[:5],
                            'source': '画面'
                        })
            except Exception:
                pass
        # 合并ASR和画面匹配结果，去重后取前3
        all_candidates = scored + scene_candidates
        all_candidates.sort(key=lambda x: -x['score'])
        candidates = []
        used_ranges = []
        for c in all_candidates:
            overlap_existing = False
            for ur in used_ranges:
                ov = min(c['end'], ur[1]) - max(c['start'], ur[0])
                if ov > 0 and ov / max(0.1, c['end'] - c['start']) > 0.5:
                    overlap_existing = True
                    break
            if not overlap_existing:
                candidates.append(c)
                used_ranges.append((c['start'], c['end']))
            if len(candidates) >= 3:
                break
        self._send(200, json.dumps({'ok': True, 'candidates': candidates,
                                     'total_asr': len(asr),
                                     'total_scenes': len(scene_candidates),
                                     'keywords': list(keywords)[:10]}).encode('utf-8'), 'application/json')

    def _get_history(self):
        items = _w.load_history(50)
        # 逐条体检：成片文件已丢失的条目标记 missing（前端降级展示，不给下载/封面入口）；
        # 完好的条目附带 cover.jpg 封面（⑨记录里直接可预览/重生成）
        for h in items:
            rel = (h.get('file') or '').replace(chr(92), '/')
            if not rel:
                h['missing'] = True
                continue
            fp = os.path.join(_w.OUTDIR, os.path.dirname(rel.replace('/', os.sep)), os.path.basename(rel))
            if not os.path.isfile(fp):
                h['missing'] = True
                continue
            cover = os.path.join(os.path.dirname(fp), 'cover.jpg')
            if os.path.isfile(cover):
                h['cover'] = os.path.dirname(rel) + '/cover.jpg'
        self._send(200, json.dumps({'ok': True, 'history': items}).encode('utf-8'),
                   'application/json')

    def _get_ai_config(self):
        cfg = _w.load_ai_config()
        def mask(ch):
            ch = dict(ch or {})
            if ch.get('api_key'):
                ch['api_key'] = ('*' * 6) + ch['api_key'][-4:]
            return ch
        self._send(200, json.dumps({
            'ok': True,
            'config': {'vision': mask(cfg.get('vision')), 'tts': mask(cfg.get('tts')),
                       'local': mask(cfg.get('local')),
                       'whisper': dict(cfg.get('whisper') or {}),
                       'vlm': mask(cfg.get('vlm')),
                       'tts_local': dict(cfg.get('tts_local') or {}),
                       'mirror': dict(cfg.get('mirror') or {}),
                       'video': dict(cfg.get('video') or {})},
            'vision_available': _w._vision_available(),
            'tts_available': _w._tts_available(),
            'local_enabled': _w.local_llm_enabled(),
            'whisper_ready': _w.whisper_model_ready(),
            'vlm_enabled': _w.vlm_enabled(),
            'video_encoder': _w.video_encoder_label(),
        }).encode('utf-8'), 'application/json')

    def _get_ai_status(self):
        self._send(200, json.dumps(_w.ai_status()).encode('utf-8'), 'application/json')

    def _get_hardware(self):
        self._send(200, json.dumps(_w.detect_hardware()).encode('utf-8'), 'application/json')

    def _get_tts_voices(self):
        self._send(200, json.dumps({
            'ok': True,
            'voices': _w.EDGE_TTS_VOICES,
            'edge_installed': _w.edge_tts_available(),
            'edge_dead': _w.edge_tts_dead_reason(),
            'sherpa_installed': _w.sherpa_tts_available(),
            'sherpa_model_ready': _w.sherpa_tts_ready(),
            'sherpa_model': _w.sherpa_model_key(),
            'sherpa_models': [{'key': k, 'label': m['label'], 'ready': _w._sherpa_ready(k)}
                              for k, m in _w.SHERPA_TTS_MODELS.items()],
            'cosyvoice_installed': _w.cosyvoice_available(),
            'cosyvoice_voice': _w._COSYVOICE['voice'],
            'cfg': _w.tts_local_cfg(),
            'label': _w.local_tts_label(),
            'setup': dict(_w.TTS_SETUP),
        }).encode('utf-8'), 'application/json')

    def _get_local_test(self):
        ok, msg = _w.local_llm_ping()
        self._send(200, json.dumps({'ok': True, 'test_ok': ok, 'message': msg}).encode('utf-8'),
                   'application/json')

    def _get_local_status(self):
        ok, msg = (_w.local_llm_ping() if _w.local_llm_enabled() else (False, '本地模型未启用'))
        self._send(200, json.dumps({'ok': True, 'enabled': _w.local_llm_enabled(), 'ready': bool(ok),
                                    'message': msg, 'model': _w.local_llm_cfg()['model'],
                                    'installed': _w._installed_local_models(),
                                    'pulling': _w.LOCAL_PULL['running'], 'pull_model': _w.LOCAL_PULL['model'],
                                    'pull_ok': _w.LOCAL_PULL['ok'], 'pull_msg': _w.LOCAL_PULL['msg'],
                                    'pull_pct': _w.LOCAL_PULL.get('pct', 0)}).encode('utf-8'),
                       'application/json')

    def _get_ai_test(self):
        v_ok, v_msg = _w._test_vision()
        t_ok, t_msg = _w._test_tts()
        self._send(200, json.dumps({'ok': True,
                                    'vision': {'test_ok': v_ok, 'message': v_msg},
                                    'tts': {'test_ok': t_ok, 'message': t_msg},
                                    }).encode('utf-8'), 'application/json')

    def _get_whisper_status(self):
        md = _w.whisper_models_dir()
        avail = sorted(d for d in os.listdir(md)) if os.path.isdir(md) else []
        self._send(200, json.dumps({
            'ok': True,
            'selected': _w.whisper_model_name(),
            'models_dir': md,
            'ready': _w.whisper_model_ready(),
            'downloading': _w.WHISPER_DL['running'],
            'download_model': _w.WHISPER_DL['model'],
            'download_ok': _w.WHISPER_DL['ok'],
            'download_msg': _w.WHISPER_DL['msg'],
            'available': avail,
            'valid_models': _w._WHISPER_MODELS,
            # [B] 推理设备信息：前端据此展示「当前 GPU/CPU，CPU 时提示启用 GPU」
            'device': _w.whisper_device()[0],
            'compute_type': _w.whisper_device()[1],
        }).encode('utf-8'), 'application/json')

    def _get_vlm_status(self):
        ok, msg = (_w.vlm_ping() if _w.vlm_enabled() else (False, 'VLM 未启用'))
        _w.VLM_PULL['msg'] = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', _w.VLM_PULL.get('msg',''))
        self._send(200, json.dumps({'ok': True, 'enabled': _w.vlm_enabled(), 'ready': bool(ok),
                                    'message': msg, 'model': _w.vlm_cfg()['model'],
                                    'installed': _w._installed_local_models(),
                                    'pulling': _w.VLM_PULL['running'], 'pull_model': _w.VLM_PULL['model'],
                                    'pull_ok': _w.VLM_PULL['ok'], 'pull_msg': _w.VLM_PULL['msg'],
                                    'pull_pct': _w.VLM_PULL.get('pct', 0)}).encode('utf-8'),
                       'application/json')

    def _get_storage(self):
        try:
            self._send(200, json.dumps(_w._storage_scan()).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')

    def _post_build(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length, max_len=220 * 1024 * 1024)
            if req is None:
                self._send(200, json.dumps({'ok': False, 'error': '请求过大(>220MB)或读取失败'}).encode('utf-8'), 'application/json')
                return
            runid = self._spawn(_w.dispatch_build, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_ai_config(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            cfg = _w.load_ai_config()
            # incoming shape: { vision: {base_url,api_key,model}, tts:{base_url,api_key,model,voice}, local:{enabled,base_url,model,api_key} }
            for ch in ('vision', 'tts'):
                inc = data.get(ch)
                if not isinstance(inc, dict):
                    continue
                cur = dict(cfg.get(ch) or {})
                for k, v in inc.items():
                    if v is not None:
                        cur[k] = str(v).strip()
                    elif k in cur:
                        del cur[k]
                cfg[ch] = cur
            if isinstance(data.get('local'), dict):
                inc = data['local']
                cur = dict(cfg.get('local') or {})
                for k in ('base_url', 'model', 'api_key'):
                    if inc.get(k) is not None:
                        cur[k] = str(inc[k]).strip()
                    elif k in cur:
                        del cur[k]
                if 'enabled' in inc:
                    cur['enabled'] = bool(inc['enabled'])
                cfg['local'] = cur
            if isinstance(data.get('mirror'), dict):
                inc = data['mirror']
                cur = dict(cfg.get('mirror') or {})
                if inc.get('hf_endpoint') is not None:
                    cur['hf_endpoint'] = str(inc['hf_endpoint']).strip()
                if inc.get('ollama_proxy') is not None:
                    cur['ollama_proxy'] = str(inc['ollama_proxy']).strip()
                if 'use_hf_mirror' in inc:
                    cur['use_hf_mirror'] = bool(inc['use_hf_mirror'])
                cfg['mirror'] = cur
            if isinstance(data.get('whisper'), dict):
                inc = data['whisper']
                cur = dict(cfg.get('whisper') or {})
                if inc.get('model') is not None:
                    cur['model'] = str(inc['model']).strip()
                cfg['whisper'] = cur
            if isinstance(data.get('vlm'), dict):
                inc = data['vlm']
                cur = dict(cfg.get('vlm') or {})
                for k in ('base_url', 'model', 'api_key', 'mode'):
                    if inc.get(k) is not None:
                        cur[k] = str(inc[k]).strip()
                    elif k in cur:
                        del cur[k]
                if 'enabled' in inc:
                    cur['enabled'] = bool(inc['enabled'])
                cfg['vlm'] = cur
            if isinstance(data.get('tts_local'), dict):
                # 本地免费配音：engine(auto|edge|sherpa|sapi) + voice + rate
                inc = data['tts_local']
                cur = dict(cfg.get('tts_local') or {})
                eng = str(inc.get('engine') or '').strip().lower()
                if eng in ('auto', 'edge', 'sherpa', 'sapi'):
                    cur['engine'] = eng
                if inc.get('voice'):
                    cur['voice'] = str(inc['voice']).strip()
                mk = str(inc.get('sherpa_model') or '').strip()
                if mk in _w.SHERPA_TTS_MODELS:
                    cur['sherpa_model'] = mk
                rate = str(inc.get('rate') or '').strip()
                if rate:
                    if not rate.startswith(('+', '-')):
                        rate = '+' + rate.replace('%', '')
                    if not rate.endswith('%'):
                        rate += '%'
                    cur['rate'] = rate
                cfg['tts_local'] = cur
            if isinstance(data.get('video'), dict):
                # 编码策略：auto(默认·GPU可用则用) / cpu / gpu
                inc = data['video']
                cur = dict(cfg.get('video') or {})
                enc = str(inc.get('encoder') or '').strip().lower()
                if enc in ('auto', 'cpu', 'gpu'):
                    cur['encoder'] = enc
                cfg['video'] = cur
            _w.save_ai_config(cfg)
            self._send(200, json.dumps({'ok': True,
                                        'vision_available': _w._vision_available(),
                                        'tts_available': _w._tts_available(),
                                        'video_encoder': _w.video_encoder_label()}).encode('utf-8'),
                          'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_whisper_download(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            ok, msg = _w.whisper_download_async(data.get('model'))
            self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_vlm_pull(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            ok, msg = _w.vlm_pull_async(data.get('model'))
            self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_local_pull(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            ok, msg = _w.local_pull_async(data.get('model'))
            self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_mirror_scan(self):
        try:
            self._send(200, json.dumps({'ok': True, 'result': _w.scan_ollama_mirrors()}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_tts_install(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            ok, msg = _w.tts_install_async(str(data.get('pkg') or 'edge-tts'))
            self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'),
                       'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                       'application/json')

    def _post_tts_install_chattts(self):
        try:
            ok, msg = _w.tts_install_chattts_async()
            self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'),
                       'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                       'application/json')

    def _post_tts_cosyvoice_install(self):
        try:
            ok, msg = _w.cosyvoice_install_async()
            self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_tts_cosyvoice_voices(self):
        _vdir = os.path.join(_w.HERE, 'models', 'cosyvoice', 'voices')
        _voices = []
        if os.path.isdir(_vdir):
            for _f in sorted(os.listdir(_vdir)):
                if _f.endswith('.wav') or _f.endswith('.mp3'):
                    _name = os.path.splitext(_f)[0]
                    _voices.append({'name': _name, 'file': _f,
                                    'custom': _name not in ['中文女','中文男','英文女','英文男','粤语女','日语女']})
        self._send(200, json.dumps({'ok': True, 'voices': _voices}).encode('utf-8'), 'application/json')

    def _post_tts_cosyvoice_add_voice(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            _name = (data.get('name') or '').strip()
            _audio_b64 = data.get('audio') or ''
            if not _name or not _audio_b64:
                self._send(200, json.dumps({'ok': False, 'error': '需要音色名称和音频'}).encode('utf-8'), 'application/json')
                return
            import base64, re
            # 放宽正则：支持 audio/m4a, audio/mp4, audio/x-m4a, audio/wav, audio/webm 等
            _audio_b64 = re.sub(r'^data:audio/[^;]+;base64,', '', _audio_b64)
            try:
                _audio_data = base64.b64decode(_audio_b64)
            except Exception as _e:
                self._send(200, json.dumps({'ok': False, 'error': '音频解码失败: ' + str(_e)[:100]}).encode('utf-8'), 'application/json')
                return
            if len(_audio_data) < 1000:
                self._send(200, json.dumps({'ok': False, 'error': '音频文件太小（<1KB），请上传3秒以上的清晰人声'}).encode('utf-8'), 'application/json')
                return
            _vdir = os.path.join(_w.HERE, 'models', 'cosyvoice', 'voices')
            os.makedirs(_vdir, exist_ok=True)
            # 用随机临时名避免中文/特殊字符问题
            import uuid as _uuid
            _tmp = os.path.join(_vdir, '_clone_' + _uuid.uuid4().hex[:8] + '.bin')
            _out = os.path.join(_vdir, _name + '.wav')
            with open(_tmp, 'wb') as f:
                f.write(_audio_data)
            _ff_err = ''
            try:
                import imageio_ffmpeg, subprocess as _sp
                _ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                _r = _sp.run([_ffmpeg, '-y', '-i', _tmp, '-ar', '16000', '-ac', '1', _out],
                             capture_output=True, timeout=120)
                if _r.returncode != 0:
                    _ff_err = _r.stderr.decode('utf-8', errors='ignore')[-300:]
            except Exception as _e:
                _ff_err = str(_e)[:200]
            # 清理临时文件
            if os.path.exists(_tmp):
                try: os.unlink(_tmp)
                except: pass
            if os.path.exists(_out) and os.path.getsize(_out) > 1000:
                self._send(200, json.dumps({'ok': True, 'name': _name}).encode('utf-8'), 'application/json')
            else:
                _err = '音频转换失败'
                if _ff_err:
                    _err += ': ' + _ff_err[:150]
                self._send(200, json.dumps({'ok': False, 'error': _err}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)[:200]}).encode('utf-8'), 'application/json')

    def _post_tts_model_download(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            ok, msg = _w.tts_model_download_async(data.get('model'))
            self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'),
                       'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                       'application/json')

    def _post_tts_model_uninstall(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            ok, msg = _w.tts_model_uninstall(str(data.get('model') or ''))
            self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'),
                       'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                       'application/json')

    def _post_tts_test(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            ok, msg, engine, rel = _w.tts_test_speak(str(data.get('text') or '这是一段中文配音试听。'))
            self._send(200, json.dumps({'ok': ok, 'message': msg, 'engine': engine,
                                        'file': rel}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                       'application/json')

    def _post_clear_active_task(self):
        """用户确认后清除活动任务状态标记。"""
        _w._clear_active_task()
        self._send(200, json.dumps({'ok': True}).encode('utf-8'), 'application/json')

    def _post_cancel(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            runid = data.get('runid')
            # [P0-3] 在「判断 PROGRESS 是否还在」与后续 abort 操作之间，runner 线程结束
            # → _evict_finished_progress → PROGRESS 字典的 pop 是**未加锁**的其他线程
            # 行为。check-then-act 之间存在竞态：上次 audit v2 标注。本轮最小改动：把
            # 「检查」这一段用 dict.get() 兜底，已经被淘汰的 runid 等价于「已收尾」，
            # 直接返回 ok（用户的「取消」意图已达成，因任务自然结束了），不再报「未知 run」。
            prog = _w.PROGRESS.get(runid) if runid else None
            if not runid or not prog:
                # 区分两种情况：从未存在 vs 已自然结束。已结束的 runid 按用户意图返回 ok。
                # 已知 PROGRESS 的 runid 全集目前只在 _spawn / _analyze_plan 等少数写入点
                # 加入，外部没法直接枚举；为避免「未存在」被误判为「已结束」，引入 `_RUN_CTR`
                # 已经过号的 runid 才视为「曾经存在」——但当前没有反向映射，留保守语义。
                self._send(200, json.dumps({'ok': False, 'error': '未知 run'}).encode('utf-8'), 'application/json')
                return
            # 先判定是否为「队里还没开跑」的任务：这类任务根本没有子进程，
            # 直接从队列里弹出 + 标 done 即可终止。绝不能再去 terminate 一个
            # 还没起的 PROC，否则会拿到 KeyError 把这里整个吞掉。
            removed = False
            with _w._TASK_QUEUE_LOCK:
                for i, (_f, _r, rid, _rd, p) in enumerate(_w._TASK_QUEUE):
                    if rid == runid:
                        _w._TASK_QUEUE.pop(i)
                        p['done'] = True
                        p['aborted'] = True
                        p['error'] = '已取消（队列中）'
                        p['phase'] = '已取消'
                        # 重排剩余项的「前面还有N个」显示
                        for j, (_, _, _, _, _p) in enumerate(_w._TASK_QUEUE):
                            _p['phase'] = '排队中（前面还有%d个任务）' % (j + 1)
                        removed = True
                        break
                # 任何队列变更都立刻落盘（取消后下一个候选出列前若崩溃，磁盘上
                # 仍反映「少了这条」）。锁内一气呵成更稳。
                _w._persist_queue_unlocked()
            if removed:
                self._send(200, json.dumps({'ok': True, 'cancelled': 'queued'}).encode('utf-8'), 'application/json')
                return
            # 已经在跑：终止子进程 + 设 abort 标志位
            # [P0-3] 同样用 .get() 兜底：此处 prog 可能已被 _evict 移除，按「已结束」返回 ok
            prog = _w.PROGRESS.get(runid)
            if prog is None:
                self._send(200, json.dumps({'ok': True, 'cancelled': 'already-done'}).encode('utf-8'), 'application/json')
                return
            prog['abort'] = True
            with _w._PROC_LOCK:
                proc = _w.RUN_PROCS.get(runid)
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self._send(200, json.dumps({'ok': True, 'cancelled': 'running'}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_history_delete(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')
            ok = _w.delete_history(data.get('file'))
            self._send(200, json.dumps({'ok': ok}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_history_clear(self):
        try:
            _w.clear_history()
            self._send(200, json.dumps({'ok': True}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_cover(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self._read_json(length, max_len=64 * 1024) or {}
            fp = _w._safe_join(_w.OUTDIR, data.get('file') or '')
            if not fp:
                raise RuntimeError('视频不存在或不在产物目录内')
            run_dir = os.path.dirname(fp)
            rel = lambda p: os.path.relpath(p, _w.OUTDIR).replace('\\', '/')
            title = str(data.get('title') or '')[:80]
            sub = str(data.get('sub') or '')[:40]
            style = max(0, min(2, int(data.get('style') or 0)))
            cand_dir = os.path.join(run_dir, 'cover_cand')
            list_json = os.path.join(cand_dir, 'list.json')
            cands = []
            if os.path.isfile(list_json):
                try:
                    with open(list_json, 'r', encoding='utf-8') as f:
                        cands = json.load(f)
                except Exception:
                    cands = []
            ts = data.get('ts')
            if ts is None or not cands:
                cands = _w._cover_candidates(fp, run_dir)
                if not cands:
                    raise RuntimeError('候选帧抽取失败')
                ts = max(cands, key=lambda c: c['score'])['ts']
                try:
                    os.makedirs(cand_dir, exist_ok=True)
                    with open(list_json, 'w', encoding='utf-8') as f:
                        json.dump(cands, f, ensure_ascii=False)
                except Exception:
                    pass
            else:
                ts = round(float(ts), 2)
            cover = os.path.join(run_dir, 'cover.jpg')
            _w._cover_render(fp, ts, title, sub, style, cover)
            for c in cands:
                c['thumb'] = rel(os.path.join(cand_dir, os.path.basename(c['thumb'])))
            self._send(200, json.dumps({'ok': True, 'cover': rel(cover), 'ts': ts, 'title': title,
                                        'candidates': cands}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')

    def _post_material_upload(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self._read_json(length, max_len=220 * 1024 * 1024) or {}
            name, err = _w.material_save_bytes(data.get('name') or '',
                                            base64.b64decode(data.get('data', '') or ''))
            self._send(200, json.dumps({'ok': bool(name), 'name': name, 'error': err}).encode('utf-8'),
                       'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_material_from_upload(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self._read_json(length, max_len=64 * 1024) or {}
            src = _w._upload_final_path(data.get('upload_id'), data.get('name'))
            if not src:
                raise RuntimeError('上传会话不存在或未完成')
            name = _w.material_save_file(src)
            try:
                d = _w._upload_dir_of(data.get('upload_id'))
                if d and os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
            self._send(200, json.dumps({'ok': True, 'name': name}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_material_save_from_media(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self._read_json(length, max_len=64 * 1024) or {}
            src = _w._safe_join(_w.OUTDIR, data.get('file') or '')
            if not src:
                raise RuntimeError('源文件不存在')
            name = _w.material_save_file(src)
            self._send(200, json.dumps({'ok': True, 'name': name}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_material_delete(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self._read_json(length, max_len=64 * 1024) or {}
            ok, err = _w.material_delete(data.get('name') or '')
            self._send(200, json.dumps({'ok': ok, 'error': err}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_bili_download(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self._read_json(length, max_len=64 * 1024) or {}
            self._send(200, json.dumps(_w._bili_start_download((data.get('bvid') or '').strip())).encode('utf-8'),
                       'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_bili_cancel(self):
        try:
            _w.BILI_PULL['abort'] = True
            self._send(200, json.dumps({'ok': True}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_upload_init(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self._read_json(length, max_len=64 * 1024) or {}
            have = None
            uid = data.get('upload_id')
            if uid:
                d = _w._upload_dir_of(uid)
                if d is not None and os.path.isdir(d) and any(
                        fn.startswith('final__') for fn in os.listdir(d)):
                    uid = None   # 该会话已完成（成品待任务取走）→ 按新会话处理，避免重传覆盖
                else:
                    have = _w._upload_have_parts(uid)
                    if have is None:
                        uid = None   # 会话过期/非法 → 按新会话处理
            if uid is None:
                uid = 'up-%d-%s' % (int(time.time() * 1000), ''.join(random.choice('0123456789abcdef') for _ in range(6)))
                d = _w._upload_dir_of(uid)
                if d is None:
                    raise RuntimeError('会话 id 生成失败')
                os.makedirs(d, exist_ok=True)
                have = []
            # 清理放在会话创建之后：新会话也计入数量上限（否则长期停在 上限+1）
            _w._upload_prune()
            self._send(200, json.dumps({'ok': True, 'upload_id': uid, 'have': have}).encode('utf-8'),
                       'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_upload_chunk(self):
        try:
            ctype = self.headers.get('Content-Type', '')
            if 'multipart/form-data' in ctype:
                boundary = ctype.split('boundary=')[-1].strip().encode()
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b''
                fields = _w._parse_multipart(raw, boundary)
                ok, err = _w._upload_chunk_write(fields.get('upload_id'), fields.get('idx'),
                                              fields.get('chunk') or b'')
            else:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=16 * 1024 * 1024)
                if data is None:
                    raise RuntimeError('分片过大或读取失败')
                ok, err = _w._upload_chunk_write(data.get('upload_id'), data.get('idx'),
                                              base64.b64decode(data.get('data', '') or ''))
            self._send(200, json.dumps({'ok': ok, 'error': err}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_upload_done(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self._read_json(length, max_len=64 * 1024) or {}
            final, err = _w._upload_finalize(data.get('upload_id'), data.get('name'), data.get('chunks'))
            self._send(200, json.dumps({'ok': bool(final), 'error': err,
                                        'size': os.path.getsize(final) if final else 0}).encode('utf-8'),
                       'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_beatcut(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length, max_len=300 * 1024 * 1024)
            if req is None:
                self._send(200, json.dumps({'ok': False, 'error': '请求过大(>300MB)或读取失败'}).encode('utf-8'), 'application/json')
                return
            runid = self._spawn(_w.dispatch_beatcut, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_narrate(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length, max_len=300 * 1024 * 1024)
            if req is None:
                self._send(200, json.dumps({'ok': False, 'error': '请求过大(>300MB)或读取失败'}).encode('utf-8'), 'application/json')
                return
            runid = self._spawn(_w.dispatch_narrate, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_narrate_movie(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length, max_len=300 * 1024 * 1024)
            if req is None:
                self._send(200, json.dumps({'ok': False, 'error': '请求过大(>300MB)或读取失败'}).encode('utf-8'), 'application/json')
                return
            runid = self._spawn(_w.dispatch_movie, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_movie_tts(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length, max_len=300 * 1024 * 1024)
            if req is None:
                self._send(200, json.dumps({'ok': False, 'error': '请求过大或读取失败'}).encode('utf-8'), 'application/json')
                return
            runid = self._spawn(_w.dispatch_movie_tts, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_movie_compose(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length) if length else {}
            if req is None:
                req = {}
            runid = self._spawn(_w.dispatch_movie_compose, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_reveal(self):
        """[P1 输出可发现性] 在系统文件管理器中显示成片位置。

        安全：路径必须过 _safe_join(OUTDIR, ...)，拒绝穿越；仅 Windows 用
        explorer /select, 其他平台返回 ok=False + 提示（前端 toast 展示路径）。"""
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length) if length else b'{}')
        except Exception:
            body = {}
        rel = str(body.get('file', '')).strip()
        if not rel:
            self._send_err(400, 'param', '缺少 file 参数', stage='参数校验')
            return
        full = _w._safe_join(_w.OUTDIR, rel)
        if not full or not os.path.exists(full):
            self._send_err(404, 'path', '文件不存在或不在成片目录内', stage='定位文件',
                           hint='该文件可能已被清理或移动。刷新「最近生成」列表确认。')
            return
        if os.name == 'nt':
            try:
                import subprocess as _sp
                # explorer /select,<path>：选中文件而非打开。shell=False，数组传参防注入。
                _sp.Popen(['explorer', '/select,', os.path.normpath(full)])
                self._send(200, json.dumps({'ok': True}).encode('utf-8'), 'application/json')
            except Exception as e:
                kind, m = _classify_exception(e)
                self._send_err(500, kind, m, stage='打开文件管理器', detail=str(e))
        else:
            # 非 Windows：告诉前端路径，让用户自己复制
            self._send(200, json.dumps({'ok': False, 'error': '当前系统不支持自动打开文件管理器',
                                        'path': full}).encode('utf-8'), 'application/json')

    def _post_tts_single(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length) if length else {}
            if req is None:
                req = {}
            runid = self._spawn(_w.dispatch_tts_single, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_tts_regen_all(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length) if length else {}
            if req is None:
                req = {}
            runid = self._spawn(_w.dispatch_tts_regen_all, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_plan(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length, max_len=300 * 1024 * 1024)
            if req is None:
                self._send(200, json.dumps({'ok': False, 'error': '请求过大(>300MB)或读取失败'}).encode('utf-8'), 'application/json')
                return
            runid = self._spawn(_w._analyze_plan_job, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_confirm(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            req = self._read_json(length) if length else {}
            if req is None:
                req = {}
            runid = req.get('runid')
            if not runid or runid not in _w.PROGRESS or runid not in _w.PLANS:
                self._send(200, json.dumps({'ok': False, 'error': '方案不存在或已过期，请重新分析'}).encode('utf-8'), 'application/json')
                return
            nrunid = self._spawn(_w._render_plan_job, req)
            self._send(200, json.dumps({'ok': True, 'runid': nrunid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_narrate_align(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8') or '{}') if length else {}
            runid = data.get('runid')
            plan = _w.PLANS.get(runid) if runid else None
            if not plan or plan.get('type') != 'narrate':
                self._send(200, json.dumps({'ok': False, 'error': '方案不存在/已过期或不是解说方案，请重新分析'}).encode('utf-8'), 'application/json')
                return
            lines = [str(x).strip() for x in (data.get('lines') or []) if str(x).strip()]
            if not lines:
                self._send(200, json.dumps({'ok': False, 'error': '解说词为空'}).encode('utf-8'), 'application/json')
                return
            shots = plan.get('shots') or plan.get('segs') or []
            use_model = (str(data.get('mode') or 'auto').lower() != 'algo')
            segs, src = _w._align_shots_to_lines(shots, lines, plan.get('asr'),
                                             plan.get('params'), use_model=use_model)
            if not segs:
                self._send(200, json.dumps({'ok': False, 'error': '分镜重匹配失败'}).encode('utf-8'), 'application/json')
                return
            # 回写方案：后续 /api/confirm 直接用新分镜渲染
            plan['segs'] = segs
            plan['narr'] = lines
            plan['align_source'] = src
            try:
                plan['thumbs'] = _w._plan_thumbs(plan['video'], segs,
                                              plan.get('run_dir') or os.path.dirname(plan['video']))
            except Exception:
                pass
            rel = lambda p: (os.path.relpath(p, _w.OUTDIR).replace('\\', '/') if p and os.path.exists(p) else '')
            self._send(200, json.dumps({
                'ok': True, 'source': src, 'shots': len(shots),
                'msg': ('已按解说词语义重新匹配分镜' if src == 'model' else '模型不可用，已按解说词长度比例分配分镜'),
                'segs': [{'i': i, 'start': round(a, 3), 'end': round(b, 3), 'caption': c,
                          'thumb': rel(plan.get('thumbs', {}).get(i))}
                         for i, ((a, b), c) in enumerate(zip(segs, lines))],
            }).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_instruct(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > 300 * 1024 * 1024:
                self._send(200, json.dumps({'ok': False, 'error': '请求过大'}).encode('utf-8'), 'application/json')
                return
            req = self._read_json(length) or {}
            runid = self._spawn(_w.dispatch_instruct, req)
            self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')

    def _post_storage(self):
        try:
            self._send(200, json.dumps(_w._storage_scan()).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')

    def _post_storage_delete(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self._read_json(length, max_len=64 * 1024) or {}
            full = _w._storage_resolve_deletable(data.get('path') or '')
            if not full:
                raise RuntimeError('该路径不在可清理范围内，或尝试越权删除（已拒绝）')
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            self._send(200, json.dumps(_w._storage_scan()).encode('utf-8'), 'application/json')
        except Exception as e:
            self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')

    def _post_model_remove(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except Exception:
            body = {}
        model = str(body.get('model', '')).strip()
        if not model:
            self._send_err(400, 'other', '缺少model参数', stage='参数校验',
                           hint='要删哪个本地模型？例如 qwen2.5:7b / llama3.1:8b。先到「🤖 AI 配置」确认名字。')
            return
        try:
            import subprocess as _sp
            r = _sp.run(['ollama', 'rm', model], capture_output=True, text=True, timeout=60)
            ok = (r.returncode == 0)
            msg = (r.stdout or r.stderr or '').strip()[:200]
            self._send(200, json.dumps({'ok': ok, 'msg': msg, 'error': '' if ok else msg}).encode('utf-8'), 'application/json')
        except Exception as e:
            kind, m = _classify_exception(e)
            self._send_err(500, kind, m, stage='ollama rm 模型',
                           detail=str(e), hint='请确认：1) ollama 服务还在跑；2) 模型名拼写正确。')

    # ==================== 路由表（精确匹配优先，前缀其次，均未命中 → 404） ====================
    GET_EXACT = {
        '/': '_get_index',
        '/api/ai/config': '_get_ai_config',
        '/api/ai/test': '_get_ai_test',
        '/api/ai_status': '_get_ai_status',
        '/api/bili/search': '_get_bili_search',
        '/api/bili/status': '_get_bili_status',
        '/api/hardware': '_get_hardware',
        '/api/history': '_get_history',
        '/api/local/status': '_get_local_status',
        '/api/local/test': '_get_local_test',
        '/api/material/list': '_get_material_list',
        '/api/model/remove': '_get_model_remove',
        '/api/music/search': '_get_music_search',
        '/api/music/use': '_get_music_use',
        '/api/progress': '_get_progress',
        '/api/storage': '_get_storage',
        '/api/tasks': '_get_tasks',
        '/api/active_task': '_get_active_task',
        '/api/tts/voices': '_get_tts_voices',
        '/api/tts_recent': '_get_tts_recent',
        '/api/tts_reset': '_get_tts_reset',
        '/api/tts_state': '_get_tts_state',
        '/api/video_frame': '_get_video_frame',
        '/api/vlm/status': '_get_vlm_status',
        '/api/whisper/status': '_get_whisper_status',
        '/index.html': '_get_index',
    }
    GET_PREFIX = [
        ('/static/', '_get_static_files'),
        ('/media/', '_get_media_files'),
        ('/music_lib/', '_get_music_lib_files'),
        ('/material_lib/', '_get_material_lib_files'),
    ]
    POST_EXACT = {
        '/api/ai/config': '_post_ai_config',
        '/api/beatcut': '_post_beatcut',
        '/api/bili/cancel': '_post_bili_cancel',
        '/api/bili/download': '_post_bili_download',
        '/api/build': '_post_build',
        '/api/cancel': '_post_cancel',
        '/api/active_task/clear': '_post_clear_active_task',
        '/api/confirm': '_post_confirm',
        '/api/cover': '_post_cover',
        '/api/history/clear': '_post_history_clear',
        '/api/history/delete': '_post_history_delete',
        '/api/instruct': '_post_instruct',
        '/api/local/pull': '_post_local_pull',
        '/api/material/delete': '_post_material_delete',
        '/api/material/from_upload': '_post_material_from_upload',
        '/api/material/save_from_media': '_post_material_save_from_media',
        '/api/material/upload': '_post_material_upload',
        '/api/mirror/scan': '_post_mirror_scan',
        '/api/model/remove': '_post_model_remove',
        '/api/movie_compose': '_post_movie_compose',
        '/api/movie_tts': '_post_movie_tts',
        '/api/narrate': '_post_narrate',
        '/api/narrate/align': '_post_narrate_align',
        '/api/narrate_movie': '_post_narrate_movie',
        '/api/plan': '_post_plan',
        '/api/storage': '_post_storage',
        '/api/storage/delete': '_post_storage_delete',
        '/api/tts/cosyvoice/add_voice': '_post_tts_cosyvoice_add_voice',
        '/api/tts/cosyvoice/install': '_post_tts_cosyvoice_install',
        '/api/tts/cosyvoice/voices': '_post_tts_cosyvoice_voices',
        '/api/tts/install': '_post_tts_install',
        '/api/tts/install_chattts': '_post_tts_install_chattts',
        '/api/tts/model/download': '_post_tts_model_download',
        '/api/tts/model/uninstall': '_post_tts_model_uninstall',
        '/api/tts/test': '_post_tts_test',
        '/api/tts_regen_all': '_post_tts_regen_all',
        '/api/regen_segment': '_post_regen_segment',
        '/api/recommend_segments': '_post_recommend_segments',
        '/api/reveal': '_post_reveal',
        '/api/tts_single': '_post_tts_single',
        '/api/upload/chunk': '_post_upload_chunk',
        '/api/upload/done': '_post_upload_done',
        '/api/upload/init': '_post_upload_init',
        '/api/vlm/pull': '_post_vlm_pull',
        '/api/whisper/download': '_post_whisper_download',
    }

    def do_GET(self):
        path = _w.urlparse(self.path).path
        h = self.GET_EXACT.get(path)
        if h is None:
            for prefix, hname in self.GET_PREFIX:
                if path.startswith(prefix):
                    h = hname
                    break
        if h:
            getattr(self, h)()
        else:
            self._send(404, b'not found')

    def do_POST(self):
        path = _w.urlparse(self.path).path
        h = self.POST_EXACT.get(path)
        if h:
            getattr(self, h)()
        else:
            self._send(404, b'not found')



def start_server(port=8765, open_browser=True):
    os.makedirs(_w.WORKDIR, exist_ok=True)
    os.makedirs(_w.OUTDIR, exist_ok=True)
    _w.ensure_default_images()
    host = os.environ.get('HOST', '127.0.0.1')
    srv = ThreadingHTTPServer((host, port), Handler)
    url = f'http://{host}:{port}/'
    print('=' * 52)
    print('  [Spring Video Studio] started')
    print('  Open in browser:', url)
    print('  Press Ctrl+C to stop')
    _fok, _fmsg = _w.font_selfcheck()      # 启动自检：无中文字体时提前告警，别等成片全是方框
    if not _fok:
        print('  ' + _fmsg, flush=True)
    print('=' * 52, flush=True)
    if open_browser and host in ('127.0.0.1', 'localhost'):
        threading.Timer(0.7, lambda: webbrowser_open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # 退出前收掉所有仍在跑的 ffmpeg：否则 Ctrl+C / 关掉命令行窗口后，
        # 子进程变孤儿继续占满 CPU 并往已失效的 run_dir 写文件。
        _kill_all_child_processes()


def _content_disposition(filename, disposition='attachment'):
    """构造 Content-Disposition 头（RFC 6266）。

    HTTP 头按标准只能带 latin-1 字符，直接把中文文件名拼进去会抛
    `UnicodeEncodeError: 'latin-1' codec can't encode characters` ——
    异常发生在 send_header 里，整个响应变成 500，前端请求一直挂着。
    这里给 ASCII 兜底名 + filename* 传 UTF-8（浏览器按 RFC 5987 取后者）。
    顺带剔除引号/换行，避免文件名把头部结构搞坏。"""
    import urllib.parse as _up
    safe_ascii = ''.join(ch if ord(ch) < 128 else '_' for ch in (filename or ''))
    for bad in ('\\', '"', '\r', '\n', '\t'):
        safe_ascii = safe_ascii.replace(bad, '_')
    safe_ascii = safe_ascii.strip() or 'download'
    utf8_part = _up.quote(filename or '', safe='')
    return "%s; filename=\"%s\"; filename*=UTF-8''%s" % (disposition, safe_ascii, utf8_part)


def _evict_finished_progress(keep=100):
    """淘汰 PROGRESS 里的旧条目，防止长驻进程内存无限增长。

    只能淘汰**已结束**的任务。旧条件只看 `k not in RUN_PROCS`，而 RUN_PROCS 仅在
    ffmpeg 真正执行的窗口内有条目——正在跑 Whisper / LLM / 配音的任务会被误淘汰：
    前端 /api/progress 查不到（一直轮询到超时才提示），取消按钮也失效
    （/api/cancel 判定「未知 run」），任务白跑完还占着并发名额。"""
    if len(_w.PROGRESS) <= keep:
        return
    with _w._PROC_LOCK:
        active = set(_w.RUN_PROCS.keys())
    for k in list(_w.PROGRESS.keys())[:-keep]:
        if k in active:
            continue
        if not _w.PROGRESS[k].get('done'):
            continue          # 仍在运行，绝不淘汰
        _w.PROGRESS.pop(k, None)


def _kill_all_child_processes():
    """终止 RUN_PROCS 中登记的全部子进程（进程退出与 /api/cancel 复用同一逻辑）。"""
    try:
        with _w._PROC_LOCK:
            procs = list(_w.RUN_PROCS.values())
            _w.RUN_PROCS.clear()
    except Exception:
        procs = []
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


atexit.register(_kill_all_child_processes)


def webbrowser_open(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 公共符号注入回宿主命名空间（保持 webui_server.X 旧入口；覆盖宿主文件末尾的
# None 占位绑定）。
# ---------------------------------------------------------------------------
for _name in (
    'Handler',
    'MIME',
    'start_server',
    '_content_disposition',
    '_evict_finished_progress',
    '_kill_all_child_processes',
    'webbrowser_open',
):
    setattr(_w, _name, globals()[_name])

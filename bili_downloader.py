# -*- coding: utf-8 -*-
"""B站素材：搜索 + 下载 MP4（由 webui_server.py 拆出）。

包含：yt-dlp 搜索、playurl 直连/yt-dlp 双引擎下载、cookie 自动收割。
约定与 video_render.py 一致：宿主符号经 _w.<name> 晚绑定，
本模块公共符号在文件末尾注入回宿主命名空间。
"""
import logging, os, sys
import urllib.request as _urlreq
import urllib.error as _urlerr
import http.cookiejar as _cjar
import json, re, threading

from ffmpeg_utils import AbortError, ffmpeg_exe
from ai_providers import load_ai_config

_log = logging.getLogger('framecut.bili')

# ---- 宿主晚绑定 ----
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

# ---------------------------------------------------------------------------
# 常量与状态
# ---------------------------------------------------------------------------
_BILI_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
_BILI_HDRS = {'User-Agent': _BILI_UA, 'Referer': 'https://www.bilibili.com/',
              'Accept': 'application/json, text/plain, */*', 'Accept-Language': 'zh-CN,zh;q=0.9'}
_BV_RE = re.compile(r'^BV[0-9A-Za-z]{10}$')

BILI_PULL = {'running': False, 'ok': None, 'pct': 0, 'msg': '', 'file': '', 'title': '', 'abort': False}


def _bili_dir():
    return os.path.join(_w.OUTDIR, 'bili')


# ---------------------------------------------------------------------------
# 配置与 cookie
# ---------------------------------------------------------------------------

def bili_cfg():
    """B 站配置：可选 cookie（用户从浏览器复制的整段 Cookie 头，登录态更稳/清晰度更高）。"""
    cfg = load_ai_config().get('bili') or {}
    return {'cookie': (cfg.get('cookie') or '').strip()}


def _bili_cookie_header():
    """返回请求用的 Cookie 头：优先 ai_config 的 bili.cookie；否则自动访问 B 站首页收割 buvid3。"""
    ck = bili_cfg().get('cookie')
    if ck:
        return ck
    jar = _cjar.CookieJar()
    opener = _urlreq.build_opener(_urlreq.HTTPCookieProcessor(jar))
    for k, v in _BILI_HDRS.items():
        opener.addheaders.append((k, v))
    opener.open('https://www.bilibili.com/', timeout=15).read()
    return '; '.join('%s=%s' % (c.name, c.value) for c in jar)


def _bili_cookiefile():
    """把 Cookie 头写成 Netscape cookie 文件（yt-dlp 用）。"""
    workdir = getattr(_w, 'WORKDIR', os.path.join(_HERE, '.cache'))
    os.makedirs(workdir, exist_ok=True)
    cf = os.path.join(workdir, 'bili_cookies.txt')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write('# Netscape HTTP Cookie File\n')
        for pair in _bili_cookie_header().split(';'):
            if '=' in pair:
                k, _, v = pair.strip().partition('=')
                if k.strip():
                    f.write('.bilibili.com\tTRUE\t/\tTRUE\t2100000000\t%s\t%s\n' % (k.strip(), v))
    return cf


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

def _bili_get_json(url):
    req = _urlreq.Request(url, headers=dict(_BILI_HDRS, **{'Cookie': _bili_cookie_header()}))
    with _urlreq.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def _bili_valid_bvid(bvid):
    return bool(_BV_RE.match(bvid or ''))


def bili_search(keyword, n=8):
    """yt-dlp bilisearch 搜索 B 站视频。
    返回 [{bvid,title,author,duration,pic}]；失败抛异常（含 412 风控提示）。"""
    import yt_dlp
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
            'playlistend': max(1, min(12, int(n))), 'socket_timeout': 20,
            'cookiefile': _bili_cookiefile(), 'http_headers': dict(_BILI_HDRS)}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info('bilisearch%d:%s' % (int(n), keyword), download=False)
    except Exception as e:
        msg = str(e)
        if '412' in msg:
            raise RuntimeError('B 站风控拦截（412）：请稍后再试；或在 ai_config.json 配 bili.cookie 后重试')
        raise
    out = []
    for e in (info or {}).get('entries') or []:
        if not e or not e.get('id'):
            continue
        out.append({'bvid': e.get('id'), 'title': (e.get('title') or '')[:90],
                    'author': (e.get('uploader') or '')[:40],
                    'duration': int(e.get('duration') or 0),
                    'pic': ((e.get('thumbnails') or [{}])[-1].get('url') or '')})
    return out


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

def _bili_download_direct(bvid):
    """引擎①：playurl html5 直连下载。返回 (相对 OUTDIR 的文件路径, 标题)。"""
    v = _bili_get_json('https://api.bilibili.com/x/web-interface/view?bvid=' + bvid)
    if v.get('code') != 0:
        raise RuntimeError('读取视频信息失败：%s' % (v.get('message') or v.get('code')))
    cid = v['data']['cid']
    title = v['data'].get('title') or bvid
    BILI_PULL.update({'title': title[:60], 'pct': 5, 'msg': '获取播放地址…'})
    p = _bili_get_json('https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=64&platform=html5&high_quality=1'
                       % (bvid, cid))
    if p.get('code') != 0:
        raise RuntimeError('获取播放地址失败：%s' % (p.get('message') or p.get('code')))
    durl = (p['data'].get('durl') or [{}])[0]
    url = durl.get('url') or ''
    if not url:
        raise RuntimeError('未取得视频直链（可能需要登录/大会员）')
    size = durl.get('size') or 0
    req = _urlreq.Request(url, headers=dict(_BILI_HDRS, **{'Cookie': _bili_cookie_header()}))
    safe_name = _w._safe_filename(bvid + '.mp4')
    final = os.path.join(_bili_dir(), safe_name)
    upload_max = getattr(_w, 'UPLOAD_TOTAL_MAX', 2 * 1024 * 1024 * 1024)
    done = 0
    with _urlreq.urlopen(req, timeout=30) as resp, open(final, 'wb') as f:
        while True:
            b = resp.read(256 * 1024)
            if not b:
                break
            f.write(b)
            done += len(b)
            if done > upload_max:
                raise RuntimeError('文件超过 2GB 上限')
            if size:
                BILI_PULL['pct'] = min(95, int(done * 100 // size))
            BILI_PULL['msg'] = '下载中 %.1fMB' % (done / 1048576)
            if BILI_PULL.get('abort'):
                raise AbortError('用户取消了下载')
    return os.path.relpath(final, _w.OUTDIR).replace('\\', '/'), title


def _bili_download_ytdlp(bvid):
    """引擎②（兜底）：yt-dlp 内置 B 站提取器。"""
    import yt_dlp

    def hook(d):
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            if total:
                BILI_PULL['pct'] = min(95, int((d.get('downloaded_bytes') or 0) * 100 // total))
            BILI_PULL['msg'] = '下载中（yt-dlp）…'
        else:
            BILI_PULL['msg'] = '合并音视频…'
        if BILI_PULL.get('abort'):
            raise yt_dlp.utils.DownloadCancelled('用户取消了下载')

    opts = {'format': 'bv*[height<=720]+ba/b[height<=720]/b', 'merge_output_format': 'mp4',
            'outtmpl': os.path.join(_bili_dir(), '%(id)s.%(ext)s'),
            'ffmpeg_location': os.path.dirname(ffmpeg_exe()),
            'noplaylist': True, 'cookiefile': _bili_cookiefile(), 'socket_timeout': 20, 'retries': 2,
            'progress_hooks': [hook], 'quiet': True, 'no_warnings': True, 'http_headers': dict(_BILI_HDRS)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info('https://www.bilibili.com/video/' + bvid, download=True)
    fp = ((info or {}).get('requested_downloads') or [{}])[0].get('filepath')
    if not fp or not os.path.isfile(fp):
        raise RuntimeError('yt-dlp 未产出文件')
    return os.path.relpath(fp, _w.OUTDIR).replace('\\', '/'), (info.get('title') or bvid)


def _bili_download_thread(bvid):
    """下载编排：引擎①直连 → 失败自动切引擎②yt-dlp → 都失败给风控提示。"""
    BILI_PULL.update({'running': True, 'ok': None, 'pct': 1, 'msg': '读取视频信息…', 'file': '',
                      'title': '', 'abort': False})
    os.makedirs(_bili_dir(), exist_ok=True)
    try:
        try:
            rel, title = _bili_download_direct(bvid)
        except AbortError:
            raise
        except Exception as e1:
            if BILI_PULL.get('abort'):
                raise AbortError('用户取消了下载')
            last_err = str(e1)
            BILI_PULL.update({'pct': 3, 'msg': '直连失败（%s），改用 yt-dlp 引擎…' % last_err[:60]})
            rel, title = _bili_download_ytdlp(bvid)
        BILI_PULL.update({'running': False, 'ok': True, 'pct': 100, 'file': rel, 'title': title[:60],
                          'msg': '完成：%s' % title[:50]})
    except AbortError:
        BILI_PULL.update({'running': False, 'ok': False, 'msg': '已取消下载'})
    except Exception as e:
        msg = str(e)[:180]
        if '412' in msg or '风控' in msg:
            msg += ' —— 触发了 B 站风控：请等几分钟再试；或在 ai_config.json 配 bili.cookie（浏览器登录 Cookie）后重试，登录态更稳且清晰度更高。'
        BILI_PULL.update({'running': False, 'ok': False, 'msg': msg})


def _bili_start_download(bvid):
    """校验并启动下载线程；已有下载进行中时拒绝。"""
    if not _bili_valid_bvid(bvid):
        return {'ok': False, 'error': 'BV 号格式不正确'}
    if BILI_PULL.get('running'):
        return {'ok': False, 'error': '已有下载在进行中，请先等待或取消'}
    threading.Thread(target=_bili_download_thread, args=(bvid,), daemon=True).start()
    return {'ok': True}


# ---------------------------------------------------------------------------
# 回注宿主命名空间：handler.py / tests 继续经 _w. / S. 访问
# ---------------------------------------------------------------------------
for _sym in ('bili_cfg', '_bili_cookie_header', '_bili_cookiefile', '_bili_get_json',
             '_bili_valid_bvid', 'bili_search', '_bili_download_direct', '_bili_download_ytdlp',
             '_bili_download_thread', '_bili_start_download', 'BILI_PULL', 'BILI_DIR'):
    if _sym == 'BILI_DIR':
        setattr(_w, _sym, _bili_dir())
    else:
        setattr(_w, _sym, globals()[_sym])

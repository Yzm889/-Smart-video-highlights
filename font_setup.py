# -*- coding: utf-8 -*-
"""中文字体探测/选择/自检（由 webui_server.py 拆出，v1.4.70）。

【为什么单独成模块】这段逻辑是「找不到含中文字形的字体就明确报错」——它决定
成片会不会变成一屏豆腐块，却混在 5600 行的宿主文件里，改起来没人敢动。

【宿主晚绑定规则，勿改】FONT_ENV / FONT_DIR / _FONT_CACHE / _FONT_LOCK /
_FONT_CANDIDATES / 扫描上限等仍定义在 webui_server 里：测试会
`monkeypatch.setattr(S, 'FONT_DIR', ...)`、`S._FONT_CACHE = {...}` 直接打补丁，
所以这里一律 `_host().X` 调用期取值——按值绑定会让补丁打空（历史踩坑同
web_storage / movie_narrator 的拆分说明）。
"""
import os
import sys
import time
import threading

from PIL import Image, ImageDraw, ImageFont

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


def _cfg(name, default):
    """宿主上有就取宿主（测试会 monkeypatch 宿主），否则用本模块的默认值。"""
    return getattr(_host(), name, default)


# 字号 → ImageFont 对象缓存（纯内部状态，不参与测试打补丁）
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
        miss = _bmp(_host()._PUA_PROBE)
        if not any(miss):                       # 该字体不绘制 .notdef（空白）
            return all(any(_bmp(ch)) for ch in _host()._CJK_SAMPLE)
        return all(_bmp(ch) != miss for ch in _host()._CJK_SAMPLE)
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
        for fn in sorted(os.listdir(_host().FONT_DIR)):
            if fn.lower().endswith(('.ttf', '.otf', '.ttc')):
                yield os.path.join(_host().FONT_DIR, fn)
    except OSError:
        return


def _scan_system_fonts(key, deadline_ts):
    """遍历系统字体目录找第一个含中文字形的字体（限文件数与时间）。"""
    n = 0
    for root in _cfg('_FONT_SCAN_DIRS', _FONT_SCAN_DIRS).get(key, []):
        if not os.path.isdir(root):
            continue
        for dirpath, _dn, filenames in os.walk(root):
            for fn in sorted(filenames):
                if not fn.lower().endswith(('.ttf', '.otf', '.ttc')):
                    continue
                n += 1
                if n > _cfg('_FONT_SCAN_MAX_FILES', _FONT_SCAN_MAX_FILES) or time.time() > deadline_ts:
                    return ''
                p = os.path.join(dirpath, fn)
                if _font_has_cjk(p):
                    return p
    return ''


def _resolve_cjk_font(force=False):
    """惰性解析中文字体路径，结果进程内缓存。返回 '' 表示没找到（附 reason）。"""
    with _host()._FONT_LOCK:
        if _host()._FONT_CACHE['checked'] and not force:
            return _host()._FONT_CACHE['path']
        key = _platform_key()
        found, notes = '', []
        deadline = time.time() + _cfg('_FONT_SCAN_DEADLINE', _FONT_SCAN_DEADLINE)

        env = (os.environ.get(_host().FONT_ENV) or '').strip()
        if env:
            if os.path.isfile(env) and _font_has_cjk(env):
                found = os.path.abspath(env)
            else:
                notes.append(f'环境变量 {_host().FONT_ENV} 指定的字体不可用或不含中文字形：{env}')
        if not found:
            for p in _iter_bundled_fonts():     # 仓库自带（OFL 字体放这里即可，无需改代码）
                if _font_has_cjk(p):
                    found = p
                    break
        if not found:
            for p in _cfg('_FONT_CANDIDATES', _FONT_CANDIDATES).get(key, []):
                if os.path.isfile(p) and _font_has_cjk(p):
                    found = p
                    break
        if not found:
            found = _scan_system_fonts(key, deadline)
        if not found and not notes:
            notes.append('已检查 环境变量 / assets/fonts / 系统常见中文字体 / 系统字体目录，均未找到含中文字形的字体')
        _host()._FONT_CACHE.update(checked=True, path=found, reason='；'.join(notes))
        _host().FONT_PATH = found or ''
        return _host()._FONT_CACHE['path']


class FontMissingError(RuntimeError):
    """找不到含中文字形的字体。绝不静默降级为不含中文的字体（那会产出豆腐块）。"""


def font_missing_help():
    """缺字体时的可执行修复指引（各平台安装命令 + 两种免安装兜底）。"""
    return (
        '未找到含中文字形的字体，已中止渲染 —— 不会生成「豆腐块」字幕/封面标题。'
        f'（{_host()._FONT_CACHE.get("reason") or "系统无可用中文字体"}）\n'
        '任选一种方式修复：\n'
        '  1) 安装开源中文字体（推荐思源黑体 Noto Sans SC，SIL OFL 协议，可商用）：\n'
        '     · Debian/Ubuntu : sudo apt-get install -y fonts-noto-cjk\n'
        '     · Alpine(Docker): apk add --no-cache font-noto-cjk\n'
        '     · CentOS/RHEL   : sudo yum install -y google-noto-sans-cjk-fonts\n'
        '  2) 把字体文件（.ttf/.otf/.ttc）放进项目的 assets/fonts/ 目录后重启\n'
        f'  3) 用环境变量指定：{_host().FONT_ENV}=/path/to/NotoSansSC-Regular.otf'
    )


def cjk_font(size):
    """加载中文字体。找不到含中文字形的字体时抛 FontMissingError（而非静默降级）。"""
    p = _resolve_cjk_font()
    if not p:
        raise FontMissingError(font_missing_help())
    with _host()._FONT_LOCK:
        f = _font_cache_by_size.get(size)
    if f is None:
        try:
            f = ImageFont.truetype(p, size)
        except Exception as e:
            raise FontMissingError(f'字体加载失败：{p}（{e}）。{font_missing_help()}')
        with _host()._FONT_LOCK:
            _font_cache_by_size[size] = f
    return f


def font_selfcheck():
    """启动自检：返回 (ok, 说明文本)。缺字体时给出警告而非让用户在成片里踩坑。"""
    p = _resolve_cjk_font()
    if p:
        return True, f'中文字体：{p}'
    return False, '[警告] ' + font_missing_help().replace('\n', '\n        ')

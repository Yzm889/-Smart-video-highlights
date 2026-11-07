# -*- coding: utf-8 -*-
"""一帧成片 · 桌面版启动器（PyInstaller 打包入口）。

双击 EXE 后的行为：
  1. 起本地服务（127.0.0.1，优先 8765，被占用则自动换空闲端口）
  2. 等服务就绪
  3. 用 Edge 的 --app 模式打开无边框应用窗口（Win10/11 自带 Edge，无需浏览器
     操作；检测不到 Edge 时回退默认浏览器）
  4. 主线程守着服务直到窗口关闭/进程退出

另提供 --selftest：验证打包环境里重依赖（faster-whisper/ctranslate2/sherpa-onnx
等）能否导入，供构建流水线做冒烟检查，不启动服务。
"""
import os
import sys
import socket
import secrets
import threading
import time
import webbrowser

_TOKEN = secrets.token_urlsafe(16)   # 每次启动重新生成：本地路径形态的会话凭证


def _selftest():
    """构建冒烟：确认重依赖能导入、ffmpeg 可执行、桌面窗口组件可用。"""
    import faster_whisper  # noqa: F401
    import ctranslate2  # noqa: F401
    import sherpa_onnx  # noqa: F401
    import soundfile  # noqa: F401
    import numpy  # noqa: F401
    import webview  # noqa: F401  桌面窗口组件（缺了会静默回退网页版）
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    assert os.path.exists(exe), 'ffmpeg 缺失: ' + exe
    print('SELFTEST OK | ffmpeg =', exe)
    sys.exit(0)


def _port_free(port):
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pick_port():
    """优先 8765（与网页版一致）；被占用则找空闲端口（不跟别的实例抢）。"""
    for port in (8765, 8766, 8767):
        if _port_free(port):
            return port
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(port, timeout=60):
    """轮询服务就绪（超时也不放弃启动窗口——加载慢总比白屏好）。"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/' % port, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


class DesktopApi:
    """暴露给界面 JS 的桌面能力（window.pywebview.api.*）。
    桌面版专属：原生文件对话框、本地路径直连凭证——浏览器里没有这些。"""

    def get_desktop_info(self):
        return {'token': _TOKEN, 'version': '桌面版 v1.2.0'}

    def pick_video(self):
        import webview
        res = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=('视频文件 (*.mp4;*.mkv;*.avi;*.mov;*.flv;*.wmv;*.webm;*.m4v;*.ts)',
                        '所有文件 (*.*)'))
        if not res:
            return {}
        path = res[0]
        return {'path': path, 'name': os.path.basename(path)}

    def open_folder(self, rel):
        """在资源管理器中打开并选中成片文件（rel 为 OUTDIR 相对路径）。"""
        import subprocess
        target = os.path.join(webui_server.OUTDIR, str(rel).replace('/', os.sep))
        if os.path.exists(target):
            subprocess.Popen(['explorer', '/select,', target])
        else:
            subprocess.Popen(['explorer', os.path.dirname(target) or webui_server.OUTDIR])
        return {'ok': True}

    def pick_srt(self):
        import webview
        res = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=('字幕文件 (*.srt)', '所有文件 (*.*)'))
        if not res:
            return {}
        path = res[0]
        return {'path': path, 'name': os.path.basename(path)}

    def read_text_file(self, path, max_mb=8):
        """读取本地文本文件（桌面版专属：SRT 导入等）。路径须为绝对路径且存在。"""
        path = str(path or '')
        if not os.path.isabs(path) or not os.path.isfile(path):
            return {'ok': False, 'error': '文件不存在'}
        if os.path.getsize(path) > max_mb * 1024 * 1024:
            return {'ok': False, 'error': '文件过大'}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return {'ok': True, 'text': f.read()}

    def read_file_b64(self, path, max_mb=24):
        """读取本地二进制文件并 base64 编码（桌面版专属：声音克隆上传参考音频）。"""
        import base64
        path = str(path or '')
        if not os.path.isabs(path) or not os.path.isfile(path):
            return {'ok': False, 'error': '文件不存在'}
        if os.path.getsize(path) > max_mb * 1024 * 1024:
            return {'ok': False, 'error': '文件过大（>' + str(max_mb) + 'MB）'}
        with open(path, 'rb') as f:
            return {'ok': True, 'b64': base64.b64encode(f.read()).decode('ascii')}

    def pick_music(self):
        import webview
        res = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=('音频文件 (*.mp3;*.wav;*.m4a;*.flac;*.ogg)', '所有文件 (*.*)'))
        if not res:
            return {}
        path = res[0]
        return {'path': path, 'name': os.path.basename(path)}

    def pick_folder(self):
        """目录选择对话框（升级迁移：选旧版本安装目录）。"""
        import webview
        res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return {}
        return {'path': res[0]}

    def has_config(self):
        """新包是否已有配置（首次启动的旧版本导入横幅据此显示）。"""
        try:
            import webui_server
            return {'ok': True, 'has': os.path.isfile(os.path.join(webui_server.HERE, 'ai_config.json'))}
        except Exception as e:
            return {'ok': False, 'error': str(e)[:120]}

    def migrate_from(self, old_root):
        """从旧版本安装目录导入：配置 / 历史 / 成片 / 工作区 / 素材与音乐库，
        models 按情况重建 junction（旧侧是指向别处的 junction）或整体移动（实体目录）。
        在 EXE 里由「升级导入横幅」触发，运行于服务已就绪之后。返回摘要 dict。"""
        import shutil, subprocess as _sp, json
        import webui_server
        new_here = webui_server.HERE
        old_root = str(old_root or '').strip()
        old_int = os.path.join(old_root, '_internal')
        if not os.path.isdir(old_int):
            if os.path.basename(os.path.normpath(old_root)) == '_internal' and os.path.isdir(old_root):
                old_int = old_root
            else:
                return {'ok': False, 'error': '所选目录下没有 _internal——请选择旧版 FrameCut 安装目录（含 FrameCut.exe 的那层）'}
        moved, notes = [], []
        # 1) 配置覆盖导入；历史**合并**（2026-11-07 教训：测试时覆盖过真实 history.json
        #    导致索引全丢——历史索引按 file 键去重合并，两侧成片都在时一条不丢）
        src_cfg = os.path.join(old_int, 'ai_config.json')
        if os.path.isfile(src_cfg):
            shutil.copy2(src_cfg, os.path.join(new_here, 'ai_config.json'))
            moved.append('ai_config.json')
        src_hist = os.path.join(old_int, 'history.json')
        if os.path.isfile(src_hist):
            dst_hist = os.path.join(new_here, 'history.json')
            old_items, new_items = [], []
            try:
                old_items = json.load(open(src_hist, encoding='utf-8')) or []
            except Exception:
                old_items = []
            try:
                new_items = json.load(open(dst_hist, encoding='utf-8')) or []
            except Exception:
                new_items = []
            seen = {it.get('file') for it in new_items if isinstance(it, dict)}
            for it in old_items:
                if isinstance(it, dict) and it.get('file') and it['file'] not in seen:
                    new_items.append(it)
                    seen.add(it['file'])
            new_items.sort(key=lambda x: x.get('time', ''), reverse=True)
            json.dump(new_items, open(dst_hist, 'w', encoding='utf-8'), ensure_ascii=False)
            moved.append('history.json（合并 %d+%d）' % (len(old_items), len(new_items)))
        # 2) 数据目录：新侧已有则递归并入（不覆盖同名），没有则整体复制
        def _merge_tree(src, dst):
            cnt = 0
            for root, _dirs, files in os.walk(src):
                rel = os.path.relpath(root, src)
                droot = dst if rel == '.' else os.path.join(dst, rel)
                os.makedirs(droot, exist_ok=True)
                for f in files:
                    s2, d2 = os.path.join(root, f), os.path.join(droot, f)
                    if os.path.exists(d2):
                        continue
                    shutil.copy2(s2, d2)
                    cnt += 1
            return cnt

        for dn in ('webui_output', 'webui_workspace', 'material_library', 'music_library'):
            src = os.path.join(old_int, dn)
            if not os.path.isdir(src):
                continue
            dst = os.path.join(new_here, dn)
            if os.path.isdir(dst):
                cnt = _merge_tree(src, dst)
                if cnt:
                    moved.append('%s（并入 %d 项）' % (dn, cnt))
            else:
                shutil.copytree(src, dst)
                moved.append(dn)
        # 3) models：junction → 重建；实体目录 → 移动/复制（大，同卷改名瞬间完成）
        src_models = os.path.join(old_int, 'models')
        dst_models = os.path.join(new_here, 'models')
        if os.path.isdir(src_models) and not os.path.exists(dst_models):
            target = None
            try:
                target = os.readlink(src_models)   # junction 可读；实体目录抛 OSError
            except OSError:
                target = None
            if target:
                _sp.run(['cmd', '/c', 'mklink', '/J', dst_models, target],
                        capture_output=True, text=True)
                if os.path.isdir(dst_models):
                    moved.append('models（junction → %s）' % target)
                else:
                    notes.append('models junction 重建失败，请手动 mklink /J')
            else:
                try:
                    os.rename(src_models, dst_models)
                    moved.append('models（整体移动）')
                    notes.append('旧版目录的 models 已移至新版；旧版再打开需重新下载模型')
                except OSError:
                    shutil.copytree(src_models, dst_models)
                    moved.append('models（跨盘复制，GB 级需等待）')
        return {'ok': True, 'moved': moved, 'notes': notes}


def _launch_app_window(url):
    """优先 Edge --app 模式（无地址栏的独立应用窗口）；回退默认浏览器。"""
    candidates = []
    pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    pf = os.environ.get('ProgramFiles', r'C:\Program Files')
    candidates.append(os.path.join(pf86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
    candidates.append(os.path.join(pf, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
    local = os.environ.get('LOCALAPPDATA')
    if local:
        candidates.append(os.path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    for exe in candidates:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, '--app=' + url, '--window-size=1440,900'],
                                 close_fds=True)
                return 'app-window'
            except Exception:
                pass
    webbrowser.open(url)
    return 'browser'


def _run_desktop_window(url):
    """原生桌面窗口（pywebview，Windows 用 Edge WebView2 渲染）。
    关闭窗口 = 退出程序。返回 True=正常走完；False=环境不支持，调用方回退。"""
    try:
        import webview
    except Exception as e:
        print('pywebview 不可用（%s），回退浏览器。' % e)
        return False
    # 【曾犯的错】这里曾写死网页版根地址 '/'——EXE 打开的是网页版而非桌面 UI
    # （主人实测抓到）。必须加载调用方传入的桌面 UI 地址。
    webview.create_window('一帧成片', url, width=1440, height=900, min_size=(1100, 700))
    webview.start()   # 阻塞直到窗口关闭
    return True


def main():
    if '--selftest' in sys.argv:
        _selftest()
    import webui_server as S   # 打包入口：导入即初始化全部引擎层
    S.set_desktop_token(_TOKEN)   # 本地路径直连的会话凭证（网页模式无此令牌）

    port = _pick_port()
    url = 'http://127.0.0.1:%d/static/desktop/index.html' % port
    t = threading.Thread(target=S.start_server, args=(port, False), daemon=True)
    t.start()
    ready = _wait_ready(port)
    print('一帧成片已启动: %s (%s, 就绪=%s)' % (url, '8765' if port == 8765 else '端口%d' % port, ready))
    if _run_desktop_window(url):
        return
    # 兜底路径（浏览器打开）没有 js_api：令牌不生效，本地路径形态被拒（安全设计）
    # 回退：Edge --app 独立窗口 → 再不行默认浏览器
    _launch_app_window(url)
    try:
        t.join()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""v1.4.37：桌面版视频/字幕持久化测试。

验证 localStorage 键名正确、save/read/clear 往返一致、
newProjectReset 后复用按钮仍可用（视频/字幕跨项目保留）、
以及 _renderLastMedia 的 DOM 绑定。
"""
import os
import re
import sys
import unittest

# 确保项目根目录在 sys.path 里
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _read_desktop_js():
    """读取 static/desktop/desktop.js 源码（用于静态断言）。"""
    p = os.path.join(_ROOT, 'static', 'desktop', 'desktop.js')
    with open(p, 'r', encoding='utf-8') as f:
        return f.read()


def _read_index_html():
    """读取 static/desktop/index.html 源码。"""
    p = os.path.join(_ROOT, 'static', 'desktop', 'index.html')
    with open(p, 'r', encoding='utf-8') as f:
        return f.read()


class TestLastVideoPersistence(unittest.TestCase):
    """视频路径持久化：lastVideoSave/Read/Clear 键名与往返。"""

    def test_last_video_key_defined(self):
        """LAST_VIDEO_KEY 常量已定义且值正确。"""
        js = _read_desktop_js()
        m = re.search(r"const\s+LAST_VIDEO_KEY\s*=\s*'([^']+)'", js)
        self.assertIsNotNone(m, 'LAST_VIDEO_KEY 未定义')
        self.assertEqual(m.group(1), 'framecut_desktop_last_video_v1')

    def test_last_srt_key_defined(self):
        """LAST_SRT_KEY 常量已定义且值正确。"""
        js = _read_desktop_js()
        m = re.search(r"const\s+LAST_SRT_KEY\s*=\s*'([^']+)'", js)
        self.assertIsNotNone(m, 'LAST_SRT_KEY 未定义')
        self.assertEqual(m.group(1), 'framecut_desktop_last_srt_v1')

    def test_last_video_save_only_local_path(self):
        """lastVideoSave 只在 isLocalPath=true 时写盘（浏览器上传的 upload_id 不存）。"""
        js = _read_desktop_js()
        # 函数体里必须有 isLocalPath 守卫
        m = re.search(r'function\s+lastVideoSave\(\)\s*\{(.*?)\}', js, re.S)
        self.assertIsNotNone(m, 'lastVideoSave 函数未找到')
        self.assertIn('isLocalPath', m.group(1),
                      'lastVideoSave 必须检查 isLocalPath')

    def test_last_video_save_called_in_accept_video(self):
        """acceptVideo 里调用 lastVideoSave。"""
        js = _read_desktop_js()
        m = re.search(r'function\s+acceptVideo\([^)]*\)\s*\{(.*?)\n\}', js, re.S)
        self.assertIsNotNone(m, 'acceptVideo 函数未找到')
        self.assertIn('lastVideoSave', m.group(1),
                      'acceptVideo 必须调用 lastVideoSave')

    def test_last_srt_save_called_in_load_srt_file(self):
        """_loadSrtFile 里调用 lastSrtSave。"""
        js = _read_desktop_js()
        m = re.search(r'function\s+_loadSrtFile\([^)]*\)\s*\{(.*?)\n\}', js, re.S)
        self.assertIsNotNone(m, '_loadSrtFile 函数未找到')
        self.assertIn('lastSrtSave', m.group(1),
                      '_loadSrtFile 必须调用 lastSrtSave')

    def test_last_srt_save_called_in_import_srt(self):
        """importSrt（桌面原生路径）里也调用 lastSrtSave。"""
        js = _read_desktop_js()
        # importSrt 函数体里必须有 lastSrtSave
        m = re.search(r'function\s+importSrt\(\)\s*\{(.*?)\n\}', js, re.S)
        self.assertIsNotNone(m, 'importSrt 函数未找到')
        self.assertIn('lastSrtSave', m.group(1),
                      'importSrt 必须调用 lastSrtSave')


class TestRenderLastMedia(unittest.TestCase):
    """_renderLastMedia DOM 绑定与按钮渲染。"""

    def test_render_last_media_function_exists(self):
        """_renderLastMedia 函数已定义。"""
        js = _read_desktop_js()
        self.assertIn('function _renderLastMedia()', js)

    def test_render_last_media_called_on_load(self):
        """页面加载时调用 _renderLastMedia（draft restore 之后）。"""
        js = _read_desktop_js()
        # _draftRestored 之后必须有 _renderLastMedia
        m = re.search(r'const\s+_draftRestored\s*=\s*draftRestore\(\);', js)
        self.assertIsNotNone(m, 'draftRestore 调用未找到')
        # 在该行之后查找 _renderLastMedia
        after = js[m.end():m.end() + 200]
        self.assertIn('_renderLastMedia', after,
                      '_renderLastMedia 必须在 draftRestore 之后调用')

    def test_render_last_media_called_in_new_project_reset(self):
        """newProjectReset 里也调用 _renderLastMedia（新建项目后刷新按钮）。"""
        js = _read_desktop_js()
        m = re.search(r'function\s+newProjectReset\(\)\s*\{(.*?)\n\}', js, re.S)
        self.assertIsNotNone(m, 'newProjectReset 函数未找到')
        self.assertIn('_renderLastMedia', m.group(1),
                      'newProjectReset 必须调用 _renderLastMedia')

    def test_reuse_video_button_handler(self):
        """复用视频按钮点击时调用 acceptVideo。"""
        js = _read_desktop_js()
        # _renderLastMedia 里必须有 npReuseVideo 的事件绑定
        m = re.search(r'function\s+_renderLastMedia\(\)\s*\{(.*?)\n\}', js, re.S)
        self.assertIsNotNone(m, '_renderLastMedia 函数未找到')
        body = m.group(1)
        self.assertIn('npReuseVideo', body, '必须有 npReuseVideo 按钮')
        self.assertIn('acceptVideo', body, 'npReuseVideo 必须调用 acceptVideo')

    def test_reuse_srt_button_handler(self):
        """复用字幕按钮点击时设置 _srt 并调用 _syncSrtUi。"""
        js = _read_desktop_js()
        m = re.search(r'function\s+_renderLastMedia\(\)\s*\{(.*?)\n\}', js, re.S)
        self.assertIsNotNone(m, '_renderLastMedia 函数未找到')
        body = m.group(1)
        self.assertIn('npReuseSrt', body, '必须有 npReuseSrt 按钮')
        self.assertIn('_syncSrtUi', body, 'npReuseSrt 必须调用 _syncSrtUi')


class TestHtmlSlot(unittest.TestCase):
    """index.html 里有 npLastMedia slot。"""

    def test_np_last_media_slot_exists(self):
        """index.html 里有 id=npLastMedia 的 div。"""
        html = _read_index_html()
        self.assertIn('id="npLastMedia"', html,
                      'index.html 必须有 id=npLastMedia 的容器')

    def test_np_last_media_after_np_srt(self):
        """npLastMedia 在 npSrt 按钮之后（视觉位置合理）。"""
        html = _read_index_html()
        srt_pos = html.find('id="npSrt"')
        last_pos = html.find('id="npLastMedia"')
        self.assertGreater(last_pos, srt_pos,
                           'npLastMedia 必须在 npSrt 之后')


class TestCacheVersionBump(unittest.TestCase):
    """缓存版本号 bump 到 24。"""

    def test_desktop_js_v24(self):
        """desktop.js?v=27。"""
        html = _read_index_html()
        self.assertIn('desktop.js?v=27', html)

    def test_desktop_css_v24(self):
        """desktop.css?v=27。"""
        html = _read_index_html()
        self.assertIn('desktop.css?v=27', html)

    def test_js_css_versions_equal(self):
        """desktop.js 和 desktop.css 的版本号必须相等。"""
        html = _read_index_html()
        js_m = re.search(r'desktop\.js\?v=(\d+)', html)
        css_m = re.search(r'desktop\.css\?v=(\d+)', html)
        self.assertIsNotNone(js_m, 'desktop.js?v= 未找到')
        self.assertIsNotNone(css_m, 'desktop.css?v= 未找到')
        self.assertEqual(js_m.group(1), css_m.group(1),
                         'desktop.js 和 desktop.css 版本号必须相等')


if __name__ == '__main__':
    unittest.main()


def test_srt_import_shows_name_alignment_reminder():
    """v1.4.47 主人拍板：字幕导入前必须弹「人名译名对齐」提醒（三个入口全覆盖）。

    主人实测：解说稿写「柯蒂斯」、字幕写「科特」——同一角色译名不一致，
    语义匹配抓不住，画面配到别人的对话上。导入前提醒用户先用 AI 统一译名。
    """
    js = _read_desktop_js()
    assert 'function _confirmSrtNames()' in js, '提醒守卫函数必须存在'
    assert '同一套译名' in js and '柯蒂斯' in js and '科特' in js, '提醒文案要点名译名不一致问题'
    # 入口①：按钮/工具条导入（原生对话框与浏览器兜底共用 importSrt）
    assert 'await _confirmSrtNames()' in js
    # 入口②：拖拽 .srt/.ass/.ssa/.vtt
    assert '_confirmSrtNames().then(ok => { if(ok) _loadSrtFile(f); })' in js
    # 入口③：复用上次字幕（npReuseSrt）
    assert js.count('_confirmSrtNames()') >= 3, '三个导入入口都要过提醒'


def test_preset_modal_msg_supports_multiline():
    """提醒文案是多行文本：presetModalMsg 必须支持 pre-line 换行，否则挤成一行。"""
    html = _read_index_html()
    assert 'white-space:pre-line' in html

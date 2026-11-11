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
        """desktop.js?v=30。"""
        html = _read_index_html()
        self.assertIn('desktop.js?v=30', html)

    def test_desktop_css_v24(self):
        """desktop.css?v=30。"""
        html = _read_index_html()
        self.assertIn('desktop.css?v=30', html)

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


def test_read_text_file_decodes_gbk_srt(tmp_path):
    """v1.4.49 桌面桥编码自适应：GBK 字幕必须正确解码（Netflix 官方简体实测是 GBK）。

    旧实现固定 utf-8 errors=replace → 官方中文字幕在读取那一步就乱码，
    信息丢失后端无法恢复，静默退回 ASR 白跑三分钟。
    """
    import desktop
    p = tmp_path / 'gbk.srt'
    content = '1\n00:00:15,015 --> 00:00:16,099\n-准备好了？\n-好了\n'
    p.write_bytes(content.encode('gbk'))
    r = desktop.DesktopApi().read_text_file(str(p))
    assert r['ok'] is True
    assert '准备好了' in r['text'], 'GBK 字幕读取乱码：%r' % r['text'][:60]


def test_read_text_file_utf8_still_works(tmp_path):
    import desktop
    p = tmp_path / 'u8.srt'
    content = '1\n00:00:15,015 --> 00:00:16,099\n-准备好了？\n'
    p.write_bytes(content.encode('utf-8'))
    r = desktop.DesktopApi().read_text_file(str(p))
    assert r['ok'] is True and '准备好了' in r['text']


def test_srt_import_decodes_gbk_and_warns_merged_blocks():
    """前端导入链：解码器链（utf-8 严格→gbk→兜底）+ 大块合并体检，源码钉。"""
    js = _read_desktop_js()
    assert 'function _decodeSubtitleBytes(' in js
    assert "TextDecoder('utf-8', {fatal: true})" in js, 'utf-8 必须严格解码（坏字节即失败才轮到 GBK）'
    assert "TextDecoder('gbk')" in js
    assert 'file.arrayBuffer()' in js, '导入必须走 arrayBuffer + 解码器链'
    assert 'function _warnMergedBlocks(' in js, '大块合并体检必须存在'
    assert '_warnMergedBlocks(text)' in js, '导入成功后必须跑体检'
    # v1.4.47/48 的人名提醒仍在（三入口 + 文案）
    assert 'function _confirmSrtNames()' in js
    assert js.count('_confirmSrtNames()') >= 3


_SRT_EXEC_HARNESS = r'''
const fs = require("fs");
const raw = fs.readFileSync(process.env.FC_JS, "utf8");
const lines = raw.split("\n");
function blockOf(prefix){
  const start = lines.findIndex(l => l.startsWith(prefix));
  if(start < 0) throw new Error("not found: " + prefix);
  let depth = 0, started = false;
  for(let i = start; i < lines.length; i++){
    for(const ch of lines[i]){
      if(ch === "{"){ depth++; started = true; } else if(ch === "}"){ depth--; }
    }
    if(started && depth === 0) return lines.slice(start, i + 1).join("\n");
  }
  throw new Error("no end: " + prefix);
}
function lineOf(prefix){
  const l = lines.find(l => l.startsWith(prefix));
  if(!l) throw new Error("no decl: " + prefix);
  return l;
}
/* 变量声明行必须原样保留：v1.4.49 曾把 let _srt 误删——严格模式下赋值直接抛
   ReferenceError（「字幕导入失败：_srt is not defined」主人实测）。这类 bug
   node --check 查不出（语法合法），只有真执行才炸，所以本夹具真跑导入链。 */
const src = [
  lineOf("const LAST_SRT_KEY"),
  lineOf("let _srt"),
  blockOf("function lastSrtSave"),
  blockOf("function lastSrtRead"),
  blockOf("function _decodeSubtitleBytes"),
  blockOf("function _warnMergedBlocks"),
  blockOf("function _syncSrtUi"),
  blockOf("function _loadSrtFile"),
].join("\n");
const els = {};
function $(id){ return (els[id] = els[id] || { textContent: "", innerHTML: "", style: {} }); }
function setBtnIcon(){ els.__setBtnIcon = true; }
function _esc(s){ return String(s == null ? "" : s); }
function _clean(s){ return String(s || ""); }
function toast(){ els.__toast = true; }
const store = {};
const localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
const api = new Function("$", "setBtnIcon", "toast", "_esc", "_clean", "localStorage",
  "TextEncoder", "TextDecoder", src +
  "\nreturn { load: _loadSrtFile, read: lastSrtRead, ls: () => localStorage.getItem(LAST_SRT_KEY) };")(
  $, setBtnIcon, toast, _esc, _clean, localStorage, TextEncoder, TextDecoder);
const NL = String.fromCharCode(10);
const u8 = new TextEncoder().encode("1" + NL + "00:00:15,015 --> 00:00:16,099" + NL + "-准备好了？" + NL);
const fake = { name: "t.srt", arrayBuffer: () => Promise.resolve(u8.buffer) };
let thrown = null;
try { api.load(fake); } catch (e) { thrown = String(e && e.message || e); }
setTimeout(() => {
  const out = { thrown: thrown };
  let saved = null;
  try { saved = api.ls(); } catch (e) { out.lsErr = String(e && e.message || e); }
  out.lsHas = !!saved;
  try { out.lsName = saved ? (JSON.parse(saved).name || null) : null; } catch (e) { out.lsName = "parse"; }
  try { out.readName = (api.read() || {}).name || null; } catch (e) { out.readName = "err"; }
  console.log(JSON.stringify(out));
}, 400);
'''


def test_srt_import_flow_actually_runs():
    """v1.4.50 回归钉：导入链必须**真实执行**跑通（不是源码钉）。

    v1.4.49 重构时误删了 `let _srt = null;` 声明——严格模式下赋值抛
    ReferenceError，按钮导入全炸成「字幕导入失败：_srt is not defined」，
    而 node --check / 源码钉全部查不出。本测试在 node 里真跑导入链。
    """
    import json
    import subprocess
    env = dict(os.environ)
    env["FC_JS"] = os.path.join(_ROOT, "static", "desktop", "desktop.js")
    p = subprocess.run(["node", "-"], input=_SRT_EXEC_HARNESS, capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60, cwd=str(_ROOT))
    assert p.returncode == 0, "harness 失败 rc=%s err=%s" % (p.returncode, (p.stderr or "")[-800:])
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert not out.get("thrown"), "导入链抛错：%r" % out.get("thrown")
    assert not out.get("harnessErr"), "harness 错误：%r" % out.get("harnessErr")
    assert out.get("lsHas"), "localStorage 未写入（导入链没跑完）"
    assert out.get("lsName") == "t.srt" and out.get("readName") == "t.srt", out


def test_music_persist_and_reuse_all():
    """v1.4.51 配乐记忆 + 一键复用全部（源码钉）。"""
    js = _read_desktop_js()
    assert "LAST_MUSIC_KEY = 'framecut_desktop_last_music_v1'" in js
    assert 'function lastMusicSave()' in js and 'function lastMusicRead()' in js
    assert 'function reuseLastMusic()' in js
    # 只存本地路径形态（与视频/字幕同一规则：upload_id/base64 不存）
    assert 'isLocalPath && _music.path' in js
    assert 'lastMusicSave();   // v1.4.51' in js, '选曲（桥路径）必须写盘'
    # 复用按钮三件套 + 一键全部
    assert 'npReuseMusic' in js and 'npReuseAll' in js
    assert '一键复用上次全部' in js
    assert 'reuseLastMusic()' in js
    html = _read_index_html()
    assert 'desktop.js?v=30' in html and 'desktop.css?v=30' in html

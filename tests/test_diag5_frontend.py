# -*- coding: utf-8 -*-
"""DIAG_5 前端两项修复的回归钉（P1-1 配音状态文案 / P1-2 稿件拖入）。

主人真机两个现象：
  P1-1「选了配音引擎，设置页还提示没有配置」—— 实测状态栏文案来自
       `#ttsStateHint`，而它的判断只看 sherpa/cosyvoice（上一代引擎），
       完全没看 `v.tts2`。主人换用 Qwen3-TTS/IndexTTS2 后两者都是 false，
       于是「两个新引擎都就绪」被写成「还没装本地引擎」。
  P1-2「写好的稿子拖不进窗口」—— drop 处理器只认 srt/ass/ssa/vtt，
       txt/md 拖进来**一句话都不提示**。

本文件不靠「字面量还在文件里」，而是把 desktop.js 里**真实的函数块**
抽出来在 node 里跑，再回读可观察结果（状态栏字符串 / npPlot 的值）。
纯静态断言证明不了接线，而这里要钉的正是接线。
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_PATH = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')
NODE = shutil.which('node')

pytestmark = pytest.mark.skipif(not NODE, reason='未找到 node，跳过桌面版行为回归')


def _load(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


# ─────────────────────────────────────────────────────────────
# 一、P1-1：状态栏文案（纯函数，可直接在 node 里调）
# ─────────────────────────────────────────────────────────────
HINT_HARNESS = r'''
const fs = require('fs');
const raw = fs.readFileSync(process.env.FC_JS, 'utf8');
const lines = raw.split(/\r?\n/);
const start = lines.findIndex(l => l.startsWith('function _ttsStateHint('));
if(start < 0) throw new Error('找不到 _ttsStateHint');
let depth = 0, started = false, end = -1;
for(let i = start; i < lines.length; i++){
  for(const ch of lines[i]){
    if(ch === '{'){ depth++; started = true; } else if(ch === '}'){ depth--; }
  }
  if(started && depth === 0){ end = i; break; }
}
if(end < 0) throw new Error('找不到 _ttsStateHint 结束行');
let block = lines.slice(start, end + 1).join('\n');

/* 变异：把「看 v.tts2」那一层抽掉，还原成修复前只看 sherpa/cosyvoice 的写法。
   先在**函数块内**做替换（不是全文 —— 全文里 renderTtsInstalls 也读 v.tts2，
   那样 leftover 自检必然误报）。 */
if(process.env.FC_MUTATE === 'legacy_only'){
  const before = block;
  // 从 `const t2 = v.tts2...` 一路吃到函数末尾（末行的 `return st;\n}`），
  // 整段换成修复前「只看 sherpa/cosyvoice」的写法。用贪婪匹配锚定函数收尾，
  // 避免只替换到中间某个 `}` 而留下半截引用 t2 的死代码。
  block = block.replace(
    /const t2 = v\.tts2 \|\| \{\};[\s\S]*\n  return st;\n\}/,
    "const legacy = !!(v.sherpa_installed || v.cosyvoice_installed);\n" +
    "  if(!legacy){\n" +
    "    st.push('⚠ 还没装本地引擎：微软服务不稳时会退回机械音，建议在下方装离线模型');\n" +
    "    return st;\n" +
    "  }\n" +
    "  st.push('✓ 本地引擎优先，联网服务只作兜底');\n" +
    "  return st;\n}");
  if(block === before) throw new Error('变异失败：正则没匹配上（结构变了？）');
  if(block.includes('v.tts2')) throw new Error('变异失败：函数体内 v.tts2 仍在');
  if(!block.includes('还没装本地引擎')) throw new Error('变异失败：替身文案没进去');
}

const cases = JSON.parse(process.env.FC_CASES);
const fn = new Function('v', 'cfg', block + '\nreturn _ttsStateHint(v, cfg);');
const out = cases.map(c => fn(c.v, c.cfg));
console.log(JSON.stringify(out));
'''


def _run_hint(cases, mutate=None):
    env = dict(os.environ)
    env['FC_JS'] = JS_PATH
    env['FC_CASES'] = json.dumps(cases, ensure_ascii=False)
    if mutate:
        env['FC_MUTATE'] = mutate
    p = subprocess.run([NODE, '-'], input=HINT_HARNESS, capture_output=True,
                       text=True, encoding='utf-8', env=env, timeout=60, cwd=str(ROOT))
    assert p.returncode == 0, 'node 夹具失败 rc=%s\n%s' % (p.returncode, (p.stderr or '')[-1500:])
    return json.loads(p.stdout.strip().splitlines()[-1])


# 主人真机的形状：两个新引擎都就绪，sherpa/cosyvoice 都没有，engine=auto
LIVE = {
    'label': 'Qwen3-TTS',
    'sherpa_installed': False,
    'cosyvoice_installed': False,
    'tts2': {'qwen3tts': True, 'indextts2': True, 'dir': 'D:/x'},
}


def test_ready_new_engines_are_not_reported_as_missing():
    """★主人报的那条★ 新引擎都就绪时，绝不能出现「还没装本地引擎」。"""
    out = _run_hint([{'v': LIVE, 'cfg': {'engine': 'auto'}}])[0]
    joined = '   '.join(out)
    assert '还没装本地引擎' not in joined, (
        '两个新引擎都就绪却被报成没装 —— 这正是主人看到的「选了引擎还提示没有配置」：\n' + joined)
    assert '就绪' in joined, joined


def test_auto_prefers_indextts2_in_the_wording():
    """auto 的真实顺序是 ['indextts2','qwen3tts']（克隆优先），文案要说 IndexTTS2。"""
    out = _run_hint([{'v': LIVE, 'cfg': {'engine': 'auto'}}])[0]
    assert 'IndexTTS2' in '   '.join(out), out


def test_chosen_engine_not_ready_says_so_precisely():
    """显式选了 qwen3tts 但它没就绪 → 说清「会换用另一个本地引擎」，而不是笼统说没装。"""
    v = {'sherpa_installed': False, 'cosyvoice_installed': False,
         'tts2': {'qwen3tts': False, 'indextts2': True}}
    joined = '   '.join(_run_hint([{'v': v, 'cfg': {'engine': 'qwen3tts'}}])[0])
    assert '还没装本地引擎' not in joined, joined
    assert 'Qwen3-TTS' in joined and '没就绪' in joined, joined


def test_no_local_engine_at_all_still_warns():
    """真的一个都没有时，仍然要给出「还没装」的警告（别把警告一起删了）。"""
    v = {'sherpa_installed': False, 'cosyvoice_installed': False, 'tts2': {}}
    joined = '   '.join(_run_hint([{'v': v, 'cfg': {'engine': 'auto'}}])[0])
    assert '还没装本地引擎' in joined, joined


def test_legacy_engine_still_counted():
    """老引擎（sherpa）装了就仍然算「有本地引擎」，不能因为改了新逻辑把老路说成没装。"""
    v = {'sherpa_installed': True, 'cosyvoice_installed': False, 'tts2': {}}
    joined = '   '.join(_run_hint([{'v': v, 'cfg': {'engine': 'auto'}}])[0])
    assert '还没装本地引擎' not in joined, joined


def test_degrade_and_dead_messages_preserved():
    """降级/熔断提示（v1.4.14 的成果）必须原样保留。"""
    v = dict(LIVE); v['degrade_msg'] = 'IndexTTS2 不可用，已降级'
    out = _run_hint([{'v': v, 'cfg': {'engine': 'auto'}}])[0]
    assert any('已降级' in x for x in out), out
    v2 = dict(LIVE); v2['edge_dead'] = '连接被拒'
    out2 = _run_hint([{'v': v2, 'cfg': {'engine': 'auto'}}])[0]
    assert any('微软服务不可用' in x for x in out2), out2


def test_hint_logic_is_nonvacuous(mutate='legacy_only'):
    """非空转证明：还原成「只看旧引擎」的旧写法后，主人那条场景必须变红。"""
    out = _run_hint([{'v': LIVE, 'cfg': {'engine': 'auto'}}], mutate=mutate)[0]
    joined = '   '.join(out)
    assert '还没装本地引擎' in joined, (
        '变异版竟然也通过了 —— 说明这条断言测不到 tts2 那一层，是空转的')


# ─────────────────────────────────────────────────────────────
# 二、P1-2：稿件拖入
# ─────────────────────────────────────────────────────────────
DROP_HARNESS = r'''
const fs = require('fs');
let raw = fs.readFileSync(process.env.FC_JS, 'utf8');

/* 变异：还原成修复前的 drop —— 只认 srt/ass/ssa/vtt，其它一律静默返回。 */
if(process.env.FC_MUTATE === 'old_drop'){
  raw = raw.replace(
    /document\.addEventListener\('drop', e => \{[\s\S]*?\r?\n\}\);\r?\n/,
    "document.addEventListener('drop', e => {\n" +
    "  e.preventDefault();\n" +
    "  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];\n" +
    "  if(!f) return;\n" +
    "  const ext = (f.name.split('.').pop() || '').toLowerCase();\n" +
    "  if(['srt', 'ass', 'ssa', 'vtt'].indexOf(ext) >= 0){ __loadSrt(f); }\n" +
    "});\n");
  if(raw.includes("_SCRIPT_EXT.indexOf")) throw new Error('变异失败：_SCRIPT_EXT 仍在（正则没匹配上）');
}

const lines = raw.split(/\r?\n/);
function blockOf(prefix){
  const start = lines.findIndex(l => l.startsWith(prefix));
  if(start < 0) throw new Error('找不到: ' + prefix);
  let depth = 0, started = false;
  for(let i = start; i < lines.length; i++){
    for(const ch of lines[i]){
      if(ch === '{'){ depth++; started = true; } else if(ch === '}'){ depth--; }
    }
    if(started && depth === 0) return lines.slice(start, i + 1).join('\n');
  }
  throw new Error('找不到结束行: ' + prefix);
}
const constLine = lines.find(l => l.startsWith('const _SCRIPT_EXT'));
if(!constLine) throw new Error('找不到 _SCRIPT_EXT');

// 只抽「类型分派」那一段：把 drop 回调体拿来，但把 _loadSrtFile / switchView 等
// 换成记录桩，避免把整个 desktop.js 的宿主依赖搬进来。
const dropStart = lines.findIndex(l => l.startsWith("document.addEventListener('drop'"));
if(dropStart < 0) throw new Error('找不到 drop 监听');
let depth = 0, started = false, dropEnd = -1;
for(let i = dropStart; i < lines.length; i++){
  for(const ch of lines[i]){
    if(ch === '{'){ depth++; started = true; } else if(ch === '}'){ depth--; }
  }
  if(started && depth === 0){ dropEnd = i; break; }
}
const dropBlock = lines.slice(dropStart, dropEnd + 1).join('\n');

const calls = { toast: [], srt: [], plot: [], confirm: [] };
const els = {};
function makeEl(id){
  const o = { id: id, style: {}, disabled: false, checked: false, _t: '' };
  let v = '';
  Object.defineProperty(o, 'value', { get: () => v,
    set: x => { v = (x === undefined || x === null) ? '' : String(x); }, enumerable: true });
  Object.defineProperty(o, 'textContent', { get: () => o._t,
    set: x => { o._t = String(x); }, enumerable: true });
  return o;
}
function $(id){ return (els[id] = els[id] || makeEl(id)); }
function toast(m, k){ calls.toast.push({ m: m, k: k }); }
function switchView(v){ calls.plot.push('switchView:' + v); }
function updateNewProj(){ calls.plot.push('updateNewProj'); }
function draftScheduleSave(){ calls.plot.push('draftScheduleSave'); }
function _loadSrtFile(f){ calls.srt.push(f.name); }
async function uiConfirm(t, d, ok){ calls.confirm.push({ t: t, d: d, ok: ok }); return true; }

// FileReader 桩：readAsText 直接把预设内容交给 onload
let READ = '';
class FileReader {
  readAsText(){ this.result = READ; setTimeout(() => this.onload && this.onload(), 0); }
}

const doc = { _h: {}, addEventListener(t, fn){ (this._h[t] = this._h[t] || []).push(fn); } };
const document = doc;

const src = constLine + '\n' + blockOf('function _loadScriptIntoPlot(') + '\n' + dropBlock
  + '\nreturn { fire: (name, content) => { READ = content;'
  + "  const f = { name: name };"
  + "  const ev = { preventDefault(){}, dataTransfer: { files: [f] } };"
  + "  doc._h['drop'].forEach(fn => fn(ev));"
  + '  return new Promise(r => setTimeout(() => r(true), 30)); } };';
const api = new Function('$', 'toast', 'switchView', 'updateNewProj', 'draftScheduleSave',
  '_loadSrtFile', 'uiConfirm', 'FileReader', 'document', src)(
  $, toast, switchView, updateNewProj, draftScheduleSave, _loadSrtFile, uiConfirm,
  FileReader, document);

(async () => {
  const cases = JSON.parse(process.env.FC_DROP_CASES);
  const out = [];
  for(const c of cases){
    calls.toast.length = 0; calls.srt.length = 0; calls.plot.length = 0;
    for(const k of Object.keys(els)) delete els[k];
    els.newProj = makeEl('newProj'); els.newProj.style.display = '';
    els.npPlot = makeEl('npPlot');
    if(c.pre) els.npPlot.value = c.pre;
    await api.fire(c.name, c.content === undefined ? '正文' : c.content);
    out.push({ name: c.name, toast: calls.toast, srt: calls.srt,
               plot: els.npPlot.value, plotCalls: calls.plot,
               confirm: calls.confirm });
  }
  console.log(JSON.stringify(out));
})().catch(e => console.log(JSON.stringify({ error: String((e && e.message) || e) })));
'''


def _run_drop(cases, mutate=None):
    env = dict(os.environ)
    env['FC_JS'] = JS_PATH
    env['FC_DROP_CASES'] = json.dumps(cases, ensure_ascii=False)
    if mutate:
        env['FC_MUTATE'] = mutate
    p = subprocess.run([NODE, '-'], input=DROP_HARNESS, capture_output=True,
                       text=True, encoding='utf-8', env=env, timeout=60, cwd=str(ROOT))
    assert p.returncode == 0, 'node 夹具失败 rc=%s\n%s' % (p.returncode, (p.stderr or '')[-1500:])
    d = json.loads(p.stdout.strip().splitlines()[-1])
    assert 'error' not in d, 'drop 夹具内抛错：%s' % d.get('error')
    return d


def test_txt_dropped_into_plot():
    """★主人报的那条★ 写好的 .txt 拖进来，必须真的进剧情稿框。"""
    r = _run_drop([{'name': '我的稿子.txt', 'content': '深夜的港口，一艘货船悄悄离岸……'}])[0]
    assert r['plot'] == '深夜的港口，一艘货船悄悄离岸……', (
        'txt 拖进来没有回填 npPlot（旧版对 txt 一句话都不说，像坏了）')
    assert any('已导入' in t['m'] for t in r['toast']), r['toast']


def test_md_dropped_into_plot():
    r = _run_drop([{'name': 'notes.md', 'content': '# 大纲\n第一幕'}])[0]
    assert r['plot'] == '# 大纲\n第一幕', r


def test_drop_feeds_draft_layer_and_button_state():
    """回填后必须联动草稿层与按钮态，否则关窗口就丢。"""
    r = _run_drop([{'name': 'a.txt', 'content': '正文'}])[0]
    assert 'draftScheduleSave' in r['plotCalls'], r['plotCalls']
    assert 'updateNewProj' in r['plotCalls'], r['plotCalls']


def test_drop_over_existing_text_asks_first():
    """已有内容时不静默覆盖。"""
    r = _run_drop([{'name': 'a.txt', 'content': '新稿子', 'pre': '手打的旧稿子'}])[0]
    assert r['confirm'], '覆盖已有稿子前没有确认'
    assert '覆盖' in r['confirm'][0]['ok'], r['confirm']


def test_drop_into_empty_box_does_not_ask():
    """空框直接进，零打扰（否则每次拖稿都多一个弹框）。"""
    r = _run_drop([{'name': 'a.txt', 'content': '新稿子'}])[0]
    assert not r['confirm'], '空框也弹了确认'


def test_empty_file_is_rejected_with_message():
    r = _run_drop([{'name': 'empty.txt', 'content': '   '}])[0]
    assert r['plot'] == '', '空文件竟然把框清空了'
    assert any('空' in t['m'] for t in r['toast']), r['toast']


def test_srt_still_goes_to_srt_loader():
    """字幕类不能被抢走（老功能不许倒车）。"""
    r = _run_drop([{'name': 'sub.srt', 'content': '1'}])[0]
    assert r['srt'] == ['sub.srt'], r['srt']
    assert r['plot'] == '', 'srt 不该被当成稿件填进 npPlot'


def test_docx_gets_an_honest_message():
    """.docx 必须明确告知「另存为 txt」，不许静默。"""
    r = _run_drop([{'name': 'a.docx', 'content': 'x'}])[0]
    assert r['toast'], '.docx 拖进来一句话都没有 —— 就是主人说的「拖了没反应」'
    assert any('txt' in t['m'] for t in r['toast']), r['toast']
    assert r['plot'] == ''


def test_unknown_type_gets_a_message():
    """别的类型也要有回话，不能静默吞掉。"""
    r = _run_drop([{'name': 'a.xyz', 'content': 'x'}])[0]
    assert r['toast'], '未知类型静默无反应'


def test_drop_logic_is_nonvacuous():
    """非空转证明：还原成旧的「只认字幕」写法后，txt 那条必须红。"""
    r = _run_drop([{'name': '我的稿子.txt', 'content': '深夜的港口'}], mutate='old_drop')[0]
    assert r['plot'] == '', '变异版竟然也填了 npPlot —— 说明断言测不到新增分支'
    assert not r['toast'], '变异版竟然也提示了 —— 说明断言测不到新增分支'

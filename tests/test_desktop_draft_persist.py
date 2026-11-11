# -*- coding: utf-8 -*-
"""剧情稿草稿持久化 + newProject 不得静默销毁稿子（桌面版回归钉）。

背景（主人实测痛点）：长篇剧情稿此前没有任何持久化 —— 刷新页面、关窗口、
或误按一次 Ctrl+N 就当场蒸发，且无法撤销。主人反复重做同一影片 5+ 次。

钉两件事：
  (a) 草稿 key 真的被写 / 被读（不是只有一堆没接线的函数）；
  (b) newProject() 不再静默销毁非空稿子 —— 用户取消时一个字都不许动。

做法：不从 Python 里假装浏览器，而是把 desktop.js 里**真实的源码块**
（草稿层 + newProject / newProjectReset）抽出来，塞进 stub DOM +
内存版 localStorage，用 node 真跑一遍行为再回读可观察事实。
纯静态断言只能证明「字面量还在文件里」，证明不了「接线正确」，
而这里要钉的正是接线（例如 input 事件有没有真的绑上、取消分支真的会 return）。

为什么脚本走 stdin 而不是临时文件：本仓库的沙箱对 %TEMP% 下的 pytest
临时目录会 PermissionError（正是门禁必须用 .dsh_tools/run_tests.py 的原因），
`node -` 读 stdin 完全绕开临时文件。

非空转证明（本文件对修复前的代码必定变红）：
  见文件末尾 test_revert_proof_* 与 __main__ 段说明。
"""

import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(ROOT, 'static', 'desktop')
JS_PATH = os.path.join(DESKTOP, 'desktop.js')
HTML_PATH = os.path.join(DESKTOP, 'index.html')

NODE = shutil.which('node')

DRAFT_KEY = 'framecut_desktop_draft_v1'


def _load(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def _strip_js_comments(js):
    js = re.sub(r'/\*[\s\S]*?\*/', '', js)
    js = re.sub(r'(?m)^\s*//.*$', '', js)
    return js


# ─────────────────────────────────────────────────────────────
# node 行为夹具
# ─────────────────────────────────────────────────────────────
HARNESS = r'''
const fs = require('fs');
const jsPath = process.env.FC_JS;
const scenario = process.env.FC_SCENARIO;
let raw = fs.readFileSync(jsPath, 'utf8');

/* ── 变异支持（非空转证明）──
   FC_MUTATE=prefix 时在当前源码上把「草稿层 + 确认式 newProject」还原成修复前的样子，
   用来证明本文件的断言**确实**对旧行为变红。用变异而不是 git ref：
   git ref 会随 Lead 提交而指向已修复版本，那样的证明会自己消失。 */
if(process.env.FC_MUTATE === 'prefix'){
  // 注意：本文件是 CRLF，行尾必须用 \r?\n 匹配，否则替换会静默失效、
  // 「修复前必红」的证明就变成了假绿。
  raw = raw.replace(/const DRAFT_KEY[\s\S]*?const _draftRestored = draftRestore\(\);\r?\n/, '');
  const PREFIX_NEWPROJECT = [
    'function newProject(){',
    '  _pushUndo();',
    "  _items = []; _runDir = ''; _videoFile = ''; _vdur = 0; _sel = -1; _curSeg = -1;",
    '  _narrEls = {};',
    "  document.querySelectorAll('audio[id^=\"narr-\"]').forEach(a => a.remove());",
    '  _video = null;',
    "  $('projName').textContent = '未打开项目';",
    "  $('tbGenerate').disabled = false;",
    "  $('tbCompose').disabled = true;",
    '  showEditor(false);',
    "  switchView('media');",
    "  $('npGo').disabled = false;",
    "  $('npPickLabel').textContent = '从电脑选择视频';",
    "  $('npPick').classList.remove('has');",
    "  const ep = $('exportPanel'); if(ep) ep.style.display = 'none';",
    "  const vs = $('videoShell'); if(vs) vs.style.display = '';",
    "  $('npPlot').value = '';",
    "  setStatus('就绪');",
    '}',
  ].join('\n');
  // 同理，这里也必须容忍 CRLF，否则整段还原会静默不生效。
  raw = raw.replace(
    /async function newProject\(\)\{[\s\S]*?\r?\nfunction newProjectReset\(\)\{[\s\S]*?\r?\n\}\r?\n/,
    PREFIX_NEWPROJECT + '\n');
  if(raw.includes('newProjectReset')) throw new Error('变异失败：newProjectReset 仍在（CRLF 没匹配上？）');
}
if(process.env.FC_MUTATE === 'npscript'){
  /* 定点变异：只把 npScript 那一半还原成「修之前」的样子 —— 草稿层照旧存在，
     但复选框既不存也不回填。用来单独证明「勾选必须与稿子同生共死」那几条
     断言确实是非空转（用 prefix 变异证明不了这一点：它把整层删了，场景直接抛错，
     红的原因变成了「没有草稿层」而不是「没回填勾选」）。

     用花括号配对整块删（而不是正则）：DRAFT_CHECKS 的三个用处形状不同
     —— 一行式、多行块、多行块各一个，正则很容易只匹配上其中一个而静默半生效。
     下面 leftover 自检就是为了让「半生效」当场炸掉而不是变假绿。 */
  const kept = [];
  const src = raw.split(/\r?\n/);
  for(let i = 0; i < src.length; i++){
    const line = src[i];
    if(line.includes('DRAFT_CHECKS') && !line.includes('const DRAFT_CHECKS')){
      // 跳过整块：从这一行开始配对花括号，depth 归零即块结束
      let depth = 0, started = false;
      for(; i < src.length; i++){
        for(const ch of src[i]){
          if(ch === '{'){ depth++; started = true; } else if(ch === '}'){ depth--; }
        }
        if(started && depth === 0) break;
      }
      continue;
    }
    kept.push(line);
  }
  raw = kept.join('\n');
  const leftover = (raw.match(/DRAFT_CHECKS/g) || []).length;
  if(leftover !== 1) throw new Error('变异失败：DRAFT_CHECKS 还剩 ' + leftover + ' 处（应为 1，即只剩声明）');
  if(!raw.includes('const DRAFT_CHECKS = ')) throw new Error('变异失败：把声明也删了，那测的就不是这条缺陷');
}

const lines = raw.split(/\r?\n/);

function idxStartingWith(prefix){
  const i = lines.findIndex(l => l.startsWith(prefix));
  if(i < 0) throw new Error('找不到源码块: ' + prefix);
  return i;
}
function idxMatching(re){
  const i = lines.findIndex(l => re.test(l));
  if(i < 0) throw new Error('找不到源码块: ' + re);
  return i;
}
// 草稿层：从 DRAFT_KEY 常量声明到加载时回填那一行。
// 变异版（修复前）没有这一层 —— 用空块，让 newProject 照常跑。
const dStart = lines.findIndex(l => l.startsWith('const DRAFT_KEY'));
let draftBlock = '';
if(dStart >= 0){
  let dEnd = -1;
  for(let i = dStart; i < lines.length; i++){
    if(lines[i].startsWith('const _draftRestored = draftRestore();')){ dEnd = i; break; }
  }
  if(dEnd < 0) throw new Error('找到草稿层起始但没有结束行（draftRestore 调用）');
  draftBlock = lines.slice(dStart, dEnd + 1).join('\n');
}

// newProject (+ newProjectReset 若存在)：花括号配对。
// 修复前的版本只有 newProject、没有 Reset，所以 Reset 是「可选续块」。
function endOfFunction(startIdx){
  let depth = 0, started = false;
  for(let i = startIdx; i < lines.length; i++){
    for(const ch of lines[i]){
      if(ch === '{'){ depth++; started = true; } else if(ch === '}'){ depth--; }
    }
    if(started && depth === 0) return i;
  }
  return -1;
}
const nStart = idxMatching(/^(async )?function newProject\(\)\{/);
const nEnd = endOfFunction(nStart);
if(nEnd < 0) throw new Error('找不到 newProject 结束行');
let npBlock = lines.slice(nStart, nEnd + 1).join('\n');
const rStart = lines.findIndex(l => l.startsWith('function newProjectReset('));
if(rStart >= 0){
  const rEnd = endOfFunction(rStart);
  if(rEnd < 0) throw new Error('找不到 newProjectReset 结束行');
  npBlock += '\n' + lines.slice(rStart, rEnd + 1).join('\n');
}

// ── stub 环境 ──
function makeEl(id){
  const o = { id: id, disabled: false, style: {}, checked: false, _text: '', _h: {} };
  let v = '';
  Object.defineProperty(o, 'value', {
    get: () => v,
    set: x => { v = (x === undefined || x === null) ? '' : String(x); },
    enumerable: true });
  Object.defineProperty(o, 'textContent', {
    get: () => o._text, set: x => { o._text = String(x); }, enumerable: true });
  o.classList = { add(){}, remove(){}, contains(){ return false; } };
  o.addEventListener = (t, fn) => { (o._h[t] = o._h[t] || []).push(fn); };
  o.fire = t => { (o._h[t] || []).forEach(fn => fn({ target: o, type: t })); };
  return o;
}

// 种子分两份，必须分开：
//   STORE_SEED = localStorage 里「上次存过的草稿」
//   ELS_SEED   = 页面上「一开始就填着的内容」（用户手打的）
// restore 场景必须 ELS 为空、STORE 有值，否则草稿回填坏了测试也会假绿。
const STORE_SEED = {
  restore_on_load: { npPlot: '这是上次写了一半的稿子', npName: '惩罚者' },
  // 勾了「完整解说稿」的稿子：恢复时必须**连勾一起**回来，
  // 否则同一段文字会被静默当成剧情梗概重写（「文字不改」的承诺失效）。
  restore_script_on_load: { npPlot: '整篇已经写好的解说稿正文', npName: '惩罚者', npScript: true },
  clear_cancel:    { npPlot: '深夜的港口，一艘货船悄悄离岸……', npName: '惩罚者 第一季第一集', npScript: true },
  clear_confirm:   { npPlot: '深夜的港口，一艘货船悄悄离岸……', npName: '惩罚者 第一季第一集', npScript: true },
  // 只勾了复选框、稿子是空的（脏数据 / 用户勾了又清空）→ 不算「有内容」
  blank_checkbox_only: { npScript: true },
  debounce_cancelled_by_clear: {},
};
const ELS_SEED = {
  clear_cancel:  { npPlot: '深夜的港口，一艘货船悄悄离岸……', npName: '惩罚者 第一季第一集', npScript: true },
  clear_confirm: { npPlot: '深夜的港口，一艘货船悄悄离岸……', npName: '惩罚者 第一季第一集', npScript: true },
};

function build(){
  const store = new Map();
  const els = {};
  const ss = STORE_SEED[scenario];
  if(ss && Object.keys(ss).length) store.set('framecut_desktop_draft_v1', JSON.stringify(ss));
  const es = ELS_SEED[scenario] || {};
  // newProjectReset 会碰到的元素，先一次性建好
  ['projName','tbGenerate','tbCompose','npGo','npPickLabel','npPick',
   'exportPanel','videoShell','npScript','npPlot','npName'].forEach(k => {
    els[k] = makeEl(k);
  });
  els.npScript.type = 'checkbox';   // 模拟真实 <input type="checkbox">
  // 布尔值按 checked 种进页面（勾选框），字符串按 value（文本框）
  Object.entries(es).forEach(([k, v]) => {
    if(typeof v === 'boolean') els[k].checked = v;
    else els[k].value = v;
  });

  const calls = { uiConfirm: [], setStatus: [], switchView: [], showEditor: [],
                  updateNewProj: 0 };
  // 这里的答案必须是**布尔**，严格照抄真实 uiConfirm 的契约：
  //   async function uiConfirm(...){ return !!(await presetModal('confirm', ...)); }
  // 取消 → false。若这里返回字符串 'cancel'（truthy），newProject 的
  // `if(!keep) return;` 就会当成「确认」，测试会漏掉取消分支 —— 这正是本用例要钉的东西。
  let confirmAnswer = true;

  const prologue = `
    /* desktop.js 依赖的宿主环境桩：$ / localStorage / document。
       $ 按需创建元素，模拟 index.html 里本就存在的那些 id。 */
    const $ = id => (els[id] = els[id] || newEl(id));
    const localStorage = {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { store.set(k, String(v)); },
      removeItem: k => { store.delete(k); },
    };
    const document = { querySelectorAll: () => [] };
    let _draftTimer = null;
    let _items = [], _sel = -1, _curSeg = -1, _runDir = '', _videoFile = '', _vdur = 0;
    let _narrEls = {}, _video = null, _srt = null;
    function _pushUndo(){}
    function _renderLastMedia(){}
    function setBtnIcon(){}
    function showEditor(on){ __calls.showEditor.push(on); }
    function switchView(v){ __calls.switchView.push(v); }
    function setStatus(s, kind){ __calls.setStatus.push(s); }
    function updateNewProj(){ __calls.updateNewProj++; }
    async function uiConfirm(title, detail, okText){
      __calls.uiConfirm.push({ title: title, detail: detail, okText: okText });
      return __confirmAnswer();
    }
    `;

  const runner = `
    const snap = () => ({ npPlot: els.npPlot.value, npName: els.npName.value,
                          npScript: els.npScript.checked });
    const key = () => store.get('framecut_desktop_draft_v1') || null;

    if(scenario === 'save_and_read'){
      els.npPlot.value = '一段很长的剧情稿';
      els.npPlot.fire('input');
      els.npName.value = '惩罚者';
      els.npName.fire('input');
      await new Promise(r => setTimeout(r, DRAFT_DEBOUNCE_MS + 250));
      return { scenario: scenario, raw: key(), snapshot: snap() };
    }
    if(scenario === 'save_checkbox_only'){
      // 只勾复选框、稿子空 → 不该写草稿（否则下次打开冒出「有内容」的假象）
      els.npScript.checked = true;
      els.npScript.fire('change');
      await new Promise(r => setTimeout(r, DRAFT_DEBOUNCE_MS + 250));
      return { scenario: scenario, raw: key() };
    }
    if(scenario === 'save_with_checkbox'){
      els.npPlot.value = '整篇写好的解说稿';
      els.npPlot.fire('input');
      els.npScript.checked = true;
      els.npScript.fire('change');
      await new Promise(r => setTimeout(r, DRAFT_DEBOUNCE_MS + 250));
      return { scenario: scenario, raw: key(), snapshot: snap() };
    }
    if(scenario === 'restore_on_load'){
      return { scenario: scenario, restored: _draftRestored, snapshot: snap(),
               updateNewProjCalls: __calls.updateNewProj };
    }
    if(scenario === 'restore_script_on_load'){
      return { scenario: scenario, restored: _draftRestored, snapshot: snap() };
    }
    if(scenario === 'blank_checkbox_only'){
      // 脏草稿：只有勾、没有稿子 → 不该回填（draftHasContent 只认文本）
      return { scenario: scenario, restored: _draftRestored, snapshot: snap() };
    }
    if(scenario === 'no_content_no_key'){
      els.npPlot.value = ''; els.npName.value = '';
      draftSave();
      return { scenario: scenario, raw: key() };
    }
    if(scenario === 'clear_field_removes_key'){
      els.npPlot.value = '先写点东西';
      draftSave();
      const before = key();
      els.npPlot.value = '';
      draftSave();
      return { scenario: scenario, before: before, after: key() };
    }
    if(scenario === 'clear_cancel'){
      __setConfirm(false);
      const before = snap(), beforeKey = key();
      await newProject();
      return { scenario: scenario, before: before, after: snap(),
               beforeKey: beforeKey, afterKey: key(),
               uiConfirm: __calls.uiConfirm.length,
               uiConfirmTitle: __calls.uiConfirm[0] ? __calls.uiConfirm[0].title : null,
               projName: els.projName.textContent };
    }
    if(scenario === 'clear_confirm'){
      __setConfirm(true);
      draftSave();
      await newProject();
      return { scenario: scenario, after: snap(), afterKey: key(),
               uiConfirm: __calls.uiConfirm.length,
               projName: els.projName.textContent };
    }
    if(scenario === 'blank_new_no_prompt'){
      __setConfirm(false);   // 若真弹框且被取消，就什么都清不掉 → 断言能抓到
      await newProject();
      return { scenario: scenario, uiConfirm: __calls.uiConfirm.length,
               projName: els.projName.textContent };
    }
    if(scenario === 'debounce_cancelled_by_clear'){
      els.npPlot.value = '刚敲完就点新建';
      els.npPlot.fire('input');       // 挂上一个 400ms 的写入定时器
      draftClear();                   // 用户确认清空
      await new Promise(r => setTimeout(r, DRAFT_DEBOUNCE_MS + 300));
      return { scenario: scenario, raw: key() };
    }
    if(scenario === 'clear_confirm_then_reopen'){
      // 完整走一遍真实序列：点新建 → 确认清空 → 关窗口 → 重开（再跑一次 draftRestore）
      __setConfirm(true);
      await newProject();
      const afterClear = snap();
      const restoredAgain = draftRestore();
      return { scenario: scenario, afterClear: afterClear,
               restoredAgain: restoredAgain, afterReopen: snap(), afterKey: key() };
    }
    throw new Error('未知 scenario: ' + scenario);
    `;

  const src = [prologue, draftBlock, npBlock,
    'return (async () => {\n' + runner + '\n})();'].join('\n');

  const fn = new Function('els', 'store', '__calls', '__confirmAnswer', '__setConfirm',
    'scenario', 'newEl', src);
  return { fn: fn, els: els, store: store, calls: calls,
           setAnswer: a => { confirmAnswer = a; },
           getAnswer: () => confirmAnswer };
}

const ctx = build();
const res = ctx.fn(ctx.els, ctx.store, ctx.calls, () => ctx.getAnswer(),
                   a => ctx.setAnswer(a), scenario, makeEl);
Promise.resolve(res).then(r => {
  r.DRAFT_KEY = 'framecut_desktop_draft_v1';
  console.log(JSON.stringify(r));
}).catch(e => {
  // 不用非零退出码：夹具内抛错也是一种**可断言的结果**
  // （「还原成修复前版本后该场景直接跑不通」正是证明的一部分），
  // 所以照样打印 JSON，由 Python 侧判断。
  console.log(JSON.stringify({ error: String((e && e.message) || e) }));
});
'''


def _run_harness(scenario, mutate=None):
    """在 stub DOM 里真跑 desktop.js 的草稿层 + newProject，回读可观察事实。

    mutate='prefix' 时先在内存里把源码还原成修复前的样子（只在内存里改，
    不落任何文件），用于「修复前必红」的非空转证明。
    """
    if not NODE:
        pytest.skip('未找到 node，跳过桌面版行为回归')
    env = dict(os.environ)
    env['FC_JS'] = JS_PATH
    env['FC_SCENARIO'] = scenario
    if mutate:
        env['FC_MUTATE'] = mutate
    p = subprocess.run([NODE, '-'], input=HARNESS, capture_output=True, text=True,
                       encoding='utf-8', env=env, timeout=90,
                       cwd=str(ROOT))
    assert p.returncode == 0, 'node 夹具失败 rc=%s\nstdout=%s\nstderr=%s' % (
        p.returncode, (p.stdout or '')[-2000:], (p.stderr or '')[-2000:])
    return json.loads(p.stdout.strip().splitlines()[-1])


def _harness_result(scenario, mutate=None):
    """跑夹具并返回 (结果, 是否正常跑通)。修复前的版本没有草稿层，
    某些场景会直接抛错 —— 那也算「红」。"""
    d = _run_harness(scenario, mutate=mutate)
    return d, ('error' not in d)


# ─────────────────────────────────────────────────────────────
# (a) 草稿 key 真的被写 / 被读
# ─────────────────────────────────────────────────────────────
def test_typing_writes_draft_key():
    """在剧情稿/片名里打字 → debounce 后草稿真的落进 localStorage。"""
    d = _run_harness('save_and_read')
    assert d['raw'], '打字后草稿 key 没有落盘（自动保存没接线）'
    saved = json.loads(d['raw'])
    assert saved['npPlot'] == '一段很长的剧情稿'
    assert saved['npName'] == '惩罚者'


def test_draft_restored_on_load():
    """localStorage 里有草稿 → 页面加载自动回填进输入框。"""
    d = _run_harness('restore_on_load')
    assert d['restored'] is True, '加载时没有回填草稿'
    assert d['snapshot']['npPlot'] == '这是上次写了一半的稿子'
    assert d['snapshot']['npName'] == '惩罚者'
    assert d['updateNewProjCalls'] >= 1, '回填后没刷新「开始生成」按钮态'


def test_empty_draft_writes_no_key():
    """两边都空 → 不写空草稿（否则下次打开又冒出空壳）。"""
    d = _run_harness('no_content_no_key')
    assert d['raw'] is None, '空内容也写了草稿 key'


def test_user_clearing_fields_removes_key():
    """用户自己把稿子删光 → 草稿 key 跟着删，不留幽灵稿。"""
    d = _run_harness('clear_field_removes_key')
    assert d['before'], '前置条件失败：先写的内容没落盘'
    assert d['after'] is None, '用户清空后草稿 key 仍在'


# ── npScript：「完整解说稿」勾选必须与稿子同生共死 ──
# 它决定同一段 npPlot 文本的语义（勾上 = 一字不改的成稿；不勾 = 让 AI 写解说）。
# 只回填文字不回填勾选 = 恢复出自相矛盾的半份状态，主人看不出来。
def test_script_flag_restored_with_plot():
    """勾了「完整解说稿」的稿子，重开后勾必须还在 —— 否则整篇成稿被静默重写。"""
    d = _run_harness('restore_script_on_load')
    assert d['restored'] is True, '前置条件失败：草稿根本没回填'
    assert d['snapshot']['npPlot'] == '整篇已经写好的解说稿正文'
    assert d['snapshot']['npScript'] is True, \
        '恢复出「有文字、没勾选」的半份状态：整篇稿子会被当成剧情梗概重写'


def test_script_flag_saved_as_boolean_not_on():
    """复选框必须按 checked 存布尔，不能走 el.value（那会永远存成字符串 'on'）。"""
    d = _run_harness('save_with_checkbox')
    raw = json.loads(d['raw'])
    assert raw['npScript'] is True, 'npScript 没按布尔存下来（拿到 %r）' % (raw['npScript'],)
    assert raw['npPlot'] == '整篇写好的解说稿'


def test_checkbox_alone_is_not_content():
    """只勾复选框、稿子是空的 → 不写草稿（空白零打扰不能被一个勾破坏）。"""
    d = _run_harness('save_checkbox_only')
    assert d['raw'] is None, '只有勾、没有稿子也写了草稿 key'


def test_checkbox_only_stale_draft_is_not_restored():
    """脏草稿（只有勾、没有文本）不该被当成「有内容」回填。"""
    d = _run_harness('blank_checkbox_only')
    assert d['restored'] is False, '只有勾的脏草稿被当成有内容回填了'


def test_clear_then_reopen_stays_empty():
    """点新建 → 确认清空 → 关窗口 → 重开：必须恢复成全空（稿子没、勾也没）。

    守住 Lead 提醒的那条：draftClear() 之后不能再有任何路径把草稿写回去
    —— 包括 newProjectReset() 里 `checked = false` 这次程序赋值（它不发 change 事件）。
    """
    d = _run_harness('clear_confirm_then_reopen')
    assert d['afterClear']['npPlot'] == ''
    assert d['afterClear']['npScript'] is False, '清空后复选框没复位'
    assert d['afterKey'] is None, '清空后草稿 key 还在'
    assert d['restoredAgain'] is False, '重开居然又从草稿恢复出东西'
    assert d['afterReopen']['npPlot'] == '', '重开后稿子又冒出来了'
    assert d['afterReopen']['npScript'] is False, '重开后复选框又被勾上了'


# ─────────────────────────────────────────────────────────────
# (b) newProject 不再静默销毁非空稿子
# ─────────────────────────────────────────────────────────────
def test_newproject_asks_before_destroying():
    """框里有内容时点「新建」必须先问一句，且问的是页面内 uiConfirm。"""
    d = _run_harness('clear_cancel')
    assert d['uiConfirm'] == 1, '有新内容时 newProject 没有弹确认框（仍是静默销毁）'
    assert d['uiConfirmTitle'], '确认框没有标题'
    assert '剧情稿' in d['uiConfirmTitle'], '确认框标题没点明会清空剧情稿'


def test_newproject_cancel_keeps_everything():
    """用户点「取消」→ 稿子、片名、草稿 key、项目状态一个字都不许动。"""
    d = _run_harness('clear_cancel')
    assert d['after']['npPlot'] == d['before']['npPlot'], '取消后剧情稿被改动了'
    assert d['after']['npName'] == d['before']['npName'], '取消后片名被改动了'
    assert d['after']['npScript'] == d['before']['npScript'], '取消后「完整解说稿」勾选被改动了'
    assert d['afterKey'] and d['afterKey'] == d['beforeKey'], '取消后草稿被删了'
    assert d['projName'] != '未打开项目', '取消后却执行了清空（项目被重置）'


def test_newproject_confirm_really_clears():
    """用户确认「清空并新建」→ 才真的清空：输入框清空 + 草稿 key 删除 + 项目重置。
    （守住「点新建时期望的确实是清空」这个老手感，不能改成永不删。）"""
    d = _run_harness('clear_confirm')
    assert d['uiConfirm'] == 1
    assert d['after']['npPlot'] == ''
    assert d['after']['npName'] == ''
    assert d['after']['npScript'] is False, '确认清空后「完整解说稿」勾选没复位'
    assert d['afterKey'] is None, '确认清空后草稿 key 还在'
    assert d['projName'] == '未打开项目', '确认后项目状态没重置'


def test_newproject_blank_is_zero_friction():
    """空白时点「新建」不打扰用户：不弹框，直接清（一键清空的老手感）。
    夹具这里把确认答案设成 cancel：若仍然弹了框，项目就重置不掉，断言立刻抓到。"""
    d = _run_harness('blank_new_no_prompt')
    assert d['uiConfirm'] == 0, '空白时也弹了确认框，平白多一步'
    assert d['projName'] == '未打开项目', '空白时新建没有直接清空'


def test_pending_debounce_cannot_resurrect_draft():
    """刚敲完 400ms 内点新建清空 → 挂着的 debounce 不能把旧稿写回去。"""
    d = _run_harness('debounce_cancelled_by_clear')
    assert d['raw'] is None, '清空后挂起的 debounce 又把旧稿写回了 localStorage'


# ─────────────────────────────────────────────────────────────
# (c) 静态契约（便宜的守门，防以后被人改回去）
# ─────────────────────────────────────────────────────────────
def test_draft_key_and_fields_declared():
    js = _load(JS_PATH)
    assert "const DRAFT_KEY = '%s';" % DRAFT_KEY in js
    assert "const DRAFT_TEXT_IDS = ['npPlot', 'npName'];" in js
    assert "const DRAFT_CHECKS = ['npScript'];" in js


def test_non_obvious_fields_are_excluded():
    """宁少勿滥：会话瞬态字段和模态输入框不进草稿。"""
    js = _load(JS_PATH)
    m = re.search(r"const DRAFT_TEXT_IDS = \[([^\]]*)\]", js)
    assert m, '找不到 DRAFT_TEXT_IDS'
    ids = re.findall(r"'([^']+)'", m.group(1))
    for bad in ('coverTitle', 'coverSub', 'musicQ',   # 每次打开相关流程都被覆盖/清空
                'cloneName', 'presetNameInput',        # 模态输入框
                'npCut', 'npBgm', 'npTier'):           # 生成参数，已由 ADV_IDS/ADV_CHECKS 记忆
        assert bad not in ids, '%s 不该进草稿表（见 desktop.js 注释里的排除理由）' % bad
    # npScript 反过来：它必须进草稿（见 test_script_flag_restored_with_plot）。
    # 它长得像 npCut/npBgm，但语义完全不同 —— 它决定 npPlot 那段文字的含义。
    assert 'npScript' not in ids, \
        'npScript 是复选框，不该进「文本」表（会永远存成 "on"）—— 应进 DRAFT_CHECKS'
    mc = re.search(r"const DRAFT_CHECKS = \[([^\]]*)\]", js)
    assert mc, '找不到 DRAFT_CHECKS'
    assert 'npScript' in re.findall(r"'([^']+)'", mc.group(1)), \
        'npScript 必须在 DRAFT_CHECKS 里，否则恢复出「有文字、没勾选」的半份状态'


def test_npscript_is_not_covered_by_adv_save():
    """钉住「npScript 此前根本没有任何持久化」这个事实。

    排除它的旧理由「已由 ADV_CHECKS 记忆」是错的：ADV_ALL = ADV_IDS.concat(ADV_CHECKS)，
    而 npScript 两个表都不在。所以它既不是「已有真相来源」，也不能借 ADV 表恢复。
    哪天有人把 npScript 加进 ADV_* 表，这条会红 —— 那时要重新想清楚是不是两处都存。
    """
    js = _load(JS_PATH)
    def _list(name):
        m = re.search(r"const %s = \[([^\]]*)\]" % name, js)
        assert m, '找不到 %s' % name
        return re.findall(r"'([^']+)'", m.group(1))
    adv = set(_list('ADV_IDS')) | set(_list('ADV_CHECKS'))
    assert 'npScript' not in adv, (
        'npScript 居然进了 ADV 表：它与 npPlot 的语义绑定，必须随草稿走；'
        '若两处都存会出现两个真相来源，需要重新设计')


def test_newproject_uses_uiconfirm_not_native():
    """必须走页面内 uiConfirm；原生 confirm 在 pywebview 里会静默失败（v1.4.1 事故）。"""
    js = _strip_js_comments(_load(JS_PATH))
    start = js.index('async function newProject(){')
    end = js.index('function newProjectReset(){')
    block = js[start:end]
    assert 'uiConfirm(' in block, 'newProject 没有确认环节'
    assert 'draftClear()' in block, 'newProject 没有销毁草稿'
    assert 'await uiConfirm(' in block, 'uiConfirm 必须 await，否则确认结果丢失'
    for native in (r'(?<![\w.])confirm\s*\(', r'(?<![\w.])prompt\s*\(', r'(?<![\w.])alert\s*\('):
        assert re.search(native, block) is None, 'newProject 里出现原生对话框：%s' % native


def test_guarded_dom_access_for_new_listeners():
    """坑 3：新增的顶层 DOM 绑定必须守卫式，否则该行之后顶层代码静默死亡。"""
    js = _load(JS_PATH)
    assert "if(el) el.addEventListener('input', draftScheduleSave);" in js
    assert 'const el = $(id); if(el) v[id] = el.value;' in js


def test_input_event_is_debounced_not_change():
    """textarea 只绑 change 的话，写完直接关窗口就白写了 —— 必须绑 input。"""
    js = _load(JS_PATH)
    assert "el.addEventListener('input', draftScheduleSave)" in js
    assert 'DRAFT_DEBOUNCE_MS = 400;' in js
    assert 'setTimeout(() => { _draftTimer = null; draftSave(); }, DRAFT_DEBOUNCE_MS)' in js


def test_protected_dom_ids_still_exist():
    """只加不删：草稿涉及的 DOM id 一个都不能少。"""
    html = _load(HTML_PATH)
    for i in ('npPlot', 'npName', 'npScript', 'npGo', 'npPick'):
        assert ('id="%s"' % i) in html, 'index.html 少了 id=%s' % i


def test_cache_version_bumped_and_equal():
    """改了前端就必须 bump `?v=N`，且 js/css 两个数字必须相等。"""
    html = _load(HTML_PATH)
    js_v = re.search(r'desktop\.js\?v=(\d+)', html)
    css_v = re.search(r'desktop\.css\?v=(\d+)', html)
    assert js_v and css_v, 'index.html 缺 ?v=N 缓存戳'
    assert js_v.group(1) == css_v.group(1), (
        'desktop.js?v=%s 与 desktop.css?v=%s 版本号不一致' % (js_v.group(1), css_v.group(1)))
    assert int(js_v.group(1)) >= 14, (
        '本轮改过 static/desktop/，缓存戳必须 >= 14（实际 %s），否则 EXE 里仍加载旧 JS'
        % js_v.group(1))


# ─────────────────────────────────────────────────────────────
# (d) 非空转证明：把源码还原成「修复前」的样子，断言必须变红
#
# 没有这一组，上面那些断言有可能只是「测试写得太松所以永远绿」。
# 这里在内存里把草稿层整段删掉、把 newProject 还原成无条件清空的旧版
# （不落任何文件），然后要求「旧行为」被本文件的断言判定为坏。
# ─────────────────────────────────────────────────────────────
def test_proof_old_newproject_silently_destroys():
    """旧版 newProject 无条件清空 → 本文件的「必须先问一句」断言会红。"""
    d, ok = _harness_result('clear_cancel', mutate='prefix')
    assert ok, '前置条件失败：还原成修复前版本后夹具本身就崩了，证明不了什么'
    # 旧版行为：没有确认框，直接清掉 —— 这就是「误按 Ctrl+N 稿子当场蒸发」
    assert d['uiConfirm'] == 0, '还原版居然弹了确认框，证明脚本没生效'
    assert d['after']['npPlot'] == '', '还原版居然没清空，证明脚本没生效'
    assert d['projName'] == '未打开项目'
    # 旧版**只**清 npPlot，片名留着。所以这里断言的是「片名还在」：
    # 「新建时把片名一起清」是本次**新增**的行为（否则「查剧情」还留旧片名），
    # 不能反过来要求旧版也具备 —— 那会把新做的改进误判成旧版应有的行为。
    assert d['after']['npName'] == d['before']['npName'], \
        '旧版居然动了片名，说明变异没还原干净'
    # 这三条正是本文件要求的「好行为」，对旧版全部不成立 → 断言非空转
    assert d['after']['npPlot'] != d['before']['npPlot'], \
        '旧版确实销毁了非空稿子 —— 这正是本用例要防的回归'
    # 旧版没有草稿层，于是预置的草稿 key 被留成**孤儿**：字段已空、key 还宣称有内容。
    # （修复后 draftClear() 会把它删掉，见 test_newproject_confirm_really_clears。）
    assert d['afterKey'] is not None, \
        '还原版居然把草稿 key 也删了，说明变异没还原干净'


def test_proof_old_code_has_no_draft_layer():
    """旧版根本没有草稿层 → 自动保存与回填两类断言都会红（场景直接抛错）。"""
    for scen in ('save_and_read', 'restore_on_load'):
        d, ok = _harness_result(scen, mutate='prefix')
        assert not ok, '还原成修复前版本后 %s 竟然跑通了，说明该场景没测到草稿层' % scen


def test_proof_npscript_regression_is_caught():
    """定点证明：只把 npScript 的存取去掉（草稿层其余照旧），
    「勾选必须与稿子同生共死」那几条断言必须变红 —— 否则它们对本次修复毫无鉴别力。

    为什么不用 prefix 变异：那个把整层删了，场景直接抛错，
    红的原因变成「没有草稿层」，掩盖了「没回填勾选」这个具体缺陷。
    """
    # ① 存储侧：勾选没落进草稿
    d, ok = _harness_result('save_with_checkbox', mutate='npscript')
    assert ok, '定点变异后夹具本身崩了（证明不了什么）'
    raw = json.loads(d['raw'])
    assert 'npScript' not in raw, \
        '变异版居然还存了 npScript，说明定点变异没生效（那下面的红就不是这条缺陷引起的）'

    # ② 回填侧：勾了「完整解说稿」的稿子，重开后勾丢了 —— 正是要被抓住的半份状态
    d2, ok2 = _harness_result('restore_script_on_load', mutate='npscript')
    assert ok2, '定点变异后夹具本身崩了（证明不了什么）'
    assert d2['snapshot']['npPlot'] == '整篇已经写好的解说稿正文', \
        '变异版连文字都没回填，说明红的原因不对'
    assert d2['snapshot']['npScript'] is False, \
        '变异版居然把勾也恢复了，说明定点变异没生效'

    # ③ 对照组：同一批场景跑当前代码必须是绿的（否则「红」可能只是夹具坏了）
    for scen in ('save_with_checkbox', 'restore_script_on_load'):
        _, ok3 = _harness_result(scen)
        assert ok3, '当前代码在场景 %s 下没跑通' % scen


def test_proof_current_code_is_green():
    """对照：同一批场景跑当前代码必须全绿（否则上面的「红」可能只是因为夹具坏了）。"""
    for scen in ('save_and_read', 'restore_on_load', 'restore_script_on_load',
                 'save_with_checkbox', 'save_checkbox_only', 'blank_checkbox_only',
                 'clear_cancel', 'clear_confirm', 'clear_confirm_then_reopen',
                 'blank_new_no_prompt', 'debounce_cancelled_by_clear',
                 'no_content_no_key', 'clear_field_removes_key'):
        _, ok = _harness_result(scen)
        assert ok, '当前代码在场景 %s 下没跑通' % scen


if __name__ == '__main__':
    """
    手工核对用：
        & $py .dsh_tools\\run_tests.py tests/test_desktop_draft_persist.py -q
    预期 19 passed。

    非空转证明（无需 git，也不落临时文件）：
        FC_MUTATE=prefix 时夹具会把 desktop.js 里
        「草稿层 + 确认式 newProject」在内存中还原成修复前版本。
        此时 test_proof_old_* 要求的「红」正好与实测一致：
        - clear_cancel 场景 uiConfirm==0、npPlot 被清空、草稿 key 被删；
        - save_and_read / restore_on_load 因找不到草稿层直接抛错。
        若哪天这些「坏行为」不再被判红，本文件的其余断言就失去了意义。
    """
    pass

/**
 * 任务1 撤销/重做 —— 纯逻辑回归测试
 * 从 static/app.js 中抽取 undo 模块源码（行 3291 到 _onSegFieldChange 结束），
 * 在 stub DOM 环境里验证撤销栈行为，不改动 app.js 本身。
 * 运行：node tests/test_undo_stack.js
 */
const fs = require('fs');
const path = require('path');

const appPath = path.join(__dirname, '..', 'static', 'app.js');
const lines = fs.readFileSync(appPath, 'utf8').split(/\r?\n/);

const start = lines.findIndex(l => l.startsWith('let _adjustState = {'));
if (start < 0) throw new Error('找不到 undo 模块起始行');
let end = -1;
for (let i = start; i < lines.length; i++) {
  if (lines[i].startsWith('function _onSegFieldChange')) {
    for (let j = i + 1; j < lines.length; j++) {
      if (lines[j] === '}') { end = j; break; }
    }
    break;
  }
}
if (end < 0) throw new Error('找不到 undo 模块结束行');
const src = lines.slice(start, end + 1).join('\n');

// ---- stub 环境 ----
// EL 模拟页面上真实存在的输入框；renderAdjustPanel 重建列表时会重新生成它们
let EL = {};
// 模拟浏览器 input.value：赋值任何类型都会被转成字符串
function newEl() {
  const o = { disabled: false, style: {}, textContent: '', title: '' };
  let v = '';
  Object.defineProperty(o, 'value', {
    get: () => v,
    set: x => { v = (x === undefined || x === null) ? '' : String(x); },
    enumerable: true
  });
  return o;
}
global.document = {
  getElementById: id => (EL[id] || (EL[id] = newEl())),
  querySelectorAll: sel => {
    if (sel.indexOf('.adjust-item') >= 0) {
      return { length: (global.__items ? global.__items.length : 0) };
    }
    return { length: 0 };
  },
  addEventListener: () => {}
};
// 模拟 renderAdjustPanel 重建列表：回写 items，并按 items 重新生成输入框 DOM
function syncEL(items) {
  EL = {};
  items.forEach((it, i) => {
    EL['adjText' + i] = Object.assign(newEl(), { value: it.text });
    EL['adjVStart' + i] = Object.assign(newEl(), { value: String(it.video_start) });
    EL['adjVEnd' + i] = Object.assign(newEl(), { value: String(it.video_end) });
  });
}
function renderAdjustPanel(items) {
  global.__items = JSON.parse(JSON.stringify(items));
  global.__adjustStateRef.items = global.__items;   // 真实 renderAdjustPanel 会重建 _adjustState
  syncEL(global.__items);
}
function renderTimeline() {}
function scheduleAutoSave() {}

// ---- 载入被测代码 ----
const factory = new Function('renderAdjustPanel', 'renderTimeline', 'scheduleAutoSave',
  src + '\n return { get _adjustState(){return _adjustState;}, set _adjustState(v){_adjustState=v;},' +
  ' get _undoStack(){return _undoStack;}, get _redoStack(){return _redoStack;},' +
  ' _pushUndo, undoAdjust, redoAdjust, _snapshotOnFocus, _commitFocusSnapshot, _onSegFieldChange,' +
  ' _dropUndoIfUnchanged, _updateUndoButtons, MAX_UNDO,' +
  ' _resetStacks: function(){ _undoStack.length=0; _redoStack.length=0; _pendingSnapshot=null; } };');
const M = factory(renderAdjustPanel, renderTimeline, scheduleAutoSave);
global.__adjustStateRef = M._adjustState;

// ---- 断言 ----
let pass = 0, fail = 0;
function ok(cond, name, extra) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); }
}
const seg = (s, e, d) => ({ text: '第' + s + '段', audio: 'a.mp3', duration: d || 3, video_start: s, video_end: e, audio_offset: 0 });
function reset(n) {
  const items = [];
  for (let i = 0; i < n; i++) items.push(seg(i * 10, i * 10 + 5));
  M._adjustState = { runDir: 'r', mode: 'movie', items, changed: {} };
  global.__adjustStateRef = M._adjustState;
  global.__items = items;
  M._resetStacks();
  syncEL(items);
}
const cur = () => M._adjustState.items;
const snap = () => JSON.stringify(M._adjustState.items);

console.log('\n[1] 裁剪后 Ctrl+Z 能恢复');
reset(3);
const before1 = snap();
M._pushUndo();
cur()[0].video_start = 1.5; cur()[0].video_end = 9;      // 裁剪第1段
ok(snap() !== before1, '裁剪确实改变了状态');
M.undoAdjust();
ok(snap() === before1, '撤销后与裁剪前完全一致');
M.redoAdjust();
ok(snap() !== before1 && cur()[0].video_start === 1.5, '重做后回到裁剪后状态');

console.log('\n[2] 拖动片段后 Ctrl+Z 能恢复原位');
reset(3);
const before2 = snap();
M._pushUndo();
const it2 = cur()[1];
const len = it2.video_end - it2.video_start;
it2.video_start = 100; it2.video_end = 100 + len;        // 整体拖动，长度不变
M.undoAdjust();
ok(snap() === before2 && (cur()[1].video_end - cur()[1].video_start) === len, '撤销后位置与长度都还原');

console.log('\n[3] 删除片段后 Ctrl+Z 能恢复');
reset(4);
const before3 = snap();
M._pushUndo();
cur().splice(1, 1);
ok(cur().length === 3, '删除后剩 3 段');
M.undoAdjust();
ok(cur().length === 4 && snap() === before3, '撤销后 4 段全部恢复且内容一致');

console.log('\n[4] 连续撤销 5 次后再重做 5 次，回到原状态');
reset(3);
const origin = snap();
const afterEach = [];
for (let i = 0; i < 5; i++) {
  M._pushUndo();
  cur()[0].video_start += 1;
  afterEach.push(snap());
}
for (let i = 0; i < 5; i++) M.undoAdjust();
ok(snap() === origin, '撤销 5 次回到最初状态');
ok(M._undoStack.length === 0, '撤销栈已清空');
let allMatch = true;
for (let i = 0; i < 5; i++) { M.redoAdjust(); if (snap() !== afterEach[i]) allMatch = false; }
ok(allMatch && snap() === afterEach[4], '重做 5 次逐步回到最终状态');

console.log('\n[5] 撤销后做新操作，redo 栈正确清空');
reset(3);
M._pushUndo(); cur()[0].video_start = 50;
M._pushUndo(); cur()[0].video_end = 60;
M.undoAdjust();
ok(M._redoStack.length === 1, '撤销一次后 redo 栈有 1 项');
M._pushUndo(); cur()[1].audio_offset = 2;                // 新操作
ok(M._redoStack.length === 0, '新操作后 redo 栈被清空');
M.redoAdjust();
ok(cur()[1].audio_offset === 2, 'redo 已失效，状态保持为新操作结果');

console.log('\n[6] 撤销栈上限 50 步');
reset(3);
for (let i = 0; i < 80; i++) { M._pushUndo(); cur()[0].video_start += 1; }
ok(M._undoStack.length === M.MAX_UNDO && M.MAX_UNDO === 50, '栈长度被限制在 50', '实际 ' + M._undoStack.length);

console.log('\n[7] 快照包含配音轨状态（audio_offset）');
reset(3);
const before7 = snap();
M._pushUndo();
cur()[2].audio_offset = -3.5;
M.undoAdjust();
ok(snap() === before7 && cur()[2].audio_offset === 0, '配音轨偏移也随撤销还原');

console.log('\n[8] 拖拽没产生实际位移时不留空撤销帧');
reset(3);
const depthBefore = M._undoStack.length;
M._pushUndo();
M._dropUndoIfUnchanged(M._undoStack.length);             // 模拟 tlEndResize 判定 dirty=false
ok(M._undoStack.length === depthBefore, '空操作被丢弃，栈未增长');

console.log('\n[9] 输入框改时间：可撤销，且值没变时不入栈');
reset(3);
const before9 = snap();
M._snapshotOnFocus();                                    // 用户点进输入框
M._commitFocusSnapshot();                                // 没改就点走
ok(M._undoStack.length === 0, '未修改时不产生撤销帧');
M._snapshotOnFocus();                                    // 再次点进并改成 2.5
EL['adjVStart0'].value = '2.5';
M._onSegFieldChange(0, 'vs');
ok(cur()[0].video_start === 2.5, '新值已写回数据');
ok(M._undoStack.length === 1, '真正改动后产生 1 帧');
M.undoAdjust();
ok(snap() === before9, '撤销回到编辑前');
ok(EL['adjVStart0'].value === '0', '输入框 DOM 也同步还原');

console.log('\n[10] 输入框改文字：可撤销');
reset(3);
const before10 = snap();
M._snapshotOnFocus();
EL['adjText0'].value = '改过的解说词';
M._onSegFieldChange(0, 'text');
ok(cur()[0].text === '改过的解说词', '文本已写回数据');
M.undoAdjust();
ok(snap() === before10, '撤销后文本还原');

console.log('\n---------------------------------------');
console.log(`结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);

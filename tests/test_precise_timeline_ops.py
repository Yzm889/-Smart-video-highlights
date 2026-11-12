# -*- coding: utf-8 -*-
"""精剪时间轴的「整理」操作（v1.4.72）：合并 / 按时间重排。

为什么用 node 真跑而不是源码钉：这两条是纯数据变换，源码钉只能防「被改回」，
防不住「算错」——比如合并时把音量取错一段、排序把原数组改掉、越界时抛异常。
这里直接把 desktop.js 里的函数块抠出来在 node 里跑真用例。
"""
import json
import os
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, "static", "desktop", "desktop.js")
_HTML = os.path.join(_ROOT, "static", "desktop", "index.html")

_HARNESS = r'''
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
eval(blockOf("function lcpMergeSegs("));
eval(blockOf("function lcpSortSegsByStart("));
eval(blockOf("function lcpExportSummary("));

const out = {};

/* ① 相邻两段合并：取并集，音量跟随第一段（被选中的那段） */
const base = [
  {start: 10, end: 20, volume: 1.5},
  {start: 20, end: 30, volume: 0.5},
  {start: 40, end: 50, volume: 1}
];
const m1 = lcpMergeSegs(base, 0);
out.m1_len = m1.segs.length;
out.m1_span = [m1.segs[0].start, m1.segs[0].end];
out.m1_vol = m1.segs[0].volume;
out.m1_rest = [m1.segs[1].start, m1.segs[1].end];
out.m1_input_untouched = base.length === 3 && base[0].end === 20;

/* ② 有缝隙的两段合并：必须把缝也并进来（否则用户以为合了其实中间还断着） */
const gap = [{start: 10, end: 20, volume: 1}, {start: 26, end: 30, volume: 1}];
const m2 = lcpMergeSegs(gap, 0);
out.m2_span = [m2.segs[0].start, m2.segs[0].end];

/* ③ 最后一段没有下一段 → null（UI 据此提示，而不是静默无反应） */
out.m3 = lcpMergeSegs(base, 2);

/* ④ 越界 / 空时间轴 → null，不抛 */
out.m4 = lcpMergeSegs(base, -1);
out.m5 = lcpMergeSegs([], 0);

/* ⑤ 缺省音量按 1 处理（老存档没有 volume 字段） */
const m6 = lcpMergeSegs([{start: 1, end: 2}, {start: 2, end: 3}], 0);
out.m6_vol = m6.segs[0].volume;

/* ⑥ 按源片时间重排：不改原数组、同起点按结束时间排 */
const shuffled = [
  {start: 40, end: 50, volume: 1},
  {start: 10, end: 30, volume: 1},
  {start: 10, end: 20, volume: 1}
];
const sorted = lcpSortSegsByStart(shuffled);
out.s_order = sorted.map(s => s.start + "-" + s.end);
out.s_input_untouched = shuffled[0].start === 40;
out.s_same_start_short_first = sorted[0].end === 20;

/* ⑦ 导出前总览：段数/总时长/最长最短/音量范围 */
const s1 = lcpExportSummary([
  {start: 0, end: 10, volume: 1},
  {start: 20, end: 32.5, volume: 0.5},
  {start: 40, end: 41, volume: 1.5}
]);
out.e_count = s1.n;
out.e_total = s1.total;
out.e_max = s1.max;
out.e_min = s1.min;
out.e_vol = [s1.vmin, s1.vmax];

/* ⑧ 重叠：同一段素材出现两次要报出来（>0.5s 才算） */
const s2 = lcpExportSummary([
  {start: 0, end: 10, volume: 1},
  {start: 5, end: 15, volume: 1},     // 与 #1 重叠 5s
  {start: 30, end: 40, volume: 1},
  {start: 39.6, end: 50, volume: 1}   // 与 #3 只重叠 0.4s → 不算
]);
out.e_overlaps = s2.overlaps;

/* ⑨ 过短 / 静音 / 超 60 段 / 非法时间 */
const s3 = lcpExportSummary([
  {start: 0, end: 0.5, volume: 1},    // 过短
  {start: 10, end: 20, volume: 0},    // 静音
  {start: 30, end: 30, volume: 1},    // 非法：时长为 0
  {start: "a", end: 5, volume: 1}     // 非法：起止非数字
]);
out.e_tooShort = s3.tooShort;
out.e_muted = s3.muted;
out.e_bad = s3.bad;
out.e_n_valid = s3.n;

const many = [];
for(let i = 0; i < 61; i++) many.push({start: i * 10, end: i * 10 + 5, volume: 1});
out.e_overCount = lcpExportSummary(many).overCount;
out.e_empty = lcpExportSummary([]).n;

console.log(JSON.stringify(out));
'''


def _run():
    env = dict(os.environ)
    env["FC_JS"] = _JS
    p = subprocess.run(["node", "-"], input=_HARNESS, capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60, cwd=_ROOT)
    assert p.returncode == 0, "harness 失败 rc=%s err=%s" % (p.returncode, (p.stderr or "")[-800:])
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_merge_adjacent_segments():
    o = _run()
    assert o["m1_len"] == 2, "两段应并成一段"
    assert o["m1_span"] == [10, 30], "合并后取并集区间"
    assert o["m1_vol"] == 1.5, "音量必须跟随被选中的那一段（可预期），不能取下一段"
    assert o["m1_rest"] == [40, 50], "后面的段落原样保留"
    assert o["m1_input_untouched"], "纯函数不得改动传入数组"


def test_merge_covers_gap():
    o = _run()
    assert o["m2_span"] == [10, 30], "中间有缝隙也要一并覆盖，否则用户以为合了其实还断着"


def test_merge_edge_cases():
    o = _run()
    assert o["m3"] is None, "最后一段没有下一段，应返回 null 让 UI 提示"
    assert o["m4"] is None and o["m5"] is None, "越界/空时间轴必须安全返回，不能抛"
    assert o["m6_vol"] == 1, "老存档没有 volume 字段时按 1.0 处理"


def test_sort_by_source_time():
    o = _run()
    assert o["s_order"] == ["10-20", "10-30", "40-50"], o["s_order"]
    assert o["s_input_untouched"], "排序不得原地改数组（否则撤销栈会跟着被改）"
    assert o["s_same_start_short_first"], "同起点时短的排前面，结果稳定"


def test_wiring_source_pins():
    """链路：按钮/滑条存在且都接上了，快捷键 M 绑定合并。"""
    with open(_JS, encoding="utf-8") as f:
        js = f.read()
    with open(_HTML, encoding="utf-8") as f:
        html = f.read()
    assert 'id="lcpResort"' in html and '按时间重排' in html
    assert 'id="lcpVolAll"' in html and 'id="lcpVolApply"' in html
    assert "$('lcpResort').addEventListener('click', lcpResort)" in js
    assert "$('lcpVolApply').addEventListener('click', lcpApplyAllVolume)" in js
    assert "$('lcpVolAll').addEventListener('input', lcpAllVolShow)" in js
    assert "bMerge.textContent = '⇥ 合并下一段'" in js
    assert "ev.key === 'm' || ev.key === 'M'" in js, 'M 快捷键必须绑定合并'


def test_export_summary_counts():
    o = _run()
    assert o["e_count"] == 3
    assert abs(o["e_total"] - 23.5) < 1e-9, "总时长要按段累加"
    assert o["e_max"] == 12.5 and o["e_min"] == 1
    assert o["e_vol"] == [0.5, 1.5]


def test_export_summary_flags_overlaps():
    o = _run()
    assert o["e_overlaps"] == [[1, 2, 5]], o["e_overlaps"]  # 0.4s 的轻微重叠不报


def test_export_summary_flags_short_muted_bad():
    o = _run()
    assert o["e_tooShort"] == [1]
    assert o["e_muted"] == [2]
    assert o["e_bad"] == [3, 4], "时长为 0 / 起止非数字都要挑出来"
    assert o["e_n_valid"] == 2


def test_export_summary_blockers():
    o = _run()
    assert o["e_overCount"] is True, "超过 60 段后端会拒，必须提前拦"
    assert o["e_empty"] == 0

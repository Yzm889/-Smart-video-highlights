# -*- coding: utf-8 -*-
"""batch4-3.3a 机械拆分脚本：把 webui_server.py 的调度层拆到 workflows.py。

做法（与 3.1/3.2 同风格，全部机械化、其余内容逐字一致）：
1. 运行时导入 webui_server 取 dir() —— 覆盖 star-import 进来的符号（静态 AST 看不到）；
2. 对迁移段做 AST 作用域分析：函数/类/lambda/推导式各自成作用域，收集各层绑定名；
3. 迁移段内对「宿主模块级符号」的 Name 引用（Load 上下文）逐个改写为 _w.<name>，
   调用时经宿主命名空间解析 —— 测试对 webui_server.<符号> 的 monkeypatch、
   conftest 对 OUTDIR/HISTORY_PATH 等的隔离改写因此继续生效；
4. 生成 workflows.py（头部 _host() 引用宿主 + 末尾把公共符号 setattr 回宿主），
   webui_server.py 删除迁移段并在文件末尾加静态兜底绑定（pyflakes/IDE 可解析）。

用法：python tools/split_workflows.py [--apply]
默认 dry-run 只打印报告；--apply 才写盘。
"""
import ast
import builtins
import os
import sys
import types as _types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, 'webui_server.py')
DST = os.path.join(ROOT, 'workflows.py')

# 迁移的顶层符号（按文件中出现顺序）
MOVED = [
    'parse_instruction', '_resolve_music', 'fail_task', '_music_catalog_entry',
    '_task_credits', '_finish_task_credits', 'dispatch_build', '_plan_thumbs',
    '_plan_to_ui', '_analyze_plan_job', '_render_plan_job', 'dispatch_beatcut',
    'dispatch_narrate', 'dispatch_movie', 'dispatch_movie_tts', 'dispatch_movie_compose',
    'dispatch_tts_single', 'dispatch_tts_regen_all', 'dispatch_instruct',
    'collect_partial', 'assemble', 'finalize', '_start_next_queued',
]

BUILTINS = set(dir(builtins))


def load_host_names():
    import webui_server  # noqa: F401  模块级仅定义常量与函数，无副作用
    return {n for n in dir(webui_server) if not n.startswith('__')}, webui_server


class ScopeAnalysis:
    """对一段（可作模块解析的）代码做作用域分析，产出需要改写的 Name 位置。

    规则：Name(Load) 若被任一外层函数/类/推导式作用域绑定 → 不改；
    否则若属于宿主模块级符号（且不是新模块自行 import 的名字）→ 改写为 _w.<name>。
    """

    def __init__(self, host_names, local_module_names):
        self.host_names = host_names
        self.local_module_names = local_module_names
        self.rewrites = []   # (lineno, col, name)
        self.unknown = []    # 既非局部也非宿主符号（应为空，否则人工审查）
        self.def_time = []   # 顶层 def 默认值里的引用（导入期求值，需确认）

    def analyze(self, tree):
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._walk_fn_prelude(node, [set()])
                self._walk_scope_body(node, [set(), self._fn_bindings(node)])
            else:
                self._walk_scope_body(node, [set()])
        return self

    # ---- 绑定名收集 ----
    def _fn_bindings(self, fn):
        b = set()
        a = fn.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            b.add(arg.arg)
        if a.vararg:
            b.add(a.vararg.arg)
        if a.kwarg:
            b.add(a.kwarg.arg)
        self._collect_stmts(fn.body, b)
        return b

    def _class_bindings(self, node):
        b = set()
        self._collect_stmts(node.body, b)
        return b

    def _collect_stmts(self, stmts, bound):
        for st in stmts:
            self._collect_stmt(st, bound)

    def _collect_stmt(self, st, bound):
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(st.name)
            return
        if isinstance(st, ast.Assign):
            for t in st.targets:
                self._bind_target(t, bound)
        elif isinstance(st, (ast.AugAssign, ast.AnnAssign)):
            if isinstance(st.target, ast.Name):
                bound.add(st.target.id)
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            self._bind_target(st.target, bound)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            for item in st.items:
                if item.optional_vars is not None:
                    self._bind_target(item.optional_vars, bound)
        elif isinstance(st, ast.Import):
            for a in st.names:
                bound.add(a.asname or a.name.split('.')[0])
        elif isinstance(st, ast.ImportFrom):
            for a in st.names:
                bound.add(a.asname or a.name)
        elif isinstance(st, (ast.Global, ast.Nonlocal)):
            bound.update(st.names)
        for ch in ast.iter_child_nodes(st):
            if isinstance(ch, ast.stmt):
                self._collect_stmt(ch, bound)
            elif isinstance(ch, ast.ExceptHandler):
                if ch.name:
                    bound.add(ch.name)
                self._collect_stmts(ch.body, bound)

    def _bind_target(self, t, bound):
        for n in ast.walk(t):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                bound.add(n.id)

    # ---- 引用走查 ----
    def _walk_scope_body(self, node, scopes):
        for ch in ast.iter_child_nodes(node):
            self._walk(ch, scopes)

    def _walk(self, node, scopes):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._walk_fn_prelude(node, scopes)
            self._walk_scope_body(node, scopes + [self._fn_bindings(node)])
            return
        if isinstance(node, ast.Lambda):
            for d in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                self._walk(d, scopes)
            b = set()
            a = node.args
            for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                b.add(arg.arg)
            if a.vararg:
                b.add(a.vararg.arg)
            if a.kwarg:
                b.add(a.kwarg.arg)
            self._walk_scope_body(node, scopes + [b])
            return
        if isinstance(node, ast.ClassDef):
            for d in node.decorator_list:
                self._walk(d, scopes)
            for base in node.bases:
                self._walk(base, scopes)
            for kw in node.keywords:
                self._walk(kw.value, scopes)
            self._walk_scope_body(node, scopes + [self._class_bindings(node)])
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            b = set()
            for gen in node.generators:
                self._bind_target(gen.target, b)
            self._walk_scope_body(node, scopes + [b])
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
            if any(name in s for s in scopes):
                return
            if name in BUILTINS or name in self.local_module_names:
                return
            if name in self.host_names:
                self.rewrites.append((node.lineno, node.col_offset, name))
                return
            self.unknown.append((getattr(node, 'lineno', 0), name))
            return
        for ch in ast.iter_child_nodes(node):
            self._walk(ch, scopes)

    def _walk_fn_prelude(self, node, scopes):
        is_toplevel = len(scopes) == 1
        for d in node.decorator_list:
            self._walk(d, scopes)
        a = node.args
        for d in list(a.defaults) + [d for d in a.kw_defaults if d]:
            self._walk(d, scopes)
            if is_toplevel:
                for n in ast.walk(d):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                        self.def_time.append((n.lineno, n.id))
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs) + \
                ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else []):
            if arg.annotation is not None:
                self._walk(arg.annotation, scopes)
        ra = getattr(node, 'returns', None)
        if ra is not None:
            self._walk(ra, scopes)


def rewrite_text(text, rewrites):
    """按 (行,列) 从后往前把 name 替换为 _w.name。标识符不跨行。

    ast 的 col_offset 是该行 UTF-8 编码的字节偏移（中文每字 3 字节），
    必须按字节定位再解码，否则含中文的行会错位。
    """
    lines = text.split('\n')
    for (ln, col, name) in sorted(rewrites, key=lambda x: (-x[0], -x[1])):
        raw = lines[ln - 1].encode('utf-8')
        assert raw[col:col + len(name)] == name.encode('utf-8'), (ln, col, name, lines[ln - 1])
        raw = raw[:col] + b'_w.' + name.encode('utf-8') + raw[col + len(name):]
        lines[ln - 1] = raw.decode('utf-8')
    return '\n'.join(lines)


def main():
    do_apply = '--apply' in sys.argv
    host_names, host_mod = load_host_names()

    raw = open(SRC, 'rb').read()
    crlf = b'\r\n' in raw[:500]
    text = raw.decode('utf-8').replace('\r\n', '\n')
    lines = text.split('\n')
    tree = ast.parse(text)

    nodes = {}
    for node in tree.body:
        name = getattr(node, 'name', None)
        if name in MOVED:
            nodes[name] = node
    missing = [n for n in MOVED if n not in nodes]
    assert not missing, 'top-level defs not found: %s' % missing

    # 抽取连续区间：相邻 moved 节点之间若只有空行/注释则并入（注释随迁，空行坍缩）
    order = [n for n in tree.body if getattr(n, 'name', None) in MOVED]
    ranges = []
    for node in order:
        start, end = node.lineno, node.end_lineno
        if ranges:
            gap = lines[ranges[-1][1]:start - 1]
            if (start - ranges[-1][1]) <= 40 and \
                    all((not l.strip()) or l.strip().startswith('#') for l in gap):
                ranges[-1] = (ranges[-1][0], end)
                continue
        ranges.append((start, end))

    # 段首上方紧贴的注释块（“# Phase 4 …” 分节头）一并带走
    first_start = ranges[0][0]
    s = first_start - 1  # 0-based：def 的上一行
    while s >= 1 and lines[s - 1].strip().startswith('#'):
        s -= 1
    ranges[0] = (s + 1, ranges[0][1])
    if s >= 2 and lines[s - 2].strip() == '':
        ranges[0] = (s - 1, ranges[0][1])

    seg_lines = []
    for (a, b) in ranges:
        seg_lines.extend(lines[a - 1:b])
    segment = '\n'.join(seg_lines).rstrip('\n') + '\n'
    seg_tree = ast.parse(segment)

    # 新模块本地 import 的标准库名（与宿主同名同对象：threading 等模块的属性级
    # patch 对两者同时可见）。os/sys 是头部 _host() 自身要用的，强制包含。
    local_module_names = {'os', 'sys'}
    for node in ast.walk(seg_tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            n = node.id
            if n in host_names and n not in BUILTINS:
                if isinstance(getattr(host_mod, n, None), _types.ModuleType):
                    local_module_names.add(n)
    analysis = ScopeAnalysis(host_names, local_module_names).analyze(seg_tree)

    print('== 迁移区间(1-based 行号): %s' % (ranges,))
    print('== 段落 %d 行 / %d 个顶层符号' % (len(seg_lines), len(MOVED)))
    print('== 新模块本地 import: %s' % sorted(local_module_names))
    from collections import Counter
    cnt = Counter(r[2] for r in analysis.rewrites)
    print('== 改写宿主引用 %d 处，涉及符号 %d 个:' % (len(analysis.rewrites), len(cnt)))
    for k, v in sorted(cnt.items(), key=lambda x: (-x[1], x[0])):
        print('   %-32s %d' % (k, v))
    if analysis.unknown:
        print('!! 未知名（需人工审查）: %s' % sorted(set(n for _, n in analysis.unknown)))
    if analysis.def_time:
        print('!! 顶层 def 导入期求值引用: %s' % analysis.def_time)
    if analysis.unknown or analysis.def_time:
        print('!! 存在需人工处理项，未写盘')
        return 1

    new_text = rewrite_text(segment, analysis.rewrites)

    header = '''# -*- coding: utf-8 -*-
"""工作流调度层（batch4-3.3 由 webui_server.py 拆出，符号与拆分前等价）。

约定：
- 宿主（webui_server）的模块级符号（管道函数 / PROGRESS / 目录常量 / AI·TTS 入口等）
  一律经 `_w.<符号>` 在调用时解析——测试对 `webui_server.<符号>` 的 monkeypatch、
  conftest 对 OUTDIR / HISTORY_PATH 等隔离改写因此继续生效；
- 本层公共符号在文件末尾注入回宿主命名空间，`webui_server.dispatch_*` 等旧入口不变。
"""
import %s

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


''' % (', '.join(sorted(local_module_names)),)

    inject = '''

# ---------------------------------------------------------------------------
# 公共符号注入回宿主命名空间（保持 webui_server.X 旧入口；覆盖宿主文件末尾的
# None 占位绑定）。任何导入顺序下宿主此刻都已在 sys.modules。
# ---------------------------------------------------------------------------
for _name in (
%s
):
    setattr(_w, _name, globals()[_name])
''' % ('\n'.join("    '%s'," % n for n in MOVED))

    workflows_py = header + new_text.rstrip('\n') + '\n' + inject

    # ---- 宿主：删迁移段，末尾插静态兜底绑定（保持在 __main__ 块之前） ----
    keep = []
    for i, l in enumerate(lines):
        if not any(a <= i + 1 <= b for a, b in ranges):
            keep.append(l)
    host_new = '\n'.join(keep)

    bottom = '''
# ---------------------------------------------------------------------------
# [3.3] 工作流调度层已拆至 workflows.py：其加载完成时会把下列符号注入回本命名空间。
# 此处静态兜底绑定让 pyflakes/IDE 可解析（standalone 导入 workflows 的罕见顺序下
# 先为 None，随后被其末尾注入覆盖为真值；正常顺序下直接拿到真函数）。
# ---------------------------------------------------------------------------
import workflows as _workflows_mod  # noqa: E402
%s
''' % ('\n'.join("%s = getattr(_workflows_mod, '%s', None)" % (n, n) for n in MOVED))

    marker = "if __name__ == '__main__':"
    idx = host_new.rindex(marker)
    host_new = host_new[:idx] + bottom.lstrip('\n') + '\n\n' + host_new[idx:]

    if do_apply:
        def write(path, content):
            data = content.replace('\n', '\r\n') if crlf else content
            with open(path, 'wb') as f:
                f.write(data.encode('utf-8'))
        write(DST, workflows_py)
        write(SRC, host_new)
        print('已写盘: workflows.py(%d 行) / webui_server.py(%d 行)' %
              (workflows_py.count('\n'), host_new.count('\n')))
    else:
        print('dry-run：未写盘（--apply 生效）')
    return 0


if __name__ == '__main__':
    sys.exit(main())

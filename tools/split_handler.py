# -*- coding: utf-8 -*-
"""batch4-3.3b 机械拆分脚本：把 webui_server.py 的 HTTP 层拆到 handler.py。

与 3.3a（split_workflows.py）同一套路：AST 作用域分析 + 字节级精确改写，
迁移段内对宿主模块级符号的引用改为 _w.<name> 调用时解析（monkeypatch /
conftest 隔离语义不变），公共符号末尾注入回宿主。

差异点：
- 迁移段是单个连续区间（MIME + class Handler + start_server/_content_disposition/
  _evict_finished_progress/_kill_all_child_processes + atexit.register + webbrowser_open）；
- 类体内的路由表用字符串方法名派发（getattr(self, h)），无导入期宿主引用；
- MIME / 迁移符号自身 / BaseHTTPRequestHandler / ThreadingHTTPServer 在新模块
  本地解析（同对象），其余宿主符号一律 _w.。

用法：python tools/split_handler.py [--apply]
"""
import ast
import builtins
import os
import sys
import types as _types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, 'webui_server.py')
DST = os.path.join(ROOT, 'handler.py')

MOVED_DEFS = ['Handler', 'start_server', '_content_disposition',
              '_evict_finished_progress', '_kill_all_child_processes', 'webbrowser_open']
MOVED = MOVED_DEFS + ['MIME']

# 新模块本地解析（同对象）：标准库模块自动探测，这几个类/常量显式指定
EXTRA_LOCAL = {'BaseHTTPRequestHandler', 'ThreadingHTTPServer', 'MIME'}

BUILTINS = set(dir(builtins))


def load_host_names():
    import webui_server  # noqa: F401
    return {n for n in dir(webui_server) if not n.startswith('__')}, webui_server


class ScopeAnalysis:
    def __init__(self, host_names, local_names):
        self.host_names = host_names
        self.local_names = local_names
        self.rewrites = []
        self.unknown = []

    def analyze(self, tree):
        for node in tree.body:
            # 统一经 _walk 派发（FunctionDef/ClassDef 各自进自己的作用域）
            self._walk(node, [set()])
        return self

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

    def _walk_scope_body(self, node, scopes):
        for ch in ast.iter_child_nodes(node):
            self._walk(ch, scopes)

    def _walk(self, node, scopes):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._walk_scope_body(node, scopes + [self._fn_bindings(node)])
            return
        if isinstance(node, ast.Lambda):
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
            if name in BUILTINS or name in self.local_names:
                return
            if name in self.host_names:
                self.rewrites.append((node.lineno, node.col_offset, name))
                return
            self.unknown.append((getattr(node, 'lineno', 0), name))
            return
        for ch in ast.iter_child_nodes(node):
            self._walk(ch, scopes)


def rewrite_text(text, rewrites):
    """按 (行,列) 从后往前把 name 替换为 _w.name。col_offset 是 UTF-8 字节偏移。"""
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
        if name in MOVED_DEFS:
            nodes[name] = node
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == 'MIME':
                    nodes['MIME'] = node
    missing = [n for n in MOVED if n not in nodes]
    assert not missing, 'not found: %s' % missing

    # 单个连续区间：从 MIME 上方的注释块头，到 webbrowser_open 结束
    start = nodes['MIME'].lineno
    s = start - 1  # 0-based：MIME 的上一行
    while s >= 1 and lines[s - 1].strip().startswith('#'):
        s -= 1
    seg_start = s + 1
    if s >= 2 and lines[s - 2].strip() == '':
        seg_start = s - 1
    seg_end = nodes['webbrowser_open'].end_lineno
    ranges = [(seg_start, seg_end)]

    seg_lines = lines[seg_start - 1:seg_end]
    segment = '\n'.join(seg_lines).rstrip('\n') + '\n'
    seg_tree = ast.parse(segment)

    # 本地 import：段内用到的、宿主命名空间里的模块对象 + 显式指定
    local_names = {'os', 'sys'} | EXTRA_LOCAL | set(MOVED_DEFS)
    for node in ast.walk(seg_tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            n = node.id
            if n in host_names and n not in BUILTINS and n not in local_names:
                if isinstance(getattr(host_mod, n, None), _types.ModuleType):
                    local_names.add(n)
    analysis = ScopeAnalysis(host_names, local_names).analyze(seg_tree)

    print('== 迁移区间(1-based): %s (%d 行)' % (ranges, len(seg_lines)))
    print('== 本地 import: %s' % sorted(n for n in local_names if n not in MOVED_DEFS))
    from collections import Counter
    cnt = Counter(r[2] for r in analysis.rewrites)
    print('== 改写宿主引用 %d 处，涉及符号 %d 个:' % (len(analysis.rewrites), len(cnt)))
    for k, v in sorted(cnt.items(), key=lambda x: (-x[1], x[0])):
        print('   %-32s %d' % (k, v))
    if analysis.unknown:
        print('!! 未知名（需人工审查）: %s' % sorted(set(n for _, n in analysis.unknown)))
        return 1

    # 本地 import 收敛为 import 行；模块别名（如宿主的 threading as _threading）
    # 按 __name__ 还原成 `import threading as _threading`
    import_parts = []
    for n in sorted(m for m in local_names if m not in MOVED_DEFS and m not in EXTRA_LOCAL):
        mod = getattr(host_mod, n, None)
        if isinstance(mod, _types.ModuleType) and mod.__name__ != n:
            import_parts.append('import %s as %s' % (mod.__name__, n))
        else:
            import_parts.append('import %s' % n)
    import_line = '\n'.join(import_parts)
    extra_line = 'from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer'

    new_text = rewrite_text(segment, analysis.rewrites)

    header = '''# -*- coding: utf-8 -*-
"""HTTP 服务层（batch4-3.3 由 webui_server.py 拆出，符号与拆分前等价）。

约定（与 workflows.py 相同）：
- 宿主（webui_server）的模块级符号（dispatch_* 调度 / PROGRESS / 目录常量 /
  上传与历史等）一律经 `_w.<符号>` 在调用时解析——测试对 `webui_server.<符号>`
  的 monkeypatch、conftest 的 OUTDIR/HISTORY_PATH 隔离改写因此继续生效；
- 本层公共符号在文件末尾注入回宿主命名空间，`webui_server.Handler` /
  `webui_server.start_server` 等旧入口不变。
"""
%s
%s

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


''' % (import_line, extra_line)

    inject = '''

# ---------------------------------------------------------------------------
# 公共符号注入回宿主命名空间（保持 webui_server.X 旧入口；覆盖宿主文件末尾的
# None 占位绑定）。
# ---------------------------------------------------------------------------
for _name in (
%s
):
    setattr(_w, _name, globals()[_name])
''' % ('\n'.join("    '%s'," % n for n in MOVED_DEFS))

    handler_py = header + new_text.rstrip('\n') + '\n' + inject

    # ---- 宿主：删区间，末尾插静态兜底绑定（在 workflows 兜底块之后、__main__ 之前） ----
    keep = []
    for i, l in enumerate(lines):
        if not any(a <= i + 1 <= b for a, b in ranges):
            keep.append(l)
    host_new = '\n'.join(keep)

    bottom = '''# ---------------------------------------------------------------------------
# [3.3] HTTP 层已拆至 handler.py：其加载完成时会把下列符号注入回本命名空间。
# 静态兜底绑定同上（pyflakes/IDE 可解析；standalone 导入顺序下先 None 后被覆盖）。
# ---------------------------------------------------------------------------
import handler as _handler_mod  # noqa: E402
%s
''' % ('\n'.join("%s = getattr(_handler_mod, '%s', None)" % (n, n) for n in MOVED_DEFS))

    marker = "if __name__ == '__main__':"
    idx = host_new.rindex(marker)
    host_new = host_new[:idx] + bottom + '\n' + host_new[idx:]

    # 宿主顶部不再使用的 http.server 导入删除（Handler 类已迁走）
    host_new = host_new.replace(
        'from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n', '', 1)

    if do_apply:
        def write(path, content):
            data = content.replace('\n', '\r\n') if crlf else content
            with open(path, 'wb') as f:
                f.write(data.encode('utf-8'))
        write(DST, handler_py)
        write(SRC, host_new)
        print('已写盘: handler.py(%d 行) / webui_server.py(%d 行)' %
              (handler_py.count('\n'), host_new.count('\n')))
    else:
        print('dry-run：未写盘（--apply 生效）')
    return 0


if __name__ == '__main__':
    sys.exit(main())

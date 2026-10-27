# -*- coding: utf-8 -*-
"""多线程 Range 并行下载器回归（零新增依赖的加速兜底）。

背景（2026-10-26 实测）：没装 aria2c 时，走单线程 urllib 从 hf-mirror 拉 5GB 只有
~0.5MB/s（要 1 小时以上）——用户实测就是卡在 31% 不动了。于是用标准库 urllib +
Range 头实现了 8 线程并行下载，作为无 aria2c 时的兜底。

本文件锁定：
  ① 并行下载的内容与源**逐字节一致**（分片拼接顺序不能错）；
  ② 支持断点续传（已有前缀不重下）；
  ③ **旧区间的残留分片不得被误用**（目标文件被删、分片残留的场景——
     这是实现时真实踩到的坑：分片名不编码区间就会拼出损坏文件）。
"""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import webui_server as S


PAYLOAD = bytes((i * 7 + 13) % 251 for i in range(1024 * 1024 + 4096))  # ~1MB，字节可校验


class _RangeHandler(BaseHTTPRequestHandler):
    """支持 Range 的最小 HTTP 源。"""

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get('Range', '')
        start, end = 0, len(PAYLOAD) - 1
        if rng.startswith('bytes='):
            spec = rng[6:]
            if '-' in spec:
                a, _, b = spec.partition('-')
                start = int(a) if a else 0
                end = int(b) if b else end
        end = min(end, len(PAYLOAD) - 1)
        chunk = PAYLOAD[start:end + 1]
        self.send_response(206 if rng else 200)
        self.send_header('Content-Length', str(len(chunk)))
        self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(chunk)


@pytest.fixture(scope='module')
def src_server():
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _RangeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield 'http://127.0.0.1:%d/model.gguf' % srv.server_address[1]
    srv.shutdown()


def test_parallel_download_matches_source(src_server, tmp_path):
    tgt = str(tmp_path / 'out.bin')
    ok, msg = S._parallel_range_download(src_server, tgt, len(PAYLOAD), parts=8,
                                        chunk_dir=str(tmp_path))
    assert ok, msg
    assert os.path.getsize(tgt) == len(PAYLOAD)
    with open(tgt, 'rb') as f:
        assert f.read() == PAYLOAD, '并行下载内容与源不一致（分片顺序/区间错乱）'


def test_parallel_download_resumes(src_server, tmp_path):
    """已有前缀应被跳过、只补剩余部分。"""
    tgt = str(tmp_path / 'out.bin')
    pre = len(PAYLOAD) // 3
    with open(tgt, 'wb') as f:
        f.write(PAYLOAD[:pre])
    ok, msg = S._parallel_range_download(src_server, tgt, len(PAYLOAD), parts=8,
                                        chunk_dir=str(tmp_path))
    assert ok, msg
    with open(tgt, 'rb') as f:
        assert f.read() == PAYLOAD, '续传后内容不一致'


def test_stale_parts_from_other_range_are_not_reused(src_server, tmp_path):
    """目标文件被删、但残留**别的区间**的分片 → 绝不能被拼进结果（会产出损坏文件）。"""
    tgt = str(tmp_path / 'out.bin')
    # 伪造一批「旧区间」分片：内容是随机字节，区间标记为很大（模拟上一轮从 1.7GB 开始）
    for s in (1700000000, 1700000001):
        p = os.path.join(str(tmp_path), 'out.bin.part%d-%d' % (s, s + 1023))
        with open(p, 'wb') as f:
            f.write(b'\xde\xad\xbe\xef' * 256)
    ok, msg = S._parallel_range_download(src_server, tgt, len(PAYLOAD), parts=8,
                                        chunk_dir=str(tmp_path))
    assert ok, msg
    with open(tgt, 'rb') as f:
        got = f.read()
    assert got == PAYLOAD, '残留的旧区间分片被误用了 → 成品损坏'
    assert b'\xde\xad\xbe\xef' not in got[:4096]


def test_existing_complete_file_is_noop(src_server, tmp_path):
    tgt = str(tmp_path / 'out.bin')
    with open(tgt, 'wb') as f:
        f.write(PAYLOAD)
    ok, msg = S._parallel_range_download(src_server, tgt, len(PAYLOAD), parts=8,
                                        chunk_dir=str(tmp_path))
    assert ok and '已存在' in msg, msg

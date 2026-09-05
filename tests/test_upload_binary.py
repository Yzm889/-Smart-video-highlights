# -*- coding: utf-8 -*-
"""batch5-5.2 上传二进制分片化的回归锁定。

改动：内联 base64 阈值从 64MB 降到 8MB——视频/图片/音乐/素材库超阈值一律走
multipart 分片（每片 4MB），base64 JSON 内联只留给小载荷。动机：base64 让
请求体膨胀 1.37 倍，服务端 json 解析 + b64 解码各复制一份，60MB 视频内联的
内存峰值约 320MB；分片路径每片仅 ~12MB 常驻。
"""
import inspect
import os

import webui_server as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_resolve_music_supports_upload_id(monkeypatch, tmp_path):
    """音乐分片上传：_resolve_music 支持 upload_id 形态（copy 成品，不动会话）。"""
    import base64 as b64
    monkeypatch.setattr(S, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(S, 'WORKDIR', str(tmp_path / 'work'))
    assert S._resolve_music({'name': 'm.mp3', 'upload_id': 'up-none'}) is None, '无效会话须为 None'
    d = str(tmp_path / 'uploads' / 'up-m1')
    os.makedirs(d)
    open(os.path.join(d, 'final__m.mp3'), 'wb').write(b'music-bytes')
    p = S._resolve_music({'name': 'm.mp3', 'upload_id': 'up-m1'})
    assert p and open(p, 'rb').read() == b'music-bytes', '应从上传会话取到成品内容'
    assert os.path.isfile(os.path.join(d, 'final__m.mp3')), '音乐取件用 copy，会话留给 prune 清理'
    # 旧形态（base64）不受影响
    p2 = S._resolve_music({'name': 'm2.mp3', 'data': b64.b64encode(b'old-path').decode()})
    assert p2 and open(p2, 'rb').read() == b'old-path'


def test_build_image_items_support_upload_id():
    """源码 pin：一键合成的图片素材支持分片上传（upload_id），大图不再撑爆 JSON 体。"""
    code = inspect.getsource(S.dispatch_build)
    assert "it.get('upload_id')" in code, '图片分支必须先于 data 兜底识别 upload_id'
    assert "_upload_final_path" in code


def test_frontend_inline_threshold_lowered():
    """前端协议契约：内联阈值 8MB；视频/图片/音乐/素材库四路全部按阈值分流。"""
    with open(os.path.join(ROOT, 'static', 'app.js'), encoding='utf-8') as f:
        src = f.read()
    assert 'INLINE_UPLOAD_MAX = 8*1024*1024' in src, '内联阈值必须是 8MB'
    assert '64*1024*1024' not in src, '不允许残留 64MB 旧阈值'
    assert 'async function musicToBody' in src, '音乐必须有分片路由 helper'
    assert src.count('musicToBody(MUSIC.file)') >= 6, '全部音乐上传点都要走 musicToBody'

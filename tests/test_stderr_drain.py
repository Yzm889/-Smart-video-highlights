# -*- coding: utf-8 -*-
"""ffmpeg_run stderr 排水回归钉（2026-11-06）。

旧实现：stderr 攒满 _ERR_CHUNK_MAX 块（默认 600×64KB≈37.5MB）后读取线程 break，
ffmpeg 写 stderr 状态行被管道缓冲卡死——长任务（无 on_progress）停摆、输出 0 字节。
实测：4K→720p 预览代理 ~5 分钟卡死。

钉住：把保留上限钳到极小后，verbose 编码必须完整跑完（rc==0）——
旧代码会在这里卡死直到超时被杀（rc!=0）。
"""
import webui_server as S


def test_stderr_keeps_draining_after_cap(tmp_path, monkeypatch):
    import ffmpeg_utils as fu

    monkeypatch.setattr(fu, '_ERR_CHUNK_MAX', 2)     # 2 块=128KB 后进入丢弃区
    # loglevel debug 的输出量远超 128KB，旧代码会在中途卡死管道
    rc, _o, e = S.ffmpeg_run([
        '-loglevel', 'debug', '-y', '-f', 'lavfi',
        '-i', 'testsrc2=duration=2:size=320x240:rate=30',
        '-f', 'null', '-'], timeout=60)
    assert rc == 0, 'stderr 超上限后必须继续排水，不得卡死编码：' + e.decode('utf-8', 'ignore')[-200:]

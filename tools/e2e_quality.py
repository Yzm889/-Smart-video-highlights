# -*- coding: utf-8 -*-
"""质量路线真机端到端验证（分步执行，便于观察每一阶段真实耗时）。

用法（在项目根目录运行，webui_output 等相对路径才落对地方）：
  py -3.13 tools/e2e_quality.py narr  <video> <plot文件>
  py -3.13 tools/e2e_quality.py comp  <run_dir>
  py -3.13 tools/e2e_quality.py audio <final.mp4>     # 检查配音是否全程有声
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根目录（脚本在 tools/ 下）

if os.environ.get('E2E_DUMP'):
    import faulthandler
    faulthandler.dump_traceback_later(int(os.environ['E2E_DUMP']), repeat=True)

import webui_server  # noqa: F401  解除循环依赖
import movie_narrator as mn


class Prog(dict):
    """记录 progress 每次阶段变化的时间点，用于输出各阶段真实耗时。"""

    def __init__(self):
        super().__init__()
        self.t0 = time.time()
        self.marks = []
        self._last = None

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        if k == 'phase' and v != self._last:
            self._last = v
            dt = time.time() - self.t0
            self.marks.append((round(dt, 1), v))
            print('[%7.1fs] %s' % (dt, v), flush=True)

    def dump(self):
        print('\n=== 阶段耗时 ===')
        prev = 0.0
        for dt, ph in self.marks:
            print('  %7.1fs  (+%5.1fs)  %s' % (dt, dt - prev, ph))
            prev = dt
        print('  总计 %.1fs' % (time.time() - self.t0))


def step_narr(video, plot_file, script_file=None):
    plot = open(plot_file, encoding='utf-8').read().strip() if plot_file else ''
    params = {}
    if script_file:
        params['user_script'] = open(script_file, encoding='utf-8').read().strip()
        print('== 用户稿子模式 ==')
    run_dir = os.path.join('webui_output', '_e2e_quality_' + time.strftime('%H%M%S'))
    os.makedirs(run_dir, exist_ok=True)
    print('run_dir =', run_dir)
    prog = Prog()
    t0 = time.time()
    mn.narrate_movie('', plot, video, params, run_dir, progress=prog, tts_only=True)
    prog.dump()
    print('\nPHASE1 总耗时 %.1fs' % (time.time() - t0))
    st = os.path.join(run_dir, 'tts_state.json')
    if os.path.exists(st):
        s = json.load(open(st, encoding='utf-8'))
        print('narr 段数=%d  segs=%d' % (len(s.get('narr') or []), len(s.get('segs') or [])))
        print('\n--- 解说稿 ---')
        for i, t in enumerate(s.get('narr') or []):
            print('%2d. %s' % (i, t))
    else:
        print('!! 没有生成 tts_state.json')
    print('RUN_DIR=' + run_dir)


def step_comp(run_dir):
    prog = Prog()
    t0 = time.time()
    final = mn.compose_movie_from_tts(run_dir, prog)
    prog.dump()
    print('\nPHASE2 总耗时 %.1fs' % (time.time() - t0))
    print('final =', final)
    if final and os.path.exists(final):
        print('size = %.1f MB' % (os.path.getsize(final) / 1e6))
        step_audio(final)
    else:
        print('!! 成片不存在')


def step_audio(f):
    """每 10 秒测一次平均音量，用来确认解说没有中途消失。"""
    import imageio_ffmpeg
    import subprocess
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    dur = 0.0
    r = subprocess.run([exe, '-i', f], capture_output=True, text=True, errors='ignore')
    m = re.search(r'Duration: (\d+):(\d+):([\d.]+)', r.stderr)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    print('\n=== 音量巡检（每 10s）===')
    quiet = 0
    for t in range(0, int(dur), 10):
        rr = subprocess.run([exe, '-ss', str(t), '-t', '10', '-i', f,
                             '-af', 'volumedetect', '-f', 'null', '-'],
                            capture_output=True, text=True, errors='ignore')
        mm = re.search(r'mean_volume: (-?[\d.]+) dB', rr.stderr)
        v = float(mm.group(1)) if mm else -999
        flag = '' if v > -45 else '  <== 近乎无声'
        if v <= -45:
            quiet += 1
        print('%4d-%4ds  %6.1f dB%s' % (t, t + 10, v, flag))
    print('近乎无声的区间: %d 个' % quiet)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'narr'
    if cmd == 'narr':
        step_narr(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None,
                  sys.argv[4] if len(sys.argv) > 4 else None)
    elif cmd == 'comp':
        step_comp(sys.argv[2])
    elif cmd == 'audio':
        step_audio(sys.argv[2])

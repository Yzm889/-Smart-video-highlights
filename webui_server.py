# -*- coding: utf-8 -*-
"""
webui_server.py — 春天短视频工坊 · 本地图形化 WebUI 后端

功能：
  - 在 http://127.0.0.1:PORT/ 提供图形化网页
  - 支持拖入/上传 图片 和 视频，混排成一条时间线
  - 点击“合成”，把 图片(带 Ken Burns 镜头运动) + 视频片段 用交叉淡入淡出
    串成一段短.mp4，网页内预览并可保存。

依赖：Pillow / numpy / imageio-ffmpeg（第一次会自动 pip 安装）
"""
import os, sys, json, math, random, shutil, subprocess, threading, time, base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(HERE, 'webui_workspace')
PROGRESS = {}          # runid -> mutable progress dict for the UI poller
RUNSEQ = [0]           # monotonic run id counter
import threading as _threading
OUTDIR = os.path.join(HERE, 'webui_output')
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
AI_CONFIG_PATH = os.path.join(HERE, 'ai_config.json')
HISTORY_PATH = os.path.join(HERE, 'history.json')
STATIC_DIR = os.path.join(HERE, 'static')
_LAST_TTS_ERR = ''

def load_ai_config():
    try:
        with open(AI_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_ai_config(cfg):
    with open(AI_CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def load_history(limit=50):
    try:
        with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
            items = json.load(f)
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []
    return items[:limit]


def add_history(entry):
    try:
        items = load_history(500)
        items.insert(0, entry)
        with open(HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(items[:100], f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# Offline caption fallback: turn a plain filename into a short spring-y caption if the
# user has not configured any AI. Keeps the pipeline functional without a key.
def offline_caption(name, idx, n_total):
    base = os.path.splitext(os.path.basename(name))[0]
    import re as _re
    m = _re.search(r'up_(\d+)_\d+_img', base)
    if m:
        i = int(m.group(1))
        phrases = ['春意初醒', '花开时节', '绿野青青', '溪水潺潺', '山色葱茏', '暖阳正好']
        return f'{phrases[(i - 1) % len(phrases)]} · 第{i}帧'
    if 'spring' in base or 'img' in base:
        return f'第 {idx} 帧 · 春日风景'
    return f'第 {idx} 帧 · {base}'

W, H = 1920, 1080

# ---------------------------------------------------------------------------
# 依赖安装（首次）
# ---------------------------------------------------------------------------
def ensure_deps():
    def has(mod):
        try:
            __import__(mod); return True
        except Exception:
            return False
    missing = []
    if not has('PIL'): missing.append('Pillow')
    if not has('numpy'): missing.append('numpy')
    if not has('imageio_ffmpeg'): missing.append('imageio-ffmpeg')
    if missing:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                               '--no-input'] + missing)

def ffmpeg_exe():
    from imageio_ffmpeg import get_ffmpeg_exe
    return get_ffmpeg_exe()

# ---------------------------------------------------------------------------
# 图片生成（复用 spring_video 的绘制逻辑）
# ---------------------------------------------------------------------------
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

def hx(h):
    h = h.lstrip('#'); return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def gradient(w, h, top, bot):
    top = hx(top); bot = hx(bot)
    img = Image.new('RGB', (w, h)); px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bot[0]-top[0])*t); g = int(top[1] + (bot[1]-top[1])*t); b = int(top[2] + (bot[2]-top[2])*t)
        for x in range(0, w, 3):
            px[x, y] = (r, g, b)
            if x+1 < w: px[x+1, y] = (r, g, b)
            if x+2 < w: px[x+2, y] = (r, g, b)
    return img.resize((w, h)).convert('RGB')

def blend(base, layer):
    if base.mode != 'RGBA': base = base.convert('RGBA')
    return Image.alpha_composite(base, layer).convert('RGB')

def sun_layer(cx, cy, r, color, blur=20, st=20):
    s = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(s)
    for i in range(8, 0, -1):
        d.ellipse([cx-r*i, cy-r*i, cx+r*i, cy+r*i], fill=color + (max(0, int(st*i)),))
    s = s.filter(ImageFilter.GaussianBlur(blur))
    ImageDraw.Draw(s).ellipse([cx-r, cy-r, cx+r, cy+r], fill=color + (255,))
    return s

def clouds(d, x, y, s, shade):
    r = int(60*s)
    for dx, dy, rr in [(0,0,r),(int(r*.8),-int(r*.3),int(r*.75)),(-int(r*.9),int(r*.15),int(r*.7)),(int(r*.9),int(r*.1),int(r*.6))]:
        d.ellipse([x+dx-rr, y+dy-int(rr*.6), x+dx+rr, y+dy+int(rr*.7)], fill=shade)

def tree(d, x, y, gh, tc, l1, l2):
    tw = max(4, int(gh*.05)); d.rectangle([x-tw//2, y-gh, x+tw//2, y], fill=tc)
    for i in range(5):
        ox = (i-2)*gh*.28; oy = -gh*(.35+.12*((i % 3)-1)); rr = gh*(.28+.1*(i % 2))
        d.ellipse([x+ox-rr, y+oy-rr, x+ox+rr, y+oy+rr], fill=l1)
        d.ellipse([x+ox-int(rr*.5), y+oy-int(rr*.4), x+ox+int(rr*.5), y+oy+int(rr*.6)], fill=l2)

def scene1():
    img = gradient(W, H, '#cfe8ff', '#eef7ff')
    img = blend(img, sun_layer(int(W*.78), int(H*.18), 60, (255, 244, 200)))
    c = ImageDraw.Draw(img, 'RGBA')
    clouds(c, int(W*.2), int(H*.12), 1.1, (255,255,255,220))
    clouds(c, int(W*.6), int(H*.08), .8, (255,255,255,200))
    clouds(c, int(W*.92), int(H*.2), .6, (255,255,255,190))
    rnd = random.Random(11); b1 = int(H*.42); p = []
    for i in range(25): p.append((i/24*W, b1 - int(rnd.random()*180)))
    p += [(W, H), (0, H)]; c.polygon(p, fill=hx('#9fc5e0'))
    rnd2 = random.Random(12); b2 = int(H*.5); p = []
    for i in range(19): p.append((i/18*W, b2 - int(rnd2.random()*220)))
    p += [(W, H), (0, H)]; c.polygon(p, fill=hx('#8fb3d6'))
    c.polygon([(0, int(H*.55)), (W, int(H*.5)), (W, H), (0, H)], fill=hx('#9ed66a'))
    c.polygon([(0, int(H*.62)), (W, int(H*.56)), (W, H), (0, H)], fill=hx('#7fbe57'))
    sd = random.Random(7)
    for _ in range(400):
        x = sd.randint(0, W); y = sd.randint(int(H*.62), H)
        c.ellipse([x-2, y-6, x+2, y], fill=hx(sd.choice(['#5da73f','#79c04f','#8ed060'])))
    bloom = Image.new('RGBA', (W, H), (0, 0, 0, 0)); bd = ImageDraw.Draw(bloom)
    for bx, gh, fl in [(int(W*.12), 360, 1), (int(W*.88), 420, -1)]:
        tw = int(gh*.03); bd.line([(bx, 0), (bx, int(gh*.4))], fill=(120,70,50,255), width=tw)
        for i in range(4):
            hy = int(gh*(.2+.16*i)); bx2 = bx + fl*int(gh*(.2+.1*i))
            bd.line([(bx, int(gh*.4)), (bx2, hy)], fill=(130,80,55,255), width=int(tw*.6))
            rd = random.Random(20+i)
            for _ in range(14):
                ox = bx2 + rd.randint(-int(gh*.28), int(gh*.28)); oy = hy + rd.randint(-int(gh*.15), int(gh*.06)); rr = rd.randint(6, 14)
                bd.ellipse([ox-rr, oy-rr, ox+rr, oy+rr], fill=(247,180,197,200))
                bd.ellipse([ox-int(rr*.5), oy-int(rr*.5), ox+int(rr*.5), oy+int(rr*.5)], fill=(255,230,240,230))
    img = blend(img, bloom)
    pet = Image.new('RGBA', (W, H), (0, 0, 0, 0)); pd = ImageDraw.Draw(pet); rd = random.Random(55)
    for _ in range(60):
        x = rd.randint(0, W); y = rd.randint(int(H*.3), H); rr = rd.randint(3, 7)
        pd.ellipse([x-rr, y-rr//2, x+rr, y+rr//2], fill=(255,200,214,200))
    return blend(img, pet)

def scene2():
    img = gradient(W, H, '#8fd0f5', '#e6f6ff')
    img = blend(img, sun_layer(int(W*.25), int(H*.18), 55, (255, 246, 200), blur=18))
    c = ImageDraw.Draw(img, 'RGBA')
    for (a, b, s) in [(.1, .14, 1.2), (.45, .1, .9), (.8, .22, .8), (.95, .12, .5)]:
        clouds(c, int(W*a), int(H*b), s, (255,255,255,225))
    c.ellipse([-W*.4, int(H*.28), W*.6, int(H*.8)], fill=hx('#a6d87a'))
    c.ellipse([W*.3, int(H*.26), W*1.3, int(H*.82)], fill=hx('#8fd06b'))
    c.polygon([(0, int(H*.5)), (W, int(H*.46)), (W, H), (0, H)], fill=hx('#f2d94e'))
    c.polygon([(0, int(H*.6)), (W, int(H*.56)), (W, H), (0, H)], fill=hx('#e6c93c'))
    rd = random.Random(3)
    for by, cnt in [(int(H*.66), 3), (int(H*.78), 2), (int(H*.9), 2)]:
        for i in range(cnt):
            y = by + i*int(H*.05) + rd.randint(-8, 8); c.line([(0, y), (W, y)], fill=hx('#cfad2e'), width=4)
    rd = random.Random(4)
    for _ in range(9):
        x = rd.randint(200, W-200); y = int(H*.47)+rd.randint(-14, 10); w = rd.randint(50, 90); gh = rd.randint(30, 50)
        c.polygon([(x, y), (x+w, y), (x+w//2, y-gh)], fill=hx('#b0442f'))
        c.rectangle([x+w//4, y, x+w*3//4, y+gh], fill=hx('#efe0c8'))
        c.rectangle([x+w//2-3, y+4, x+w//2+3, y+14], fill=hx('#6b4a2a'))
    rd = random.Random(5)
    for _ in range(6):
        x = rd.randint(100, W-100); y = int(H*.47)+rd.randint(0, 20); gh = rd.randint(140, 240)
        tree(c, x, y, gh, hx('#5a4632'), hx('#4e7a38'), hx('#6f9e46'))
    rd = random.Random(8)
    for _ in range(500):
        x = rd.randint(0, W); y = rd.randint(int(H*.6), H)
        c.ellipse([x-2, y-2, x+2, y+2], fill=hx(rd.choice(['#fff3a0','#f7d94b','#ffd93d'])))
    return img

def scene3():
    img = gradient(W, H, '#bde5ff', '#f2faff')
    img = blend(img, sun_layer(int(W*.7), int(H*.15), 50, (255, 242, 190), blur=16))
    c = ImageDraw.Draw(img, 'RGBA')
    clouds(c, int(W*.15), int(H*.12), 1.0, (255,255,255,220)); clouds(c, int(W*.85), int(H*.2), .7, (255,255,255,200))
    c.ellipse([-W*.5, int(H*.3), W*.7, int(H*.85)], fill=hx('#b7e2a0'))
    c.ellipse([W*.4, int(H*.3), W*1.4, int(H*.85)], fill=hx('#a7d98f'))
    c.polygon([(int(W*.34), 0), (int(W*.58), 0), (int(W*.5), H), (int(W*.3), H)], fill=hx('#bfe8f5'))
    c.polygon([(0, int(H*.42)), (int(W*.34), int(H*.4)), (int(W*.3), H), (0, H)], fill=hx('#7ec850'))
    c.polygon([(int(W*.58), int(H*.4)), (W, int(H*.44)), (W, H), (int(W*.5), H)], fill=hx('#7ec850'))
    for _ in range(40):
        x = random.Random(200+_).uniform(int(W*.31), int(W*.55)); y = random.Random(60+_).uniform(int(H*.45), H-20)
        ln = random.Random(80+_).uniform(30, 120); c.line([(x, y), (x+ln, y)], fill=hx('#e2f6ff'), width=2)
    def pt(c, _x, _y, gh):
        tw = int(gh*.05); c.line([(_x, _y), (_x, int(_y-gh*.5))], fill=(120,66,44,255), width=tw)
        rd = random.Random(int(_x))
        for i in range(6):
            hy = _y-int(gh*(.3+.12*i)); bx = _x+int(gh*(.2+.08*i))*(-1 if i % 2 else 1)
            c.line([(_x, int(_y-gh*.5)), (bx, hy)], fill=(130,72,46,255), width=int(tw*.6))
            for j in range(9):
                ox = bx+rd.randint(-int(gh*.26), int(gh*.26)); oy = hy+rd.randint(-int(gh*.14), int(gh*.05)); rr = rd.randint(6, 12)
                c.ellipse([ox-rr, oy-rr, ox+rr, oy+rr], fill=(245,168,185,210))
                c.ellipse([ox-int(rr*.5), oy-int(rr*.5), ox+int(rr*.5), oy+int(rr*.5)], fill=(255,222,232,235))
    pt(c, int(W*.16), int(H*.66), 250); pt(c, int(W*.84), int(H*.7), 280)
    for wx in [int(W*.62), int(W*.75)]:
        c.line([(wx, int(H*.4)), (wx, int(H*.55))], fill=(130,80,50,255), width=6); rd = random.Random(wx)
        for k in range(18):
            ox = wx+rd.randint(-20, 20); oy = int(H*.55)+k*4
            c.line([(ox, int(H*.55)), (ox+rd.randint(-8, 8), oy)], fill=(140,190,120,200), width=3)
    for _ in range(40):
        x = random.Random(300+_).uniform(int(W*.32), int(W*.54)); y = random.Random(90+_).uniform(int(H*.45), H-20)
        c.ellipse([x-4, y-2, x+4, y+2], fill=(255,190,205,210))
    return img

def scene4():
    img = gradient(W, H, '#aee0f7', '#f0faff')
    img = blend(img, sun_layer(int(W*.5), int(H*.12), 70, (255, 246, 190), blur=30, st=26))
    beam = Image.new('RGBA', (W, H), (0, 0, 0, 0)); bd = ImageDraw.Draw(beam); rd = random.Random(40)
    for i in range(8):
        a0 = rd.uniform(-.5, .5); a1 = rd.uniform(.6, 1.4)
        bd.polygon([(int(W*.5), int(H*.12)), (int(W*.5)+math.sin(a0)*1500, H+200), (int(W*.5)+math.sin(a1)*1500, H+200)], fill=(255,255,210, int(14+i*3)))
    beam = beam.filter(ImageFilter.GaussianBlur(6)); img = blend(img, beam)
    c = ImageDraw.Draw(img, 'RGBA')
    c.rectangle([0, int(H*.55), W, H], fill=hx('#9ed76e'))
    c.polygon([(0, int(H*.5)), (W, int(H*.46)), (W, int(H*.6)), (0, int(H*.62))], fill=hx('#8ccb60'))
    rd = random.Random(33)
    for i in range(9):
        x = int(W*(.05+.11*i)); tw = rd.randint(18, 30)
        c.rectangle([x-tw//2, int(H*.35), x+tw//2, int(H*.62)], fill=(120,84,50,255))
        c.line([(x, int(H*.4)), (x-tw*2, int(H*.34))], fill=(120,84,50,255), width=int(tw*.4))
        c.line([(x, int(H*.42)), (x+tw*2, int(H*.36))], fill=(120,84,50,255), width=int(tw*.4))
    tn = Image.new('RGBA', (W, H), (0, 0, 0, 0)); td = ImageDraw.Draw(tn); rd = random.Random(31)
    for i in range(9):
        x = int(W*(.05+.11*i)); hy = int(H*(.34+rd.uniform(0, .06)))
        for j in range(7):
            ox = x+rd.randint(-70, 70); oy = hy+rd.randint(-50, 30); rr = rd.randint(40, 80)
            td.ellipse([ox-rr, oy-rr//2, ox+rr, oy+rr//2], fill=(120,180,110,200))
    tn = tn.filter(ImageFilter.GaussianBlur(2)); img = blend(img, tn)
    c2 = ImageDraw.Draw(img, 'RGBA'); rd = random.Random(77)
    for _ in range(400):
        x = rd.randint(0, W); y = rd.randint(int(H*.55), H)
        c2.ellipse([x-2, y-8, x+2, y], fill=hx(rd.choice(['#6cb94a','#82cc5c','#5da83f'])))
    rd = random.Random(120)
    for _ in range(90):
        x = rd.randint(0, W); y = rd.randint(int(H*.6), H); rr = rd.randint(6, 14)
        colc = rd.choice(['#ffffff','#ffe77a','#ff9bd2','#ffffff','#ffd9a0'])
        for a in range(8):
            ax = x+int(rr*.8*math.cos(a*math.pi/4)); ay = y+int(rr*.5*math.sin(a*math.pi/4))
            c2.ellipse([ax-3, ay-3, ax+3, ay+3], fill=hx(colc))
        c2.ellipse([x-3, y-3, x+3, y+3], fill=hx('#f5c241'))
    return img

SCENES = [scene1, scene2, scene3, scene4]
SCENE_TITLES = ['花开似锦 · 樱花漫山', '金色田野 · 油菜花开', '桃花流水 · 春水盈盈', '春林新绿 · 阳光正好']

def stamp_title(img, text):
    try:
        font = ImageFont.truetype(FONT_PATH, 54); font_small = ImageFont.truetype(FONT_PATH, 30)
    except Exception:
        font = ImageFont.load_default(); font_small = font
    d = ImageDraw.Draw(img, 'RGBA'); sub = '· 春日 ·'
    d.text((34, H-120), text, font=font, fill=(255,255,255,120)); d.text((36, H-116), sub, font=font_small, fill=(255,255,255,120))
    d.text((30, H-120), text, font=font, fill=(40,70,40,255)); d.text((32, H-118), sub, font=font_small, fill=(60,90,50,255))
    return img

def ensure_default_images():
    """Write the 4 built-in spring images if not present."""
    out = []
    for i, fn in enumerate(SCENES):
        path = os.path.join(HERE, f'img{i+1}.png')
        if not os.path.exists(path):
            img = stamp_title(fn(), SCENE_TITLES[i]).convert('RGB').resize((W, H), Image.LANCZOS)
            img.save(path, 'PNG')
        out.append(path)
    return out

# ---------------------------------------------------------------------------
# 合成引擎
# ---------------------------------------------------------------------------
def ffmpeg_run(args, input_data=None):
    exe = ffmpeg_exe()
    proc = subprocess.Popen([exe] + args, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(input_data)
    return proc.returncode, out, err

def probe_duration(path):
    rc, out, err = ffmpeg_run(['-i', path, '-f', 'null', '-'])
    import re
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', err.decode('utf-8', 'ignore'))
    if not m:
        return None
    h, mm, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mm * 60 + s

def make_image_clip(img_path, dur, motion, out_path, w, h, fps):
    """Render one Ken Burns image clip (dur seconds) as an mp4 segment."""
    im = np.asarray(Image.open(img_path).convert('RGB'), dtype=np.float32)
    N = int(round(dur * fps))
    base_w = int(w * 1.2); base_h = int(h * 1.2)
    iw, ih = im.shape[1], im.shape[0]
    # downscale once to a small working canvas (output x ~1.2) for speed
    scale = max(base_w / iw, base_h / ih)
    nw = int(round(iw * scale)); nh = int(round(ih * scale))
    pil = Image.fromarray(np.clip(im, 0, 255).astype(np.uint8)).resize((nw, nh), Image.LANCZOS)
    ox = (nw - base_w) // 2; oy = (nh - base_h) // 2
    canvas_pil = pil.crop((ox, oy, ox + base_w, oy + base_h))
    canvas_w, canvas_h = canvas_pil.size

    def move(which, t):
        iw2, ih2 = canvas_w, canvas_h
        if which == 0:
            z = 1 - 0.26*t; cw = iw2*z; ch = ih2*z; cx = iw2/2; cy = ih2/2
        elif which == 1:
            cw = iw2*0.85; ch = ih2; cx = iw2/2 + (iw2*0.15/2)*t; cy = ih2/2
        elif which == 2:
            z = 0.74 + 0.26*t; cw = iw2*z; ch = ih2*z; cx = iw2/2; cy = ih2/2
        else:
            cw = iw2*0.8; ch = ih2*0.8; cx = iw2*(0.5+0.5*t); cy = ih2*(0.5+0.5*t)
        cw = min(cw, iw2); ch = min(ch, ih2)
        cx = max(cw/2, min(iw2-cw/2, cx)); cy = max(ch/2, min(ih2-ch/2, cy))
        return cx, cy, cw, ch

    # crop is cheap on PIL; resize per frame with BILINEAR (fast, good enough).
    out_frames = []
    for k in range(max(1, N)):
        t = k / max(1, N - 1)
        cx, cy, cw, ch = move(motion % 4, t)
        x0 = max(0, int(round(cx - cw / 2))); y0 = max(0, int(round(cy - ch / 2)))
        x1 = min(canvas_w, int(x0 + cw)); y1 = min(canvas_h, int(y0 + ch))
        win = canvas_pil.crop((x0, y0, x1, y1))
        out_frames.append(np.asarray(win.resize((w, h), Image.BILINEAR), dtype=np.float32))
    data = np.stack(out_frames) if len(out_frames) > 1 else out_frames[0][None]
    rc, o, e = ffmpeg_run(['-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}',
                            '-r', str(fps), '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                            '-preset', 'veryfast', '-threads', '0', '-crf', '20', out_path], input_data=data.astype(np.uint8).tobytes())
    return out_path

def make_video_clip(src, dur, out_path, w, h, fps):
    """Trim a source video to dur seconds and scale/pad to w x h; returns its real duration."""
    real = probe_duration(src) or dur
    use = min(dur, real)
    if use < 0.5:
        use = dur
    rc, o, e = ffmpeg_run(['-y', '-i', src, '-t', f'{use:.3f}',
                            '-vf', f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1',
                            '-r', str(fps), '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                            '-preset', 'veryfast', '-threads', '0', '-crf', '20', '-an', out_path])
    return out_path, use


def probe_audio_len(path):
    """Return audio duration in seconds using ffmpeg."""
    rc, out, err = ffmpeg_run(['-i', path])
    import re
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', err.decode('utf-8', 'ignore'))
    if not m:
        return None
    hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return hh * 3600 + mm * 60 + ss


def analyze_beats(path):
    """Analyze audio with librosa: return (bpm, beat_times_in_seconds).
    beat_times are precise floats (ms precision). Returns None if analysis fails."""
    try:
        import librosa
    except Exception:
        return None
    try:
        y, sr = librosa.load(path, sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units='frames')
        if beat_frames is None or len(beat_frames) == 0:
            tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beats = [float(v) for v in librosa.frames_to_time(beat_frames, sr=sr)]
        # filter out beats beyond audio length
        T = float(len(y)) / sr
        beats = [b for b in beats if b < T - 0.05]
        bpm_val = None
        try:
            arr = np.asarray(tempo)
            bpm_val = float(arr.ravel()[0] if arr.ndim else arr)
        except Exception:
            bpm_val = None
        return bpm_val, beats
    except Exception:
        return None


def plan_beat_durations(item_durs, beats, fade, step=1):
    """Return per-item display durations (list) such that:
       - total video length = sum(item_durs)  (driven by photo/video count & durations)
       - the N-1 interior cuts land on music beats, spaced `step` beats apart.
       step: 0.5 = every half-beat, 1 = every beat, 2 = every other beat, 4 = every 4th.
       Falls back to equal item_durs when beats are unusable."""
    N = len(item_durs)
    total = float(sum(item_durs))
    if N <= 1:
        return list(item_durs)
    try:
        step = float(step or 1.0)
    except (TypeError, ValueError):
        step = 1.0
    cum = []
    acc = 0.0
    for i in range(N - 1):
        acc += item_durs[i]
        cum.append(acc)

    # build a grid of allowed cut instants from the beats (incl. half-beat midpoints if step<1)
    grid = []
    if beats and len(beats) >= 2:
        for i in range(len(beats) - 1):
            b0, b1 = beats[i], beats[i + 1]
            if step < 1:
                grid.append((b0 + b1) / 2.0)
            grid.append(b1)
        grid = sorted(set(round(g, 4) for g in grid if g > 0))

    def nearest_allowed(cp):
        if not grid:
            return cp
        return min(grid, key=lambda g: abs(g - cp))

    snaps = [nearest_allowed(cp) for cp in cum]
    for i in range(1, len(snaps)):
        if snaps[i] <= snaps[i - 1]:
            snaps[i] = snaps[i - 1] + (0.3 * step if step >= 1 else 0.15)
    disp = []
    prev = 0.0
    for i in range(N - 1):
        disp.append(max(0.5, snaps[i] - prev))
        prev = snaps[i]
    last = max(0.6, total - prev)
    disp.append(last)
    return disp



# ---------------------------------------------------------------------------
# 内置免费踩点音乐曲库（Incompetech / CC.BY，可商用，署名即可）
# bpm/duration 为估算值，音频来自逐轨真实下载。
# ---------------------------------------------------------------------------
MUSIC_DIR = os.path.join(HERE, 'music_library')

MUSIC_CATALOG = [
    {'id': 'rising-game',    'title': 'Rising Game',      'genre': '电子/律动', 'bpm': 128, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Rising%20Game.mp3'},
    {'id': 'electro-cabello','title': 'Electro Cabello',  'genre': '电子/流行', 'bpm': 120, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Electro%20Cabello.mp3'},
    {'id': 'glitter-blast',  'title': 'Glitter Blast',    'genre': '电子/活力', 'bpm': 132, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Glitter%20Blast.mp3'},
    {'id': 'long-stroll',    'title': 'Long Stroll',      'genre': '轻快/行走', 'bpm': 110, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Long%20Stroll.mp3'},
    {'id': 'carefree',       'title': 'Carefree',         'genre': '轻快/乐观', 'bpm': 96,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3'},
    {'id': 'cambodian-odyssey','title': 'Cambodian Odyssey','genre': '世界/律动','bpm': 100, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Cambodian%20Odyssey.mp3'},
    {'id': 'wholesome',      'title': 'Wholesome',        'genre': '温暖/治愈', 'bpm': 90,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Wholesome.mp3'},
    {'id': 'wallpaper',      'title': 'Wallpaper',        'genre': '氛围/环境', 'bpm': 80,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Wallpaper.mp3'},
    {'id': 'monkeys-spinning','title': 'Monkeys Spinning Monkeys','genre':'幽默/欢乐','bpm':156, 'license':'CC.BY 4.0','attri':'Kevin MacLeod (incompetech.com)','licenseUrl':'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Monkeys%20Spinning%20Monkeys.mp3'},
    {'id': 'fluffing-a-duck','title': 'Fluffing a Duck',  'genre': '轻快/趣味', 'bpm': 105, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Fluffing%20a%20Duck.mp3'},
    {'id': 'airport-lounge', 'title': 'Airport Lounge',   'genre': '爵士/氛围', 'bpm': 92,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Airport%20Lounge.mp3'},
    {'id': 'prelude-and-action','title': 'Prelude and Action','genre':'电影/磅礴','bpm':118, 'license':'CC.BY 4.0','attri':'Kevin MacLeod (incompetech.com)','licenseUrl':'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Prelude%20and%20Action.mp3'},
    {'id': 'lightless-dawn', 'title': 'Lightless Dawn',   'genre': '氛围/缓拍', 'bpm': 84,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Lightless%20Dawn.mp3'},
]

def catalog_cached_size(mid):
    p = os.path.join(MUSIC_DIR, mid + '.mp3')
    if os.path.exists(p) and os.path.getsize(p) > 5000:
        try:
            return round(probe_audio_len(p), 2)
        except Exception:
            return None
    return None

def search_catalog(q=''):
    q = (q or '').strip().lower()
    os.makedirs(MUSIC_DIR, exist_ok=True)
    out = []
    for t in MUSIC_CATALOG:
        hay = (t['title'] + ' ' + t['genre'] + ' ' + t['id']).lower()
        if q and q not in hay:
            continue
        d = catalog_cached_size(t['id'])
        out.append({'id': t['id'], 'title': t['title'], 'genre': t['genre'],
                    'bpm': t['bpm'], 'license': t['license'], 'attri': t['attri'],
                    'licenseUrl': t['licenseUrl'], 'cached': d is not None,
                    'length': d})
    return out

def catalog_path(mid):
    return os.path.join(MUSIC_DIR, mid + '.mp3')

def download_catalog(mid):
    """Download a catalog track if not cached; return path or raise."""
    path = catalog_path(mid)
    if os.path.exists(path) and os.path.getsize(path) > 5000:
        return path
    track = next((t for t in MUSIC_CATALOG if t['id'] == mid), None)
    if not track:
        raise RuntimeError('未知曲目')
    os.makedirs(MUSIC_DIR, exist_ok=True)
    import urllib.request
    req = urllib.request.Request(track['licenseUrl'], headers={'User-Agent': 'Mozilla/5.0 SpringStudio'})
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, 'wb') as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(path) < 5000:
        os.remove(path)
        raise RuntimeError('下载失败')
    return path


# ---------------------------------------------------------------------------
# AI 能力：两套独立接口 —— 视觉(看图写文案) 与 TTS(中文配音) 可分别配 base_url/key/model
# 未配置该通道的 key 时，自动退回对应离线兜底，能力不中断。
# 配置结构（ai_config.json）：
#   vision:  {base_url, api_key, model}
#   tts:     {base_url, api_key, model, voice}
# ---------------------------------------------------------------------------
def _vision_available():
    v = load_ai_config().get('vision') or {}
    return bool(v.get('base_url') and v.get('api_key') and v.get('model'))


def _tts_available():
    t = load_ai_config().get('tts') or {}
    if not (t.get('api_key') and t.get('model')):
        return False
    # DashScope / MiMo have default endpoints, so base_url is optional
    if (t.get('provider') or 'openai').lower() in ('dashscope', 'mimo'):
        return True
    return bool(t.get('base_url'))


def ai_describe_image(img_path, name=''):
    """Use a vision (OpenAI-compatible) model to write a short Chinese caption for
    one image. Returns a short Chinese sentence. Falls back to offline template."""
    cfg = (load_ai_config().get('vision') or {})
    if not (cfg.get('base_url') and cfg.get('api_key') and cfg.get('model')):
        return offline_caption(name or img_path, 1, 1)
    try:
        import urllib.request, base64 as _b64, json as _json
        im = fromPIL(img_path, max_side=512)
        b64 = _b64.b64encode(im).decode('ascii')
        payload = {
            'model': cfg.get('model'),
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': '请用一句不超过20字的中文，描写这张春天风景图片的内容与氛围，直接输出这一句话，不要引号。'},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
                ],
            }],
            'max_tokens': 500,
            'temperature': 0.7,
        }
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        # content may be a string or a list; extract text, fall back if empty
        content = data['choices'][0]['message'].get('content')
        txt = ''
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            parts = [p.get('text', '') for p in content if isinstance(p, dict) and p.get('text')]
            txt = ''.join(parts)
        txt = (txt or '').strip()
        if txt:
            return txt[:40]
        return offline_caption(name or img_path, 1, 1)
    except Exception:
        return offline_caption(name or img_path, 1, 1)


def fromPIL(path, max_side=512):
    from PIL import Image as _I
    im = _I.open(path).convert('RGB')
    w, h = im.size
    scale = max_side / max(w, h)
    if scale < 1:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), _I.BILINEAR)
    import io
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=70)
    return buf.getvalue()


def ai_tts(text, out_path, voice=None):
    """Synthesize Chinese narration via configured TTS provider (openai-openai兼容 / dashscope通义千问). """
    cfg = (load_ai_config().get('tts') or {})
    api_key = cfg.get('api_key')
    model = cfg.get('model')
    if not (api_key and model):
        return False
    provider = (cfg.get('provider') or 'openai').lower()
    try:
        import urllib.request, json as _json
        if provider == 'dashscope':
            # 通义千问 / DashScope 非实时语音合成 (Model Studio) — 用配置的主机，缺省公共端点
            return _tts_dashscope(text, out_path, api_key, model, voice or cfg.get('voice', 'allmina'),
                                  cfg.get('base_url'))
        if provider == 'mimo':
            # 小米 MiMo 语音合成 (mimo.mi.com) — OpenAI 兼容 chat/completions + audio 参数
            return _tts_mimo(text, out_path, api_key, model, voice or cfg.get('voice', 'mimo_default'),
                             cfg.get('base_url'))
        # OpenAI-compatible
        url = (cfg.get('base_url', '').rstrip('/')) + '/audio/speech'
        payload = {'model': model, 'input': text,
                   'voice': voice or cfg.get('voice', 'alloy')}
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + api_key})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
        with open(out_path, 'wb') as f:
            f.write(data)
        return os.path.getsize(out_path) > 500
    except Exception:
        return False


def _tts_dashscope(text, out_path, api_key, model, voice, base_url=None):
    """通义千问 DashScope 非实时语音合成 HTTP API.
    Uses the configured base_url (defaults to the public DashScope endpoint).
    model 例: qwen-audio-turbo / qwen2.5-audio-turbo / cosyvoice-v1
    voice 例: allmina / longxiaochun / cherry ...
    Sets _LAST_TTS_ERR on failure for the test panel. Returns True on success."""
    global _LAST_TTS_ERR
    import urllib.request, json as _json
    base = (base_url or 'https://dashscope.aliyuncs.com/api/v1').rstrip('/')
    # DashScope non-realtime TTS path is appended to the /api/v1 root
    url = base + '/services/aigc/text2audio/tts'
    payload = {
        'model': model,
        'input': {'text': text},
        'voice': voice,
        'parameters': {'format': 'mp3', 'sample_rate': 48000},
    }
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + api_key})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            ctype = resp.headers.get('Content-Type', '')
            data = resp.read()
    except Exception as e:
        code = getattr(e, 'code', None)
        body = ''
        try:
            raw = getattr(e, 'read', lambda: b'')() if hasattr(e, 'read') else b''
            body = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        _LAST_TTS_ERR = f'HTTP{code}: {body[:200]}' if code else str(e)[:200]
        return False
    # If JSON came back, it's an error/status payload
    if 'json' in ctype.lower():
        try:
            obj = _json.loads(data.decode('utf-8', 'ignore'))
            _LAST_TTS_ERR = '服务返回: ' + json.dumps(obj, ensure_ascii=False)[:200]
            return False
        except Exception:
            pass
    with open(out_path, 'wb') as f:
        f.write(data)
    return os.path.getsize(out_path) > 500


def _tts_mimo(text, out_path, api_key, model, voice, base_url=None):
    """小米 MiMo 语音合成（mimo.mi.com）。走 OpenAI 兼容 chat/completions + audio 参数。
    model 例: mimo-v2.5-tts / mimo-v2.5-tts-voicedesign / mimo-v2.5-tts-voiceclone
    voice 例: mimo_default / Mia / Chloe / Milo / Dean
    文本放在 assistant 消息；返回 audio.data(base64)。失败时设置 _LAST_TTS_ERR。"""
    global _LAST_TTS_ERR
    import urllib.request, json as _json, base64 as _b64
    base = (base_url or 'https://api.xiaomimimo.com/v1').rstrip('/')
    url = base + '/chat/completions'
    payload = {
        'model': model,
        'messages': [{'role': 'assistant', 'content': text}],
        'audio': {'voice': voice, 'format': 'mp3'},
    }
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + api_key})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode('utf-8', 'ignore'))
    except Exception as e:
        code = getattr(e, 'code', None)
        body = ''
        try:
            raw = getattr(e, 'read', lambda: b'')() if hasattr(e, 'read') else b''
            body = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        _LAST_TTS_ERR = (f'HTTP{code}: {body[:200]}' if code else str(e)[:200])
        return False
    try:
        audio = data['choices'][0]['message']['audio']
        b64 = audio['data']
        raw = _b64.b64decode(b64)
        with open(out_path, 'wb') as f:
            f.write(raw)
        return os.path.getsize(out_path) > 500
    except Exception:
        _LAST_TTS_ERR = '返回格式异常: ' + _json.dumps(data, ensure_ascii=False)[:200]
        return False


# ---------------------------------------------------------------------------
# 逐通道测试：用当前填写的配置实调一次接口，反馈是否有效
# ---------------------------------------------------------------------------
def _test_vision():
    """Test the configured vision channel with a bundled spring image. Returns (ok, msg)."""
    cfg = (load_ai_config().get('vision') or {})
    if not (cfg.get('base_url') and cfg.get('api_key') and cfg.get('model')):
        return False, '未配置：请填 ① 视觉 的 base_url + api_key + model'
    try:
        import urllib.request, base64 as _b64, json as _json
        test_img = None
        for i in (1, 2, 3, 4):
            p = os.path.join(HERE, f'img{i}.png')
            if os.path.exists(p):
                test_img = p
                break
        if not test_img:
            test_img = os.path.join(HERE, 'img1.png')
        im = fromPIL(test_img, max_side=256)
        b64 = _b64.b64encode(im).decode('ascii')
        payload = {
            'model': cfg.get('model'),
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': '只回复“OK”，表示你能看到图片。'},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
            ]}],
            'max_tokens': 20,
        }
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        txt = data['choices'][0]['message']['content'].strip()
        return True, f'有效（模型回复：{txt[:20]}）'
    except Exception as e:
        body = ''
        try:
            raw = getattr(e, 'read', lambda: b'')() if hasattr(e, 'read') else b''
            body = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        code = getattr(e, 'code', None)
        return False, f'失败{(" HTTP"+str(code)) if code else ""}：{str(e)[:120]} {body[:200]}'


def _test_tts():
    """Test the configured TTS channel by synthesizing a short phrase. Returns (ok, msg)."""
    global _LAST_TTS_ERR
    cfg = (load_ai_config().get('tts') or {})
    if not (cfg.get('api_key') and cfg.get('model')):
        return False, '未配置：请填 ② TTS 的 api_key + 模型'
    os.makedirs(WORKDIR, exist_ok=True)
    out = os.path.join(WORKDIR, f'_tts_test_{int(time.time()*1000)}.mp3')
    _LAST_TTS_ERR = ''
    ok = ai_tts('春天来了', out)
    if ok and os.path.exists(out):
        size = os.path.getsize(out)
        try:
            os.remove(out)
        except Exception:
            pass
        return True, f'有效（生成 {round(size/1024,1)}KB 音频）'
    try:
        if os.path.exists(out):
            os.remove(out)
    except Exception:
        pass
    if _LAST_TTS_ERR:
        return False, '失败：' + _LAST_TTS_ERR
    provider = (cfg.get('provider') or 'openai').lower()
    if provider == 'dashscope':
        return False, '失败：请确认 DashScope Key 有效、模型已开通（如 qwen-audio-turbo）'
    return False, '失败：请确认 base_url/Key/model 正确，接口位于 {base_url}/audio/speech'


def build_srt(captions, starts, durs, out_path):
    """captions[i] with start/end seconds -> SRT subtitle file."""
    def ts(sec):
        hh = int(sec // 3600); mm = int((sec % 3600) // 60); ss = int(sec % 60); ms = int((sec % 1) * 1000)
        return f'{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}'
    lines = []
    for i, cap in enumerate(captions, 1):
        s, d = starts[i - 1], durs[i - 1]
        lines.append(str(i))
        lines.append(f'{ts(s)} --> {ts(s + d)}')
        lines.append(cap)
        lines.append('')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return out_path


# ---------------------------------------------------------------------------
# 🎯 智能强卡点引擎：场景切换/动作停顿帧 ↔ 音乐大鼓点 匹对
# 全部本地计算（ffmpeg scene 检测 + librosa 强拍检测 + numpy 帧差），不花 API 钱
# ---------------------------------------------------------------------------
def detect_scene_cuts(video_path, threshold=0.30):
    """用 ffmpeg scene 滤镜检测视频场景切换点，返回切点秒列表（升序）。"""
    import re
    rc, out, err = ffmpeg_run(['-hide_banner', '-nostats', '-i', video_path,
                               '-vf', f"select='gt(scene,{threshold})',showinfo",
                               '-an', '-f', 'null', '-'])
    cuts = []
    for m in re.finditer(r'pts_time:([0-9.]+)', err.decode('utf-8', 'ignore')):
        t = float(m.group(1))
        if t > 0.3:
            cuts.append(round(t, 3))
    cuts = sorted(set(cuts))
    return cuts


def detect_motion_points(video_path, fps_s=4.0, min_gap=0.6):
    """抽帧做帧间差：动作剧烈处(帧差峰值)作为候选切点，停顿处(低帧差)作为休息点。
    返回候选切点秒列表（升序）。"""
    try:
        import subprocess as _sp
        from PIL import Image as _I
        import io
    except Exception:
        return []
    # sample frames into memory via ffmpeg rawvideo
    exe = ffmpeg_exe()
    proc = _sp.Popen([exe, '-hide_banner', '-i', video_path, '-vf', f'fps={fps_s}',
                      '-f', 'rawvideo', '-pix_fmt', 'gray', '-vcodec', 'rawvideo', '-'],
                     stdout=_sp.PIPE, stderr=_sp.DEVNULL)
    # need width/height: probe
    import re
    rc, out, err = ffmpeg_run(['-i', video_path])
    m = re.search(r'(\d{2,4})x(\d{2,4})', err.decode('utf-8', 'ignore'))
    if not m:
        try:
            proc.kill()
        except Exception:
            pass
        return []
    w, h = int(m.group(1)), int(m.group(2))
    frame_bytes = w * h
    prev = None
    diffs = []
    t = 0.0
    while True:
        data = proc.stdout.read(frame_bytes)
        if not data or len(data) < frame_bytes:
            break
        import numpy as np
        cur = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        if prev is not None:
            diffs.append((t, float(np.abs(cur - prev).mean())))
        prev = cur
        t += 1.0 / fps_s
    try:
        proc.kill()
    except Exception:
        pass
    if len(diffs) < 4:
        return []
    vals = [d for _, d in diffs]
    import numpy as np
    med = float(np.median(vals))
    # motion spike = diff much larger than baseline; use adaptive threshold
    th = max(2.5 * med, 1.0)
    cands = [t for t, d in diffs if d > th]
    # filter: keep spikes, drop within min_gap
    out2 = []
    last = -1e9
    for c in cands:
        if c - last >= min_gap:
            out2.append(round(c, 2))
            last = c
    return out2


def detect_strong_beats(music_path, top_k=None, min_sep=0.25):
    """检测音乐"大鼓点"（强 onset 峰值）。返回(强拍秒列表升序, 每秒拍数估计)。"""
    try:
        import librosa
        import numpy as np
    except Exception:
        return [], None
    try:
        y, sr = librosa.load(music_path, sr=22050, mono=True)
        if len(y) < sr:
            return [], None
        hop = 512
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
        times = librosa.times_like(onset_env, sr=sr, hop_length=hop)
        # peaks
        peaks = librosa.util.peak_pick(onset_env, pre_max=8, post_max=8, pre_avg=8,
                                       post_avg=8, delta=0.18, wait=min_sep)
        if len(peaks) == 0:
            return [], None
        pts = [float(times[p]) for p in peaks]
        vals = [float(onset_env[p]) for p in peaks]
        # keep the strongest onsets as "大鼓点"
        if top_k and len(vals) > top_k:
            idx = sorted(range(len(vals)), key=lambda i: vals[i], reverse=True)[:top_k]
            idx.sort()
            pts = [pts[i] for i in idx]
        # estimate beats-per-second from inter-peak median
        gaps = [pts[i+1] - pts[i] for i in range(len(pts) - 1) if pts[i+1] - pts[i] > 0.2]
        bps = None
        if gaps:
            import numpy as np
            bps = 1.0 / float(np.median(gaps))
        return pts, bps
    except Exception:
        return [], None


def plan_beat_cuts(scene_cuts, motion_cuts, beats, video_dur, min_seg=0.8, max_seg=9.0, tol=0.35):
    """把视频切点(场景+动作)匹对到最近强拍，生成强卡点时间线。
    返回切点列表（含 0 与 video_dur 两端）。"""
    if not beats:
        beats = []
    merged = sorted(set([round(x, 2) for x in (scene_cuts or []) + (motion_cuts or [])]))
    cuts = []
    used = set()
    for c in merged:
        if c <= 0.3 or c >= video_dur - 0.3:
            continue
        # nearest strong beat
        best = min(beats, key=lambda b: abs(b - c)) if beats else None
        target = best if (best is not None and abs(best - c) <= tol) else c
        if any(abs(target - u) < min_seg for u in used):
            continue
        cuts.append(round(target, 3))
        used.add(target)
    cuts.sort()
    # build final timeline: 0 ... cuts ... video_dur, enforce segment length bounds
    timeline = [0.0]
    for c in cuts:
        if c - timeline[-1] >= min_seg and video_dur - c >= min_seg * 0.6:
            timeline.append(c)
    if video_dur - timeline[-1] < 0.4:
        timeline.pop()
    timeline.append(video_dur)
    return timeline


def beat_cut_video(video_path, music_path, run_dir, params, progress=None):
    """智能强卡点主流程：分析→对齐→硬切拼接→配乐。返回 final 路径与诊断信息。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    up('检测场景切换', 5)
    scene_cuts = detect_scene_cuts(video_path, threshold=float(params.get('sceneTh', 0.30)))
    up('检测动作停顿帧', 12)
    motion_cuts = detect_motion_points(video_path, fps_s=4.0)
    up('分析音乐大鼓点', 20)
    strong_beats, bps = detect_strong_beats(music_path, top_k=int(params.get('maxCuts', 30)))
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        raise RuntimeError('无法读取视频时长')
    timeline = plan_beat_cuts(scene_cuts, motion_cuts, strong_beats, vdur)
    up('按鼓点硬切拼接', 30)
    # hard-cut concat segments
    segs = []
    for i in range(len(timeline) - 1):
        seg = os.path.join(run_dir, f'bc{i}.mp4')
        segs.append(seg)
        seg_dur = timeline[i+1] - timeline[i]
        make_video_clip(video_path, seg_dur, seg, w=int(params.get('w', W)), h=int(params.get('h', H)), fps=int(params.get('fps', 30)))
    # concat
    parts = ''.join(f'[{i}:v]' for i in range(len(segs)))
    fc = f'{parts}concat=n={len(segs)}:v=1:a=0[vout]'
    silent = os.path.join(run_dir, 'bc_silent.mp4')
    cmd = ['-y']
    for s in segs:
        cmd += ['-i', s]
    cmd += ['-filter_complex', fc, '-map', '[vout]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-preset', 'veryfast', '-threads', '0', silent]
    rc, o, e = ffmpeg_run(cmd)
    if rc != 0:
        raise RuntimeError('卡点拼接失败: ' + e.decode('utf-8', 'ignore')[-400:])
    up('合成配乐', 55)
    final = os.path.join(run_dir, 'final.mp4')
    # music looped/truncated to video length
    cmd = ['-y', '-stream_loop', '-1', '-i', music_path, '-i', silent,
           '-map', '1:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
           '-shortest', '-movflags', '+faststart', final]
    rc, o, e = ffmpeg_run(cmd)
    if rc != 0:
        raise RuntimeError('配乐失败: ' + e.decode('utf-8', 'ignore')[-400:])
    if progress:
        progress['done'] = True
        progress['pct'] = 100
        progress['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
    diag = {
        'scene_cuts': scene_cuts,
        'motion_cuts': motion_cuts,
        'strong_beats': strong_beats[:40],
        'timeline': timeline,
        'segments': len(timeline) - 1,
    }
    return final, diag


def assemble(items, params, music=None, progress=None):
    """items: list of {kind:'image'|'video', src, dur, motion}
       params: {w, h, fps, transition}
       music: optional path to an audio file (mp3/wav).
       progress: optional mutable dict updated with phase/pct/done/error for a UI poller.
       If music given: clips are beat-aligned, audio is the music, total = music length.
       If no music: fall back to equal-duration clips with no audio track.
       Returns path to final mp4, or raises."""
    def up(phase, pct):
        if progress is not None:
            progress['phase'] = phase
            progress['pct'] = pct
    up('解析素材', 2)
    shutil.rmtree(OUTDIR, ignore_errors=True)
    os.makedirs(OUTDIR, exist_ok=True)
    run_dir = os.path.join(OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(run_dir, exist_ok=True)
    w = int(params.get('w', W)); h = int(params.get('h', H))
    fps = int(params.get('fps', 30))
    trans = params.get('transition', 'fade')

    # ---- resolve per-item display durations (driven by photo/video count & durations ----
    N = len(items)
    if N == 0:
        raise RuntimeError('没有可合成的素材')
    item_durs = [float(it.get('dur', 3)) for it in items]
    d0 = item_durs[0] if item_durs else 3
    if params.get('hardCut'):
        fade = 0.0   # hard cut: precise on-beat switching with no crossfade
    else:
        fade = min(0.6, d0 / 2)
    target_total = float(sum(item_durs))

    if music:
        analysis = analyze_beats(music)
        bpm, beats = (analysis if analysis else (None, []))
        mlen = probe_audio_len(music) or 0.0
        if mlen <= 0:
            raise RuntimeError('无法读取音乐时长')
        # video length = photo-driven; interior cuts land near beats with given interval
        step = float(params.get('beatStep', 1) or 1)
        disp = plan_beat_durations(item_durs, beats or [], fade, step)
        total_len = float(sum(disp))
        beat_info = {'bpm': round(float(bpm), 1) if bpm is not None else None,
                     'beat_count': len(beats),
                     'music_len': round(mlen, 2),
                     'clips': N,
                     'beatStep': step,
                     'durations': [round(float(x), 3) for x in disp]}
    else:
        total_len = target_total
        beat_info = {'bpm': None, 'beat_count': 0,
                     'durations': [round(float(x), 3) for x in item_durs]}
        disp = list(item_durs)

    # timing list of (start, disp) for segment building
    timing = []
    s = 0.0
    for i, d in enumerate(disp):
        timing.append((s, d))
        s += d
    if music and total_len is not None and total_len > 0:
        total_len = s

    # ---- 1) build segments with their intended display duration + fade padding ----
    segments = []
    real_durs = []
    for idx, it in enumerate(items):
        if progress is not None and progress.get('abort'):
            raise RuntimeError('已取消')
        up(f'渲染镜头 {idx + 1}/{len(items)}', 8 + int(52 * idx / max(1, len(items))))
        start, disp = timing[idx]
        # pad each segment by `fade` so the xfade overlap keeps the total timeline
        seg_dur = disp + fade
        seg = os.path.join(run_dir, f'seg{idx}.mp4')
        if it['kind'] == 'image':
            make_image_clip(it['src'], seg_dur, int(it.get('motion', idx % 4)), seg, w, h, fps)
            real_durs.append(seg_dur)
        else:
            seg, real = make_video_clip(it['src'], seg_dur, seg, w, h, fps)
            real_durs.append(real)
        segments.append(seg)
    up('合并片段(转场)', 64)

    if len(segments) == 1:
        src_out = os.path.join(run_dir, 'vid_silent.mp4')
        shutil.copy(segments[0], src_out)
    else:
        for i, s in enumerate(segments):
            if not os.path.exists(s) or os.path.getsize(s) < 100:
                raise RuntimeError(f'片段 {i} 生成失败')
        if fade <= 0:
            # hard cut: pure concat (each segment exact), total = sum(disp)
            parts = ''.join(f'[{i}:v]' for i in range(len(segments)))
            filter_str = f'{parts}concat=n={len(segments)}:v=1:a=0[vout]'
        else:
            # xfade chain: offset_k puts the transition near the beat start.
            offsets = []
            acc = real_durs[0]
            for i in range(1, len(segments)):
                offsets.append(acc - fade)
                acc += real_durs[i] - fade
            chain = []
            prev = '[0:v]'
            for i in range(1, len(segments)):
                out_label = 'vout' if i == len(segments) - 1 else f'x{i}'
                chain.append(f"{prev}[{i}:v]xfade=transition={trans}:duration={fade:.3f}:offset={offsets[i-1]:.3f}[{out_label}]")
                prev = f'[{out_label}]'
            filter_str = ';'.join(chain)
        cmd = ['-y']
        for s in segments:
            cmd += ['-i', s]
        cmd += ['-filter_complex', filter_str, '-map', '[vout]', '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p', '-preset', 'veryfast', '-threads', '0',
                os.path.join(run_dir, 'vid_silent.mp4')]
        rc, o, e = ffmpeg_run(cmd)
        if rc != 0:
            raise RuntimeError('合成失败: ' + e.decode('utf-8', 'ignore')[-600:])

    # assemble returns the concatenated silent video (no audio yet).
    if progress is not None:
        progress['pct'] = 70
        progress['done'] = False
        progress['beat'] = beat_info
        progress['duration'] = round(float(total_len), 2)
    return os.path.join(run_dir, 'vid_silent.mp4'), total_len, beat_info


# ---------------------------------------------------------------------------
# 后期：烧字幕(.srt) + AI配音 + 背景音乐混音，输出最终 final.mp4
# ---------------------------------------------------------------------------
def finalize(video_path, params, music, captions, durations=None, progress=None):
    """video_path: assembled silent mp4 (len = photo-driven total)
       music: optional bg audio
       captions: optional list of per-clip Chinese captions (else no subs/narration)
       durations: optional per-clip display durations (from assemble) for exact subtitle/narration timing
       Returns final mp4 path. Raises on failure."""
    run_dir = os.path.dirname(video_path)
    w = int(params.get('w', W)); h = int(params.get('h', H))
    voice_over = bool(captions)
    voice_ok = False
    narration_path = None
    srt_path = None

    # 1) subtitles (align each caption to its clip's display window)
    total = probe_audio_len(video_path) or 0.0
    N = len(captions) if captions else 0
    if N > 0:
        starts = []; durs = []
        if durations and len(durations) == N:
            acc = 0.0
            for dd in durations:
                starts.append(acc); acc += dd
            durs = list(durations)
            # scale to actual total if slight mismatch
            sm = sum(durs)
            if sm > 0 and abs(sm - total) > 0.05:
                k = total / sm
                durs = [x * k for x in durs]
                starts = []
                acc = 0.0
                for dd in durs:
                    starts.append(acc); acc += dd
        else:
            d = total / max(1, N)
            starts = [i * d for i in range(N)]
            durs = [d for _ in range(N)]
        if progress:
            progress['phase'] = '生成字幕'
            progress['pct'] = 74
        srt_path = os.path.join(run_dir, 'subs.srt')
        build_srt(captions, starts, durs, srt_path)
        # burn subtitles (needs libass) — fallback: if filter fails, keep unburned
        burned = os.path.join(run_dir, 'vid_sub.mp4')
        # escape path for filter
        esc = srt_path.replace('\\', '/').replace(':', '\\:').replace('\'', '\\\'')
        rc, o, e = ffmpeg_run(['-y', '-i', video_path,
                               '-vf', f"subtitles='{esc}':force_style='FontName=Microsoft YaHei,FontSize=20,Alignment=2,MarginV=40'",
                               '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                               '-preset', 'veryfast', '-threads', '0', '-an', burned])
        if rc == 0 and os.path.exists(burned):
            video_path = burned
        if progress:
            progress['phase'] = '配音合成'
            progress['pct'] = 80

    # 2) TTS narration for each caption (only if TTS configured & succeeded)
    narration_path = None
    if voice_over and _tts_available() and not params.get('economy'):
        clips = []
        for i, cap in enumerate(captions if N else []):
            if not (cap and cap.strip()):
                continue
            np_ = os.path.join(run_dir, f'nar{i}.mp3')
            od = starts[i] if i < len(starts) else (total / max(1, N)) * i
            if ai_tts(cap, np_):
                clips.append((np_, od, durs[i] if i < len(durs) else 0))
        if clips:
            narration_path = os.path.join(run_dir, 'narration.m4a')
            inputs = []
            fparts = []
            for k2, (np_, od, odur) in enumerate(clips):
                inputs += ['-i', np_]
                fparts.append(f'[{k2}:a]adelay={int(od*1000)}|{int(od*1000)},apad=whole_dur={int(total*1000)}[v{k2}]')
            mixin = ''.join(f'[v{k2}]' for k2 in range(len(clips)))
            fparts.append(f'{mixin}amix=inputs={len(clips)}:normalize=0,atrim=0:{total:.3f},aformat=fltp[aout]')
            fc = ';'.join(fparts)
            cmd = ['-y'] + inputs + ['-filter_complex', fc, '-map', '[aout]',
                                     '-c:a', 'aac', '-b:a', '160k', narration_path]
            rc, o, e = ffmpeg_run(cmd)
            if rc == 0 and os.path.exists(narration_path) and os.path.getsize(narration_path) > 500:
                voice_ok = True
            else:
                narration_path = None

    # 3) final mux: video(+subs burned) + bg music + narration(mixed) if any
    final = os.path.join(run_dir, 'final.mp4')
    if progress:
        progress['phase'] = '合成音频轨'
        progress['pct'] = 90
    if music and narration_path and os.path.exists(narration_path):
        # video + narration as main audio, music as quieter bg
        cmd = ['-y', '-stream_loop', '-1', '-i', music, '-i', narration_path, '-i', video_path,
               '-filter_complex',
               f'[0:a]volume=0.45[bg0];[1:a]aformat=fltp[na];[bg0][na]amix=inputs=2:normalize=0[aout]',
               '-map', '2:v:0', '-map', '[aout]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-shortest', '-movflags', '+faststart', final]
    elif narration_path and os.path.exists(narration_path):
        # narration only (no bg music)
        cmd = ['-y', '-i', narration_path, '-i', video_path,
               '-map', '1:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-movflags', '+faststart', final]
    elif music:
        cmd = ['-y', '-stream_loop', '-1', '-i', music, '-i', video_path,
               '-map', '1:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac',
               '-b:a', '192k', '-shortest', '-movflags', '+faststart', final]
    else:
        cmd = ['-y', '-i', video_path,
               '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart', final]
    rc, o, e = ffmpeg_run(cmd)
    if rc != 0:
        raise RuntimeError('最终合成失败: ' + e.decode('utf-8', 'ignore')[-600:])
    if progress:
        progress['phase'] = '完成'
        progress['pct'] = 100
        progress['done'] = True
        progress['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
    return final


# ---------------------------------------------------------------------------
# 本地 HTTP 服务 + 图形化前端
# ---------------------------------------------------------------------------
MIME = {
    '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.mp4': 'video/mp4',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, content, ctype='text/plain; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            idx = os.path.join(STATIC_DIR, 'index.html')
            if os.path.exists(idx):
                self._send(200, open(idx, 'rb').read(), 'text/html; charset=utf-8')
            else:
                self._send(500, '前端文件缺失：请确保 static/ 目录存在'.encode('utf-8'), 'text/html; charset=utf-8')
            return
        if path.startswith('/static/'):
            name = path[len('/static/'):].split('?')[0]
            full = os.path.join(STATIC_DIR, os.path.basename(name))
            if os.path.isfile(full):
                ext = os.path.splitext(full)[1].lower()
                self._send(200, open(full, 'rb').read(), MIME.get(ext, 'application/octet-stream'))
                return
            self._send(404, b'not found')
            return
        if path.startswith('/media/'):
            name = path[len('/media/'):].split('?')[0]
            # first look in run output dir, then the folder containing built-in assets
            for base in (OUTDIR, HERE):
                full = os.path.join(base, name)
                if os.path.isfile(full):
                    ext = os.path.splitext(full)[1].lower()
                    self._send(200, open(full, 'rb').read(), MIME.get(ext, 'application/octet-stream'))
                    return
            self._send(404, b'not found')
            return
        if path.startswith('/music_lib/'):
            name = path[len('/music_lib/'):].split('?')[0]
            full = os.path.join(MUSIC_DIR, name)
            if os.path.isfile(full):
                self._send(200, open(full, 'rb').read(), MIME.get('.mp3', 'audio/mpeg'))
                return
            self._send(404, b'not found')
            return
        if path == '/api/music/search':
            q = parse_qs(urlparse(self.path).query).get('q', [''])[0]
            self._send(200, json.dumps({'ok': True, 'results': search_catalog(q)}).encode('utf-8'),
                       'application/json')
            return
        if path == '/api/music/use':
            q = parse_qs(urlparse(self.path).query).get('id', [None])[0]
            if not q:
                self._send(200, json.dumps({'ok': False, 'error': '缺少 id'}).encode('utf-8'), 'application/json')
                return
            try:
                p = download_catalog(q)
                self._send(200, json.dumps({'ok': True, 'file': os.path.basename(p),
                                            'url': '/music_lib/' + os.path.basename(p)}).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/progress':
            runid = parse_qs(urlparse(self.path).query).get('run', [None])[0]
            if not runid or runid not in PROGRESS:
                self._send(404, json.dumps({'error': '未知 run'}).encode('utf-8'), 'application/json')
                return
            self._send(200, json.dumps(PROGRESS[runid]).encode('utf-8'), 'application/json')
            return
        if path == '/api/history':
            self._send(200, json.dumps({'ok': True, 'history': load_history(50)}).encode('utf-8'),
                       'application/json')
            return
        if path == '/api/ai/config':
            cfg = load_ai_config()
            def mask(ch):
                ch = dict(ch or {})
                if ch.get('api_key'):
                    ch['api_key'] = ('*' * 6) + ch['api_key'][-4:]
                return ch
            self._send(200, json.dumps({
                'ok': True,
                'config': {'vision': mask(cfg.get('vision')), 'tts': mask(cfg.get('tts'))},
                'vision_available': _vision_available(),
                'tts_available': _tts_available(),
            }).encode('utf-8'), 'application/json')
            return
        if path == '/api/ai/test':
            # run the tests (network) and report both channels; block current thread until done
            v_ok, v_msg = _test_vision()
            t_ok, t_msg = _test_tts()
            self._send(200, json.dumps({'ok': True,
                                        'vision': {'test_ok': v_ok, 'message': v_msg},
                                        'tts': {'test_ok': t_ok, 'message': t_msg},
                                        }).encode('utf-8'), 'application/json')
            return
        self._send(404, b'not found')

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/build':
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length > 220 * 1024 * 1024:
                    self._send(200, json.dumps({'ok': False, 'error': '请求过大(>220MB)'}).encode('utf-8'), 'application/json')
                    return
                # read body in chunks to keep the socket responsive
                raw = b''
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    raw += chunk
                    remaining -= len(chunk)
                req = json.loads(raw.decode('utf-8'))
                RUNSEQ[0] += 1
                runid = 'run-%d' % RUNSEQ[0]
                PROGRESS[runid] = {'phase': '排队', 'pct': 0, 'done': False}
                # run the build in a background thread; the UI polls /api/progress
                def _do_build():
                    prog = PROGRESS[runid]
                    try:
                        params = req.get('params', {})
                        items = req.get('items', [])
                        music_data = req.get('music')
                        music_path = None
                        if music_data:
                            if music_data.get('source') == 'catalog':
                                prog['phase'] = '准备曲库音乐'
                                music_path = download_catalog(music_data.get('catalogId', ''))
                            elif music_data.get('data'):
                                mdata = base64.b64decode(music_data.get('data', ''))
                                mname = music_data.get('name', 'music.mp3')
                                mpath = os.path.join(WORKDIR, 'music_' + str(int(time.time() * 1000)) +
                                                     (os.path.splitext(mname)[1] or '.mp3'))
                                os.makedirs(WORKDIR, exist_ok=True)
                                open(mpath, 'wb').write(mdata)
                                music_path = mpath
                        # build work items
                        work = []
                        for idx, it in enumerate(items):
                            if it['kind'] == 'image':
                                data = base64.b64decode(it.get('data', ''))
                                ext = os.path.splitext(it.get('name', 'x.jpg'))[1] or '.jpg'
                                fp = os.path.join(WORKDIR, f'up_{len(work)}_{idx}_img{ext}')
                                os.makedirs(WORKDIR, exist_ok=True)
                                open(fp, 'wb').write(data)
                                work.append({'kind': 'image', 'src': fp, 'dur': it.get('dur', 3), 'motion': len(work) % 4})
                            else:
                                data = base64.b64decode(it.get('data', ''))
                                fp = os.path.join(WORKDIR, f'up_{len(work)}_{idx}_vid.mp4')
                                os.makedirs(WORKDIR, exist_ok=True)
                                open(fp, 'wb').write(data)
                                work.append({'kind': 'video', 'src': fp, 'dur': it.get('dur', 3)})
                        if not work:
                            defaults = ensure_default_images()
                            single = params.get('singleDur', 3) or 3
                            for i, p in enumerate(defaults):
                                work.append({'kind': 'image', 'src': p, 'dur': single, 'motion': i})
                        # optional automatic captions (vision-ai or offline template)
                        captions = None
                        if params.get('ai_captions') and work:
                            prog['phase'] = '按画面生成文案'
                            prog['pct'] = 4
                            economy = bool(params.get('economy'))
                            captions = []
                            for w_ in work:
                                if economy:
                                    # 省流模式：用离线模板，不调用付费视觉模型
                                    cap = offline_caption(w_.get('src', ''), 0, len(work))
                                else:
                                    cap = ai_describe_image(w_['src'], w_.get('src', ''))
                                captions.append(cap)
                            params['economy'] = economy
                        up_local = prog
                        vid, total_len, beat_info = assemble(work, params, music_path, prog)  # music used for beat analysis (no mux here)
                        final = finalize(vid, params, music_path, captions,
                                         (beat_info or {}).get('durations'), prog)
                        prog['done'] = True
                        prog['pct'] = 100
                        prog['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
                        prog['duration'] = round(float(total_len), 2)
                        prog['beat'] = beat_info
                        prog['captions'] = captions
                        # record to history
                        try:
                            add_history({
                                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                                'file': prog['file'],
                                'duration': prog['duration'],
                                'music': (music_data.get('name') if isinstance(music_data, dict) else None),
                                'voice': bool(captions and _tts_available()),
                                'captions': captions,
                                'w': params.get('w', W), 'h': params.get('h', H),
                                'fps': params.get('fps', 30),
                            })
                        except Exception:
                            pass
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        prog['done'] = True
                        prog['error'] = str(e)
                _threading.Thread(target=_do_build, daemon=True).start()
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/ai/config':
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                cfg = load_ai_config()
                # incoming shape: { vision: {base_url,api_key,model}, tts:{base_url,api_key,model,voice} }
                for ch in ('vision', 'tts'):
                    inc = data.get(ch)
                    if not isinstance(inc, dict):
                        continue
                    cur = dict(cfg.get(ch) or {})
                    for k, v in inc.items():
                        if v is not None:
                            cur[k] = str(v).strip()
                        elif k in cur:
                            del cur[k]
                    cfg[ch] = cur
                save_ai_config(cfg)
                self._send(200, json.dumps({'ok': True,
                                            'vision_available': _vision_available(),
                                            'tts_available': _tts_available()}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/cancel':
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                runid = data.get('runid')
                if runid and runid in PROGRESS:
                    PROGRESS[runid]['abort'] = True
                    self._send(200, json.dumps({'ok': True}).encode('utf-8'), 'application/json')
                else:
                    self._send(200, json.dumps({'ok': False, 'error': '未知 run'}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/beatcut':
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length > 300 * 1024 * 1024:
                    self._send(200, json.dumps({'ok': False, 'error': '请求过大'}).encode('utf-8'), 'application/json')
                    return
                raw = b''
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    raw += chunk
                    remaining -= len(chunk)
                req = json.loads(raw.decode('utf-8'))
                params = req.get('params', {})
                RUNSEQ[0] += 1
                runid = 'run-%d' % RUNSEQ[0]
                PROGRESS[runid] = {'phase': '排队', 'pct': 0, 'done': False}
                def _do_bc():
                    prog = PROGRESS[runid]
                    try:
                        shutil.rmtree(OUTDIR, ignore_errors=True)
                        os.makedirs(OUTDIR, exist_ok=True)
                        run_dir = os.path.join(OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
                        os.makedirs(run_dir, exist_ok=True)
                        # save video
                        vdata = base64.b64decode(req.get('video', {}).get('data', ''))
                        vp = os.path.join(run_dir, 'src_video' + (os.path.splitext(req.get('video', {}).get('name', 'x.mp4'))[1] or '.mp4'))
                        open(vp, 'wb').write(vdata)
                        # music: catalog or upload
                        mdata = req.get('music')
                        mp = None
                        if mdata:
                            if mdata.get('source') == 'catalog':
                                mp = download_catalog(mdata.get('catalogId', ''))
                            elif mdata.get('data'):
                                mfile = os.path.join(WORKDIR, 'bc_music_' + str(int(time.time()*1000)) + '.mp3')
                                os.makedirs(WORKDIR, exist_ok=True)
                                open(mfile, 'wb').write(base64.b64decode(mdata.get('data', '')))
                                mp = mfile
                        if not mp:
                            raise RuntimeError('请先选择背景音乐')
                        final, diag = beat_cut_video(vp, mp, run_dir, params, prog)
                        prog['done'] = True
                        prog['pct'] = 100
                        prog['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
                        prog['diag'] = diag
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        prog['done'] = True
                        prog['error'] = str(e)
                _threading.Thread(target=_do_bc, daemon=True).start()
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        self._send(404, b'not found')


def start_server(port=8765, open_browser=True):
    os.makedirs(WORKDIR, exist_ok=True)
    os.makedirs(OUTDIR, exist_ok=True)
    ensure_default_images()
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = f'http://127.0.0.1:{port}/'
    print('=' * 52)
    print('  [Spring Video Studio] started')
    print('  Open in browser:', url)
    print('  Press Ctrl+C to stop')
    print('=' * 52, flush=True)
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser_open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def webbrowser_open(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ensure_deps()
    port = int(os.environ.get('PORT', '8765'))
    start_server(port)


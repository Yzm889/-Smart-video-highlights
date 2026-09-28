# -*- coding: utf-8 -*-
"""端到端验证：/api/narrate 真实跑 10s 样片，检查 cut_plan 落盘 + 成片产出。"""
import base64, json, os, sys, time, urllib.request

BASE = 'http://127.0.0.1:8765'
VIDEO = r'C:\Users\XOS\Desktop\spring_video\spring10s.mp4'
if not os.path.exists(VIDEO):
    print('SKIP: no sample video'); sys.exit(0)

def post(path, payload, timeout=300):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def get(path, timeout=20):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

with open(VIDEO, 'rb') as f:
    b64 = base64.b64encode(f.read()).decode()

print('POST /api/narrate ...')
out = post('/api/narrate', {
    'video': {'name': os.path.basename(VIDEO), 'data': b64},
    'params': {'maxSeg': 3, 'w': 1280, 'h': 720, 'fps': 30, 'autoCut': True},
})
if not out.get('ok'):
    print('FAIL submit:', out); sys.exit(1)
runid = out['runid']
print('runid =', runid)

t0 = time.time()
last = None
while time.time() - t0 < 600:
    time.sleep(3)
    try:
        p = get('/api/progress?run=' + runid)
    except Exception as e:
        print('poll err:', e); continue
    last = p
    if p.get('done'):
        break
    print('  %s %s%%' % (p.get('phase', ''), p.get('pct', 0)))

print('\n===== RESULT =====')
if not last:
    print('NO progress'); sys.exit(1)
if last.get('error'):
    print('ERROR:', last.get('error'))
print('done =', last.get('done'), '| phase =', last.get('phase'))
print('file =', last.get('file'))
cp = last.get('cut_plan')
if cp:
    print('\ncut_plan:')
    print('  coverage =', cp.get('coverage'), '% | kept =', cp.get('kept'),
          '| removed =', cp.get('removed'), '| removed_sec =', cp.get('removed_sec'),
          '| src_dur =', cp.get('src_dur'))
    for s in (cp.get('segments') or [])[:12]:
        print('   [%s] %6.1f-%6.1f keep=%s %s | %s' % (
            s.get('importance'), s.get('start'), s.get('end'), s.get('keep'),
            s.get('reason'), (s.get('caption') or '')[:30]))
else:
    print('\ncut_plan: MISSING (检查 _narrate_analysis)')
print('\nwarnings =', last.get('warnings'))
d = last.get('diag') or {}
print('diag = segments:', d.get('segments'), '| voice_clips:', d.get('voice_clips'),
      '| cut:', d.get('cut'))
# 成片校验
if last.get('file'):
    fp = os.path.join(r'C:\Users\XOS\Desktop\spring_video\output', last['file'].replace('\\', '/'))
    if os.path.exists(fp):
        print('FINAL OK: %s (%.1f KB)' % (last['file'], os.path.getsize(fp) / 1024))
    else:
        print('FINAL MISSING at:', fp)
else:
    print('FINAL: none')

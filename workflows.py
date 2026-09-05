# -*- coding: utf-8 -*-
"""工作流调度层（batch4-3.3 由 webui_server.py 拆出，符号与拆分前等价）。

约定：
- 宿主（webui_server）的模块级符号（管道函数 / PROGRESS / 目录常量 / AI·TTS 入口等）
  一律经 `_w.<符号>` 在调用时解析——测试对 `webui_server.<符号>` 的 monkeypatch、
  conftest 对 OUTDIR / HISTORY_PATH 等隔离改写因此继续生效；
- 本层公共符号在文件末尾注入回宿主命名空间，`webui_server.dispatch_*` 等旧入口不变。
"""
import base64, os, shutil, sys, threading, time

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




# ---------------------------------------------------------------------------
# Phase 4 · 指令解析层（自然语言 → 路由到对应工作流）
# ---------------------------------------------------------------------------
def parse_instruction(text, ctx=None):
    """解析自然语言指令 → {action, params, movie, note}。action ∈ beatcut|narrate|movie|compose。"""
    import re as _re
    t = (text or '').strip()
    tl = t.lower()
    ctx = ctx or {}
    action = 'compose'
    params = {}
    movie = ''
    m = _re.search(r'《([^》]+)》', t)
    if m:
        movie = m.group(1)
    # 联网搜索/按片名解说 → movie 工作流；本地短片解说（有《》但更可能是上传视频）→ 仍走 movie 以利用联网梗概
    if movie or any(k in tl for k in ('联网', '搜索', '网上', '剧情梗概', '查一下', '简介')):
        action = 'movie'
    elif '解说' in t or '剧情' in t or '旁白' in t or 'narration' in tl:
        action = 'narrate'
    elif any(k in tl for k in ('卡点', '踩点', '强卡点', '配乐', '鼓点', '剪一段', '混剪', '卡点视频')):
        action = 'beatcut'
    else:
        action = 'compose'
    # 模式：省流(免费) vs 真AI(花钱)
    if any(k in tl for k in ('省流', '免费', '0元', '不花钱', '离线', '模板')):
        params['economy'] = True
    elif any(k in tl for k in ('真ai', '智能', '花钱', '付费', 'ai解说')):
        params['economy'] = False
    # 分辨率
    if any(k in tl for k in ('竖屏', '抖音', '快手', '9:16')):
        params['w'], params['h'] = 1080, 1920
    elif '横屏' in tl:
        params['w'], params['h'] = 1920, 1080
    # 携带音乐/最大分段等上下文
    if ctx.get('music'):
        params['music'] = ctx['music']
    for k in ('maxSeg', 'fps', 'mode'):
        if k in ctx:
            params[k] = ctx[k]
    return {'action': action, 'params': params, 'movie': movie, 'note': []}


def _resolve_music(music_data):
    """把 req 的 music 字段解析为本地路径（catalog 下载 / 分片上传 / base64 上传）。无则返回 None。"""
    if not music_data:
        return None
    try:
        if music_data.get('source') == 'catalog':
            return _w.download_catalog(music_data.get('catalogId', ''))
        if music_data.get('upload_id'):
            # 大音乐文件分片上传（batch5-5.2）：从上传会话取成品 copy 到工作目录，
            # 不 move——会话留给 prune 统一清理，重复解析也安全
            usrc = _w._upload_final_path(music_data.get('upload_id'), music_data.get('name'))
            if not usrc:
                return None
            mname = music_data.get('name', 'music.mp3')
            mpath = os.path.join(_w.WORKDIR, 'music_' + str(int(time.time() * 1000)) +
                                 (os.path.splitext(mname)[1] or '.mp3'))
            os.makedirs(_w.WORKDIR, exist_ok=True)
            shutil.copy2(usrc, mpath)
            return mpath
        if music_data.get('data'):
            mdata = base64.b64decode(music_data.get('data', ''))
            mname = music_data.get('name', 'music.mp3')
            mpath = os.path.join(_w.WORKDIR, 'music_' + str(int(time.time() * 1000)) +
                                 (os.path.splitext(mname)[1] or '.mp3'))
            os.makedirs(_w.WORKDIR, exist_ok=True)
            open(mpath, 'wb').write(mdata)
            return mpath
    except Exception:
        return None
    return None


def fail_task(prog, e):
    """统一的任务失败收尾：落 error 三件套 + 收集已产出的中间文件。

    prog['error']       保留原有的一句话错误（前端主展示，语义不变）
    prog['error_stage'] 出错时任务所处的阶段（prog['phase'] 的快照，先取后改）
    prog['error_detail']异常类型 + 消息 + 末尾若干帧堆栈（给前端「查看详情」用）
    各 dispatch_* 原先各写一份相同的 except body，抽出来避免字段遗漏。"""
    import traceback
    traceback.print_exc()
    stage = prog.get('phase') or '未开始'          # 必须在改 phase 之前快照
    prog['error_stage'] = stage
    try:
        prog['error_detail'] = ('%s: %s\n%s' % (
            type(e).__name__, e, traceback.format_exc(limit=8)))[:2000]
    except Exception:
        prog['error_detail'] = '%s: %s' % (type(e).__name__, e)
    prog['done'] = True
    prog['error'] = str(e)
    if prog.get('run_dir'):
        try:
            prog['partial'] = _w.collect_partial(prog['run_dir'])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CC.BY 音乐署名（合规）
#
# 内置曲库 12 首来自 Incompetech（Kevin MacLeod，CC BY 4.0）。按协议，公开发布
# 成片必须给出 TASL 四要素：Title / Author / Source / License。以往选曲界面展示了
# 许可信息，但成片里没有任何署名——用户一发抖音/B站就踩侵权线。
# 本轮只做「机制」：生成署名文本 → 写 run_dir/credits.txt + 放进 prog['credits']，
# 由前端展示并支持一键复制。文案最终措辞待法务定稿；不进渲染管线、不加片尾卡。
# ---------------------------------------------------------------------------
def _music_catalog_entry(music_data):
    """任务参数里的 music 字段若指向内置曲库，返回曲库条目；否则返回 None（用户自带音乐不署名）。"""
    if not isinstance(music_data, dict):
        return None
    if music_data.get('source') != 'catalog':
        return None
    mid = (music_data.get('catalogId') or '').strip()
    if not mid:
        return None
    return next((t for t in _w.MUSIC_CATALOG if t['id'] == mid), None)


def _task_credits(req):
    """按 CC.BY 4.0 的 TASL 要求生成纯文本署名；没用内置曲库音乐时返回 ''（空串=无需署名）。"""
    entries = []
    # 音乐可能挂在 req['music']（直接调用）或 req['params']['music']（指令解析层下传）
    for cand in (req.get('music'), (req.get('params') or {}).get('music')):
        t = _w._music_catalog_entry(cand)
        if t and t not in entries:
            entries.append(t)
    if not entries:
        return ''
    lines = ['背景音乐署名（CC BY 4.0 协议要求，公开发布本片时请保留以下信息）：']
    for t in entries:
        lic = str(t.get('license') or 'CC BY 4.0').replace('CC.BY', 'CC BY')
        lines += [
            '',
            'Title: %s' % t.get('title', ''),
            'Author: %s' % (t.get('attri') or 'Kevin MacLeod'),
            'Source: %s' % (t.get('licenseUrl') or 'https://incompetech.com/'),
            'License: %s — https://creativecommons.org/licenses/by/4.0/' % lic,
        ]
    return '\n'.join(lines)


def _finish_task_credits(req, prog):
    """任务成功收尾：写 run_dir/credits.txt 并把同一段文本放进 prog['credits']。

    没用曲库音乐时 prog['credits'] 为空串、不落文件、不塞占位文案。
    署名只是附加产物，任何失败都吞掉，绝不能因为它把已完成的成片判成失败。"""
    try:
        text = _w._task_credits(req)
    except Exception:
        text = ''
    prog['credits'] = text
    if not text or not prog.get('run_dir'):
        return
    try:
        with open(os.path.join(prog['run_dir'], 'credits.txt'), 'w', encoding='utf-8') as f:
            f.write('# 生成时间：%s\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
            f.write(text)
            f.write('\n')
    except OSError:
        pass


def dispatch_build(req, prog):
    """通用合成（图片/视频混排 + 节拍对齐）。与 /api/build 共用。"""
    try:
        params = req.get('params', {})
        items = req.get('items', [])
        music_path = _w._resolve_music(req.get('music'))
        # 落盘名必须带 runid：旧实现是 up_{序号}_{idx}_ext，两个并发任务若素材结构相同
        # （比如都是 3 张图）会写到同一路径，后写的覆盖先写的，成片里混入另一个任务的素材。
        _rid = (prog or {}).get('runid') or getattr(_w._TLS, 'runid', None) or ('t%d' % int(time.time()))
        _rid = str(_rid).replace('\\', '_').replace('/', '_')
        work = []
        for idx, it in enumerate(items):
            if it['kind'] == 'image':
                ext = os.path.splitext(it.get('name', 'x.jpg'))[1] or '.jpg'
                fp = os.path.join(_w.WORKDIR, f'up_{_rid}_{idx}_img{ext}')
                os.makedirs(_w.WORKDIR, exist_ok=True)
                if it.get('mlib'):
                    msrc = _w._material_path(it.get('mlib'))
                    if not msrc:
                        raise RuntimeError('素材库中找不到 %s，请刷新素材库' % it.get('mlib'))
                    shutil.copy2(msrc, fp)
                elif it.get('upload_id'):
                    # 大图分片上传（batch5-5.2：内联阈值降到 8MB 后图片也走分片）
                    usrc = _w._upload_final_path(it.get('upload_id'), it.get('name'))
                    if not usrc:
                        raise RuntimeError('素材 %s 上传会话已过期，请重新上传' % (it.get('name') or idx))
                    shutil.copy2(usrc, fp)
                else:
                    data = base64.b64decode(it.get('data', ''))
                    open(fp, 'wb').write(data)
                # name 保留用户原始文件名：省流文案直接对用户展示，不能露出 up_N 内部名
                work.append({'kind': 'image', 'src': fp, 'name': it.get('name', ''), 'dur': it.get('dur', 3), 'motion': len(work) % 4})
            else:
                # 视频素材两种形态：base64（小文件）或分片上传 upload_id（大文件，直接 move 免二次拷贝）
                fp = os.path.join(_w.WORKDIR, f'up_{_rid}_{idx}_vid.mp4')
                os.makedirs(_w.WORKDIR, exist_ok=True)
                src = _w._resolve_upload_video(it, _w.WORKDIR, f'up_{_rid}_{idx}_vid')
                if src is None:
                    raise RuntimeError('素材 %s 缺少数据（data/upload_id），请重新上传' % (it.get('name') or idx))
                work.append({'kind': 'video', 'src': src, 'name': it.get('name', ''), 'dur': it.get('dur', 3)})
        if not work:
            defaults = _w.ensure_default_images()
            single = params.get('singleDur', 3) or 3
            for i, p in enumerate(defaults):
                work.append({'kind': 'image', 'src': p, 'dur': single, 'motion': i})
        captions = None
        if params.get('ai_captions') and work:
            prog['phase'] = '按画面生成文案'
            prog['pct'] = 4
            economy = not _w.ai_enabled('vision')   # 自动：配置了云端视觉 key 用 AI 文案，否则离线模板
            captions = []
            for w_ in work:
                if economy:
                    cap = _w.offline_caption(w_.get('name') or w_.get('src', ''), 0, len(work))
                else:
                    cap = _w.ai_describe_image(w_['src'], w_.get('src', ''))
                captions.append(cap)
            params['economy'] = economy
        vid, total_len, beat_info = _w.assemble(work, params, music_path, prog, run_dir=prog.get('run_dir'))
        final = _w.finalize(vid, params, music_path, captions,
                         (beat_info or {}).get('durations'), prog)
        prog['done'] = True
        prog['pct'] = 100
        prog['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
        prog['duration'] = round(float(total_len), 2)
        prog['beat'] = beat_info
        prog['captions'] = captions
        prog['mode'] = 'ai' if (params.get('ai_captions') and _w.ai_enabled('vision')) else 'free'
        try:
            _w.add_history({
                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                'file': prog['file'], 'duration': prog['duration'],
                'music': (req.get('music') or {}).get('name') if isinstance(req.get('music'), dict) else None,
                'voice': bool(captions and _w._tts_available()), 'captions': captions,
                'mode': prog['mode'],
                'w': params.get('w', _w.W), 'h': params.get('h', _w.H), 'fps': params.get('fps', 30),
            })
        except Exception:
            pass
    except Exception as e:
        _w.fail_task(prog, e)


def _plan_thumbs(video_path, segs, run_dir, max_side=220):
    """为每个镜头段抽一张中间帧缩略图(jpg)，用于人机协同预览。返回 {idx: 绝对路径}。"""
    try:
        return _w.extract_segment_frames(video_path, segs, os.path.join(run_dir, 'thumbs'), max_side=max_side)
    except Exception:
        return {}


def _plan_to_ui(plan, run_dir):
    """把内部 plan 转成前端可渲染的 JSON（缩略图换成 /media 相对路径、不含二进制）。"""
    rel = lambda p: (os.path.relpath(p, _w.OUTDIR).replace('\\', '/') if p and os.path.exists(p) else '')
    ui = {'type': plan['type'], 'run_dir': os.path.basename(run_dir)}
    if plan['type'] == 'beatcut':
        tl = plan['timeline']
        ui['vdur'] = plan['vdur']
        ui['segs'] = []
        for i in range(len(tl) - 1):
            ui['segs'].append({'i': i, 'start': tl[i], 'end': tl[i + 1],
                               'thumb': rel(plan.get('thumbs', {}).get(i))})
        ui['cuts'] = [{'t': round(t, 3)} for t in tl[1:-1]]
    else:  # narrate
        ui['mode'] = plan.get('mode')
        outline = plan.get('outline') or []
        ui['segs'] = []
        for i, (s0, s1) in enumerate(plan['segs']):
            o = outline[i] if i < len(outline) else {}
            ui['segs'].append({'i': i, 'start': s0, 'end': s1,
                               'caption': plan['narr'][i] if i < len(plan['narr']) else '',
                               'thumb': rel(plan.get('thumbs', {}).get(i)),
                               'importance': o.get('importance', 'advance'),
                               'keep': o.get('keep', True)})
    return ui


def _analyze_plan_job(req, prog):
    """人机协同·分析阶段：分析素材生成「规划方案」，存 PLANS，等待用户确认/微调后渲染。"""
    try:
        run_dir = prog['run_dir']
        ptype = req.get('type') or 'beatcut'
        params = req.get('params') or {}
        # 视频两种形态：base64（小文件）或分片上传/素材库引用（长视频走这两者——此前漏接导致长视频分析预览报错）
        vobj = req.get('video') or {}
        vp = _w._resolve_upload_video(vobj, run_dir, 'src_video')
        had_video = bool(vobj.get('data') or vobj.get('upload_id') or vobj.get('mlib'))
        if had_video and not vp:
            raise RuntimeError('视频读取失败（上传会话可能已过期，请重新分析）')
        if ptype == 'beatcut':
            if not vp:
                raise RuntimeError('请先上传视频')
            music_path = _w._resolve_music(req.get('music'))
            if not music_path:
                raise RuntimeError('请先选择背景音乐')
            timeline, diag, vdur = _w._analyze_beatcut(vp, music_path, params, prog)
            segs = [(timeline[i], timeline[i + 1]) for i in range(len(timeline) - 1)]
            plan = {'type': 'beatcut', 'video': vp, 'music': music_path, 'timeline': timeline,
                    'vdur': vdur, 'params': params, 'diag': diag,
                    'thumbs': _w._plan_thumbs(vp, segs, run_dir)}
        elif ptype == 'narrate':
            if not vp:
                raise RuntimeError('请先上传视频')
            plot = (req.get('plot') or '').strip()
            outline = []
            if plot:
                # 🎭 剧情驱动：不靠 AI 识别画面，按用户剧情剪分镜 + 写解说
                segs, narr, asr, _frames, _mode, events, _nmap = _w._narrate_by_plot(
                    vp, plot, params, run_dir, prog, movie_name='')
                diag = {'segments': len(segs), 'asr_lines': len(asr),
                        'narration': narr, 'plot_driven': True, 'events': len(events)}
                mode = 'movie'
                # 剧情驱动没有「主线浓缩」这一步，也就没有 _condense_segs 产出的 outline；
                # 但预览面板与渲染阶段都按 outline 取每段的保留标记，这里补一份全保留的默认值
                # （历史 bug：此分支漏赋值 outline → 组合 plan 时 UnboundLocalError）
                outline = [{'start': s0, 'end': s1, 'importance': 'advance', 'keep': True}
                           for (s0, s1) in segs]
            else:
                segs, narr, asr, diag, mode, outline = _w._analyze_narrate(vp, params, run_dir, prog)
            music_path = _w._resolve_music(req.get('music'))
            # 额外保存「未合并的细粒度候选镜头」与台词：用户改完解说词后
            # 需要按新解说重新匹配分镜（/api/narrate/align），合并后的环节粒度太粗无法重排
            shots = _w._narrate_candidate_shots(vp, params)
            plan = {'type': 'narrate', 'video': vp, 'segs': segs, 'narr': narr,
                    'shots': shots, 'asr': asr, 'run_dir': run_dir, 'outline': outline,
                    'params': params, 'music': music_path, 'diag': diag, 'mode': mode,
                    'thumbs': _w._plan_thumbs(vp, segs, run_dir)}
        else:
            raise RuntimeError('未知分析类型: ' + str(ptype))
        _w.PLANS[prog['runid']] = plan
        # 用户可能分析后不确认：只保留最近 30 个方案，防止长驻进程内存增长
        if len(_w.PLANS) > 30:
            for k in list(_w.PLANS.keys())[:-30]:
                _w.PLANS.pop(k, None)
        prog['plan_ready'] = True
        prog['plan'] = _w._plan_to_ui(plan, run_dir)
        prog['phase'] = '规划完成，请在下方微调后点击「按我的调整合成」'
        prog['pct'] = 100
    except Exception as e:
        _w.fail_task(prog, e)


def _render_plan_job(req, prog):
    """人机协同·渲染阶段：按用户微调后的方案（编辑过的切点/解说稿）合成成片。"""
    try:
        # plan 挂在「分析阶段」的旧 runid 上；confirm 请求透传了该 runid
        src_runid = req.get('runid') or prog['runid']
        plan = _w.PLANS.get(src_runid)
        if not plan:
            raise RuntimeError('规划方案不存在或已过期，请重新分析')
        params = dict(plan.get('params') or {})
        extra = req.get('params') or {}
        if extra:
            params.update(extra)
        edits = req.get('edits') or {}
        if plan['type'] == 'beatcut':
            vdur = plan['vdur']
            seg_edits = edits.get('segs') or []
            tl2 = [0.0]
            if seg_edits:
                # 由每段的保留开关 + 段尾时间重建切点（末段 end=vdur 不是切点）；未传 on 默认保留
                for s in seg_edits:
                    if s.get('on', True):
                        t = round(float(s.get('end', 0)), 3)
                        if 0.3 < t < vdur - 0.3 and t - tl2[-1] >= 0.8:
                            tl2.append(t)
            else:
                for c in (edits.get('cuts') or []):
                    if c.get('on', True):
                        t = round(float(c['t']), 3)
                        if 0.3 < t < vdur - 0.3 and t - tl2[-1] >= 0.8:
                            tl2.append(t)
            tl2 = sorted(set(tl2))
            if vdur - tl2[-1] < 0.4:
                tl2[-1] = vdur
            else:
                tl2.append(vdur)
            tl2 = sorted(set(tl2))
            final = _w._render_beatcut(plan['video'], plan['music'], tl2, params, prog['run_dir'],
                                    prog, diag=None)
        elif plan['type'] == 'narrate':
            segs, narr = [], []
            for s in (edits.get('segs') or []):
                if not s.get('on', True):
                    continue
                segs.append((float(s['start']), float(s['end'])))
                narr.append(str(s.get('caption', '')))
            if not segs:
                raise RuntimeError('没有保留任何片段，请至少勾选一段')
            # _render_narrate 返回 (final_path, voice_clips, cut_info)，必须解包
            final, voice_clips, cut_info = _w._render_narrate(
                plan['video'], segs, narr, params, prog['run_dir'], prog,
                music_path=plan.get('music'), mode=plan.get('mode'),
                auto_cut=bool(params.get('autoCut', True)))
        else:
            raise RuntimeError('未知方案类型')
        prog['done'] = True
        prog['pct'] = 100
        prog['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
        prog['diag'] = dict(plan.get('diag') or {})
        prog['diag']['segments'] = len(tl2) - 1 if plan['type'] == 'beatcut' else len(segs)
        if plan['type'] == 'narrate':
            prog['diag']['voice_clips'] = voice_clips
            prog['diag']['cut'] = cut_info
        _w._record_history(req, prog, 'plan-' + plan['type'])
        _w.PLANS.pop(src_runid, None)
    except Exception as e:
        _w.fail_task(prog, e)


def dispatch_beatcut(req, prog):
    """强卡点。与 /api/beatcut 共用。params.beatSync=True 时走「节拍同步」新引擎。"""
    try:
        run_dir = prog.get('run_dir') or os.path.join(_w.OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(run_dir, exist_ok=True)
        vp = _w._resolve_upload_video(req.get('video'), run_dir, 'src_video')
        if not vp:
            raise RuntimeError('未收到视频（或上传会话已过期，请重新上传）')
        mp = _w._resolve_music(req.get('music'))
        if not mp:
            raise RuntimeError('请先选择背景音乐')
        params = req.get('params', {})
        if params.get('beatSync'):
            final = os.path.join(run_dir, 'final.mp4')
            ret = _w.generate_beat_sync_video(
                vp, mp, final,
                beat_sensitivity=float(params.get('beat_sensitivity', 0.5)),
                min_clip_dur=float(params.get('min_clip_dur', 0.6)),
                progress=prog)
            prog['done'] = True
            prog['pct'] = 100
            prog['file'] = os.path.relpath(ret['output'], _w.OUTDIR).replace('\\', '/')
            prog['diag'] = {
                'mode': 'beat_sync',
                'beat_num': ret['beat_num'],
                'clip_num': ret['clip_num'],
                'warning': ret['warning'],
            }
            prog['mode'] = 'free'  # 节拍同步为离线模板，无需 LLM
            _w._record_history(req, prog, 'beatsync')
        else:
            final, diag = _w.beat_cut_video(vp, mp, run_dir, params, prog)
            prog['done'] = True
            prog['pct'] = 100
            prog['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
            prog['diag'] = diag
            prog['mode'] = 'free'  # 强卡点为离线节拍模板，无需 LLM
            _w._record_history(req, prog, 'beatcut')
    except Exception as e:
        _w.fail_task(prog, e)


def dispatch_narrate(req, prog):
    """电影解说（本地短片版）。与 /api/narrate 共用，支持可选 BGM。"""
    try:
        run_dir = prog.get('run_dir') or os.path.join(_w.OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(run_dir, exist_ok=True)
        vp = _w._resolve_upload_video(req.get('video'), run_dir, 'src')
        if not vp:
            raise RuntimeError('未收到视频（或上传会话已过期，请重新上传）')
        music_path = _w._resolve_music(req.get('music'))
        final, diag = _w.narrate_video(vp, req.get('params', {}), run_dir, prog, music_path=music_path)
        prog['done'] = True
        prog['pct'] = 100
        prog['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
        prog['diag'] = diag
        prog['mode'] = prog.get('mode') or _w.compute_mode(req.get('params', {}), needs_chat=True)
        _w._record_history(req, prog, 'narrate')
    except Exception as e:
        _w.fail_task(prog, e)


def dispatch_movie(req, prog):
    """联网搜索 + 自动解说（Phase 3）。"""
    try:
        run_dir = prog.get('run_dir') or os.path.join(_w.OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(run_dir, exist_ok=True)
        vp = _w._resolve_upload_video(req.get('video'), run_dir, 'src')
        if vp is None and req.get('video'):
            raise RuntimeError('未收到视频（或上传会话已过期，请重新上传）')
        music_path = _w._resolve_music(req.get('music'))
        final, diag = _w.narrate_movie(req.get('movie', ''), req.get('plot', ''), vp,
                                    req.get('params', {}), run_dir, prog, music_path=music_path)
        prog['done'] = True
        prog['pct'] = 100
        if final:
            prog['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
        elif vp:
            prog['error'] = '解说稿已生成，但视频合成失败（请检查ffmpeg或配音引擎日志）'
            prog['phase'] = '⚠️ 解说稿完成，视频合成失败'
        else:
            prog['phase'] = '✅ 解说稿已生成（未上传视频，仅产出文案）'
        prog['diag'] = diag
        _w._record_history(req, prog, 'movie')
    except Exception as e:
        _w.fail_task(prog, e)


def dispatch_movie_tts(req, prog):
    """Phase 1：生成解说稿+所有配音，暂停等用户确认。"""
    try:
        run_dir = prog.get('run_dir') or os.path.join(_w.OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(run_dir, exist_ok=True)
        vp = _w._resolve_upload_video(req.get('video'), run_dir, 'src')
        if vp is None and req.get('video'):
            raise RuntimeError('未收到视频（或上传会话已过期，请重新上传）')
        _final, diag = _w.narrate_movie(req.get('movie', ''), req.get('plot', ''), vp,
                                     req.get('params', {}), run_dir, prog,
                                     music_path=_w._resolve_music(req.get('music')),
                                     tts_only=True)
        prog['diag'] = diag
        _w._record_history(req, prog, 'movie_tts')
    except Exception as e:
        _w.fail_task(prog, e)


def dispatch_movie_compose(req, prog):
    """Phase 2：用户确认配音后，裁剪视频+合成。"""
    try:
        run_dir_name = req.get('run_dir') or prog.get('run_dir')
        if not run_dir_name:
            raise RuntimeError('缺少run_dir参数')
        run_dir = os.path.join(_w.OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
        if not os.path.exists(os.path.join(run_dir, 'tts_state.json')):
            raise RuntimeError('配音状态不存在，请先生成配音')
        music_path = _w._resolve_music(req.get('music'))
        adjusted = req.get('items')
        skip = req.get('skip') or []
        user_params = req.get('params') or {}
        final = _w.compose_movie_from_tts(run_dir, prog, music_path=music_path,
                                       adjusted_items=adjusted, skip=skip,
                                       user_params=user_params)
        prog['done'] = True
        prog['pct'] = 100
        if final:
            prog['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
        _w._record_history(req, prog, 'movie')
    except Exception as e:
        _w.fail_task(prog, e)


def dispatch_tts_single(req, prog):
    """单段配音重生成。"""
    try:
        text = (req.get('text') or '').strip()
        run_dir_name = req.get('run_dir') or ''
        idx = int(req.get('index', 0))
        if not text or not run_dir_name:
            prog['error'] = '缺少text或run_dir'
            prog['done'] = True
            return
        run_dir = os.path.join(_w.OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
        os.makedirs(run_dir, exist_ok=True)
        out_path = os.path.join(run_dir, 'narr%d.mp3' % idx)
        ok, _eng, lp = _w.local_tts_speak(text, out_path)
        if not ok:
            # 重试一次
            import time as _t; _t.sleep(0.5)
            ok, _eng, lp = _w.local_tts_speak(text, out_path)
        if ok:
            prog['done'] = True
            prog['audio'] = os.path.relpath(lp, _w.OUTDIR).replace('\\', '/')
            prog['duration'] = round(_w.probe_audio_len(lp) or 0, 1)
        else:
            prog['error'] = '配音生成失败，请检查网络或TTS引擎'
            prog['done'] = True
    except Exception as e:
        _w.fail_task(prog, e)


def dispatch_tts_regen_all(req, prog):
    """全部配音重生成。"""
    try:
        texts = req.get('texts') or []
        run_dir_name = req.get('run_dir') or ''
        if not texts or not run_dir_name:
            prog['error'] = '缺少texts或run_dir'; prog['done'] = True; return
        run_dir = os.path.join(_w.OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
        os.makedirs(run_dir, exist_ok=True)
        results = _w._generate_all_tts(texts, run_dir, progress=prog)
        items = [{'index': i, 'text': texts[i] if i < len(texts) else '',
                  'audio': os.path.relpath(p, _w.OUTDIR).replace('\\', '/'),
                  'duration': round(_w.probe_audio_len(p) or 0, 1)}
                 for i, p in results]
        prog['done'] = True
        prog['items'] = items
    except Exception as e:
        _w.fail_task(prog, e)


def dispatch_instruct(req, prog):
    """指令解析层：解析自然语言 → 路由到对应工作流。与 /api/instruct 共用。"""
    instr = req.get('instruction', '')
    ctx = req.get('context', {}) or {}
    parsed = _w.parse_instruction(instr, ctx)
    action = parsed['action']
    params = dict(parsed.get('params', {}))
    prog['phase'] = '解析指令 → ' + action
    prog['pct'] = 2
    if action == 'movie':
        mreq = {
            'movie': parsed.get('movie', '') or ctx.get('movie', ''),
            'plot': ctx.get('plot', '') or req.get('plot', ''),
            'video': req.get('video') or ctx.get('video'),
            'params': {**params, 'economy': params.get('economy', True), 'maxSeg': params.get('maxSeg', 25)},
            'music': params.get('music'),
        }
        return _w.dispatch_movie(mreq, prog)
    if action == 'narrate':
        nreq = {
            'video': req.get('video') or ctx.get('video'),
            'params': {**params, 'economy': params.get('economy', True), 'maxSeg': params.get('maxSeg', 25)},
            'music': params.get('music'),
        }
        return _w.dispatch_narrate(nreq, prog)
    if action == 'beatcut':
        breq = {
            'video': req.get('video') or ctx.get('video'),
            'music': params.get('music') or req.get('music'),
            'params': {**params},
        }
        return _w.dispatch_beatcut(breq, prog)
    # compose
    bread = {
        'items': req.get('items') or ctx.get('items', []),
        'music': params.get('music') or req.get('music'),
        'params': {**params},
    }
    return _w.dispatch_build(bread, prog)


def collect_partial(run_dir):
    """任务失败时收集 run_dir 中已生成的中间产物，便于用户拿到部分成果。
    返回 {'files':[{name,ext,size,kind,rel,url}], 'text':<首个文本文件内容>, 'best_video':<最成品视频url>}。"""
    if not run_dir or not os.path.isdir(run_dir):
        return {'files': [], 'text': None, 'best_video': None}
    files, text, best_video = [], None, None
    try:
        for fn in sorted(os.listdir(run_dir)):
            fp = os.path.join(run_dir, fn)
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(fn)[1].lower()
            rel = os.path.relpath(fp, _w.OUTDIR).replace('\\', '/')
            size = os.path.getsize(fp)
            if ext == '.mp4':
                kind = 'video'
            elif ext in ('.wav', '.mp3', '.m4a', '.aac', '.ogg'):
                kind = 'audio'
            elif ext == '.srt':
                kind = 'subtitle'
            elif ext == '.txt':
                kind = 'text'
            else:
                kind = 'file'
            entry = {'name': fn, 'ext': ext, 'size': size, 'kind': kind,
                     'rel': rel, 'url': '/media/' + rel}
            if kind == 'text' and text is None and size < 200000:
                try:
                    text = open(fp, 'r', encoding='utf-8', errors='ignore').read()
                except Exception:
                    pass
            files.append(entry)
    except Exception:
        pass
    videos = [e for e in files if e['kind'] == 'video']

    def vrank(e):
        n = e['name']
        if n == 'final.mp4':
            return 0
        if n == 'vid_sub.mp4':
            return 1
        if n.startswith('vid'):
            return 2
        if n.startswith('bc'):
            return 3
        if n.startswith('nar'):
            return 4
        if n.startswith('seg'):
            return 5
        return 6
    if videos:
        videos.sort(key=vrank)
        best_video = videos[0]['url']
    kind_order = {'video': 0, 'audio': 1, 'subtitle': 2, 'text': 3, 'file': 4}
    files.sort(key=lambda e: (kind_order.get(e['kind'], 9), e['name']))
    return {'files': files, 'text': text, 'best_video': best_video}


def assemble(items, params, music=None, progress=None, run_dir=None):
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
    run_dir = run_dir or os.path.join(_w.OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(run_dir, exist_ok=True)
    w = int(params.get('w', _w.W)); h = int(params.get('h', _w.H))
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
        analysis = _w.analyze_beats(music)
        bpm, beats = (analysis if analysis else (None, []))
        mlen = _w.probe_audio_len(music) or 0.0
        if mlen <= 0:
            raise RuntimeError('无法读取音乐时长')
        # video length = photo-driven; interior cuts land near beats with given interval
        step = float(params.get('beatStep', 1) or 1)
        disp = _w.plan_beat_durations(item_durs, beats or [], fade, step)
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
            _w.make_image_clip(it['src'], seg_dur, int(it.get('motion', idx % 4)), seg, w, h, fps)
            real_durs.append(seg_dur)
        else:
            seg, real = _w.make_video_clip(it['src'], seg_dur, seg, w, h, fps)
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
        cmd += ['-filter_complex', filter_str, '-map', '[vout]'] + _w.video_encode_args() + [
                '-threads', '0', os.path.join(run_dir, 'vid_silent.mp4')]
        rc, o, e = _w.ffmpeg_run(cmd)
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
    voice_over = bool(captions)
    narration_path = None
    srt_path = None

    # 1) subtitles (align each caption to its clip's display window)
    total = _w.probe_audio_len(video_path) or 0.0
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
        _w.build_srt(captions, starts, durs, srt_path)
        # burn subtitles (needs libass) — fallback: if filter fails, keep unburned
        burned = os.path.join(run_dir, 'vid_sub.mp4')
        # escape path for filter
        esc = srt_path.replace('\\', '/').replace(':', '\\:').replace('\'', '\\\'')
        sub_style2 = _w._build_subtitle_style(params)
        rc, o, e = _w.ffmpeg_run(['-y', '-i', video_path,
                               '-vf', f"subtitles='{esc}':force_style='{sub_style2}'",
                               ] + _w.video_encode_args() + ['-threads', '0', '-an', burned])
        if rc == 0 and os.path.exists(burned):
            video_path = burned
        if progress:
            progress['phase'] = '配音合成'
            progress['pct'] = 80

    # 2) TTS narration for each caption (only if TTS configured & succeeded)
    narration_path = None
    if voice_over and _w._tts_available():
        clips = []
        for i, cap in enumerate(captions if N else []):
            if not (cap and cap.strip()):
                continue
            np_ = os.path.join(run_dir, f'nar{i}.mp3')
            od = starts[i] if i < len(starts) else (total / max(1, N)) * i
            if _w.ai_tts(cap, np_):
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
            rc, o, e = _w.ffmpeg_run(cmd)
            if rc == 0 and os.path.exists(narration_path) and os.path.getsize(narration_path) > 500:
                pass
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
               '[0:a]volume=0.45[bg0];[1:a]aformat=fltp[na];[bg0][na]amix=inputs=2:normalize=0[aout]',
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
    rc, o, e = _w.ffmpeg_run(cmd)
    if rc != 0:
        raise RuntimeError('最终合成失败: ' + e.decode('utf-8', 'ignore')[-600:])
    if progress:
        progress['phase'] = '完成'
        progress['pct'] = 100
        progress['done'] = True
        progress['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
    return final
def _start_next_queued():
    """从排队队列中取出下一个任务并启动。"""
    with _w._TASK_QUEUE_LOCK:
        if not _w._TASK_QUEUE:
            return
        fn, req, runid, run_dir, prog = _w._TASK_QUEUE.pop(0)
        # 更新队列中其他任务的排队位置
        for i, (_f, _r, _rid, _rd, _p) in enumerate(_w._TASK_QUEUE):
            _p['phase'] = '排队中（前面还有%d个任务）' % (i + 1)
        # 取出后立刻落盘：磁盘上看到的永远是最新的队列内容
        _w._persist_queue_unlocked()
    if not _w._TASK_SEM.acquire(blocking=False):
        # 名额又被占了（极端情况），重新放回队列头部
        with _w._TASK_QUEUE_LOCK:
            _w._TASK_QUEUE.insert(0, (fn, req, runid, run_dir, prog))
            _w._persist_queue_unlocked()
        return
    prog['queued'] = False
    prog['phase'] = '开始执行'
    print(f'[DIAG] 排队任务启动: {runid}，剩余队列={len(_w._TASK_QUEUE)}')
    def _queued_runner():
        _w._TLS.runid = runid
        try:
            del _w._TLS.tts_engine
        except Exception:
            pass
        try:
            fn(req, prog)
            if not prog.get('error'):
                _w._finish_task_credits(req, prog)
        except _w.AbortError:
            prog['done'] = True
            prog['aborted'] = True
            prog['error'] = '已取消（用户中断）'
        except Exception as e:
            _w.fail_task(prog, e)
        finally:
            _w._TASK_SEM.release()
            _w._start_next_queued()
    t = threading.Thread(target=_queued_runner, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# 公共符号注入回宿主命名空间（保持 webui_server.X 旧入口；覆盖宿主文件末尾的
# None 占位绑定）。任何导入顺序下宿主此刻都已在 sys.modules。
# ---------------------------------------------------------------------------
for _name in (
    'parse_instruction',
    '_resolve_music',
    'fail_task',
    '_music_catalog_entry',
    '_task_credits',
    '_finish_task_credits',
    'dispatch_build',
    '_plan_thumbs',
    '_plan_to_ui',
    '_analyze_plan_job',
    '_render_plan_job',
    'dispatch_beatcut',
    'dispatch_narrate',
    'dispatch_movie',
    'dispatch_movie_tts',
    'dispatch_movie_compose',
    'dispatch_tts_single',
    'dispatch_tts_regen_all',
    'dispatch_instruct',
    'collect_partial',
    'assemble',
    'finalize',
    '_start_next_queued',
):
    setattr(_w, _name, globals()[_name])

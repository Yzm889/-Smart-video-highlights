/* ============================================================
   main.js — 交互层
   原则：只动 transform / opacity；尊重 prefers-reduced-motion；
        所有键盘交互走原生元素的语义（button / a / input）
   ============================================================ */
(() => {
  'use strict';

  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $  = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  /* ---------------- 滚动入场：一次编排好的错峰序列 ---------------- */
  function initReveal() {
    const items = $$('.reveal');
    if (!items.length) return;

    if (reduce || !('IntersectionObserver' in window)) {
      items.forEach(el => el.classList.add('is-in'));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add('is-in');
          io.unobserve(e.target);      // 只播一次，避免来回滚动抖动
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -8% 0px' });

    items.forEach(el => io.observe(el));
  }

  /* ---------------- 数字滚动：仪表读数从 0 跳到目标值 ---------------- */
  function initCounters() {
    const nodes = $$('[data-count]');
    if (!nodes.length) return;

    const run = (el) => {
      const target = Number(el.dataset.count) || 0;
      const unit = el.querySelector('sup');
      if (reduce || target === 0) {           // 保留 <sup> 单位不被覆盖
        el.firstChild.nodeValue = String(target);
        return;
      }
      const dur = 900;
      const t0 = performance.now();
      const tick = (now) => {
        const p = Math.min((now - t0) / dur, 1);
        const eased = 1 - Math.pow(1 - p, 3);
        el.firstChild.nodeValue = String(Math.round(target * eased));
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
      void unit;
    };

    if (!('IntersectionObserver' in window)) { nodes.forEach(run); return; }
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) { run(e.target); io.unobserve(e.target); }
      });
    }, { threshold: 0.4 });
    nodes.forEach(el => io.observe(el));
  }

  /* ---------------- Toast ---------------- */
  const toastHost = () => $('#toasts');

  function toast(msg, kind = 'info', sticky = false) {
    const host = toastHost();
    if (!host) return;
    const el = document.createElement('div');
    el.className = 'toast' + (kind !== 'info' ? ` toast--${kind}` : '');
    el.setAttribute('role', kind === 'err' ? 'alert' : 'status');

    const icon = document.createElement('span');
    icon.className = 'lamp ' + (kind === 'ok' ? 'lamp--ok'
      : kind === 'warn' ? 'lamp--warn'
      : kind === 'err' ? 'lamp--err' : 'lamp--live');
    icon.setAttribute('aria-hidden', 'true');

    const text = document.createElement('span');
    text.textContent = msg;

    el.append(icon, text);
    host.appendChild(el);

    const kill = () => {
      el.classList.add('is-out');
      setTimeout(() => el.remove(), reduce ? 0 : 320);
    };
    if (sticky) {
      el.style.cursor = 'pointer';
      el.title = '点击关闭';
      el.addEventListener('click', kill);
    } else {
      setTimeout(kill, 3200);
    }
  }
  window.FCtoast = toast;

  /* ---------------- 复制到剪贴板 ---------------- */
  function copyText(t) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(t);
    }
    // 本地 file:// 打开时的兜底
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (_) { /* 忽略 */ }
    ta.remove();
    return Promise.resolve();
  }

  function initCopy() {
    $$('[data-copy]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        await copyText(btn.dataset.copy);
        const label = btn.querySelector('.swatch__name');
        toast(`已复制：${label ? label.textContent : btn.dataset.copy}`, 'ok');
      });
    });
  }

  /* ---------------- 高对比模式 ---------------- */
  function initContrast() {
    const btn = $('#contrastBtn');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const on = document.documentElement.dataset.contrast === 'high';
      if (on) { delete document.documentElement.dataset.contrast; }
      else { document.documentElement.dataset.contrast = 'high'; }
      btn.setAttribute('aria-pressed', String(!on));
      toast(on ? '已切回标准对比度' : '已开启高对比模式', 'ok');
    });
  }

  /* ---------------- 步骤切换（tablist） ---------------- */
  function initStepper(tabSel, panePrefix, logNode) {
    const tabs = $$(tabSel);
    if (!tabs.length) return;

    const select = (i) => {
      tabs.forEach((t, idx) => {
        const on = idx === i;
        t.setAttribute('aria-selected', String(on));
        if (on) { t.setAttribute('aria-current', 'step'); }
        else { t.removeAttribute('aria-current'); }
        const pane = document.getElementById(panePrefix + (idx + 1));
        if (pane) pane.hidden = !on;
      });
      if (logNode) pushLog(logNode, 'info', `切换到 STEP 0${i + 1}`);
      // 键盘用户切换后焦点跟随，不丢失位置
      tabs[i].focus({ preventScroll: true });
    };

    tabs.forEach((t, i) => {
      t.addEventListener('click', () => select(i));
      t.addEventListener('keydown', (e) => {
        const map = { ArrowRight: 1, ArrowLeft: -1, Home: -99, End: 99 };
        if (!(e.key in map)) return;
        e.preventDefault();
        let next = i + map[e.key];
        if (e.key === 'Home') next = 0;
        if (e.key === 'End') next = tabs.length - 1;
        select(Math.max(0, Math.min(tabs.length - 1, next)));
      });
    });
  }

  /* ---------------- 工作台步骤导航：滚动 + 高亮，而不是隐藏面板 ---------------- */
  function initFlowNav() {
    const steps = $$('.stepper [data-target]');
    if (!steps.length) return;

    const activate = (btn) => {
      steps.forEach(s => s.removeAttribute('aria-current'));
      btn.setAttribute('aria-current', 'step');
      const target = document.getElementById(btn.dataset.target);
      if (!target) return;
      target.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
      target.classList.remove('is-focus');
      // 强制重排以便重复点击都能重新触发高亮动画
      void target.offsetWidth;
      target.classList.add('is-focus');
      setTimeout(() => target.classList.remove('is-focus'), 1200);
    };

    steps.forEach(btn => btn.addEventListener('click', () => activate(btn)));
  }

  /* ---------------- 日志控制台 ---------------- */
  let logSeq = 0;
  function clock() {
    const s = (logSeq += 0.7 + Math.random() * 1.6);
    const m = Math.floor(s / 60);
    return `${String(m).padStart(2, '0')}:${(s % 60).toFixed(1).padStart(4, '0')}`;
  }
  function pushLog(host, lv, msg) {
    if (!host) return;
    const line = document.createElement('div');
    line.className = 'console__line';
    line.innerHTML =
      `<span class="console__t"></span>` +
      `<span class="console__lv console__lv--${lv}"></span>` +
      `<span class="console__msg"></span>`;
    line.querySelector('.console__t').textContent = clock();
    line.querySelector('.console__lv').textContent = lv.toUpperCase();
    line.querySelector('.console__msg').textContent = msg;
    host.appendChild(line);
    host.scrollTop = host.scrollHeight;
    while (host.children.length > 60) host.removeChild(host.firstChild);
  }

  /* ---------------- 滑块读数 ---------------- */
  function initSliders() {
    const bind = (sliderId, valueId, fmt) => {
      const s = document.getElementById(sliderId);
      const v = document.getElementById(valueId);
      if (!s || !v) return;
      const sync = () => { v.textContent = fmt(Number(s.value)); };
      s.addEventListener('input', sync);
      sync();
    };
    bind('s-speed', 'v-speed', n => (n / 100).toFixed(1) + '×');
    bind('s-emo', 'v-emo', n => n + ' 档');
    bind('s-wspeed', 'v-wspeed', n => (n / 100).toFixed(1) + '×');
    bind('s-wemo', 'v-wemo', n => n + ' 档');
  }

  /* ---------------- 套件页的小演示 ---------------- */
  function initKitDemos() {
    // 加载态
    const loadDemo = $('#loadDemo');
    if (loadDemo) {
      loadDemo.addEventListener('click', () => {
        loadDemo.classList.add('is-loading');
        loadDemo.setAttribute('aria-busy', 'true');
        setTimeout(() => {
          loadDemo.classList.remove('is-loading');
          loadDemo.removeAttribute('aria-busy');
          toast('渲染任务已完成', 'ok');
        }, 2000);
      });
    }

    // 进度推进
    const pbtn = $('#pbtn');
    if (pbtn) {
      let p = 62;
      pbtn.addEventListener('click', () => {
        p = p >= 100 ? 0 : Math.min(100, p + 13);
        $('#pbar').style.width = p + '%';
        $('#pnum').textContent = p + '%';
        if (p === 100) toast('全部任务渲染完成', 'ok');
      });
    }

    // Toast 触发
    $$('[data-toast]').forEach((b) => {
      b.addEventListener('click', () => {
        const kind = b.dataset.kind || 'info';
        toast(b.dataset.toast, kind, kind === 'err');
      });
    });

    // 一键查剧情：模拟回填
    const fill = $('#fillDemo');
    if (fill) {
      fill.addEventListener('click', () => {
        const ta = $('#t1');
        fill.disabled = true;
        fill.textContent = '⟳ 查询中';
        setTimeout(() => {
          ta.value = '第一幕：土匪假扮县长进入鹅城，与地头蛇黄四郎正面冲撞。\n'
                   + '第二幕：双方几次试探，真假县长身份互换，局面失控。\n'
                   + '第三幕：张麻子亮明身份，用一场对赌把黄四郎逼到绝路，结尾留一个背影。';
          fill.disabled = false;
          fill.textContent = '⚡ 一键查剧情';
          toast('已回填分幕摘要，可直接修改', 'ok');
        }, 1400);
      });
    }

    // 侧边目录滚动高亮
    const links = $$('.kit__nav a');
    if (links.length && 'IntersectionObserver' in window) {
      const map = new Map();
      links.forEach((a) => {
        const sec = document.querySelector(a.getAttribute('href'));
        if (sec) map.set(sec, a);
      });
      const io = new IntersectionObserver((entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            links.forEach(a => a.classList.remove('is-active'));
            map.get(e.target)?.classList.add('is-active');
          }
        });
      }, { rootMargin: '-40% 0px -55% 0px' });
      map.forEach((_, sec) => io.observe(sec));
    }
  }

  /* ---------------- 落地页流程日志（氛围用，非真实任务） ---------------- */
  function seedFlowLog() {
    const host = $('#flowConsole');
    if (!host) return;
    [
      ['info', '载入影片 让子弹飞.mp4（02:12:04 · 1080P）'],
      ['ok',   '镜头边界检测完成，共 128 段'],
      ['warn', '第 42 段置信度 0.61，已标黄待确认'],
      ['ok',   '节拍分析完成，鼓点 214 个'],
      ['info', '等待剧情输入…'],
    ].forEach(([lv, msg]) => pushLog(host, lv, msg));
  }

  /* ---------------- 工作台 ---------------- */
  function initWorkbench() {
    const console_ = $('#wbConsole');
    if (!console_) return;

    // 素材选择
    $$('.clipitem').forEach((item) => {
      item.addEventListener('click', () => {
        $$('.clipitem').forEach(i => i.classList.remove('is-sel'));
        item.classList.add('is-sel');
        const name = item.querySelector('.clipitem__t').textContent;
        pushLog(console_, 'info', `切换素材：${name}`);
      });
    });

    // 时间线片段选择
    $$('.clip').forEach((clip) => {
      const activate = () => {
        $$('.clip').forEach(c => c.classList.remove('is-sel'));
        clip.classList.add('is-sel');
        if (clip.style.borderLeftColor.includes('warn') || clip.textContent.includes('待确认')) {
          pushLog(console_, 'warn', '该片段置信度 0.61，请人工确认入出点');
        }
      };
      clip.addEventListener('click', activate);
      clip.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
      });
    });

    // 播放按钮
    const play = $('#playBtn');
    if (play) {
      let playing = false;
      play.addEventListener('click', () => {
        playing = !playing;
        play.innerHTML = playing
          ? '<svg width="18" height="22" viewBox="0 0 18 22" aria-hidden="true"><rect x="0" y="0" width="6" height="22" fill="currentColor"/><rect x="12" y="0" width="6" height="22" fill="currentColor"/></svg>'
          : '<svg width="22" height="24" viewBox="0 0 22 24" aria-hidden="true"><path d="M0 0 L22 12 L0 24 Z" fill="currentColor"/></svg>';
        play.setAttribute('aria-label', playing ? '暂停预览' : '播放预览');
        pushLog(console_, 'info', playing ? '开始预览' : '暂停预览');
      });
    }

    // 一键查剧情
    const fetchPlot = $('#fetchPlot');
    const plotInput = $('#plotInput');
    const plotMsg = $('#plotMsg');
    if (fetchPlot && plotInput) {
      fetchPlot.addEventListener('click', () => {
        fetchPlot.disabled = true;
        fetchPlot.textContent = '⟳ 查询中';
        pushLog(console_, 'info', '调用 DeepSeek 检索剧情摘要…');
        setTimeout(() => {
          plotInput.value = '第一幕：土匪假扮县长进入鹅城。\n'
                          + '第二幕：与黄四郎多次试探，真假身份互换。\n'
                          + '第三幕：亮明身份，一场对赌逼对方到绝路，结尾留背影。';
          fetchPlot.disabled = false;
          fetchPlot.textContent = '⚡ 查剧情';
          if (plotMsg) plotMsg.textContent = '已回填，可直接改；改完点「开始生成」';
          pushLog(console_, 'ok', '剧情摘要回填完成（3 幕）');
          toast('剧情已回填', 'ok');
        }, 1500);
      });
    }

    // 渲染模拟
    const renderBtn = $('#renderBtn');
    if (renderBtn) {
      let running = false;
      renderBtn.addEventListener('click', () => {
        if (running) return;
        if (!plotInput.value.trim()) {
          pushLog(console_, 'err', '剧情为空，先写剧情或点「查剧情」');
          toast('剧情还是空的，先填一段再生成', 'err', true);
          plotInput.focus();
          return;
        }
        running = true;
        renderBtn.classList.add('is-loading');
        renderBtn.setAttribute('aria-busy', 'true');
        renderBtn.textContent = '生成中…';

        const steps = [
          ['info', '开始剧情句级对齐…'],
          ['ok',   '对齐完成：125 / 128 段命中'],
          ['warn', '3 段置信度低于 0.7，已标黄'],
          ['info', '调用 edge-tts 生成解说…'],
          ['ok',   '解说音频生成完成，时长 08:42'],
          ['info', '硬字幕烧录中…'],
          ['ok',   '导出完成 → output/让子弹飞_解说版.mp4'],
        ];
        let i = 0;
        const timer = setInterval(() => {
          if (i >= steps.length) {
            clearInterval(timer);
            running = false;
            renderBtn.classList.remove('is-loading');
            renderBtn.removeAttribute('aria-busy');
            renderBtn.textContent = '开始生成';
            $('#wProgNum').textContent = '完成';
            toast('成片已导出到 output/', 'ok');
            return;
          }
          const [lv, msg] = steps[i++];
          pushLog(console_, lv, msg);
          const pct = Math.round((i / steps.length) * 100);
          $('#wProg').style.width = pct + '%';
          $('#wProgNum').textContent = pct + '%';
        }, reduce ? 120 : 620);
      });
    }

    // 空态模拟
    const emptyBtn = $('#emptyDemo');
    if (emptyBtn) {
      let empty = false;
      emptyBtn.addEventListener('click', () => {
        empty = !empty;
        $('#clipList').hidden = empty;
        $('#clipEmpty').hidden = !empty;
        $('#pendingBadge').hidden = empty;
        emptyBtn.textContent = empty ? '恢复素材' : '模拟空态';
        pushLog(console_, empty ? 'warn' : 'info', empty ? '素材已清空' : '素材已恢复');
      });
    }

    // 清空日志
    const clear = $('#logClear');
    if (clear) clear.addEventListener('click', () => { console_.innerHTML = ''; });

    // 引导条关闭
    const ob = $('#onboard');
    if (ob) {
      $('#onboardClose').addEventListener('click', () => {
        ob.style.transition = 'opacity var(--dur-base) var(--ease-out), max-height var(--dur-base) var(--ease-out)';
        ob.style.maxHeight = ob.offsetHeight + 'px';
        ob.style.overflow = 'hidden';
        requestAnimationFrame(() => { ob.style.opacity = '0'; ob.style.maxHeight = '0px'; });
        setTimeout(() => ob.remove(), reduce ? 0 : 340);
      });
    }

    pushLog(console_, 'ok', '工作台就绪，显存可用 12.0 GB');
    pushLog(console_, 'info', '拖入视频或点左上角「导入视频」开始');
  }

  /* ---------------- 快捷键：W 进工作台 ---------------- */
  function initHotkey() {
    document.addEventListener('keydown', (e) => {
      const tag = document.activeElement?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key.toLowerCase() === 'w' && !document.body.dataset.wb) {
        window.location.href = 'workbench.html';
      }
    });
  }

  /* ---------------- 启动 ---------------- */
  document.addEventListener('DOMContentLoaded', () => {
    initReveal();
    initCounters();
    initCopy();
    initContrast();
    initSliders();
    initStepper('.stepper [role="tab"][id^="tab-"]', 'panel-', $('#flowConsole'));
    initFlowNav();
    initKitDemos();
    seedFlowLog();
    initWorkbench();
    initHotkey();
  });
})();

/* HomePlay browser client: shows the PC's screen, plays its sound and sends your input back. */
'use strict';
(() => {
  // ================================================================== helpers
  const $ = (id) => document.getElementById(id);
  const now = () => performance.now();
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem('homeplay.' + key); return v === null ? fallback : JSON.parse(v); }
      catch { return fallback; }
    },
    set(key, value) { try { localStorage.setItem('homeplay.' + key, JSON.stringify(value)); } catch { /* private mode */ } },
    remove(key) { try { localStorage.removeItem('homeplay.' + key); } catch { /* ignore */ } },
  };

  const ua = navigator.userAgent;
  const isIOS = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const hasTouch = navigator.maxTouchPoints > 0 || 'ontouchstart' in window;
  const touchFirst = matchMedia('(pointer: coarse)').matches;
  const hasMouse = matchMedia('(any-pointer: fine)').matches || !hasTouch;
  const standalone = matchMedia('(display-mode: standalone), (display-mode: fullscreen)').matches || navigator.standalone;
  document.body.classList.toggle('has-touch', hasTouch);
  document.body.classList.toggle('has-mouse', hasMouse);

  const RES = [['native', 'Native'], ['1440p', '1440p'], ['1080p', '1080p'], ['720p', '720p'], ['540p', '540p']];
  const FPS = [[30, '30 fps'], [60, '60 fps'], [90, '90 fps'], [120, '120 fps'], [144, '144 fps']];
  const KBPS = [3000, 5000, 8000, 12000, 15000, 20000, 30000, 40000, 50000, 80000]
    .map((k) => [k, k / 1000 + ' Mbit/s']);
  const DEFAULTS = touchFirst
    ? { res: '720p', fps: 60, kbps: 8000, mon: 0, codec: 'auto', auto: true }
    : { res: 'native', fps: 60, kbps: 15000, mon: 0, codec: 'auto', auto: false };
  let cfg = Object.assign({}, DEFAULTS, store.get('cfg', {}));
  const mode = {
    mouse: store.get('mouseMode', 'game'),        // 'game' (captured, relative) | 'desktop' (absolute)
    touch: store.get('touchMode', 'trackpad'),    // 'trackpad' | 'direct' | 'pad'
  };
  let sensitivity = store.get('sens', 1.6);
  let volume = store.get('vol', 1);
  let showStats = store.get('stats', false);

  const login = $('login'), stage = $('stage'), canvas = $('video'), surface = $('surface');
  const menu = $('menu'), kbInput = $('kbInput');
  const ctx2d = canvas.getContext('2d', { alpha: false, desynchronized: true });

  // ================================================================== UI bits
  function fillSelect(sel, items, value) {
    sel.textContent = '';
    for (const [v, label] of items) {
      const o = document.createElement('option');
      o.value = String(v);
      o.textContent = label;
      sel.appendChild(o);
    }
    sel.value = String(value);
    if (sel.value !== String(value)) sel.selectedIndex = 0;
  }
  function showCfg(suffix) {
    fillSelect($('res' + suffix), RES, cfg.res);
    fillSelect($('fps' + suffix), FPS, cfg.fps);
    fillSelect($('kbps' + suffix), KBPS, cfg.kbps);
    const auto = $('auto' + suffix);
    if (auto) { auto.checked = !!cfg.auto; auto.onchange = () => updateBitrateLabel(suffix); }
    const codec = $('codec' + suffix);
    if (codec) codec.value = cfg.codec || 'auto';
    updateBitrateLabel(suffix);
  }
  function updateBitrateLabel(suffix) {
    const auto = $('auto' + suffix), label = $('kbps' + suffix + 'Label');
    if (label) label.textContent = (auto && auto.checked) ? 'Max bitrate' : 'Bitrate';
  }
  function readCfg(suffix) {
    const auto = $('auto' + suffix), codec = $('codec' + suffix);
    return Object.assign({}, cfg, {
      res: $('res' + suffix).value, fps: +$('fps' + suffix).value, kbps: +$('kbps' + suffix).value,
      auto: auto ? auto.checked : cfg.auto, codec: codec ? codec.value : (cfg.codec || 'auto'),
    });
  }

  let toastTimer = 0;
  function showToast(text, ms = 3000) {
    const el = $('toast');
    el.textContent = text;
    el.classList.remove('hidden');
    clearTimeout(toastTimer);
    if (ms > 0) toastTimer = setTimeout(() => el.classList.add('hidden'), ms);
  }
  function hideToast() { clearTimeout(toastTimer); $('toast').classList.add('hidden'); }

  let hintTimer = 0;
  function showHint(text, ms = 3000) {
    const el = $('hint');
    el.textContent = text;
    el.classList.remove('hidden');
    clearTimeout(hintTimer);
    if (ms > 0) hintTimer = setTimeout(() => el.classList.add('hidden'), ms);
  }
  function hideHint() { clearTimeout(hintTimer); $('hint').classList.add('hidden'); }

  function setLoginMessage(text, ok = false) {
    const el = $('loginMsg');
    el.textContent = text || '';
    el.classList.toggle('ok', ok);
  }

  function browserProblem() {
    if (!window.isSecureContext) {
      return 'This page has to be opened with https:// (or as http://localhost on the PC itself). See the setup guide.';
    }
    if (!('VideoDecoder' in window)) {
      return 'This browser cannot play the stream. Use Chrome, Edge or Safari 16.4 or newer.';
    }
    return '';
  }

  // ================================================================== layout
  let videoW = 0, videoH = 0;
  function layout() {
    const W = stage.clientWidth, H = stage.clientHeight;
    if (!W || !H) return;
    const vw = canvas.width || 16, vh = canvas.height || 9;
    const s = Math.min(W / vw, H / vh);
    const w = Math.round(vw * s), h = Math.round(vh * s);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    canvas.style.left = Math.round((W - w) / 2) + 'px';
    canvas.style.top = Math.round((H - h) / 2) + 'px';
  }
  window.addEventListener('resize', layout);
  if (window.visualViewport) window.visualViewport.addEventListener('resize', layout);

  // ================================================================== connection
  let ws = null;
  let password = '';
  let inStage = false;
  let authed = false;
  let userLeft = false;
  let reconnectTimer = 0;
  let reconnectAttempts = 0;
  let hostInfo = {};
  let streamInfo = {};

  const connected = () => authed && ws && ws.readyState === 1;
  function send(msg) {
    if (ws && ws.readyState === 1 && authed) ws.send(JSON.stringify(msg));
  }

  // Which codecs can this device actually decode? Newer ones give a much better
  // picture at the same bitrate, but only some devices (and browsers) handle them.
  let codecSupport = { h264: true, hevc: false, av1: false };
  async function probeCodecs() {
    const can = async (codec) => {
      try { const r = await VideoDecoder.isConfigSupported({ codec, optimizeForLatency: true }); return !!(r && r.supported); }
      catch { return false; }
    };
    codecSupport = {
      h264: true,
      hevc: (await can('hvc1.1.6.L120.90')) || (await can('hev1.1.6.L120.90')),
      av1: await can('av01.0.08M.08'),
    };
    return codecSupport;
  }

  function cfgForHost() {
    return { res: cfg.res, fps: cfg.fps, kbps: cfg.kbps, mon: cfg.mon || 0, cursor: true,
             codec: cfg.codec || 'auto', auto: !!cfg.auto };
  }

  function connect() {
    clearTimeout(reconnectTimer);
    if (ws) { try { ws.onclose = null; ws.close(); } catch { /* ignore */ } }
    authed = false;
    const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
    let sock;
    try { sock = new WebSocket(url); } catch (e) { onClosed({ code: 1006 }); return; }
    sock.binaryType = 'arraybuffer';
    ws = sock;
    sock.onopen = () => {
      sock.send(JSON.stringify({ t: 'hello', pw: password, cfg: cfgForHost(), codecs: codecSupport,
                                touch: touchFirst, ua: ua.slice(0, 200) }));
    };
    sock.onmessage = (ev) => {
      if (sock !== ws) return;
      if (typeof ev.data === 'string') onText(ev.data);
      else onBinary(ev.data);
    };
    sock.onclose = (ev) => {
      if (sock !== ws) return;
      ws = null;
      onClosed(ev);
    };
  }

  function onClosed(ev) {
    const wasAuthed = authed;
    authed = false;
    closeDecoder();
    padsSent.clear();
    const code = ev.code;
    if (code === 4001) { store.remove('pw'); showLogin('Wrong password.'); return; }
    if (code === 4002) { showLogin('Another device connected, so this one was disconnected.', true); return; }
    if (code === 4003) { showLogin('Too many wrong passwords. Wait two minutes and try again.'); return; }
    if (userLeft) { showLogin('Disconnected.', true); return; }
    if (!inStage) { showLogin("Can't reach the PC. Is HomePlay running there?"); return; }
    if (wasAuthed) reconnectAttempts = 0;
    reconnectAttempts++;
    if (reconnectAttempts > 12) { showLogin('Lost the connection to the PC.'); return; }
    showToast('Connection lost - reconnecting…', 0);
    reconnectTimer = setTimeout(connect, Math.min(8000, 500 * 2 ** Math.min(reconnectAttempts, 4)));
  }

  function onText(raw) {
    let m;
    try { m = JSON.parse(raw); } catch { return; }
    switch (m.t) {
      case 'welcome': onWelcome(m); break;
      case 'stream': streamInfo = m; updateMenuInfo(); hideToast(); break;
      case 'pong': stats.rtt = now() - m.v; break;
      case 'st': onHostStats(m); break;
      case 'status': showToast(m.msg, 6000); break;
      case 'rumble': rumble(m.id, m.l, m.s); break;
      case 'clip': onPcClipboard(m.text); break;
      default: break;
    }
  }

  // ================================================================== clipboard sync
  let pcClipboard = '';
  function onPcClipboard(text) {
    if (typeof text !== 'string' || text === pcClipboard) return;
    pcClipboard = text;
    const btn = $('clipFromPc');
    if (btn) btn.disabled = false;
    // Try to copy it straight to this device; if the browser blocks that without a tap,
    // the "Copy PC clipboard" menu button does it on demand.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        () => showToast('Copied from PC to this device', 2500),
        () => showToast('PC clipboard received — menu ••• › "Copy PC clipboard here"', 4000));
    } else {
      showToast('PC clipboard received — open the menu to copy it here', 4000);
    }
  }
  async function sendClipboardToPc() {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { send(['clip', text]); showToast('Sent your clipboard to the PC', 2500); }
      else showToast('Your clipboard is empty', 2500);
    } catch { showToast('This browser didn’t allow reading the clipboard', 3500); }
  }
  async function copyPcClipboardHere() {
    if (!pcClipboard) { showToast('Nothing copied on the PC yet', 2500); return; }
    try { await navigator.clipboard.writeText(pcClipboard); showToast('Copied to this device', 2500); }
    catch { showToast('This browser didn’t allow writing the clipboard', 3500); }
  }

  function onWelcome(m) {
    authed = true;
    hostInfo = m;
    reconnectAttempts = 0;
    hideToast();
    const first = !inStage;
    showStage();
    showToast(first ? 'Connected - starting the stream…' : 'Reconnected', 2500);
    setupMonitorSelect();
    updateMenuInfo();
    keepScreenOn();
    startPadPolling();
    if (vpadActive()) sendVpad();
    if (first) {
      if (touchFirst) showHint('Tap ••• at the top for the keyboard, controller and settings', 5000);
      else if (mode.mouse === 'game') showHint('Click the picture to control your PC', 0);
    }
  }

  // ================================================================== video
  let decoder = null, decoderConfig = null, waitKey = true, decodeErrors = 0;
  let currentCodec = 'h264';

  function codecFamily(codecStr) {
    if (codecStr.startsWith('hev') || codecStr.startsWith('hvc')) return 'hevc';
    if (codecStr.startsWith('av01')) return 'av1';
    return 'h264';
  }
  function configureDecoder(codecStr, desc) {
    currentCodec = codecFamily(codecStr);
    decoderConfig = { codec: codecStr, description: desc, optimizeForLatency: true };
    openDecoder();
  }
  function openDecoder() {
    closeDecoder();
    if (!decoderConfig) return;
    try {
      decoder = new VideoDecoder({ output: onFrame, error: onDecoderError });
      decoder.configure(decoderConfig);
    } catch (e) {
      decoder = null;
      if (currentCodec !== 'h264') { giveUpCodec(); return; }
      showToast('This browser could not start the video (' + e.message + ').', 8000);
    }
  }
  function closeDecoder() {
    if (decoder && decoder.state !== 'closed') { try { decoder.close(); } catch { /* ignore */ } }
    decoder = null;
    waitKey = true;
  }
  function giveUpCodec() {
    // This device can't actually decode the fancy codec; ask the PC to switch to H.264.
    showToast('Switching to H.264 (this device can’t decode ' + currentCodec.toUpperCase() + ').', 4000);
    send(['nocodec', currentCodec]);
    currentCodec = 'h264';
    decodeErrors = 0;
  }
  function onDecoderError(e) {
    console.warn('Video decoder error:', e);
    decoder = null;
    decodeErrors++;
    if (currentCodec !== 'h264' && decodeErrors >= 2) { giveUpCodec(); return; }
    if (decodeErrors === 6) showToast("This device has trouble decoding the video. Try a lower resolution in the menu.", 8000);
    setTimeout(() => { if (connected()) { openDecoder(); send(['kf']); } }, 50);
  }
  function decodeChunk(key, ts, data) {
    if (!decoder || decoder.state !== 'configured') return;
    if (waitKey) {
      if (!key) return;
      waitKey = false;
    } else if (!key && decoder.decodeQueueSize > 4) {
      waitKey = true;                       // this device can't keep up: skip to the next keyframe
      stats.lateDrops++;
      return;
    }
    try {
      decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: ts * 1000, data }));
    } catch (e) {
      console.warn(e);
      waitKey = true;
    }
  }
  function onFrame(frame) {
    const w = frame.displayWidth, h = frame.displayHeight;
    if (w !== videoW || h !== videoH) {
      videoW = w; videoH = h;
      canvas.width = w; canvas.height = h;
      layout();
    }
    ctx2d.drawImage(frame, 0, 0, w, h);
    frame.close();
    stats.frames++;
    decodeErrors = 0;
  }

  function onBinary(buf) {
    const dv = new DataView(buf);
    const type = dv.getUint8(0);
    stats.bytes += buf.byteLength;
    if (type === 2) {
      const seq = dv.getUint32(2, true);
      send(['ack', seq]);
      decodeChunk((dv.getUint8(1) & 1) === 1, dv.getUint32(6, true), new Uint8Array(buf, 10));
    } else if (type === 3) {
      audio.push(buf, dv.getUint8(1), dv.getUint32(4, true));
    } else if (type === 1) {
      const n = dv.getUint8(1);                       // [1][codec-string length][codec string][record]
      const codecStr = new TextDecoder().decode(new Uint8Array(buf, 2, n));
      configureDecoder(codecStr, new Uint8Array(buf.slice(2 + n)));
    }
  }

  // ================================================================== audio
  const audio = {
    ctx: null, node: null, gain: null, ready: false,
    start() {                                     // must run inside a click / tap
      if (this.ctx) { this.ctx.resume().catch(() => {}); return; }
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC || !window.AudioWorkletNode) return;
      try { this.ctx = new AC({ latencyHint: 'interactive', sampleRate: 48000 }); }
      catch { try { this.ctx = new AC({ latencyHint: 'interactive' }); } catch { return; } }
      this.ctx.resume().catch(() => {});
      this.ctx.audioWorklet.addModule('audio-worklet.js').then(() => {
        this.node = new AudioWorkletNode(this.ctx, 'homeplay-pcm', { numberOfInputs: 0, outputChannelCount: [2] });
        this.gain = this.ctx.createGain();
        this.gain.gain.value = volume;
        this.node.connect(this.gain).connect(this.ctx.destination);
        this.ready = true;
      }).catch((e) => console.warn('Audio is not available:', e));
    },
    push(buf, channels, rate) {
      if (!this.ready) return;
      if (this.ctx.state !== 'running') this.ctx.resume().catch(() => {});
      const pcm = new Int16Array(buf.slice(8));
      this.node.port.postMessage({ pcm, channels, rate }, [pcm.buffer]);
    },
    setVolume(v) { if (this.gain) this.gain.gain.value = v; },
  };

  // ================================================================== stats
  const stats = { frames: 0, bytes: 0, lateDrops: 0, hostDrops: 0, rtt: 0, last: now(), congested: 0, lastWarn: 0 };
  setInterval(() => {
    const t = now(), dt = Math.max(0.001, (t - stats.last) / 1000);
    stats.last = t;
    const fps = stats.frames / dt, mbps = (stats.bytes * 8) / dt / 1e6;
    stats.frames = 0;
    stats.bytes = 0;
    if (showStats && inStage) {
      const cap = streamInfo.kbps ? `${(streamInfo.kbps / 1000).toFixed(streamInfo.kbps < 10000 ? 1 : 0)} Mbit/s${streamInfo.auto ? ' auto' : ''}` : '';
      $('stats').textContent =
        `${fps.toFixed(0)} fps   ${mbps.toFixed(1)} Mbit/s   ${Math.round(stats.rtt)} ms\n` +
        `${videoW}×${videoH}   ${streamInfo.encoder || ''}   ${cap}\n` +
        `dropped ${stats.hostDrops + stats.lateDrops}`;
    }
    if (connected()) send(['ping', Math.round(now() * 10) / 10]);
  }, 1000);

  function onHostStats(m) {
    stats.hostDrops += m.drop || 0;
    if ((m.drop || 0) >= 6) stats.congested++;
    else stats.congested = 0;
    // When Auto is on the host lowers the bitrate itself, so there's nothing to nag about.
    if (!cfg.auto && stats.congested >= 2 && now() - stats.lastWarn > 30000) {
      stats.lastWarn = now();
      showToast("Your connection can't keep up. Open the menu (•••) and lower the bitrate, or turn on Auto.", 6000);
    }
  }

  // ================================================================== screens
  function showStage() {
    login.classList.add('hidden');
    stage.classList.remove('hidden');
    inStage = true;
    $('stats').classList.toggle('hidden', !showStats);
    applyTouchMode();
    layout();
    surface.focus({ preventScroll: true });
    poke();
  }
  function showLogin(message, ok = false) {
    clearTimeout(reconnectTimer);
    inStage = false;
    authed = false;
    closeMenu();
    hideHint();
    hideToast();
    releaseWakeLock();
    if (document.pointerLockElement) document.exitPointerLock();
    stage.classList.add('hidden');
    login.classList.remove('hidden');
    $('connectBtn').disabled = false;
    setLoginMessage(message, ok);
  }

  // ================================================================== login form
  $('pw').value = store.get('pw', '');
  showCfg('Login');
  {
    const problem = browserProblem();
    const hint = $('envHint');
    if (problem) hint.textContent = problem;
    else if (isIOS && !standalone) hint.textContent = 'Tip: in Safari tap Share → "Add to Home Screen" to play in full screen.';
    else if (touchFirst) hint.textContent = 'Tip: turn your phone sideways. A Bluetooth controller works too.';
    else hint.textContent = 'Tip: use full screen in Chrome or Edge so keys like Esc and Alt+Tab go to the game.';
  }

  $('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const problem = browserProblem();
    if (problem) { setLoginMessage(problem); return; }
    password = $('pw').value.trim();
    if (!password) return;
    if ($('remember').checked) store.set('pw', password); else store.remove('pw');
    cfg = readCfg('Login');
    store.set('cfg', cfg);
    audio.start();                               // browsers only allow sound after a tap/click
    userLeft = false;
    reconnectAttempts = 0;
    setLoginMessage('Connecting…', true);
    $('connectBtn').disabled = true;
    await probeCodecs();                          // find out which codecs this device can decode
    connect();
  });

  // ================================================================== screen wake lock
  let wakeLock = null;
  async function keepScreenOn() {
    try {
      if ('wakeLock' in navigator && !document.hidden && !wakeLock) {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
      }
    } catch { /* not allowed right now */ }
  }
  function releaseWakeLock() { if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; } }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { releaseAll(); return; }
    if (!inStage) return;
    keepScreenOn();
    if (!ws && !userLeft) { reconnectAttempts = 0; connect(); }
  });

  // ================================================================== full screen
  let keyboardLocked = false;
  async function toggleFullscreen() {
    const doc = document, el = document.documentElement;
    if (doc.fullscreenElement || doc.webkitFullscreenElement) {
      (doc.exitFullscreen || doc.webkitExitFullscreen).call(doc);
      return;
    }
    const request = el.requestFullscreen || el.webkitRequestFullscreen;
    if (!request) {
      showToast(isIOS ? 'On iPhone: tap Share → "Add to Home Screen", then start HomePlay from the home screen.'
        : 'Full screen is not available in this browser.', 7000);
      return;
    }
    try { await request.call(el, { navigationUI: 'hide' }); } catch { return; }
    if (touchFirst && screen.orientation && screen.orientation.lock) screen.orientation.lock('landscape').catch(() => {});
    if (navigator.keyboard && navigator.keyboard.lock) {
      try { await navigator.keyboard.lock(); keyboardLocked = true; } catch { keyboardLocked = false; }
    }
  }
  const onFullscreenChange = () => {
    const fs = !!(document.fullscreenElement || document.webkitFullscreenElement);
    if (!fs) keyboardLocked = false;
    $('fsBtn').textContent = fs ? 'Exit full screen' : 'Full screen';
    setTimeout(layout, 50);
  };
  document.addEventListener('fullscreenchange', onFullscreenChange);
  document.addEventListener('webkitfullscreenchange', onFullscreenChange);

  // ================================================================== keyboard
  const keysDown = new Set();
  function onKey(e, down) {
    if (!inStage) return;
    if (!menu.classList.contains('hidden')) {
      if (down && e.key === 'Escape') closeMenu();
      return;                                   // let the menu controls work normally
    }
    const target = e.target;
    if (target === kbInput) {
      if (e.isComposing || e.keyCode === 229 || e.key === 'Unidentified') return;
      if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) return;   // typed text: 'input' handles it
    } else if (target && /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) {
      return;
    }
    if (down && e.code === 'KeyM' && e.ctrlKey && e.altKey && e.shiftKey) {
      e.preventDefault();
      openMenu();
      return;
    }
    if (!e.code || e.code === 'Unidentified') return;
    e.preventDefault();
    if (!connected()) return;
    if (down) keysDown.add(e.code);
    else if (!keysDown.delete(e.code)) return;
    send(['k', e.code, down ? 1 : 0]);
  }
  window.addEventListener('keydown', (e) => onKey(e, true), true);
  window.addEventListener('keyup', (e) => onKey(e, false), true);

  function releaseAll() {
    for (const code of keysDown) send(['k', code, 0]);
    keysDown.clear();
    for (const b of mouseButtons) send(['b', b, 0]);
    mouseButtons.clear();
    send(['rel']);
  }
  window.addEventListener('blur', releaseAll);

  // On-screen keyboard (phones): a hidden text box catches what you type.
  const SENTINEL = ' '.repeat(8);   // lets us notice Backspace even when nothing was typed
  let composing = false;
  kbInput.value = SENTINEL;
  function flushTyped() {
    const v = kbInput.value;
    let p = 0;
    while (p < v.length && p < SENTINEL.length && v[p] === SENTINEL[p]) p++;
    const deleted = SENTINEL.length - p;
    const typed = v.slice(p).replace(/ /g, ' ');
    for (let i = 0; i < deleted; i++) { send(['k', 'Backspace', 1]); send(['k', 'Backspace', 0]); }
    if (typed) send(['t', typed]);
    kbInput.value = SENTINEL;
    try { kbInput.setSelectionRange(SENTINEL.length, SENTINEL.length); } catch { /* ignore */ }
  }
  kbInput.addEventListener('compositionstart', () => { composing = true; });
  kbInput.addEventListener('compositionend', () => { composing = false; flushTyped(); });
  kbInput.addEventListener('input', () => { if (!composing) flushTyped(); });
  kbInput.addEventListener('focus', () => {
    kbInput.value = SENTINEL;
    try { kbInput.setSelectionRange(SENTINEL.length, SENTINEL.length); } catch { /* ignore */ }
  });

  // ================================================================== mouse
  let pointerLocked = false;
  let lastTouch = 0;
  const mouseButtons = new Set();
  const recentTouch = () => now() - lastTouch < 800;

  function lockPointer() {
    if (!surface.requestPointerLock) return;
    let result = null;
    try { result = surface.requestPointerLock({ unadjustedMovement: true }); } catch { result = null; }
    if (result && result.catch) result.catch(() => { try { surface.requestPointerLock(); } catch { /* ignore */ } });
  }
  document.addEventListener('pointerlockchange', () => {
    pointerLocked = document.pointerLockElement === surface;
    stage.classList.toggle('locked', pointerLocked);
    if (pointerLocked) {
      showHint(keyboardLocked ? 'Hold Esc to leave full screen' : 'Press Esc to free the mouse', 2500);
    } else {
      for (const b of mouseButtons) send(['b', b, 0]);
      mouseButtons.clear();
      if (inStage && mode.mouse === 'game' && !touchFirst && menu.classList.contains('hidden')) {
        showHint('Click the picture to control your PC', 0);
      }
    }
  });

  let moveX = 0, moveY = 0, moveQueued = false;
  function queueRelative(dx, dy) {
    moveX += dx;
    moveY += dy;
    if (moveQueued) return;
    moveQueued = true;
    queueMicrotask(() => {
      moveQueued = false;
      const x = Math.round(moveX), y = Math.round(moveY);
      moveX -= x;
      moveY -= y;
      if (x || y) send(['m', x, y]);
    });
  }
  function videoPoint(clientX, clientY) {
    const r = canvas.getBoundingClientRect();
    const x = clamp((clientX - r.left) / r.width, 0, 1), y = clamp((clientY - r.top) / r.height, 0, 1);
    return [Math.round(x * 65535), Math.round(y * 65535)];
  }
  let lastAbs = '';
  function sendAbsolute(clientX, clientY, force = false) {
    const p = videoPoint(clientX, clientY), key = p.join(',');
    if (!force && key === lastAbs) return;
    lastAbs = key;
    send(['a', p[0], p[1]]);
  }

  surface.addEventListener('mousedown', (e) => {
    if (recentTouch() || !connected()) return;
    e.preventDefault();
    surface.focus({ preventScroll: true });
    if (mode.mouse === 'game' && !pointerLocked) { hideHint(); lockPointer(); return; }
    if (mode.mouse === 'desktop') sendAbsolute(e.clientX, e.clientY, true);
    mouseButtons.add(e.button);
    send(['b', e.button, 1]);
  });
  window.addEventListener('mouseup', (e) => {
    if (!mouseButtons.has(e.button)) return;
    e.preventDefault();
    mouseButtons.delete(e.button);
    send(['b', e.button, 0]);
  });
  surface.addEventListener('contextmenu', (e) => e.preventDefault());
  surface.addEventListener('auxclick', (e) => e.preventDefault());

  let wheelY = 0, wheelX = 0;
  surface.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (!connected() || (mode.mouse === 'game' && !pointerLocked)) return;
    const unit = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 800 : 1;
    wheelY -= e.deltaY * unit * 1.2;
    wheelX += e.deltaX * unit * 1.2;
    const y = Math.trunc(wheelY), x = Math.trunc(wheelX);
    if (y || x) {
      wheelY -= y;
      wheelX -= x;
      send(['w', y, x]);
    }
  }, { passive: false });

  function onMouseMove(e) {
    poke();
    if (!connected()) return;
    if (pointerLocked) {
      let dx = 0, dy = 0;
      const list = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
      if (list.length) for (const c of list) { dx += c.movementX; dy += c.movementY; }
      else { dx = e.movementX; dy = e.movementY; }
      queueRelative(dx, dy);
    } else if (mode.mouse === 'desktop' && !recentTouch()) {
      sendAbsolute(e.clientX, e.clientY);
    }
  }

  // Hide the menu button while the mouse is resting (desktop).
  let idleTimer = 0;
  function poke() {
    stage.classList.remove('idle');
    clearTimeout(idleTimer);
    if (!touchFirst) idleTimer = setTimeout(() => stage.classList.add('idle'), 2500);
  }

  // ================================================================== touch: trackpad / tap to click
  const touches = new Map();
  let gesture = null;
  function buzz(ms = 10) { if (navigator.vibrate) { try { navigator.vibrate(ms); } catch { /* ignore */ } } }
  const click = (button) => { send(['b', button, 1]); send(['b', button, 0]); };

  surface.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse') return;
    e.preventDefault();
    lastTouch = now();
    if (audio.ctx && audio.ctx.state !== 'running') audio.ctx.resume().catch(() => {});
    if (!connected() || mode.touch === 'pad') return;
    try { surface.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    touches.set(e.pointerId, { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY });
    if (touches.size === 1) {
      gesture = { t0: now(), moved: false, multi: false, dragging: false, pressed: false, right: false, scrolled: false, scroll: 0, hold: 0 };
      if (mode.touch === 'trackpad') {
        gesture.hold = setTimeout(() => {           // press and hold, then drag = drag with the left button
          if (gesture && !gesture.moved && !gesture.multi) { gesture.dragging = true; send(['b', 0, 1]); buzz(15); }
        }, 450);
      } else {
        sendAbsolute(e.clientX, e.clientY, true);
        gesture.hold = setTimeout(() => {           // press and hold = right click
          if (gesture && !gesture.moved && !gesture.multi) { gesture.right = true; click(2); buzz(15); }
        }, 550);
      }
    } else if (gesture) {
      gesture.multi = true;
      clearTimeout(gesture.hold);
    }
  });

  surface.addEventListener('pointermove', (e) => {
    if (e.pointerType === 'mouse') { onMouseMove(e); return; }
    const t = touches.get(e.pointerId);
    if (!t || !gesture) return;
    lastTouch = now();
    const dx = e.clientX - t.x, dy = e.clientY - t.y;
    t.x = e.clientX;
    t.y = e.clientY;
    if (!gesture.moved && Math.hypot(e.clientX - t.sx, e.clientY - t.sy) > 8) {
      gesture.moved = true;
      if (!gesture.dragging) clearTimeout(gesture.hold);
    }
    if (gesture.multi) {                            // two fingers: scroll
      gesture.scroll += dy / Math.max(1, touches.size);
      const steps = Math.trunc(gesture.scroll / 18);
      if (steps) {
        gesture.scroll -= steps * 18;
        gesture.scrolled = true;
        send(['w', steps * 120, 0]);
      }
      return;
    }
    if (mode.touch === 'trackpad') {
      const r = canvas.getBoundingClientRect();
      const k = sensitivity * (canvas.width / Math.max(1, r.width));
      queueRelative(dx * k, dy * k);
    } else if (gesture.moved && !gesture.right) {
      if (!gesture.pressed) { gesture.pressed = true; send(['b', 0, 1]); }
      sendAbsolute(e.clientX, e.clientY);
    }
  });

  function touchEnd(e) {
    if (e.pointerType === 'mouse') return;
    if (!touches.delete(e.pointerId) || !gesture || touches.size > 0) return;
    clearTimeout(gesture.hold);
    const quick = now() - gesture.t0 < 300;
    if (gesture.dragging || gesture.pressed) send(['b', 0, 0]);
    else if (gesture.multi) { if (!gesture.scrolled && quick) click(2); }
    else if (!gesture.moved && !gesture.right && (quick || mode.touch === 'direct')) click(0);
    gesture = null;
  }
  surface.addEventListener('pointerup', touchEnd);
  surface.addEventListener('pointercancel', touchEnd);

  // ================================================================== on-screen controller
  const BTN = { A: 0x1000, B: 0x2000, X: 0x4000, Y: 0x8000, LB: 0x0100, RB: 0x0200,
    BACK: 0x0020, START: 0x0010, LS: 0x0040, RS: 0x0080 };
  const vpad = { bits: 0, lt: 0, rt: 0, lx: 0, ly: 0, rx: 0, ry: 0 };
  let vpadQueued = false;
  const vpadActive = () => mode.touch === 'pad' && hasTouch;
  function sendVpad() {
    send(['g', 'touch', vpad.bits, vpad.lt, vpad.rt, vpad.lx, vpad.ly, vpad.rx, vpad.ry]);
  }
  function vpadChanged() {
    if (vpadQueued) return;
    vpadQueued = true;
    queueMicrotask(() => { vpadQueued = false; sendVpad(); });
  }
  function holdable(el, onChange) {
    let id = null;
    el.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      if (id !== null) return;
      id = e.pointerId;
      try { el.setPointerCapture(id); } catch { /* ignore */ }
      onChange(true, e);
    });
    el.addEventListener('pointermove', (e) => { if (e.pointerId === id) onChange(true, e); });
    const end = (e) => { if (e.pointerId !== id) return; id = null; onChange(false, e); };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('contextmenu', (e) => e.preventDefault());
  }
  document.querySelectorAll('#pad [data-btn]').forEach((el) => {
    const name = el.dataset.btn;
    let on = false;
    holdable(el, (down) => {
      if (down === on) return;
      on = down;
      el.classList.toggle('on', down);
      if (name === 'LT') vpad.lt = down ? 255 : 0;
      else if (name === 'RT') vpad.rt = down ? 255 : 0;
      else vpad.bits = down ? (vpad.bits | BTN[name]) : (vpad.bits & ~BTN[name]);
      if (down) buzz(8);
      vpadChanged();
    });
  });
  document.querySelectorAll('#pad .stick').forEach((el) => {
    const knob = el.querySelector('.knob');
    const left = el.dataset.stick === 'L';
    holdable(el, (down, e) => {
      let x = 0, y = 0;
      if (down) {
        const r = el.getBoundingClientRect(), radius = r.width * 0.4;
        let dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
        const d = Math.hypot(dx, dy);
        if (d > radius) { dx *= radius / d; dy *= radius / d; }
        knob.style.transform = `translate(${dx}px, ${dy}px)`;
        x = dx / radius;
        y = dy / radius;
      } else {
        knob.style.transform = '';
      }
      const sx = Math.round(clamp(x, -1, 1) * 32767), sy = Math.round(clamp(-y, -1, 1) * 32767);
      if (left) { vpad.lx = sx; vpad.ly = sy; } else { vpad.rx = sx; vpad.ry = sy; }
      vpadChanged();
    });
  });
  {
    const dpad = $('dpad');
    const arrows = ['up', 'down', 'left', 'right'].map((n) => dpad.querySelector('.' + n));
    holdable(dpad, (down, e) => {
      let bits = 0;
      if (down) {
        const r = dpad.getBoundingClientRect();
        const dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
        if (Math.hypot(dx, dy) > r.width * 0.12) {
          const a = Math.atan2(-dy, dx) * 180 / Math.PI;      // 0 = right, 90 = up
          if (a > 22.5 && a < 157.5) bits |= 0x1;              // up
          if (a < -22.5 && a > -157.5) bits |= 0x2;            // down
          if (a > 112.5 || a < -112.5) bits |= 0x4;            // left
          if (a > -67.5 && a < 67.5) bits |= 0x8;              // right
        }
      }
      const old = vpad.bits & 0xF;
      if (bits === old) return;
      vpad.bits = (vpad.bits & ~0xF) | bits;
      arrows.forEach((el, i) => el.classList.toggle('on', !!(bits & (1 << i))));
      if (bits & ~old) buzz(6);
      vpadChanged();
    });
  }

  // ================================================================== physical gamepads
  const padsSent = new Map();
  let padTimer = 0;
  function readGamepad(gp) {
    const pressed = (i) => { const b = gp.buttons[i]; return !!b && (b.pressed || b.value > 0.5); };
    const value = (i) => { const b = gp.buttons[i]; return b ? b.value : 0; };
    const map = [[12, 0x0001], [13, 0x0002], [14, 0x0004], [15, 0x0008], [9, 0x0010], [8, 0x0020],
      [10, 0x0040], [11, 0x0080], [4, 0x0100], [5, 0x0200], [16, 0x0400],
      [0, 0x1000], [1, 0x2000], [2, 0x4000], [3, 0x8000]];
    let bits = 0;
    for (const [i, bit] of map) if (pressed(i)) bits |= bit;
    const axis = (i) => {
      const v = gp.axes[i] || 0;
      return Math.abs(v) < 0.05 ? 0 : Math.round(clamp(v, -1, 1) * 512) / 512;   // drop jitter
    };
    const s = (v) => clamp(Math.round(v * 32767), -32768, 32767);
    return [bits, Math.round(value(6) * 255), Math.round(value(7) * 255),
      s(axis(0)), s(-axis(1)), s(axis(2)), s(-axis(3))];
  }
  function pollPads() {
    if (!connected()) return;
    const list = navigator.getGamepads ? navigator.getGamepads() : [];
    const seen = new Set();
    for (const gp of list) {
      if (!gp || !gp.connected) continue;
      const id = 'p' + gp.index;
      seen.add(id);
      const state = readGamepad(gp), key = state.join(',');
      if (padsSent.get(id) !== key) { padsSent.set(id, key); send(['g', id, ...state]); }
    }
    for (const id of [...padsSent.keys()]) {
      if (!seen.has(id)) { padsSent.delete(id); send(['gx', id]); }
    }
  }
  function startPadPolling() {
    const any = navigator.getGamepads && [...navigator.getGamepads()].some((g) => g && g.connected);
    if (!padTimer && any) padTimer = setInterval(pollPads, 8);
  }
  window.addEventListener('gamepadconnected', (e) => {
    showToast('Controller connected: ' + e.gamepad.id.replace(/\(.*\)/, '').trim(), 2500);
    if (hostInfo && hostInfo.gamepad === false) {
      showToast('Controller connected, but the PC has no gamepad support yet (see the setup guide: ViGEmBus).', 7000);
    }
    startPadPolling();
  });
  function rumble(id, large, small) {
    if (id === 'touch') { if (large || small) buzz(120); return; }
    const gp = navigator.getGamepads ? navigator.getGamepads()[+id.slice(1)] : null;
    const actuator = gp && gp.vibrationActuator;
    if (!actuator || !actuator.playEffect) return;
    if (!large && !small) { if (actuator.reset) actuator.reset().catch(() => {}); return; }
    actuator.playEffect('dual-rumble', { startDelay: 0, duration: 3000, strongMagnitude: large / 255, weakMagnitude: small / 255 })
      .catch(() => {});
  }

  // ================================================================== menu
  function setSeg(seg, value) {
    seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === value));
  }
  const MOUSE_HELP = {
    game: 'Click the picture to capture the mouse, the way games expect it. Esc frees it.',
    desktop: 'The mouse moves freely over the picture. Good for menus and the Windows desktop.',
  };
  const TOUCH_HELP = {
    trackpad: 'Drag = move the pointer · tap = click · two-finger tap = right click · two fingers = scroll · hold, then drag = drag',
    direct: 'Tap where you want to click · hold = right click · drag = drag · two fingers = scroll',
    pad: 'An Xbox controller on the screen. Real controllers (Bluetooth/USB) also work in any mode.',
  };
  function applyTouchMode() {
    const padOn = vpadActive();
    $('pad').classList.toggle('hidden', !padOn);
    setSeg($('touchSeg'), mode.touch);
    $('touchHelp').textContent = TOUCH_HELP[mode.touch] || '';
    setSeg($('mouseSeg'), mode.mouse);
    $('mouseHelp').textContent = MOUSE_HELP[mode.mouse] || '';
  }
  function updateMenuInfo() {
    $('menuHost').textContent = hostInfo.host ? 'Connected to ' + hostInfo.host : 'HomePlay';
    const parts = [];
    if (streamInfo.width) parts.push(`${streamInfo.width}×${streamInfo.height}`);
    if (streamInfo.fps) parts.push(`${streamInfo.fps} fps`);
    if (streamInfo.kbps) parts.push(`${streamInfo.kbps / 1000} Mbit/s`);
    if (streamInfo.encoder) parts.push(streamInfo.encoder);
    if (hostInfo.demo) parts.push('demo mode');
    $('menuInfo').textContent = parts.join(' · ');
  }
  function setupMonitorSelect() {
    const mons = hostInfo.monitors || [];
    fillSelect($('monMenu'), mons.map((m, i) => [i, `Screen ${i + 1} (${m.w}×${m.h})`]), cfg.mon || 0);
    $('monField').classList.toggle('hidden', mons.length < 2);
  }
  function openMenu() {
    releaseAll();
    if (document.pointerLockElement) document.exitPointerLock();
    hideHint();
    showCfg('Menu');
    $('sens').value = sensitivity;
    $('vol').value = volume;
    applyTouchMode();
    updateMenuInfo();
    $('clipSection').classList.toggle('hidden', !hostInfo.clipboard);
    $('clipFromPc').disabled = !pcClipboard;
    menu.classList.remove('hidden');
  }
  function closeMenu() {
    menu.classList.add('hidden');
    if (inStage) surface.focus({ preventScroll: true });
  }

  $('menuBtn').addEventListener('click', () => (menu.classList.contains('hidden') ? openMenu() : closeMenu()));
  $('menuClose').addEventListener('click', closeMenu);
  $('fsBtn').addEventListener('click', () => { toggleFullscreen(); closeMenu(); });
  $('statsBtn').addEventListener('click', () => {
    showStats = !showStats;
    store.set('stats', showStats);
    $('stats').classList.toggle('hidden', !showStats);
    closeMenu();
  });
  $('kbBtn').addEventListener('click', () => {
    menu.classList.add('hidden');
    kbInput.focus({ preventScroll: true });      // the phone's keyboard opens; its own button hides it
  });
  $('clipToPc').addEventListener('click', () => { sendClipboardToPc(); closeMenu(); });
  $('clipFromPc').addEventListener('click', () => { copyPcClipboardHere(); closeMenu(); });
  $('mouseSeg').addEventListener('click', (e) => {
    const v = e.target.dataset && e.target.dataset.v;
    if (!v) return;
    mode.mouse = v;
    store.set('mouseMode', v);
    applyTouchMode();
  });
  $('touchSeg').addEventListener('click', (e) => {
    const v = e.target.dataset && e.target.dataset.v;
    if (!v) return;
    if (mode.touch === 'pad' && v !== 'pad') send(['gx', 'touch']);
    mode.touch = v;
    store.set('touchMode', v);
    applyTouchMode();
    if (v === 'pad') { sendVpad(); closeMenu(); }
  });
  $('sens').addEventListener('input', (e) => { sensitivity = +e.target.value; store.set('sens', sensitivity); });
  $('vol').addEventListener('input', (e) => { volume = +e.target.value; store.set('vol', volume); audio.setVolume(volume); });
  document.querySelectorAll('[data-combo]').forEach((b) => b.addEventListener('click', () => {
    const keys = b.dataset.combo.split('+');
    for (const k of keys) send(['k', k, 1]);
    for (const k of keys.slice().reverse()) send(['k', k, 0]);
  }));
  $('applyBtn').addEventListener('click', () => {
    cfg = readCfg('Menu');
    cfg.mon = +$('monMenu').value || 0;
    store.set('cfg', cfg);
    send(['cfg', cfgForHost()]);
    showToast('Changing the stream…', 2500);
    closeMenu();
  });
  $('disconnectBtn').addEventListener('click', () => {
    userLeft = true;
    releaseAll();
    if (ws) ws.close(1000); else showLogin('Disconnected.', true);
  });
  stage.addEventListener('pointerdown', (e) => {
    if (!menu.classList.contains('hidden') && !menu.contains(e.target) && e.target !== $('menuBtn')) closeMenu();
  }, true);
})();

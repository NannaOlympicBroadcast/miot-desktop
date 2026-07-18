'use strict';

// ===========================================================================
// State + API helpers
// ===========================================================================
let BASE = '';            // backend base url, e.g. http://127.0.0.1:54321
let HEALTH = {};
let DEVICES = [];
let CURRENT_DID = null;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// Thrown when the backend reports the cached mihome credential is expired or
// invalid (401), so callers can route straight to the login screen instead of
// just printing the raw error text.
class AuthRequiredError extends Error {
  constructor(message) {
    super(message);
    this.authRequired = true;
  }
}

async function api(path, opts) {
  const res = await fetch(BASE + path, opts);
  const data = await res.json().catch(() => ({ error: 'invalid json response' }));
  if (data && data.error) {
    if (data.auth_required) throw new AuthRequiredError(data.error);
    throw new Error(data.error);
  }
  return data;
}

// Central handler for an expired/invalid credential detected mid-session:
// jump to the login screen instead of leaving stale UI + an error toast.
function handleAuthExpired(message) {
  setStatus(false, '登录已过期');
  showLogin(message);
}
function apiPost(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function toast(msg, kind) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + (kind || '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), 2600);
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ===========================================================================
// Tabs
// ===========================================================================
$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.remove('active'));
    $$('.tab-panel').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    $('#tab-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'cameras') loadCameras();
    if (tab.dataset.tab === 'xiaomi') loadXiaomi();
  });
});

$('#refresh-btn').addEventListener('click', () => {
  const active = $('.tab.active').dataset.tab;
  if (active === 'devices') {
    loadDevices();
    if (CURRENT_DID) selectDevice(CURRENT_DID);
  } else if (active === 'cameras') {
    loadCameras();
  } else if (active === 'xiaomi') {
    loadXiaomi();
  }
});

$('#device-search').addEventListener('input', renderDeviceList);

// ---------------- Settings modal ----------------
$('#settings-btn').addEventListener('click', async () => {
  $('#settings-mask').classList.remove('hidden');
  try { $('#auto-launch').checked = await window.miot.getAutoLaunch(); } catch (e) { /* ignore */ }
});
$('#settings-close').addEventListener('click', () => $('#settings-mask').classList.add('hidden'));
$('#settings-mask').addEventListener('click', (e) => {
  if (e.target === $('#settings-mask')) $('#settings-mask').classList.add('hidden');
});
$('#auto-launch').addEventListener('change', async (e) => {
  const result = await window.miot.setAutoLaunch(e.target.checked);
  e.target.checked = result;
  toast(result ? '已开启开机自启' : '已关闭开机自启', 'ok');
});

// ===========================================================================
// Bootstrap
// ===========================================================================
async function boot() {
  try {
    BASE = await window.miot.getBackendUrl();
  } catch (e) {
    setStatus(false, '无法连接后端');
    return;
  }
  await refreshAuthAndRender();
}

function setStatus(online, text) {
  $('#status-dot').className = 'status-dot ' + (online ? 'online' : 'offline');
  $('#status-text').textContent = text;
}

async function refreshAuthAndRender() {
  for (let i = 0; i < 40; i++) {
    try {
      HEALTH = await api('/api/health');
      if (HEALTH.ready) {
        setStatus(true, '已连接 · ' + (HEALTH.cloud_server || '').toUpperCase());
        await loadDevices();
        return;
      }
      if (HEALTH.error) {
        setStatus(false, '未登录');
        showLogin(HEALTH.error);
        return;
      }
    } catch (e) { /* backend not up yet */ }
    setStatus(false, '初始化中…');
    await new Promise((r) => setTimeout(r, 800));
  }
  setStatus(false, '初始化超时');
  showLogin('初始化超时，可尝试登录或重启应用。');
}

// ===========================================================================
// Login (OAuth2)
// ===========================================================================
function showLogin(errMsg) {
  $('#device-list').innerHTML = '<li class="loading">请先登录小米账号</li>';
  CURRENT_DID = null;
  $('#device-detail').innerHTML = `
    <div class="login-card">
      <h2>登录小米账号</h2>
      <p class="login-msg">${escapeHtml(errMsg || '需要登录后才能加载设备。')}</p>
      <p class="login-msg">点击下方按钮，应用内窗口会弹出小米授权页面，登录后自动完成授权。</p>
      <p class="login-msg login-hint">
        提示：登录凭据保存在本地 <code>${escapeHtml(HEALTH.cache_path || '~/.miot_cache')}</code>，
        与 <strong>ssr-agent</strong>、<strong>Claude 米家桌面扩展 (claude-mijia-desktop-extension)</strong> 共享同一份凭据，
        任一端登录后其余端均可直接使用，无需重复登录。
      </p>
      <div class="login-actions">
        <button class="btn primary" id="login-start">登录小米账号</button>
      </div>
      <div id="login-url-box" class="login-url-box hidden">
        <div class="login-label">如授权窗口未弹出，请手动复制以下链接到浏览器完成登录：</div>
        <textarea id="login-url" class="text-input login-url" rows="2" readonly></textarea>
        <div class="login-label" style="margin-top:12px">
          登录完成后，将浏览器地址栏中的<strong>完整跳转 URL</strong>（含 <code>code=</code>）
          或仅复制其中的<strong>授权码（code= 后面的值）</strong>粘贴到下方：
        </div>
        <textarea id="login-redirect" class="text-input login-url" rows="2"
          placeholder="http://127.0.0.1/?code=…&state=… 或仅粘贴授权码"></textarea>
        <div class="login-actions">
          <button class="btn primary" id="login-complete">完成登录</button>
        </div>
      </div>
    </div>`;
  $('#login-start').addEventListener('click', startLogin);
}

async function startLogin() {
  const btn = $('#login-start');
  btn.disabled = true;
  btn.textContent = '登录中…';
  try {
    const r = await apiPost('/api/auth/start', {});
    $('#login-url').value = r.url;
    $('#login-complete').addEventListener('click', completeLogin, { once: false });

    // Open the authorize page in an in-app window and capture the redirect
    // ourselves, so the user doesn't have to copy/paste the URL.
    const redirectUrl = await window.miot.oauthLogin(r.url);
    if (redirectUrl) {
      await finishLogin(redirectUrl);
      return;
    }
    // User closed the login window before completing -> fall back to manual paste.
    $('#login-url-box').classList.remove('hidden');
    toast('登录窗口已关闭，可手动粘贴跳转后的 URL 完成登录', 'err');
  } catch (e) {
    toast('获取登录链接失败: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '重新登录';
  }
}

async function completeLogin() {
  const raw = ($('#login-redirect').value || '').trim();
  if (!raw) { toast('请粘贴跳转后的完整 URL 或授权码', 'err'); return; }
  await finishLogin(raw);
}

async function finishLogin(raw) {
  const btn = $('#login-complete');
  if (btn) { btn.disabled = true; btn.textContent = '登录中…'; }
  // Accept either a full redirect URL (contains code=) or a bare auth code.
  const body = raw.includes('code=') ? { url: raw } : { code: raw };
  try {
    await apiPost('/api/auth/complete', body);
    toast('登录成功', 'ok');
    try { window.miot.broadcast('logged-in'); } catch (e) {}
    await refreshAuthAndRender();
  } catch (e) {
    toast('登录失败: ' + e.message, 'err');
    if (btn) { btn.disabled = false; btn.textContent = '完成登录'; }
    $('#login-url-box').classList.remove('hidden');
  }
}

// ===========================================================================
// Devices list
// ===========================================================================
async function loadDevices() {
  $('#device-list').innerHTML = '<li class="loading"><span class="spinner"></span> 加载设备…</li>';
  try {
    DEVICES = await api('/api/devices');
    renderDeviceList();
  } catch (e) {
    if (e.authRequired) { handleAuthExpired(e.message); return; }
    $('#device-list').innerHTML = `<li class="loading" style="color:var(--red)">${escapeHtml(e.message)}</li>`;
  }
}

function renderDeviceList() {
  const q = $('#device-search').value.trim().toLowerCase();
  const list = DEVICES.filter((d) =>
    !q || (d.name || '').toLowerCase().includes(q) || (d.model || '').toLowerCase().includes(q));
  const ul = $('#device-list');
  if (!list.length) {
    ul.innerHTML = '<li class="loading">未找到设备</li>';
    return;
  }
  ul.innerHTML = list.map((d) => {
    const icon = d.icon
      ? `<img src="${escapeHtml(d.icon)}" referrerpolicy="no-referrer" />`
      : '🔌';
    return `<li class="device-item ${d.did === CURRENT_DID ? 'active' : ''}" data-did="${escapeHtml(d.did)}">
      <span class="dev-icon">${icon}</span>
      <span class="device-meta">
        <div class="device-name">${escapeHtml(d.name)}</div>
        <div class="device-sub">${escapeHtml(d.home_name || '')} · ${escapeHtml(d.model)}</div>
      </span>
      <span class="dev-online ${d.online ? 'on' : 'off'}" title="${d.online ? '在线' : '离线'}"></span>
    </li>`;
  }).join('');
  $$('#device-list .device-item').forEach((li) => {
    li.addEventListener('click', () => selectDevice(li.dataset.did));
  });
}

// ===========================================================================
// Device detail + SPEC control
// ===========================================================================
async function selectDevice(did) {
  CURRENT_DID = did;
  renderDeviceList();
  const dev = DEVICES.find((d) => d.did === did);
  const detail = $('#device-detail');
  detail.innerHTML = `
    <div class="detail-head">
      <div class="big-icon">${dev && dev.icon ? `<img src="${escapeHtml(dev.icon)}" referrerpolicy="no-referrer"/>` : '🔌'}</div>
      <div>
        <div class="detail-title">${escapeHtml(dev ? dev.name : did)}</div>
        <div class="detail-sub">${escapeHtml(dev ? dev.model : '')} · ${dev && dev.online ? '在线' : '离线'} · did ${escapeHtml(did)}</div>
      </div>
      <div class="detail-actions">
        <button class="btn small" id="read-all-btn">读取全部状态</button>
      </div>
    </div>
    <div class="loading"><span class="spinner"></span> 加载 SPEC 功能定义…</div>`;

  let spec;
  try {
    spec = await api('/api/spec?did=' + encodeURIComponent(did));
  } catch (e) {
    if (e.authRequired) { handleAuthExpired(e.message); return; }
    detail.querySelector('.loading').outerHTML =
      `<div class="loading" style="color:var(--red)">加载 SPEC 失败: ${escapeHtml(e.message)}</div>`;
    return;
  }
  renderSpec(detail, did, spec);
  // Auto-read current values for readable props.
  readAllProps(did, spec);
  $('#read-all-btn').addEventListener('click', () => readAllProps(did, spec));
}

function renderSpec(detail, did, spec) {
  const cards = spec.services.map((svc) => {
    const props = svc.properties.map((p) => renderProp(did, p)).join('');
    const actions = svc.actions.map((a) => renderAction(did, a)).join('');
    if (!props && !actions) return '';
    return `<div class="service-card">
      <div class="service-title">${escapeHtml(svc.name)} <span class="siid">service ${svc.siid}</span></div>
      ${props}${actions}
    </div>`;
  }).join('');
  detail.querySelector('.loading').outerHTML =
    cards || '<div class="placeholder"><p>该设备没有可显示的功能。</p></div>';
}

function propBadges(p) {
  const b = [];
  if (p.readable) b.push('<span class="badge">读</span>');
  if (p.writable) b.push('<span class="badge w">写</span>');
  if (p.notify) b.push('<span class="badge n">通知</span>');
  if (p.unit) b.push(`<span class="badge">${escapeHtml(p.unit)}</span>`);
  return `<div class="prop-badges">${b.join('')}</div>`;
}

function renderProp(did, p) {
  const key = `p_${p.siid}_${p.piid}`;
  let control = '';
  if (p.writable) {
    if (p.format === 'bool') {
      control = `<label class="switch"><input type="checkbox" id="${key}" /><span class="slider-sw"></span></label>`;
    } else if (p.value_list && p.value_list.length) {
      const opts = p.value_list.map((v) =>
        `<option value="${escapeHtml(JSON.stringify(v.value))}">${escapeHtml(v.description)}</option>`).join('');
      control = `<select id="${key}">${opts}</select>
        <button class="btn small primary" data-set="${key}">设置</button>`;
    } else if (p.value_range && /int/.test(p.format)) {
      const vr = p.value_range;
      control = `<input type="range" id="${key}" min="${vr.min}" max="${vr.max}" step="${vr.step || 1}" />
        <button class="btn small primary" data-set="${key}">设置</button>`;
    } else {
      control = `<input class="text-input" id="${key}" placeholder="输入值" />
        <button class="btn small primary" data-set="${key}">设置</button>`;
    }
  } else if (p.readable) {
    control = `<button class="btn small" data-get="${key}">读取</button>`;
  } else {
    control = `<span class="prop-value">—</span>`;
  }

  setTimeout(() => bindProp(did, p, key), 0);

  return `<div class="prop-row" data-key="${key}">
    <div class="prop-info">
      <div class="prop-name">${escapeHtml(p.name)}</div>
      <div class="prop-iid">${escapeHtml(p.iid)} · ${escapeHtml(p.format)}</div>
      ${propBadges(p)}
    </div>
    <div class="prop-control">
      <span class="prop-value" id="val_${key}"></span>
      ${control}
    </div>
  </div>`;
}

function bindProp(did, p, key) {
  const el = document.getElementById(key);
  const setBtn = document.querySelector(`[data-set="${key}"]`);
  const getBtn = document.querySelector(`[data-get="${key}"]`);

  if (p.format === 'bool' && el) {
    el.addEventListener('change', () => setProp(did, p, el.checked));
  }
  if (setBtn && el) {
    setBtn.addEventListener('click', () => {
      let value = el.value;
      if (p.value_list && p.value_list.length) {
        value = JSON.parse(el.value);
      } else if (/int/.test(p.format)) {
        value = parseInt(el.value, 10);
      } else if (p.format === 'float') {
        value = parseFloat(el.value);
      }
      setProp(did, p, value);
    });
  }
  if (getBtn) getBtn.addEventListener('click', () => readProp(did, p));
  // live label for range slider
  if (el && el.type === 'range') {
    el.addEventListener('input', () => { document.getElementById('val_' + key).textContent = el.value; });
  }
}

function applyPropValue(p, key, value) {
  const valEl = document.getElementById('val_' + key);
  if (valEl) valEl.textContent = value === null || value === undefined ? '—' : String(value);
  const el = document.getElementById(key);
  if (!el) return;
  if (p.format === 'bool') el.checked = !!value;
  else if (el.tagName === 'SELECT') {
    const target = JSON.stringify(value);
    Array.from(el.options).forEach((o) => { if (o.value === target) el.value = target; });
  } else if (el.type === 'range') el.value = value;
  else if (el.classList.contains('text-input')) el.value = value;
}

async function readProp(did, p) {
  try {
    const r = await apiPost('/api/prop/get', { did, siid: p.siid, piid: p.piid });
    applyPropValue(p, `p_${p.siid}_${p.piid}`, r.value);
  } catch (e) {
    if (e.authRequired) { handleAuthExpired(e.message); return; }
    toast('读取失败: ' + e.message, 'err');
  }
}

async function readAllProps(did, spec) {
  const props = [];
  spec.services.forEach((svc) => svc.properties.forEach((p) => { if (p.readable) props.push(p); }));
  if (!props.length) return;
  try {
    const r = await apiPost('/api/props/get', { did, props: props.map((p) => [p.siid, p.piid]) });
    const map = {};
    (r.results || []).forEach((it) => {
      if (it && it.siid != null && it.piid != null && 'value' in it) map[`${it.siid}.${it.piid}`] = it.value;
    });
    props.forEach((p) => {
      if (`${p.siid}.${p.piid}` in map) applyPropValue(p, `p_${p.siid}_${p.piid}`, map[`${p.siid}.${p.piid}`]);
    });
  } catch (e) {
    if (e.authRequired) { handleAuthExpired(e.message); return; }
    toast('批量读取失败: ' + e.message, 'err');
  }
}

async function setProp(did, p, value) {
  try {
    await apiPost('/api/prop/set', { did, siid: p.siid, piid: p.piid, value });
    toast(`已设置「${p.name}」`, 'ok');
    setTimeout(() => readProp(did, p), 400);
  } catch (e) {
    if (e.authRequired) { handleAuthExpired(e.message); return; }
    toast('设置失败: ' + e.message, 'err');
    readProp(did, p);
  }
}

function renderAction(did, a) {
  const key = `a_${a.siid}_${a.aiid}`;
  const inputs = a.in.map((p, i) =>
    `<input class="text-input" id="${key}_${i}" placeholder="${escapeHtml(p.name)} (${escapeHtml(p.format)})" style="max-width:140px" />`
  ).join(' ');
  setTimeout(() => {
    const btn = document.querySelector(`[data-action="${key}"]`);
    if (btn) btn.addEventListener('click', () => runAction(did, a, key));
  }, 0);
  return `<div class="action-row">
    <div class="prop-info">
      <div class="prop-name">${escapeHtml(a.name)}</div>
      <div class="prop-iid">${escapeHtml(a.iid)}</div>
    </div>
    <div class="prop-control">
      ${inputs}
      <button class="btn small primary" data-action="${key}">执行</button>
    </div>
  </div>`;
}

async function runAction(did, a, key) {
  const inList = a.in.map((p, i) => {
    const raw = document.getElementById(`${key}_${i}`).value;
    if (/int/.test(p.format)) return parseInt(raw, 10);
    if (p.format === 'float') return parseFloat(raw);
    if (p.format === 'bool') return raw === 'true' || raw === '1';
    return raw;
  });
  try {
    await apiPost('/api/action', { did, siid: a.siid, aiid: a.aiid, in: inList });
    toast(`已执行「${a.name}」`, 'ok');
  } catch (e) {
    if (e.authRequired) { handleAuthExpired(e.message); return; }
    toast('执行失败: ' + e.message, 'err');
  }
}

// ===========================================================================
// Cameras
// ===========================================================================
const camStreams = {}; // did -> { ws, url }

async function loadCameras() {
  const banner = $('#camera-banner');
  const grid = $('#camera-grid');
  if (!HEALTH.camera_native_available) {
    banner.classList.remove('hidden');
    banner.innerHTML = '⚠️ 当前平台未检测到摄像头 P2P 原生库 <code>miot_camera_lite</code>，' +
      '因此<strong>无法解码实时视频画面</strong>。摄像头设备依然会列出，并可在「设备控制」中通过 SPEC 进行控制。' +
      '若获得对应平台的原生库，将其放入 miot_kit/miot/libs/&lt;平台&gt; 后即可启用实时画面。';
  } else {
    banner.classList.add('hidden');
  }
  grid.innerHTML = '<div class="loading"><span class="spinner"></span> 加载摄像头…</div>';
  let cams;
  try {
    cams = await api('/api/cameras');
  } catch (e) {
    if (e.authRequired) { handleAuthExpired(e.message); return; }
    grid.innerHTML = `<div class="loading" style="color:var(--red)">${escapeHtml(e.message)}</div>`;
    return;
  }
  if (!cams.length) {
    grid.innerHTML = '<div class="placeholder" style="grid-column:1/-1"><div class="placeholder-icon">📷</div><p>当前账号下未发现米家摄像头设备。</p></div>';
    return;
  }
  grid.innerHTML = cams.map((c) => {
    const can = HEALTH.camera_native_available;
    return `<div class="camera-card" data-did="${escapeHtml(c.did)}">
      <div class="camera-video" id="vid_${escapeHtml(c.did)}">
        <span class="cam-overlay">${can ? '点击「播放」开始实时画面' : '实时画面不可用（缺少原生库）'}</span>
      </div>
      <div class="camera-foot">
        <span class="cam-name">${escapeHtml(c.name)}</span>
        <button class="btn small primary" data-play="${escapeHtml(c.did)}" ${can ? '' : 'disabled'}>播放</button>
        <button class="btn small" data-stop="${escapeHtml(c.did)}" disabled>停止</button>
      </div>
    </div>`;
  }).join('');
  grid.querySelectorAll('[data-play]').forEach((b) =>
    b.addEventListener('click', () => startCamera(b.dataset.play)));
  grid.querySelectorAll('[data-stop]').forEach((b) =>
    b.addEventListener('click', () => stopCamera(b.dataset.stop)));
}

function startCamera(did) {
  stopCamera(did);
  const wsUrl = BASE.replace('http', 'ws') + '/ws/camera?did=' + encodeURIComponent(did);
  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  const vid = $('#vid_' + did);
  vid.innerHTML = '<span class="cam-overlay"><span class="spinner"></span> 连接摄像头…</span>';
  $(`[data-play="${did}"]`).disabled = true;
  $(`[data-stop="${did}"]`).disabled = false;

  let imgEl = null;
  let lastUrl = null;
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'error') { vid.innerHTML = `<span class="cam-overlay">${escapeHtml(msg.message)}</span>`; toast(msg.message, 'err'); }
      else if (msg.type === 'started') vid.innerHTML = '<span class="cam-overlay"><span class="spinner"></span> 等待画面…</span>';
      return;
    }
    // binary JPEG frame
    if (!imgEl) { vid.innerHTML = ''; imgEl = document.createElement('img'); vid.appendChild(imgEl); }
    const blob = new Blob([ev.data], { type: 'image/jpeg' });
    const url = URL.createObjectURL(blob);
    imgEl.src = url;
    if (lastUrl) URL.revokeObjectURL(lastUrl);
    lastUrl = url;
  };
  ws.onclose = () => {
    $(`[data-play="${did}"]`).disabled = !HEALTH.camera_native_available;
    $(`[data-stop="${did}"]`).disabled = true;
  };
  ws.onerror = () => toast('摄像头连接出错', 'err');
  camStreams[did] = { ws };
}

function stopCamera(did) {
  const s = camStreams[did];
  if (s && s.ws) { try { s.ws.close(); } catch (e) {} }
  delete camStreams[did];
  const vid = $('#vid_' + did);
  if (vid) vid.innerHTML = '<span class="cam-overlay">已停止</span>';
}

window.addEventListener('beforeunload', () => {
  Object.keys(camStreams).forEach(stopCamera);
});

// ===========================================================================
// XiaoAI (小爱音箱) ASR/TTS bridge
//
// A *different* credential than the OAuth2 login above (see
// backend/xiaomi_asr_bridge.py): obtained via an in-app browser login window
// (main.js `xiaomi-passport-login`), the speaker is picked from the user's
// already-known miot devices, and new spoken queries can be forwarded to a
// webhook. The "如何使用 SDK" panel shows how to point the standalone
// `xiaomi-speaker-sdk` package at the same token.
// ===========================================================================
let XIAOMI = { status: null, tokenInfo: null, candidates: [], bridge: null, selectedDid: '' };

async function loadXiaomi() {
  const wrap = $('#xiaomi-wrap');
  wrap.innerHTML = '<div class="loading"><span class="spinner"></span> 加载中…</div>';
  try {
    const [status, tokenInfo, bridge] = await Promise.all([
      api('/api/xiaomi/status'),
      api('/api/xiaomi/token'),
      api('/api/xiaomi/bridge/status'),
    ]);
    XIAOMI.status = status;
    XIAOMI.tokenInfo = tokenInfo;
    XIAOMI.bridge = bridge;
    XIAOMI.selectedDid = '';
    if (status.has_token) {
      try { XIAOMI.candidates = await api('/api/xiaomi/speaker-candidates'); }
      catch (e) { XIAOMI.candidates = []; XIAOMI.candidatesError = e.message; }
    } else {
      XIAOMI.candidates = [];
    }
  } catch (e) {
    wrap.innerHTML = `<div class="loading" style="color:var(--red)">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  renderXiaomi();
}

function renderXiaomi() {
  const { status, tokenInfo, candidates, bridge } = XIAOMI;
  const wrap = $('#xiaomi-wrap');
  const device = status.device;

  const tokenCard = `
    <div class="xm-card">
      <div class="xm-card-head">
        <span>1. 获取 Token</span>
        <span class="dev-online ${tokenInfo.has_token ? 'on' : 'off'}" title="${tokenInfo.has_token ? '已获取' : '未获取'}"></span>
      </div>
      <p class="login-msg">
        小爱音箱的语音识别 (ASR) / 语音播报 (TTS) 使用的是小米账号 <strong>passport 登录凭据</strong>，
        与上方「设备控制」使用的米家开放平台 OAuth2 登录是<strong>两套不同的凭据</strong>，需要单独获取一次。
        点击下方按钮会弹出一个登录窗口，正常登录小米账号（可按提示完成短信/设备安全验证）即可，
        无需手动复制任何链接。
      </p>
      <div class="login-actions">
        <button class="btn primary" id="xm-extract-btn">提取 Token（登录小米账号）</button>
      </div>
      ${tokenInfo.has_token ? `
        <div class="login-hint" style="margin-top:14px">
          <div>Token 文件：<code>${escapeHtml(tokenInfo.token_path)}</code></div>
          <div>userId：<code>${escapeHtml(tokenInfo.user_id)}</code></div>
          <div>passToken：<code>${escapeHtml(tokenInfo.pass_token_masked)}</code>（出于安全考虑仅展示末位字符；
            完整值请直接查看本地 Token 文件，不通过界面显示）</div>
        </div>` : ''}
    </div>`;

  const candList = candidates.length ? candidates.map((d) => `
      <label class="xm-radio-row">
        <input type="radio" name="xm-speaker" value="${escapeHtml(d.did)}" data-name="${escapeHtml(d.name)}"
          ${device && device.did === d.did ? 'checked' : ''} />
        <span class="dev-online ${d.online ? 'on' : 'off'}"></span>
        <span>${escapeHtml(d.name)}</span>
        <span class="device-sub">${escapeHtml(d.home_name || '')} · ${escapeHtml(d.model || '')}</span>
      </label>`).join('')
    : `<div class="hint">${tokenInfo.has_token
        ? (XIAOMI.candidatesError ? escapeHtml(XIAOMI.candidatesError) : '在你的米家设备中未发现小爱音箱类设备。')
        : '请先完成上方 Token 提取。'}</div>`;

  const speakerCard = `
    <div class="xm-card">
      <div class="xm-card-head">
        <span>2. 选择音箱</span>
        ${device ? `<span class="device-sub">已选择：${escapeHtml(device.name)}</span>` : ''}
      </div>
      <p class="login-msg">从你米家账号下已有的音箱设备中选择一个用于 ASR / TTS（会自动与小爱账号下的设备匹配）。</p>
      <div class="xm-radio-list">${candList}</div>
      <div class="login-actions">
        <button class="btn primary" id="xm-select-btn" ${tokenInfo.has_token ? '' : 'disabled'}>确认选择</button>
      </div>
    </div>`;

  const webhookCard = `
    <div class="xm-card">
      <div class="xm-card-head">
        <span>3. Webhook 推送</span>
        <span class="dev-online ${bridge.running ? 'on' : 'off'}" title="${bridge.running ? '运行中' : '未运行'}"></span>
      </div>
      <p class="login-msg">收到新的小爱语音识别结果时，实时 POST 一份 JSON 到下面的 Webhook 地址。</p>
      <div class="xm-form-row">
        <label>Webhook URL</label>
        <input class="text-input" id="xm-webhook-url" style="max-width:360px"
          placeholder="https://your-server.com/webhook" value="${escapeHtml(bridge.webhook_url || '')}" />
      </div>
      <div class="xm-form-row">
        <label>唤醒词（可选）</label>
        <input class="text-input" id="xm-wake-word" style="max-width:200px"
          placeholder="留空=转发所有语音" value="${escapeHtml(bridge.wake_word || '')}" />
      </div>
      <div class="login-actions">
        <button class="btn" id="xm-save-config-btn">保存配置</button>
        <button class="btn ${bridge.running ? '' : 'primary'}" id="xm-toggle-bridge-btn">${bridge.running ? '停止推送' : '启动推送'}</button>
      </div>
      <div class="login-hint" style="margin-top:14px">
        <div>状态：${bridge.running ? '<span style="color:var(--green)">运行中</span>' : '未运行'}</div>
        <div>已推送次数：${bridge.forwarded_count || 0}</div>
        <div>最近一次：${bridge.last_forwarded_at ? new Date(bridge.last_forwarded_at).toLocaleString() : '—'}</div>
        ${bridge.error ? `<div style="color:var(--red)">最近错误：${escapeHtml(bridge.error)}</div>` : ''}
      </div>
      <div class="login-hint" style="margin-top:10px">
        <div class="login-label" style="margin:0 0 6px">推送的 JSON 示例：</div>
        <pre class="xm-code">{
  "request_id": "1700000000000",
  "question": "今天天气怎么样",
  "raw_question": "今天天气怎么样",
  "device_id": "...",
  "device_name": "客厅音箱",
  "timestamp": 1700000000000
}</pre>
      </div>
    </div>`;

  const speakCard = `
    <div class="xm-card">
      <div class="xm-card-head"><span>接管播报测试</span></div>
      <p class="login-msg">用来验证「直接接管音箱播报」能力：会暂停当前播放并朗读下面的文字。</p>
      <div class="xm-form-row">
        <input class="text-input" id="xm-speak-text" style="max-width:360px" placeholder="要播报的文字" />
        <button class="btn primary" id="xm-speak-btn" ${device ? '' : 'disabled'}>播报</button>
      </div>
    </div>`;

  const guideCard = `
    <div class="xm-card">
      <div class="xm-card-head"><span>如何用 SDK 开发自己的小爱音箱机器人</span></div>
      <p class="login-msg">
        上面提取到的 Token 与独立的 <code>xiaomi-speaker-sdk</code>（ssr-agent 仓库中的
        <code>xiaomi_speaker_sdk/</code> 包）使用同一套凭据格式，可以直接拿去开发自己的机器人：
        监听新的语音识别事件，或者随时接管音箱进行语音播报。
      </p>
      <ol class="login-steps">
        <li>安装 SDK：<code>pip install xiaomi-speaker-sdk</code>（或从 ssr-agent 仓库的
          <code>xiaomi_speaker_sdk/</code> 目录 <code>pip install .</code>）。</li>
        <li>Token 文件本身不经过界面展示（出于安全考虑），直接在本机把它复制过去即可——
          从 <code>${escapeHtml(tokenInfo.token_path)}</code> 复制到
          <code>~/.xiaomi_speaker_sdk/token.json</code>（两者是同一种 JSON 格式，
          SDK 会直接识别其中的 passToken/userId）。也可以打开该文件，把
          <code>passToken</code>/<code>userId</code> 两个字段的值粘到
          <code>xiaomi_speaker_sdk.token_store.import_pass_token()</code> 里。</li>
        <li>然后就可以监听 ASR / 接管播报了：</li>
      </ol>
      <pre class="xm-code">import asyncio
from xiaomi_speaker_sdk import XiaomiSpeaker, XiaomiSpeakerConfig

async def main():
    speaker = XiaomiSpeaker(XiaomiSpeakerConfig(speaker_name="${escapeHtml((device && device.name) || '客厅')}"))
    await speaker.connect()
    await speaker.speak("机器人已接管，请说话。")   # 接管播报
    async for request_id, question in speaker.listen_asr():   # 监听新的 ASR
        print("heard:", question)
        await speaker.speak(f"你说了：{question}")

asyncio.run(main())</pre>
      <p class="login-msg">
        完整示例见 ssr-agent 仓库 <code>xiaomi_speaker_sdk/examples/echo_bot.py</code> 和
        <code>xiaomi_speaker_sdk/examples/webhook_forwarder.py</code>（后者与本页「Webhook 推送」
        功能等价，可在没有本应用的情况下独立运行）。
      </p>
    </div>`;

  wrap.innerHTML = `<div class="xm-grid">${tokenCard}${speakerCard}${webhookCard}${speakCard}${guideCard}</div>`;
  bindXiaomiEvents();
}

function bindXiaomiEvents() {
  const extractBtn = $('#xm-extract-btn');
  if (extractBtn) extractBtn.addEventListener('click', xiaomiExtractToken);

  $$('input[name="xm-speaker"]').forEach((r) => {
    r.addEventListener('change', () => { XIAOMI.selectedDid = r.value; });
  });
  const selectBtn = $('#xm-select-btn');
  if (selectBtn) selectBtn.addEventListener('click', xiaomiSelectSpeaker);

  const saveBtn = $('#xm-save-config-btn');
  if (saveBtn) saveBtn.addEventListener('click', xiaomiSaveConfig);
  const toggleBtn = $('#xm-toggle-bridge-btn');
  if (toggleBtn) toggleBtn.addEventListener('click', xiaomiToggleBridge);

  const speakBtn = $('#xm-speak-btn');
  if (speakBtn) speakBtn.addEventListener('click', xiaomiSpeak);
}

async function xiaomiExtractToken() {
  const btn = $('#xm-extract-btn');
  btn.disabled = true;
  btn.textContent = '等待登录…（请在弹出的窗口中登录）';
  try {
    const cred = await window.miot.xiaomiPassportLogin();
    if (!cred) {
      toast('登录窗口已关闭，未获取到 Token', 'err');
      return;
    }
    const r = await apiPost('/api/xiaomi/token', { passToken: cred.passToken, userId: cred.userId });
    if (r.ok) {
      toast('Token 提取成功', 'ok');
    } else {
      toast('Token 已保存，但登录校验失败：' + (r.error || ''), 'err');
    }
    await loadXiaomi();
  } catch (e) {
    toast('提取 Token 失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '提取 Token（登录小米账号）';
  }
}

async function xiaomiSelectSpeaker() {
  const checked = document.querySelector('input[name="xm-speaker"]:checked');
  if (!checked) { toast('请先选择一个音箱', 'err'); return; }
  try {
    await apiPost('/api/xiaomi/select', { did: checked.value, name: checked.dataset.name });
    toast('已选择音箱', 'ok');
    await loadXiaomi();
  } catch (e) {
    toast('选择失败：' + e.message, 'err');
  }
}

async function xiaomiSaveConfig() {
  const webhookUrl = $('#xm-webhook-url').value.trim();
  const wakeWord = $('#xm-wake-word').value.trim();
  try {
    await apiPost('/api/xiaomi/config', { webhook_url: webhookUrl, wake_word: wakeWord });
    toast('配置已保存', 'ok');
    XIAOMI.bridge = await api('/api/xiaomi/bridge/status');
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
  }
}

async function xiaomiToggleBridge() {
  const btn = $('#xm-toggle-bridge-btn');
  btn.disabled = true;
  try {
    if (XIAOMI.bridge.running) {
      await apiPost('/api/xiaomi/bridge/stop', {});
      toast('已停止推送', 'ok');
    } else {
      await xiaomiSaveConfig();
      await apiPost('/api/xiaomi/bridge/start', {});
      toast('已启动推送', 'ok');
    }
    await loadXiaomi();
  } catch (e) {
    toast('操作失败：' + e.message, 'err');
    btn.disabled = false;
  }
}

async function xiaomiSpeak() {
  const text = $('#xm-speak-text').value.trim();
  if (!text) { toast('请输入要播报的文字', 'err'); return; }
  const btn = $('#xm-speak-btn');
  btn.disabled = true;
  try {
    await apiPost('/api/xiaomi/speak', { text });
    toast('已下发播报', 'ok');
  } catch (e) {
    toast('播报失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

boot();

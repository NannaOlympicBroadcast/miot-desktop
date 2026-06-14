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

async function api(path, opts) {
  const res = await fetch(BASE + path, opts);
  const data = await res.json().catch(() => ({ error: 'invalid json response' }));
  if (data && data.error) throw new Error(data.error);
  return data;
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
  });
});

$('#refresh-btn').addEventListener('click', () => {
  const active = $('.tab.active').dataset.tab;
  if (active === 'devices') {
    loadDevices();
    if (CURRENT_DID) selectDevice(CURRENT_DID);
  } else {
    loadCameras();
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
      <ol class="login-steps">
        <li>点击「获取登录链接」，浏览器会打开小米授权页面。</li>
        <li>完成登录后，浏览器会跳转到一个无法访问的 <code>127.0.0.1</code> 地址 —— 这是正常的。</li>
        <li>复制浏览器地址栏中<strong>完整的跳转后 URL</strong>（含 <code>code=</code> 与 <code>state=</code>），粘贴到下方并「完成登录」。</li>
      </ol>
      <div class="login-actions">
        <button class="btn primary" id="login-start">获取登录链接</button>
      </div>
      <div id="login-url-box" class="login-url-box hidden">
        <div class="login-label">如未自动打开，请手动复制此链接：</div>
        <textarea id="login-url" class="text-input login-url" rows="2" readonly></textarea>
        <div class="login-label" style="margin-top:10px">粘贴跳转后的完整 URL：</div>
        <textarea id="login-redirect" class="text-input login-url" rows="2" placeholder="http://127.0.0.1/?code=...&state=..."></textarea>
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
  btn.textContent = '获取中…';
  try {
    const r = await apiPost('/api/auth/start', {});
    $('#login-url').value = r.url;
    $('#login-url-box').classList.remove('hidden');
    try { await window.miot.openExternal(r.url); } catch (e) { /* user can copy manually */ }
    $('#login-complete').addEventListener('click', completeLogin, { once: false });
    toast('已在浏览器打开授权页面', 'ok');
  } catch (e) {
    toast('获取登录链接失败: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '重新获取登录链接';
  }
}

async function completeLogin() {
  const url = ($('#login-redirect').value || '').trim();
  if (!url) { toast('请粘贴跳转后的完整 URL', 'err'); return; }
  const btn = $('#login-complete');
  btn.disabled = true;
  btn.textContent = '登录中…';
  try {
    await apiPost('/api/auth/complete', { url });
    toast('登录成功', 'ok');
    try { window.miot.broadcast('logged-in'); } catch (e) {}
    await refreshAuthAndRender();
  } catch (e) {
    toast('登录失败: ' + e.message, 'err');
    btn.disabled = false;
    btn.textContent = '完成登录';
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
    toast('批量读取失败: ' + e.message, 'err');
  }
}

async function setProp(did, p, value) {
  try {
    await apiPost('/api/prop/set', { did, siid: p.siid, piid: p.piid, value });
    toast(`已设置「${p.name}」`, 'ok');
    setTimeout(() => readProp(did, p), 400);
  } catch (e) {
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

boot();

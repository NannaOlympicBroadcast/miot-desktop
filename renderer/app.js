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
    if (tab.dataset.tab === 'miloco') loadMiloco();
    if (tab.dataset.tab === 'agent') loadAgent();
    if (tab.dataset.tab === 'xiaomi') loadXiaomi();
  });
});

$('#refresh-btn').addEventListener('click', () => {
  const active = $('.tab.active').dataset.tab;
  if (active === 'devices') {
    loadDevices();
    if (CURRENT_DID) selectDevice(CURRENT_DID);
  } else if (active === 'miloco') {
    loadMiloco();
  } else if (active === 'agent') {
    loadAgent();
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

// ===========================================================================
// Miloco (Mi Home perceptive gateway) — Docker controller + web preview
// ===========================================================================
let MILOCO = { status: null, buildPoll: null, statusPoll: null };

async function loadMiloco() {
  const wrap = $('#miloco-wrap');
  try {
    MILOCO.status = await api('/api/miloco/status');
  } catch (e) {
    wrap.innerHTML = `<div class="loading" style="color:var(--red)">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  renderMiloco();
  // If it's running + healthy, keep the preview fresh; otherwise poll state.
  clearInterval(MILOCO.statusPoll);
  MILOCO.statusPoll = setInterval(refreshMilocoStatus, 4000);
}

async function refreshMilocoStatus() {
  // Only poll while the Miloco tab is active.
  if ($('.tab.active').dataset.tab !== 'miloco') { clearInterval(MILOCO.statusPoll); return; }
  try {
    const next = await api('/api/miloco/status');
    const changed = JSON.stringify(next) !== JSON.stringify(MILOCO.status);
    MILOCO.status = next;
    if (changed) renderMiloco();
  } catch (e) { /* ignore transient */ }
}

function renderMiloco() {
  const s = MILOCO.status;
  const wrap = $('#miloco-wrap');

  if (!s.docker_available) {
    wrap.innerHTML = `
      <div class="miloco-panel">
        <div class="banner">未检测到可用的 Docker。Miloco 需要通过 Docker 以 <code>--network host</code> 方式运行。
          请安装并启动 Docker Desktop / Docker Engine 后重试。</div>
        <p class="login-msg">Miloco 是小米官方的「感知家庭」网关
          (<code>github.com/XiaoMi/xiaomi-miloco</code>)，运行后本应用会自动把当前登录的米家凭据注入给
          Miloco，无需重新绑定账号。</p>
      </div>`;
    return;
  }

  const stateLabel = s.running
    ? (s.healthy ? '<span style="color:var(--green)">运行中 · 健康</span>' : '<span style="color:var(--primary)">运行中 · 启动中…</span>')
    : (s.container_state ? `已停止（${escapeHtml(s.container_state)}）` : '未运行');

  const controls = `
    <div class="miloco-toolbar">
      <div class="miloco-state">
        <span class="dev-online ${s.running && s.healthy ? 'on' : 'off'}"></span>
        <span>Miloco 状态：${stateLabel}</span>
      </div>
      <div class="miloco-actions">
        ${!s.image_exists
          ? `<button class="btn primary" id="ml-build">构建镜像</button>`
          : ''}
        ${s.image_exists && !s.running
          ? `<button class="btn primary" id="ml-start">启动 Miloco</button>` : ''}
        ${s.running
          ? `<button class="btn" id="ml-stop">停止</button>
             <button class="btn" id="ml-open">在浏览器打开</button>` : ''}
        <button class="btn" id="ml-logs">查看日志</button>
      </div>
    </div>`;

  const credNote = s.has_credentials
    ? `<div class="hint">✓ 已检测到本机米家登录凭据，启动时会自动注入给 Miloco（无需在 Miloco 里重新绑定账号）。</div>`
    : `<div class="hint" style="color:var(--red)">⚠ 未检测到米家登录凭据。请先在「设备控制」标签完成小米账号登录，再启动 Miloco。</div>`;

  let body;
  if (s.running && s.healthy && s.has_server_token) {
    body = `<div class="miloco-preview">
      <webview id="ml-webview" src="${escapeHtml(s.preview_url)}" style="width:100%;height:100%"></webview>
    </div>`;
  } else if (s.running) {
    body = `<div class="miloco-preview placeholder">
      <div class="placeholder-icon">🏠</div>
      <p>Miloco 正在启动…（等待 <code>${escapeHtml(s.url)}/health</code> 就绪）</p>
    </div>`;
  } else if (!s.image_exists) {
    body = `<div class="miloco-preview placeholder">
      <div class="placeholder-icon">📦</div>
      <p>尚未构建 Miloco 镜像。点击「构建镜像」从官方源构建（首次较慢）。</p>
      <pre class="xm-code" id="ml-build-log" style="display:none;max-height:280px;overflow:auto;text-align:left;width:100%"></pre>
    </div>`;
  } else {
    body = `<div class="miloco-preview placeholder">
      <div class="placeholder-icon">🏠</div>
      <p>镜像已就绪。点击「启动 Miloco」以 <code>--network host</code> 方式运行，并自动注入米家凭据。</p>
    </div>`;
  }

  wrap.innerHTML = `<div class="miloco-panel">${controls}${credNote}${body}</div>`;
  bindMilocoEvents();
  // If a build is in progress, resume streaming its log.
  if (MILOCO.buildPoll) pollMilocoBuild();
}

function bindMilocoEvents() {
  const b = (id, fn) => { const el = $('#' + id); if (el) el.addEventListener('click', fn); };
  b('ml-build', milocoBuild);
  b('ml-start', milocoStart);
  b('ml-stop', milocoStop);
  b('ml-logs', milocoShowLogs);
  b('ml-open', () => window.miot.openExternal(MILOCO.status.preview_url));
}

async function milocoBuild() {
  try {
    await apiPost('/api/miloco/build', {});
    toast('开始构建镜像…', 'ok');
    const logBox = $('#ml-build-log');
    if (logBox) logBox.style.display = 'block';
    pollMilocoBuild();
  } catch (e) { toast('构建失败：' + e.message, 'err'); }
}

async function pollMilocoBuild() {
  clearInterval(MILOCO.buildPoll);
  MILOCO.buildPoll = setInterval(async () => {
    let st;
    try { st = await api('/api/miloco/build/status'); } catch (e) { return; }
    const logBox = $('#ml-build-log');
    if (logBox) { logBox.textContent = (st.log_tail || []).join('\n'); logBox.scrollTop = logBox.scrollHeight; }
    if (!st.building) {
      clearInterval(MILOCO.buildPoll);
      MILOCO.buildPoll = null;
      if (st.error) toast('镜像构建失败：' + st.error, 'err');
      else toast('镜像构建完成', 'ok');
      loadMiloco();
    }
  }, 1500);
}

async function milocoStart() {
  const btn = $('#ml-start');
  if (btn) { btn.disabled = true; btn.textContent = '启动中…'; }
  try {
    const st = await apiPost('/api/miloco/start', {});
    MILOCO.status = st;
    toast(st.seeded_credentials ? '已启动并注入米家凭据' : '已启动 Miloco', 'ok');
    renderMiloco();
  } catch (e) {
    toast('启动失败：' + e.message, 'err');
    if (btn) { btn.disabled = false; btn.textContent = '启动 Miloco'; }
  }
}

async function milocoStop() {
  try {
    MILOCO.status = await apiPost('/api/miloco/stop', {});
    toast('已停止 Miloco', 'ok');
    renderMiloco();
  } catch (e) { toast('停止失败：' + e.message, 'err'); }
}

async function milocoShowLogs() {
  try {
    const r = await api('/api/miloco/logs?tail=200');
    const wrap = $('#miloco-wrap');
    const box = document.createElement('pre');
    box.className = 'xm-code';
    box.style.cssText = 'max-height:280px;overflow:auto;margin-top:12px';
    box.textContent = r.logs || '（暂无日志）';
    wrap.querySelector('.miloco-panel').appendChild(box);
  } catch (e) { toast('读取日志失败：' + e.message, 'err'); }
}

// ===========================================================================
// SSR agent chat (bundled agent driving Miloco + general assistant)
// ===========================================================================
let AGENT = { status: null, ws: null, pending: [], history: [], busy: false };

async function loadAgent() {
  const wrap = $('#agent-wrap');
  try {
    AGENT.status = await api('/api/agent/status');
  } catch (e) {
    wrap.innerHTML = `<div class="loading" style="color:var(--red)">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  if (!AGENT.status.available) {
    wrap.innerHTML = `
      <div class="agent-panel">
        <div class="banner">${escapeHtml(AGENT.status.message || 'SSR 助手不可用。')}</div>
        <p class="login-msg">SSR 是被内置进本应用 Python 运行时的智能体，用于以自然语言驱动 Miloco 与米家设备。
          安装后可在此聊天、配置模型、粘贴图片、上传文件，Miloco 的家庭事件也会进入 SSR 的事件总线供处理。</p>
      </div>`;
    return;
  }
  renderAgent();
}

function renderAgent() {
  const s = AGENT.status;
  const wrap = $('#agent-wrap');
  wrap.innerHTML = `
    <div class="agent-panel">
      <div class="agent-head">
        <div class="agent-model-info">
          <span class="dev-online ${s.has_api_key ? 'on' : 'off'}"></span>
          <span>模型：<strong>${escapeHtml(s.primary_model_name || '')}</strong>
            <span class="device-sub">(${escapeHtml(s.primary_provider || '')} · ${escapeHtml(s.primary_model || '')})</span></span>
        </div>
        <button class="btn small" id="ag-config-btn">模型配置</button>
      </div>
      ${s.missing && s.missing.length
        ? `<div class="banner">缺少 API Key：${escapeHtml(s.missing.join(', '))}。点击「模型配置」填入后即可对话。</div>` : ''}
      <div class="agent-config hidden" id="ag-config">
        <div class="xm-form-row"><label>模型 ID</label>
          <input class="text-input" id="ag-id" placeholder="如 my-gemini" value="${escapeHtml(s.primary_model || '')}" /></div>
        <div class="xm-form-row"><label>Provider</label>
          <select id="ag-provider">
            <option value="gemini">gemini</option>
            <option value="anthropic">anthropic</option>
            <option value="openai">openai</option>
          </select></div>
        <div class="xm-form-row"><label>模型名</label>
          <input class="text-input" id="ag-model" placeholder="如 gemini-3.1-flash-lite" value="${escapeHtml(s.primary_model_name || '')}" /></div>
        <div class="xm-form-row"><label>API Key</label>
          <input class="text-input" id="ag-key" type="password" placeholder="留空则不修改" /></div>
        <div class="xm-form-row"><label>Base URL</label>
          <input class="text-input" id="ag-baseurl" placeholder="可选（自建/代理端点）" /></div>
        <div class="login-actions"><button class="btn primary" id="ag-save">保存并应用</button></div>
      </div>
      <div class="chat-log" id="ag-log"></div>
      <div class="chat-input">
        <div class="chat-attachments" id="ag-attach"></div>
        <div class="chat-row">
          <textarea id="ag-text" rows="2" placeholder="给 SSR 助手发消息（Enter 发送，Shift+Enter 换行）…"></textarea>
          <div class="chat-buttons">
            <button class="btn small" id="ag-file-btn" title="上传文件">📎</button>
            <button class="btn primary" id="ag-send">发送</button>
          </div>
        </div>
        <input type="file" id="ag-file-input" multiple style="display:none" />
      </div>
    </div>`;
  bindAgentEvents();
  renderAgentHistory();
}

function bindAgentEvents() {
  $('#ag-config-btn').addEventListener('click', () => $('#ag-config').classList.toggle('hidden'));
  $('#ag-save').addEventListener('click', agentSaveModel);
  $('#ag-send').addEventListener('click', agentSend);
  $('#ag-file-btn').addEventListener('click', () => $('#ag-file-input').click());
  $('#ag-file-input').addEventListener('change', agentPickFiles);
  const ta = $('#ag-text');
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); agentSend(); }
  });
  // Paste image support.
  ta.addEventListener('paste', (e) => {
    for (const item of (e.clipboardData || {}).items || []) {
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) { agentAddAttachment(file, 'image'); e.preventDefault(); }
      }
    }
  });
  if (s_provider()) s_provider().value = (AGENT.status.primary_provider || 'gemini');
}
function s_provider() { return document.getElementById('ag-provider'); }

async function agentSaveModel() {
  const body = {
    id: $('#ag-id').value.trim(),
    provider: $('#ag-provider').value,
    model: $('#ag-model').value.trim(),
    api_key: $('#ag-key').value,
    base_url: $('#ag-baseurl').value.trim() || null,
    set_primary: true,
  };
  if (!body.id) { toast('请填写模型 ID', 'err'); return; }
  try {
    await apiPost('/api/agent/models', body);
    toast('模型配置已保存', 'ok');
    AGENT.status = await api('/api/agent/status');
    renderAgent();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
}

function agentPickFiles(e) {
  for (const file of e.target.files) {
    agentAddAttachment(file, file.type.startsWith('image/') ? 'image' : 'file');
  }
  e.target.value = '';
}

function agentAddAttachment(file, kind) {
  const reader = new FileReader();
  reader.onload = () => {
    const b64 = String(reader.result).split(',')[1] || '';
    AGENT.pending.push({ kind, mime: file.type, name: file.name || 'clipboard.png', data_b64: b64 });
    renderAttachments();
  };
  reader.readAsDataURL(file);
}

function renderAttachments() {
  const box = $('#ag-attach');
  if (!box) return;
  box.innerHTML = AGENT.pending.map((a, i) =>
    `<span class="chip">${a.kind === 'image' ? '🖼' : '📄'} ${escapeHtml(a.name)}
      <span class="chip-x" data-i="${i}">✕</span></span>`).join('');
  box.querySelectorAll('.chip-x').forEach((x) =>
    x.addEventListener('click', () => { AGENT.pending.splice(parseInt(x.dataset.i, 10), 1); renderAttachments(); }));
}

function renderAgentHistory() {
  const log = $('#ag-log');
  if (!log) return;
  log.innerHTML = AGENT.history.map((m) => {
    if (m.role === 'user') return `<div class="msg user"><div class="bubble">${escapeHtml(m.text)}</div></div>`;
    if (m.role === 'assistant') return `<div class="msg assistant"><div class="bubble">${escapeHtml(m.text)}</div></div>`;
    if (m.role === 'event') return `<div class="msg event">${escapeHtml(m.text)}</div>`;
    return '';
  }).join('');
  log.scrollTop = log.scrollHeight;
}

function ensureAgentWs() {
  if (AGENT.ws && AGENT.ws.readyState === WebSocket.OPEN) return Promise.resolve(AGENT.ws);
  return new Promise((resolve, reject) => {
    const wsUrl = BASE.replace('http', 'ws') + '/ws/agent';
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => { AGENT.ws = ws; resolve(ws); };
    ws.onerror = () => reject(new Error('WebSocket 连接失败'));
    ws.onmessage = (ev) => handleAgentEvent(JSON.parse(ev.data));
    ws.onclose = () => { AGENT.ws = null; };
  });
}

function handleAgentEvent(evt) {
  const t = evt.type;
  if (t === 'thinking' && evt.text) pushEventLine('💭 ' + evt.text);
  else if (t === 'tool_call') pushEventLine('🔧 调用工具 ' + (evt.name || evt.tool || ''));
  else if (t === 'tool_result') pushEventLine('✓ 工具返回');
  else if (t === 'reply' || t === 'done') {
    const text = evt.text || evt.reply || '';
    if (text) AGENT.history.push({ role: 'assistant', text });
    if (t === 'done') { AGENT.busy = false; setAgentSending(false); }
    renderAgentHistory();
  } else if (t === 'error') {
    AGENT.history.push({ role: 'event', text: '出错：' + (evt.message || '') });
    AGENT.busy = false; setAgentSending(false);
    renderAgentHistory();
  }
}

function pushEventLine(text) {
  // Coalesce consecutive event lines to keep the log tidy.
  AGENT.history.push({ role: 'event', text });
  renderAgentHistory();
}

function setAgentSending(sending) {
  const btn = $('#ag-send');
  if (btn) { btn.disabled = sending; btn.textContent = sending ? '思考中…' : '发送'; }
}

async function agentSend() {
  if (AGENT.busy) return;
  const text = ($('#ag-text').value || '').trim();
  if (!text && !AGENT.pending.length) { toast('请输入消息', 'err'); return; }
  let ws;
  try { ws = await ensureAgentWs(); }
  catch (e) { toast(e.message, 'err'); return; }

  const attachments = AGENT.pending.slice();
  AGENT.history.push({ role: 'user', text: text + (attachments.length ? `  [${attachments.length} 个附件]` : '') });
  AGENT.pending = [];
  renderAttachments();
  renderAgentHistory();
  $('#ag-text').value = '';
  AGENT.busy = true;
  setAgentSending(true);
  ws.send(JSON.stringify({ type: 'chat', text, attachments }));
}

boot();

'use strict';

let BASE = '';
let DID = null;
let CTRL = { volume: null, play: null, pause: null, execText: null };

const $ = (s) => document.querySelector(s);

function toast(msg, kind) {
  const t = $('#q-toast');
  t.textContent = msg;
  t.className = 'toast ' + (kind || '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), 2400);
}

async function api(path, opts) {
  const res = await fetch(BASE + path, opts);
  const d = await res.json().catch(() => ({ error: 'bad json' }));
  if (d && d.error) throw new Error(d.error);
  return d;
}
const post = (p, b) => api(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });

$('#q-open').addEventListener('click', () => window.miot.showMain());
$('#q-close').addEventListener('click', () => window.miot.hideWindow());

// --- locate control instances from the device SPEC (robust to model diffs) ---
function findControls(spec) {
  const c = { volume: null, play: null, pause: null, execText: null };
  for (const svc of spec.services) {
    for (const p of svc.properties) {
      if (!c.volume && p.writable && p.value_range &&
          (p.unit === 'percentage' || /音量|volume/i.test(p.name))) {
        c.volume = p;
      }
    }
    for (const a of svc.actions) {
      if (!c.play && a.in.length === 0 && /^播放$|play$/i.test(a.name)) c.play = a;
      if (!c.pause && /暂停|pause/i.test(a.name)) c.pause = a;
      if (!c.execText && a.in.length >= 2 && /执行文本|文本指令|execute.*text/i.test(a.name)) c.execText = a;
    }
  }
  // fallback: single-input text action
  if (!c.execText) {
    for (const svc of spec.services)
      for (const a of svc.actions)
        if (!c.execText && a.in.length >= 1 && /执行文本|文本指令/.test(a.name)) c.execText = a;
  }
  return c;
}

function pickSpeaker(devices) {
  return devices.find((d) => /wifispeaker|speaker/i.test(d.model)) || devices[0] || null;
}

async function boot() {
  BASE = await window.miot.getBackendUrl();
  // react to login completion broadcast
  window.miot.on('logged-in', () => boot());

  let health;
  try { health = await api('/api/health'); } catch (e) { health = {}; }
  if (!health.ready) {
    $('#q-body').innerHTML = '<div class="loading">尚未登录，请在主界面完成登录。</div>';
    $('#q-open').click;
    return;
  }
  let devices;
  try { devices = await api('/api/devices'); }
  catch (e) { $('#q-body').innerHTML = `<div class="loading">加载失败：${e.message}</div>`; return; }

  const sp = pickSpeaker(devices);
  if (!sp) { $('#q-body').innerHTML = '<div class="loading">未找到可控设备。</div>'; return; }
  DID = sp.did;
  $('#q-title').textContent = sp.name;
  $('#q-dot').className = 'dot ' + (sp.online ? 'on' : '');

  let spec;
  try { spec = await api('/api/spec?did=' + encodeURIComponent(DID)); }
  catch (e) { $('#q-body').innerHTML = `<div class="loading">加载 SPEC 失败：${e.message}</div>`; return; }
  CTRL = findControls(spec);
  render();
  refreshVolume();
}

function render() {
  const parts = [];
  if (CTRL.volume) {
    const vr = CTRL.volume.value_range;
    parts.push(`<div>
      <div class="block-label">音量</div>
      <div class="vol-row">
        <input type="range" id="vol" min="${vr.min}" max="${vr.max}" step="${vr.step || 1}" />
        <span class="vol-val" id="vol-val">--</span>
      </div>
    </div>`);
  }
  if (CTRL.play || CTRL.pause) {
    parts.push(`<div>
      <div class="block-label">播放控制</div>
      <div class="btn-row">
        <button class="btn" id="btn-play" ${CTRL.play ? '' : 'disabled'}>▶ 播放</button>
        <button class="btn" id="btn-pause" ${CTRL.pause ? '' : 'disabled'}>⏸ 暂停</button>
      </div>
    </div>`);
  }
  parts.push(`<div>
    <div class="block-label">自然语言指令（非静默执行）</div>
    <textarea id="cmd" rows="2" placeholder="例如：今天天气怎么样 / 播放周杰伦的歌" ${CTRL.execText ? '' : 'disabled'}></textarea>
    <div class="btn-row" style="margin-top:8px">
      <button class="btn primary" id="btn-exec" ${CTRL.execText ? '' : 'disabled'}>执行指令</button>
    </div>
    <div class="hint">指令将由音箱出声执行（关闭静默模式）。</div>
  </div>`);
  $('#q-body').innerHTML = parts.join('');

  const vol = $('#vol');
  if (vol) {
    vol.addEventListener('input', () => { $('#vol-val').textContent = vol.value; });
    vol.addEventListener('change', () => setVolume(parseInt(vol.value, 10)));
  }
  if ($('#btn-play')) $('#btn-play').addEventListener('click', () => runAction(CTRL.play, '播放'));
  if ($('#btn-pause')) $('#btn-pause').addEventListener('click', () => runAction(CTRL.pause, '暂停'));
  if ($('#btn-exec')) $('#btn-exec').addEventListener('click', execCommand);
  const cmd = $('#cmd');
  if (cmd) cmd.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || !e.shiftKey)) { e.preventDefault(); execCommand(); }
  });
}

async function refreshVolume() {
  if (!CTRL.volume) return;
  try {
    const r = await post('/api/prop/get', { did: DID, siid: CTRL.volume.siid, piid: CTRL.volume.piid });
    if ($('#vol')) { $('#vol').value = r.value; $('#vol-val').textContent = r.value; }
  } catch (e) { /* ignore */ }
}

async function setVolume(v) {
  try {
    await post('/api/prop/set', { did: DID, siid: CTRL.volume.siid, piid: CTRL.volume.piid, value: v });
    toast('音量已设为 ' + v, 'ok');
  } catch (e) { toast('设置失败: ' + e.message, 'err'); refreshVolume(); }
}

async function runAction(a, label) {
  if (!a) return;
  try {
    await post('/api/action', { did: DID, siid: a.siid, aiid: a.aiid, in: [] });
    toast(label + ' 已执行', 'ok');
  } catch (e) { toast('执行失败: ' + e.message, 'err'); }
}

async function execCommand() {
  const text = ($('#cmd').value || '').trim();
  if (!text) { toast('请输入指令', 'err'); return; }
  const a = CTRL.execText;
  // Build inputs in declared order: text(string), then silent flag if present.
  // 指令静默执行: value 1 = 关闭静默 = 非静默(出声执行)。
  const inList = a.in.map((p) => {
    if (/string/.test(p.format)) return text;
    if (/静默|silent/i.test(p.name) || /int/.test(p.format)) return 1; // non-silent
    return text;
  });
  try {
    $('#btn-exec').disabled = true;
    await post('/api/action', { did: DID, siid: a.siid, aiid: a.aiid, in: inList });
    toast('指令已下发', 'ok');
    $('#cmd').value = '';
  } catch (e) { toast('执行失败: ' + e.message, 'err'); }
  finally { $('#btn-exec').disabled = false; }
}

boot();

'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const pad = (n) => String(n).padStart(2, '0');
const hm = (ms) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const dayKey = (ms) => new Date(ms).toDateString();
const dayLabel = (ms) => {
  const d = new Date(ms), t = new Date();
  if (d.toDateString() === t.toDateString()) return 'Hôm nay';
  const y = new Date(t); y.setDate(t.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return 'Hôm qua';
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}`;
};
const when = (ms) => (dayKey(ms) === dayKey(Date.now()) ? hm(ms) : `${hm(ms)} ${dayLabel(ms)}`);
const dur = (s) => { s = Math.round(s / 60); return s < 60 ? `${s} phút` : `${Math.floor(s / 60)} giờ ${s % 60 ? (s % 60) + ' ph' : ''}`; };

async function api(path, body) {
  const r = await fetch(path, body === undefined ? { credentials: 'same-origin' } : {
    method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) { showLogin(); throw new Error('401'); }
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

// ---------- login ----------
function showLogin() {
  for (const id of ['hdr', 'devBar', 'nav', 'composer', 'recBar', 'tab-chat', 'tab-tasks', 'tab-pc', 'tab-set']) $(id).classList.add('hide');
  $('login').classList.remove('hide');
  stopPoll();
}
// A link from another parent's phone: #i=<invite code>&k=<family key> (the fragment never reaches the relay).
async function fromLink() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get('k')) { try { await KT.set(h.get('k')); } catch (e) { $('loginErr').textContent = e.message; } }
  if (h.get('i')) $('regCode').value = h.get('i');
  if (location.hash) history.replaceState(null, '', location.pathname);
}
async function boot() {
  await KT.load().catch(() => {});
  await fromLink();
  const s = await api('/auth/status');
  if (!s.loggedIn) {
    showLogin();
    if (!s.hasPasskeys) { $('btnLogin').classList.add('hide'); $('loginHint').textContent = 'Chưa có passkey nào. Nhập mã thiết lập để tạo passkey đầu tiên.'; }
    return;
  }
  $('login').classList.add('hide');
  $('hdr').classList.remove('hide');
  $('nav').classList.remove('hide');
  showTab(localStorage.getItem('tab') || 'chat');
  startPoll();
}
$('btnLogin').onclick = async () => {
  $('loginErr').textContent = '';
  try {
    const { options, cid } = await api('/auth/login/options', {});
    const response = await SimpleWebAuthnBrowser.startAuthentication({ optionsJSON: options });
    await api('/auth/login/verify', { cid, response });
    boot();
  } catch (e) { $('loginErr').textContent = 'Không đăng nhập được: ' + e.message; }
};
$('btnReg').onclick = async () => {
  $('loginErr').textContent = '';
  try {
    const who = document.querySelector('input[name=who]:checked')?.value || 'bo';
    const { options, cid } = await api('/auth/register/options', { code: $('regCode').value, who });
    const response = await SimpleWebAuthnBrowser.startRegistration({ optionsJSON: options });
    await api('/auth/register/verify', { cid, response, name: $('regName').value || navigator.platform });
    boot();
  } catch (e) { $('loginErr').textContent = 'Không tạo được passkey: ' + e.message; }
};

// ---------- tabs ----------
let tab = 'chat';
function showTab(t) {
  tab = t;
  try { localStorage.setItem('tab', t); } catch {}
  for (const n of ['chat', 'tasks', 'pc', 'set']) $('tab-' + n).classList.toggle('hide', n !== t);
  $('composer').classList.toggle('hide', t !== 'chat' || !!rec);
  $('recBar').classList.toggle('hide', t !== 'chat' || !rec);
  for (const b of document.querySelectorAll('nav button')) b.classList.toggle('act', b.dataset.tab === t);
  if (t === 'chat') scrollEnd();
  if (t === 'set') { loadSettings(); renderE2e().catch(() => {}); }
  render();
}
for (const b of document.querySelectorAll('nav button')) b.onclick = () => showTab(b.dataset.tab);

// ---------- state + polling ----------
let S = { messages: [], tasks: [], devices: [], calls: [], me: 'bo', now: Date.now() };
const PARENT = { bo: 'Bố', me: 'Mẹ' };
// DEV: which child's computer the app is on ('all' = every one). PCDEV: the one shown in "Máy con" when DEV is 'all'.
let DEV = (() => { try { return localStorage.getItem('dev') || 'all'; } catch { return 'all'; } })();
let PCDEV = null;
let lastId = 0, timer = null, busy = false, pollGen = 0;
const devObj = (id) => S.devices.find((d) => d.id === id);
const devName = (id) => devObj(id)?.name || 'con';
const pcDev = () => (DEV === 'all' ? (devObj(PCDEV) ? PCDEV : S.devices[0]?.id) : DEV);
function setDev(id) {
  DEV = id;
  try { localStorage.setItem('dev', id); } catch {}
  lastId = 0; S.messages = []; S.tasks = [];
  $('msgs').dataset.html = '';
  pollGen++; busy = false;
  renderDevBar(); poll();
}
function renderDevBar() {
  const many = S.devices.length > 1;
  $('devBar').classList.toggle('hide', !many);
  if (!many) { if (DEV !== 'all' && !devObj(DEV) && S.devices.length) DEV = 'all'; return; }
  const chips = [...S.devices.map((d) => [d.id, d.name, online(d)]), ['all', 'Tất cả', null]];
  const html = chips.map(([id, name, on]) => `<button data-dev="${esc(id)}" class="${id === DEV ? 'act' : ''}">${on === null ? '' : `<span class="dot ${on ? 'on' : ''}"></span>`}${esc(name)}</button>`).join('');
  if ($('devBar').dataset.html !== html) { $('devBar').innerHTML = html; $('devBar').dataset.html = html; }
  $('txt').placeholder = DEV === 'all' ? 'Nhắn cho tất cả các con…' : `Nhắn cho ${devName(DEV)}…`;
}
document.addEventListener('click', (e) => { const p = e.target.closest('[data-pcdev]'); if (p) { PCDEV = p.dataset.pcdev; renderPc(); } });
document.addEventListener('click', (e) => { const b = e.target.closest('[data-dev]'); if (b) setDev(b.dataset.dev); });
async function poll() {
  if (busy) return;
  busy = true;
  const gen = pollGen;
  try {
    const j = await api(`/api/state?dev=${encodeURIComponent(DEV)}` + (lastId ? `&since=${lastId}` : ''));
    await openState(j);
    if (gen !== pollGen) return;
    const fresh = j.messages.length > 0;
    if (lastId) S.messages.push(...j.messages); else S.messages = j.messages;
    if (S.messages.length) lastId = S.messages[S.messages.length - 1].id;
    const rc = new Map(j.receipts.map((r) => [r.id, r]));
    for (const m of S.messages) { const r = rc.get(m.id); if (r) { m.delivered_at = r.delivered_at; m.read_at = r.read_at; } }
    Object.assign(S, { tasks: j.tasks, devices: j.devices || [], calls: j.calls || [], me: j.me || 'bo', now: j.now, keyFp: j.keyFp || null });
    renderDevBar();
    render(fresh);
    watchCall();
  } catch (e) { if (e.message !== '401') console.warn(e); }
  if (gen === pollGen) busy = false;
}
// ---------- end-to-end encryption (crypto.js): decrypt what the relay hands us ----------
const PLAIN = new Map();   // ciphertext → plain text
async function op(s) {
  if (!KT.isEnc(s)) return s;
  if (PLAIN.has(s)) return PLAIN.get(s);
  const p = await KT.decText(s);
  if (p !== null) PLAIN.set(s, p);
  return p;
}
const UNREAD = '🔒 Không đọc được: điện thoại này chưa có khóa gia đình (Cài đặt → Mã hóa)';
async function openState(j) {
  for (const m of j.messages) {
    m.sealed = KT.isEnc(m.body);
    const p = await op(m.body);
    m.unread = p === null;
    m.body = p ?? UNREAD;
  }
  for (const t of j.tasks) t.title = (await op(t.title)) ?? '🔒 (không đọc được)';
  for (const d of j.devices || []) {
    if (d.status?.e) {
      const p = await op(d.status.e);
      try { Object.assign(d.status, p ? JSON.parse(p) : {}); } catch {}
      delete d.status.e;
    }
    if (d.lock?.msg) d.lock.msg = (await op(d.lock.msg)) ?? '';
  }
}
// Once the family has a key, a phone without it must not send plaintext (the relay refuses it anyway).
function needKey() {
  if (!S.keyFp || KT.has()) return false;
  alertBox('Điện thoại này chưa có khóa gia đình');
  showTab('set');
  return true;
}
// SDP carries the call's DTLS fingerprints: encrypted + authenticated, the relay cannot sit in the middle.
async function openSdp(s) {
  if (KT.isEnc(s)) {
    const p = await KT.decText(s);
    if (p === null) throw new Error('Không giải mã được cuộc gọi (khác khóa gia đình?)');
    return p;
  }
  if (KT.has()) throw new Error('Cuộc gọi không được mã hoá: đã từ chối để tránh nghe lén');
  return s;
}
const VURL = new Map();    // voice key → blob URL of the decrypted WAV
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-voice]');
  if (!b) return;
  const k = b.dataset.voice;
  b.disabled = true; b.textContent = '…';
  try {
    const r = await fetch('/api/voice/' + encodeURIComponent(k), { credentials: 'same-origin' });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
    const plain = await KT.decBlob(await r.arrayBuffer());
    if (!plain) throw new Error('không đọc được (thiếu khóa gia đình)');
    const url = URL.createObjectURL(new Blob([plain], { type: 'audio/wav' }));
    VURL.set(k, url);
    const a = document.createElement('audio');
    a.controls = true; a.src = url;
    b.replaceWith(a);
    a.play().catch(() => {});
  } catch (err) { b.disabled = false; b.textContent = '⚠ ' + err.message; }
});
const SHOTURL = new Map(); // screenshot id → blob URL of the decrypted JPEG
async function shotUrl(id) {
  if (SHOTURL.has(id)) return SHOTURL.get(id);
  const r = await fetch('/api/shot/' + id, { credentials: 'same-origin' });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
  const plain = await KT.decBlob(await r.arrayBuffer());
  if (!plain) throw new Error('không đọc được (thiếu khóa gia đình)');
  const u = URL.createObjectURL(new Blob([plain], { type: 'image/jpeg' }));
  SHOTURL.set(id, u);
  return u;
}

function startPoll() { stopPoll(); poll(); timer = setInterval(poll, 4000); }
function stopPoll() { if (timer) clearInterval(timer); timer = null; }
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && timer) poll(); });

function online(d) { return !!(d && d.lastSeen && S.now - d.lastSeen < 45000); }
function presence(d) {
  const st = d?.status;
  if (!online(d)) return d?.lastSeen ? `Máy tắt · ${when(d.lastSeen)}` : 'Máy chưa kết nối';
  if (st?.session === 'active') return st.idle_s > 120 ? `${d.name} rời máy ${dur(st.idle_s)}` : `${d.name} đang dùng máy`;
  return st?.session === 'locked' ? 'Máy đang khoá màn hình' : 'Máy bật, chưa đăng nhập';
}
function render(fresh) {
  if (DEV === 'all' && S.devices.length > 1) $('pres').textContent = '';
  else { const d = devObj(DEV) || S.devices[0]; $('pres').innerHTML = `<span class="dot ${online(d) ? 'on' : ''}"></span>${esc(presence(d))}`; }
  if (tab === 'chat') renderChat(fresh);
  if (tab === 'tasks') renderTasks();
  if (tab === 'pc') renderPc();
}

// ---------- chat ----------
function scrollEnd() { const m = $('tab-chat'); m.scrollTop = m.scrollHeight; }
function renderChat(fresh) {
  const m = $('tab-chat');
  const atEnd = m.scrollHeight - m.scrollTop - m.clientHeight < 80;
  let html = '', lastDay = '';
  for (const x of S.messages) {
    const dk = dayKey(x.created_at);
    if (dk !== lastDay) { html += `<div class="day">${dayLabel(x.created_at)}</div>`; lastDay = dk; }
    const parent = x.sender in PARENT;
    let tick = '';
    if (parent) tick = x.read_at ? ' · ✓✓ con đã xem' : x.delivered_at ? ' · ✓ đã tới máy' : ' · đang chờ';
    const spoke = parent && x.speak ? (x.repeat_play ? ' 🔁' : ' 🔊') : '';
    const voice = x.audio && (VURL.has(x.audio)
      ? `<audio controls src="${VURL.get(x.audio)}"></audio>`
      : `<button class="vplay" data-voice="${esc(x.audio)}">▶ Nghe</button>`);
    const body = x.audio ? voice + (x.body ? `<div class="tr">“${esc(x.body)}”</div>` : '') : esc(x.body);
    const open = S.keyFp && x.sender !== 'he' && !x.sealed && !x.audio ? ' · 🔓 không mã hoá' : '';
    const len = x.audio && x.audio_ms ? ` · 🎤 ${Math.round(x.audio_ms / 1000)}s` : x.audio ? ' · 🎤' : '';
    const who = (DEV === 'all' && S.devices.length > 1 ? `<span class="who">${parent ? '→ ' : ''}${esc(devName(x.device))}</span> · ` : '')
      + (parent && x.sender !== S.me ? `<span class="who">${PARENT[x.sender]}</span> · ` : '');
    html += `<div class="m ${parent ? 'bo' : x.sender}">${body}<small>${who}${hm(x.created_at)}${len}${spoke}${tick}${open}</small></div>`;
  }
  if (!S.messages.length) html = '<div class="mute">Chưa có tin nhắn. Tin bố gửi sẽ hiện trên máy con và được đọc to.</div>';
  const box = $('msgs');
  const playing = [...box.querySelectorAll('audio')].some((a) => !a.paused);
  if (html !== box.dataset.html && !playing) { box.innerHTML = html; box.dataset.html = html; }
  if (atEnd || fresh) scrollEnd();
}
async function send() {
  const body = $('txt').value.trim();
  if (!body || needKey()) return;
  $('btnSend').disabled = true;
  try {
    await api('/api/message', { body: await KT.encText(body), speak: $('speak').checked, repeat: $('repeat').checked, devices: DEV });
    $('txt').value = ''; $('repeat').checked = false; await poll(); scrollEnd();
  }
  catch (e) { alertBox(e.message); }
  $('btnSend').disabled = false;
}
$('btnSend').onclick = send;
$('txt').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !('ontouchstart' in window)) { e.preventDefault(); send(); } });
// ---------- voice message: record on the phone, convert to 22 kHz mono WAV, upload ----------
let rec = null;
function wavBytes(samples, rate) {
  const b = new ArrayBuffer(44 + samples.length * 2), v = new DataView(b);
  const str = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data');
  v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, samples[i])) * 0x7fff, true);
  return b;
}
async function toWav(blob) {
  const RATE = 22050;
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  ctx.close();
  const off = new OfflineAudioContext(1, Math.ceil(decoded.duration * RATE), RATE);
  const src = off.createBufferSource();
  src.buffer = decoded; src.connect(off.destination); src.start();
  const out = (await off.startRendering()).getChannelData(0);
  let peak = 0;
  for (const x of out) peak = Math.max(peak, Math.abs(x));
  if (peak > 0.02 && peak < 0.7) { const g = 0.7 / peak; for (let i = 0; i < out.length; i++) out[i] *= g; } // even out quiet phones
  return { wav: wavBytes(out, RATE), ms: Math.round(decoded.duration * 1000) };
}
async function startRec() {
  if (rec) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return alertBox('Trình duyệt không ghi âm được');
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
  catch { return alertBox('Chưa cho phép dùng micro'); }
  const type = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find((t) => MediaRecorder.isTypeSupported?.(t));
  const mr = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const chunks = [];
  mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec = { mr, stream, chunks, t0: Date.now(), send: false };
  mr.onstop = () => finishRec(rec);
  mr.start(250);
  $('composer').classList.add('hide'); $('recBar').classList.remove('hide');
  rec.timer = setInterval(() => {
    const s = Math.floor((Date.now() - rec.t0) / 1000);
    $('recTime').textContent = `${Math.floor(s / 60)}:${pad(s % 60)}`;
    if (s >= 60) stopRec(true);
  }, 250);
}
function stopRec(send) {
  if (!rec || rec.mr.state === 'inactive') return;
  rec.send = send;
  clearInterval(rec.timer);
  rec.mr.stop();
}
async function finishRec(r) {
  rec = null;
  r.stream.getTracks().forEach((t) => t.stop());
  $('recBar').classList.add('hide'); $('composer').classList.remove('hide');
  if (!r.send || needKey()) return;
  $('btnMic').disabled = true; $('btnMic').textContent = '…';
  try {
    const { wav, ms } = await toWav(new Blob(r.chunks, { type: r.chunks[0]?.type || 'audio/webm' }));
    if (ms < 600) throw new Error('Ghi âm quá ngắn');
    const res = await fetch(`/api/voice?ms=${ms}&speak=${$('speak').checked ? 1 : 0}&repeat=${$('repeat').checked ? 1 : 0}&dev=${encodeURIComponent(DEV)}`, {
      method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'audio/wav' }, body: await KT.encBlob(wav),
    });
    if (res.status === 401) return showLogin();
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.status);
    $('repeat').checked = false; await poll(); scrollEnd();
  } catch (e) { alertBox('Không gửi được ghi âm: ' + e.message); }
  $('btnMic').disabled = false; $('btnMic').textContent = '🎤';
}
$('btnMic').onclick = startRec;
$('repeat').onchange = () => { if ($('repeat').checked) $('speak').checked = true; };
$('speak').onchange = () => { if (!$('speak').checked) $('repeat').checked = false; };
$('btnRecSend').onclick = () => stopRec(true);
$('btnRecCancel').onclick = () => stopRec(false);

function alertBox(msg) { $('pres').textContent = '⚠ ' + msg; }

// ---------- tasks ----------
function taskRow(t) {
  const over = !t.done_at && t.due_at && t.due_at < S.now;
  const meta = [t.created_by === 'con' ? 'con tự thêm' : t.created_by !== S.me ? `${PARENT[t.created_by] || ''} giao` : '', t.due_at ? `hạn ${when(t.due_at)}` : '', t.done_at ? `xong ${when(t.done_at)}` : '']
    .filter(Boolean).join(' · ');
  return `<div class="task ${t.done_at ? 'done' : ''}">
    <input type="checkbox" data-done="${t.id}" ${t.done_at ? 'checked' : ''}>
    <div class="t"><div class="title">${esc(t.title)}</div><div class="meta ${over ? 'over' : ''}">${esc(meta)}${over ? ' · quá hạn' : ''}</div></div>
    <button class="x" data-del="${t.id}" title="Xoá">✕</button></div>`;
}
function renderTasks() {
  const open = S.tasks.filter((t) => !t.done_at), done = S.tasks.filter((t) => t.done_at);
  $('taskList').innerHTML = open.length ? open.map(taskRow).join('') : '<div class="mute">Không còn việc nào 🎉</div>';
  $('doneBox').classList.toggle('hide', !done.length);
  $('doneList').innerHTML = done.map(taskRow).join('');
}
document.addEventListener('click', async (e) => {
  const d = e.target.closest('[data-done]'), x = e.target.closest('[data-del]');
  try {
    if (d) { await api('/api/task/' + d.dataset.done, { done: d.checked }); poll(); }
    if (x && confirmDel(x)) { await api('/api/task/' + x.dataset.del, { deleted: true }); poll(); }
  } catch (err) { alertBox(err.message); }
});
function confirmDel(btn) {
  if (btn.dataset.armed) return true;
  btn.dataset.armed = '1'; btn.textContent = 'Xoá?';
  setTimeout(() => { delete btn.dataset.armed; btn.textContent = '✕'; }, 3000);
  return false;
}
$('btnTask').onclick = async () => {
  const title = $('tTitle').value.trim();
  if (!title || needKey()) return;
  const v = $('tDue').value;
  try { await api('/api/task', { title: await KT.encText(title), due_at: v ? new Date(v).getTime() : null, devices: DEV }); $('tTitle').value = ''; $('tDue').value = ''; poll(); }
  catch (e) { alertBox(e.message); }
};

// ---------- the child's PC ----------
function renderPc() {
  renderLock();
  const D = devObj(pcDev()) || {};
  const many = DEV === 'all' && S.devices.length > 1;
  $('pcDevBar').classList.toggle('hide', !many);
  if (many) $('pcDevBar').innerHTML = S.devices.map((d) => `<button data-pcdev="${esc(d.id)}" class="${d.id === D.id ? 'act' : ''}"><span class="dot ${online(d) ? 'on' : ''}"></span>${esc(d.name)}</button>`).join('');
  const st = D.status;
  if (!st || !online(D)) {
    $('nowCard').innerHTML = `<div class="big">Máy của ${esc(D.name || 'con')} đang tắt hoặc mất mạng</div><div class="mute">${D.lastSeen ? 'Lần cuối thấy: ' + when(D.lastSeen) : ''}</div>`;
  } else {
    const f = st.focus || {};
    const idle = st.idle_s > 120 ? `<div class="mute">Không chạm máy ${dur(st.idle_s)}</div>` : '';
    const body = st.session === 'active'
      ? `<div class="mute">Đang mở</div><div class="big">${esc(f.app || '—')}</div><div>${esc(f.title || '')}</div>${idle}`
      : st.session === 'locked' ? '<div class="big">Màn hình đang khoá</div>' : '<div class="big">Chưa có ai đăng nhập</div>';
    $('nowCard').innerHTML = body + `<div class="mute" style="margin-top:6px">Âm lượng ${st.volume ?? '?'} · cập nhật ${hm(st.at)}</div>`;
  }
  const today = st?.today || [];
  const max = Math.max(1, ...today.map((a) => a.sec));
  $('today').innerHTML = today.length ? '<table>' + today.slice(0, 12).map((a) =>
    `<tr><td>${esc(a.app)}<div class="bar" style="width:${Math.max(4, (a.sec / max) * 100)}%"></div>${(a.titles || []).slice(0, 3).map((t) => `<div class="mute">· ${esc(t.title)} (${dur(t.sec)})</div>`).join('')}</td><td class="n">${dur(a.sec)}</td></tr>`).join('') + '</table>'
    : 'Chưa có dữ liệu hôm nay.';
  if (st?.screen_s !== undefined) $('today').insertAdjacentHTML('afterbegin', `<div style="margin-bottom:6px">Tổng thời gian dùng: <b>${dur(st.screen_s)}</b></div>`);
  const recent = st?.recent || [];
  $('recent').innerHTML = recent.length ? '<table>' + recent.slice(0, 15).map((r) =>
    `<tr><td class="n" style="text-align:left;padding:4px 8px 4px 0">${hm(r.at)}</td><td>${esc(r.app)} — <span class="mute">${esc(r.title)}</span></td></tr>`).join('') + '</table>' : '—';
  const s = (D.shots || [])[0];
  if (!s) $('shot').innerHTML = '';
  else if (!s.done_at) $('shot').innerHTML = `<div style="margin-top:8px">Đang chụp… (${hm(s.created_at)})</div>`;
  else if (s.result !== 'ok') $('shot').innerHTML = `<div style="margin-top:8px">Không chụp được: ${esc(s.result)}</div>`;
  else if (!$('shot').querySelector(`img[data-id="${s.id}"]`) && $('shot').dataset.loading !== String(s.id)) {
    $('shot').dataset.loading = String(s.id);
    $('shot').innerHTML = `<div style="margin-top:8px">Chụp lúc ${when(s.done_at)} · đang giải mã…</div>`;
    shotUrl(s.id)
      .then((u) => { $('shot').innerHTML = `<div style="margin-top:8px">Chụp lúc ${when(s.done_at)}</div><a href="${u}" target="_blank"><img data-id="${s.id}" src="${u}"></a>`; })
      .catch((e) => { $('shot').innerHTML = `<div style="margin-top:8px">Ảnh lúc ${when(s.done_at)}: ${esc(e.message)}</div>`; })
      .finally(() => { delete $('shot').dataset.loading; });
  }
}
function renderLock() {
  const D = devObj(pcDev()) || {};
  const L = D.lock, A = D.status?.lock;   // L: what we asked for; A: what the laptop reports
  const want = !!(L?.on && (!L.until || L.until > S.now));
  const by = L?.by ? PARENT[L.by] : '';
  let st;
  if (want) {
    st = `Đã khóa${by ? ' bởi ' + by : ''}${L.until ? ' đến ' + when(L.until) : ' (đến khi mở)'}`;
    st += online(D) ? (A?.on ? ' · máy đã khóa ✓' : ' · đang chờ máy…') : ' · máy đang tắt, sẽ khóa khi bật';
    if (L.msg) st += ` · “${L.msg}”`;
  } else {
    st = A?.on && online(D) ? 'Đang mở khóa…' : 'Máy đang mở.';
  }
  $('lockState').textContent = st;
  $('lockForm').classList.toggle('hide', want);
  $('unlockRow').classList.toggle('hide', !want);
}
$('btnLock').onclick = async () => {
  if (needKey()) return;
  try { await api('/api/lock', { on: true, minutes: Number($('lockMin').value), msg: await KT.encText($('lockMsg').value.trim()), devices: [pcDev()] }); $('lockMsg').value = ''; poll(); }
  catch (e) { alertBox(e.message); }
};
$('btnUnlock').onclick = async () => {
  try { await api('/api/lock', { on: false, devices: [pcDev()] }); poll(); } catch (e) { alertBox(e.message); }
};
$('btnShot').onclick = async () => {
  try { await api('/api/command', { kind: 'shot', device: pcDev() }); poll(); } catch (e) { alertBox(e.message); }
};

// ---------- settings ----------
async function loadSettings() {
  const perm = 'Notification' in window ? Notification.permission : 'không hỗ trợ';
  const sub = await navigator.serviceWorker?.ready.then((r) => r.pushManager.getSubscription()).catch(() => null);
  $('pushState').textContent = sub ? 'Đã bật trên máy này.' : `Chưa bật (quyền: ${perm}).`;
  try {
    const { passkeys } = await api('/api/passkeys');
    $('pkList').innerHTML = 'Passkey: ' + passkeys.map((p) => `${esc(p.name)} (${PARENT[p.who] || 'Bố'})`).join(', ');
  } catch {}
}
function keyBytes(b64) {
  const s = atob(b64.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((b64.length + 3) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}
$('btnPush').onclick = async () => {
  try {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) throw new Error('Trình duyệt không hỗ trợ (iPhone: mở từ màn hình chính)');
    if ((await Notification.requestPermission()) !== 'granted') throw new Error('Chưa cho phép thông báo');
    const { key } = await api('/api/push/key');
    if (!key) throw new Error('Máy chủ chưa có khoá VAPID');
    const reg = await navigator.serviceWorker.ready;
    const sub = (await reg.pushManager.getSubscription()) || (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) }));
    await api('/api/push/subscribe', { endpoint: sub.endpoint });
    loadSettings();
  } catch (e) { $('pushState').textContent = e.message; }
};
$('btnPushTest').onclick = () => api('/api/push/test', {}).catch((e) => ($('pushState').textContent = e.message));
$('btnInvite').onclick = async () => {
  try {
    const { code } = await api('/api/invite', {});
    const k = KT.exportKey();
    const link = `${location.origin}/#i=${code}` + (k ? `&k=${k}` : '');
    $('invite').innerHTML = `Mã mời: ${code}<div class="mute" style="font-weight:normal;word-break:break-all;margin-top:6px">Gửi link này cho mẹ qua kênh riêng (link có khóa gia đình): <a href="${esc(link)}">${esc(link)}</a></div>`;
    if (navigator.share) navigator.share({ title: 'Gọi điện - Nhắn tin con', url: link }).catch(() => {});
  } catch (e) { $('invite').textContent = e.message; }
};
// ---------- settings: encryption + pairing a child's computer ----------
async function renderE2e() {
  const fp = await KT.fp();
  const show = (fpx) => (fpx || '').replace(/(.{4})/g, '$1 ').trim();
  let st;
  if (KT.has() && fp === S.keyFp) st = `Đang bật ✓ Dấu khóa: ${show(fp)} (phải giống nhau trên mọi điện thoại)`;
  else if (KT.has() && !S.keyFp) st = 'Điện thoại có khóa nhưng gia đình chưa bật. Bấm "Bật mã hóa".';
  else if (KT.has()) st = `⚠ Khóa trên điện thoại này (${show(fp)}) KHÁC khóa của gia đình (${show(S.keyFp)}). Dán đúng khóa.`;
  else if (S.keyFp) st = 'Gia đình đã bật mã hóa, nhưng điện thoại này chưa có khóa: mở link mời hoặc dán khóa bên dưới.';
  else st = 'Chưa bật. Tin nhắn, ghi âm, ảnh màn hình đang lưu dạng đọc được trên relay.';
  $('e2eState').textContent = st;
  $('btnE2eOn').classList.toggle('hide', !!S.keyFp && KT.has() && fp === S.keyFp);
}
$('btnE2eOn').onclick = async () => {
  try {
    if (S.keyFp && !KT.has()) throw new Error('Gia đình đã có khóa: dán khóa đó, đừng tạo khóa mới');
    if (!KT.has()) await KT.create();
    await api('/api/key', { fp: await KT.fp() });
    await poll(); renderE2e();
    $('e2eKey').textContent = 'Nhớ cài khóa này lên máy của các con (Thêm máy của con) và cất một bản ở nơi an toàn: ' + KT.exportKey();
  } catch (e) { $('e2eState').textContent = e.message; }
};
$('btnE2eShow').onclick = () => {
  $('e2eKey').textContent = KT.has() ? `Khóa gia đình (giữ kín, ai có khóa này đọc được mọi tin): ${KT.exportKey()}` : 'Chưa có khóa.';
};
$('btnE2eSet').onclick = async () => {
  try { await KT.set($('e2eIn').value); $('e2eIn').value = ''; PLAIN.clear(); lastId = 0; await poll(); renderE2e(); }
  catch (e) { $('e2eState').textContent = e.message; }
};
const slug = (s) => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/đ/gi, 'd').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 20);
$('btnDevAdd').onclick = async () => {
  const name = $('devName').value.trim();
  if (!name) return;
  try {
    const id = slug(name) || 'may-' + Math.random().toString(36).slice(2, 6);
    const tok = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32)))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(tok)))].map((b) => b.toString(16).padStart(2, '0')).join('');
    await api('/api/device', { id, name, token_hash: hash });
    const k = KT.exportKey();
    $('devCmd').textContent = `Máy của ${name} (mã này chỉ hiện một lần, giữ kín):\n\n`
      + `Ubuntu:\nsudo ./install.sh ${location.origin} ${tok} <tài-khoản-của-con>${k ? ' ' + k : ''}\n\n`
      + `Windows (PowerShell quản trị):\n.\\install.ps1 -Url ${location.origin} -Token ${tok} -Kid <tài-khoản-của-con>${k ? ' -Key ' + k : ''}`;
    $('devName').value = '';
    poll();
  } catch (e) { $('devCmd').textContent = e.message; }
};
$('btnLogout').onclick = async () => { await api('/auth/logout', {}); location.reload(); };

// ---------- live call (WebRTC voice; the relay only passes the SDP) ----------
// C: the call on this phone. dir 'down' = we call (one child or all of them: one leg per computer),
// 'up' = a child calls us (one leg). All legs share one mic stream.
let C = null;
const dismissed = new Set();
const MIC = { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } };
function cv(title, sub, mode) {
  $('callView').classList.remove('hide');
  $('callView').classList.toggle('ringing', mode === 'ringing' || mode === 'calling');
  $('cvTitle').textContent = title;
  $('cvSub').textContent = sub || '';
  $('cvAnswer').classList.toggle('hide', mode !== 'ringing');
  $('cvMute').classList.toggle('hide', mode !== 'talking');
  $('cvEnd').classList.toggle('hide', mode === 'over');
  $('cvEnd').querySelector('span').textContent = mode === 'ringing' ? 'Từ chối' : mode === 'calling' ? 'Hủy' : 'Kết thúc';
}
function hideCv() { $('callView').classList.add('hide'); }
// Ringing tone made with WebAudio (no file); silent where the browser blocks it.
let ringer = null;
function ring(on, kind) {
  if (ringer) { clearInterval(ringer.t); ringer.ctx.close().catch(() => {}); ringer = null; }
  if (navigator.vibrate) navigator.vibrate(0);
  if (!on) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const beep = () => {
      const tones = kind === 'in' ? [[880, 0, .35], [660, .4, .35]] : [[440, 0, 1]];
      for (const [f, at, len] of tones) {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = f; g.gain.value = kind === 'in' ? .25 : .08;
        o.connect(g); g.connect(ctx.destination);
        o.start(ctx.currentTime + at); o.stop(ctx.currentTime + at + len);
      }
      if (kind === 'in' && navigator.vibrate) navigator.vibrate([400, 200, 400]);
    };
    beep();
    ringer = { ctx, t: setInterval(beep, kind === 'in' ? 2000 : 3000) };
  } catch {}
}
function gather(pc, ms = 4000) {
  return new Promise((res) => {
    if (pc.iceGatheringState === 'complete') return res();
    const t = setTimeout(res, ms);
    pc.addEventListener('icegatheringstatechange', () => { if (pc.iceGatheringState === 'complete') { clearTimeout(t); res(); } });
  });
}
let iceCache = null;
async function iceServers() {
  if (!iceCache || Date.now() - iceCache.at > 3600_000) iceCache = { at: Date.now(), list: (await api('/api/ice')).iceServers };
  return iceCache.list;
}
async function makeLeg(leg) {
  const pc = new RTCPeerConnection({ iceServers: await iceServers() });
  for (const t of C.stream.getTracks()) pc.addTrack(t, C.stream);
  const a = document.createElement('audio');
  a.autoplay = true; a.setAttribute('playsinline', ''); $('callView').appendChild(a);
  pc.ontrack = (e) => { a.srcObject = e.streams[0]; a.play().catch(() => {}); };
  pc.onconnectionstatechange = () => {
    if (!C || !C.legs.includes(leg)) return;
    if (pc.connectionState === 'connected') { leg.up = true; talking(); }
    if (pc.connectionState === 'failed') endLeg(leg, 'Không kết nối được');
  };
  Object.assign(leg, { pc, audio: a });
}
const clock = (ms) => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${pad(s % 60)}`; };
const legNames = (f) => C.legs.filter(f).map((l) => devName(l.dev)).join(', ');
function talking() {
  if (!C) return;
  ring(false);
  if (!C.t0) { C.t0 = Date.now(); C.clock = setInterval(talking, 1000); }
  const waiting = C.legs.filter((l) => !l.up && !l.over).map((l) => `${devName(l.dev)}: đang chờ`).join(' · ');
  cv(`Đang nói chuyện với ${legNames((l) => l.up && !l.over) || devName(C.legs[0]?.dev)}`, clock(Date.now() - C.t0) + (waiting ? ' · ' + waiting : ''), 'talking');
}
function closeLeg(l) {
  l.over = true;
  try { l.pc?.close(); } catch {}
  if (l.audio) { l.audio.srcObject = null; l.audio.remove(); }
  dismissed.add(l.id);
}
function cleanup() {
  if (!C) return;
  ring(false);
  clearInterval(C.poll); clearInterval(C.clock);
  C.stream?.getTracks().forEach((t) => t.stop());
  C.legs.forEach(closeLeg);
  C = null;
  $('cvMute').classList.remove('on');
}
function over(text) { cleanup(); cv(text, '', 'over'); setTimeout(() => { if (!C) hideCv(); }, 2500); poll(); }
// One computer's leg ended: the call goes on while another leg is still there.
function endLeg(l, text) {
  if (!C || l.over) return;
  closeLeg(l);
  if (C.legs.every((x) => x.over)) return over(C.legs.length > 1 ? 'Đã kết thúc' : text);
  if (C.t0) talking();
}
function hangup(text) {
  if (!C) return;
  for (const l of C.legs) if (l.id && !l.over) api(`/api/call/${l.id}/end`, {}).catch(() => {});
  over(text || 'Đã kết thúc');
}
function startPolling() {
  C.poll = setInterval(async () => {
    const c = C;
    if (!c) return;
    for (const l of c.legs) {
      if (!l.id || l.over) continue;
      try {
        const x = await api(`/api/call/${l.id}`);
        if (c !== C) return;
        if (x.state === 'ended') { endLeg(l, endText(x)); continue; }
        if (c.dir === 'down' && x.state === 'active' && x.answer && !l.remote) {
          l.remote = true;
          ring(false);
          if (!c.t0) cv('Đang kết nối…', legNames((y) => y.remote), 'calling');
          await l.pc.setRemoteDescription({ type: 'answer', sdp: await openSdp(x.answer) });
        }
      } catch (e) {
        if (e.message === 'Không thấy cuộc gọi') endLeg(l, 'Đã kết thúc');
        else if (/mã hoá|giải mã/.test(e.message)) { api(`/api/call/${l.id}/end`, {}).catch(() => {}); endLeg(l, e.message); }
      }
    }
  }, 1000);
}
function endText(x) {
  const n = devName(x.device);
  const m = { declined: `${n} từ chối cuộc gọi`, missed: `${n} không nghe máy`, locked: `Máy của ${n} đang khóa`,
    nologin: `${n} chưa đăng nhập máy`, lost: 'Mất kết nối', cancelled: 'Cuộc gọi đã hủy' };
  if (x.auto && !x.answered_at && x.reason !== 'cancelled') return `Máy của ${n} không tự nghe máy được`;
  if (x.dir === 'up' && x.reason === 'cancelled') return `${n} đã tắt máy`;
  if (x.reason === 'hangup') return 'Đã kết thúc' + (x.answered_at ? ' · ' + clock(x.ended_at - x.answered_at) : '');
  return m[x.reason] || 'Đã kết thúc';
}
async function startCall(auto) {
  if (C || needKey()) return;
  const targets = DEV === 'all' ? S.devices.filter(online) : S.devices.filter((d) => d.id === DEV);
  if (!targets.length) return alertBox(DEV === 'all' ? 'Không có máy nào đang bật' : `Máy của ${devName(DEV)} đang tắt`);
  const names = targets.map((d) => d.name).join(', ');
  C = { dir: 'down', auto, legs: targets.map((d) => ({ dev: d.id })) };
  cv(auto ? `Đang gọi ${names} (máy tự trả lời)…` : `Đang gọi ${names}…`, 'Đang bật micro', 'calling');
  try {
    C.stream = await navigator.mediaDevices.getUserMedia(MIC);
    $('cvSub').textContent = 'Đang chuẩn bị…';
    const errs = [];
    await Promise.all(C.legs.map(async (l) => {
      try {
        await makeLeg(l);
        await l.pc.setLocalDescription(await l.pc.createOffer());
        await gather(l.pc);
        if (!C) return;
        l.id = (await api('/api/call', { offer: await KT.encText(l.pc.localDescription.sdp), auto: !!auto, device: l.dev })).id;
      } catch (e) { errs.push(e.message); closeLeg(l); }
    }));
    if (!C) return;
    if (C.legs.every((l) => l.over)) throw new Error(errs[0] || 'Không gọi được');
    ring(true, 'out');
    $('cvSub').textContent = (auto ? 'Đang chờ máy tự nghe máy' : 'Đang đổ chuông') + (errs.length ? ' · ' + errs.join(' · ') : '');
    startPolling();
  } catch (e) {
    const msg = /Permission|NotAllowed/i.test(e.name + e.message) ? 'Chưa cho phép dùng micro' : e.message;
    cleanup(); cv('Không gọi được', msg, 'over'); setTimeout(() => { if (!C) hideCv(); }, 3500);
  }
}
$('btnCall').onclick = () => startCall(false);
$('btnAuto').onclick = () => startCall(true);
// Incoming: the poll sees a child's call ringing.
function watchCall() {
  const calls = S.calls || [];
  if (C && C.dir === 'up' && !C.answering) {
    const k = calls.find((x) => x.id === C.legs[0].id);
    if (!k) return over(`${devName(C.legs[0].dev)} đã tắt máy`);
    if (k.state === 'active' && k.answerer !== S.me) return over(`${PARENT[k.answerer] || 'Người khác'} đã nghe máy`);
  }
  const k = calls.find((x) => x.dir === 'up' && x.state === 'ringing' && !dismissed.has(x.id));
  if (C || !k) return;
  C = { dir: 'up', legs: [{ id: k.id, dev: k.device }] };
  cv(`${devName(k.device)} đang gọi`, 'Bấm Nghe để nói chuyện', 'ringing');
  ring(true, 'in');
}
$('cvAnswer').onclick = async () => {
  if (!C || C.dir !== 'up' || C.answering) return;
  C.answering = true;
  ring(false);
  const l = C.legs[0];
  cv('Đang kết nối…', '', 'calling');
  try {
    const x = await api(`/api/call/${l.id}`);
    if (x.state !== 'ringing') return over(x.state === 'active' ? `${PARENT[x.answerer] || 'Người khác'} đã nghe máy` : `${devName(l.dev)} đã tắt máy`);
    C.stream = await navigator.mediaDevices.getUserMedia(MIC);
    await makeLeg(l);
    await l.pc.setRemoteDescription({ type: 'offer', sdp: await openSdp(x.offer) });
    await l.pc.setLocalDescription(await l.pc.createAnswer());
    await gather(l.pc);
    await api(`/api/call/${l.id}/answer`, { answer: await KT.encText(l.pc.localDescription.sdp) });
    startPolling();
  } catch (e) { cleanup(); cv('Không nghe được', e.message, 'over'); setTimeout(() => { if (!C) hideCv(); }, 3500); }
};
$('cvEnd').onclick = () => { if (C) hangup(C.dir === 'up' && !C.answering ? 'Đã từ chối' : 'Đã kết thúc'); else hideCv(); };
$('cvMute').onclick = () => {
  const t = C?.stream?.getAudioTracks()[0];
  if (!t) return;
  t.enabled = !t.enabled;
  $('cvMute').classList.toggle('on', !t.enabled);
  $('cvMute').querySelector('span').textContent = t.enabled ? 'Tắt tiếng' : 'Bật tiếng';
};
window.addEventListener('pagehide', () => {
  for (const l of C?.legs || []) if (l.id && !l.over)
    fetch(`/api/call/${l.id}/end`, { method: 'POST', credentials: 'same-origin', keepalive: true, headers: { 'content-type': 'application/json' }, body: '{}' });
});

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
boot().catch((e) => { $('login').classList.remove('hide'); $('loginErr').textContent = e.message; });

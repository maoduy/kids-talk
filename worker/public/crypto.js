'use strict';
// End-to-end encryption with one family key (AES-256-GCM, 32 bytes), shared by the parents' phones and the
// children's computers and never sent to the relay. Text: "e1:" + base64url(iv | ciphertext+tag).
// Blobs (voice, screenshots): "KT1" | iv | ciphertext+tag. Same format as the agents (kids_talk_agent.py, kids_talk_win.py).
// Loaded by app.js and by the service worker (to read push previews).
const KT = (() => {
  const te = new TextEncoder(), td = new TextDecoder();
  const AAD = te.encode('kids-talk/1');
  const MAGIC = [0x4b, 0x54, 0x31]; // "KT1"
  const b64u = (u8) => {
    let s = '';
    for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode(...u8.subarray(i, i + 0x8000));
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  };
  const unb64u = (s) => {
    s = s.replace(/-/g, '+').replace(/_/g, '/');
    const b = atob(s + '==='.slice((s.length + 3) % 4));
    return Uint8Array.from(b, (c) => c.charCodeAt(0));
  };
  // The key lives in IndexedDB so the service worker can read it too.
  const db = () => new Promise((res, rej) => {
    const r = indexedDB.open('kids-talk', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('keys');
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
  const tx = async (mode, fn) => {
    const d = await db();
    return new Promise((res, rej) => {
      const t = d.transaction('keys', mode), q = fn(t.objectStore('keys'));
      t.oncomplete = () => res(q.result);
      t.onerror = () => rej(t.error);
    });
  };
  let key = null, raw = null;
  async function load() {
    raw = await tx('readonly', (s) => s.get('family')).catch(() => null);
    key = raw ? await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']) : null;
    return !!key;
  }
  async function save(r) {
    await tx('readwrite', (s) => s.put(r, 'family'));
    await load();
  }
  async function set(text) {
    const r = unb64u(String(text).trim());
    if (r.length !== 32) throw new Error('Khóa không đúng (phải là 43 ký tự)');
    await save(r);
  }
  async function create() {
    await save(crypto.getRandomValues(new Uint8Array(32)));
    return b64u(raw);
  }
  const exportKey = () => (raw ? b64u(raw) : null);
  // Fingerprint: lets the relay tell that two phones hold the same key without ever seeing it.
  async function fp() {
    if (!raw) return null;
    const pre = te.encode('kids-talk/fp/'), all = new Uint8Array(pre.length + raw.length);
    all.set(pre); all.set(raw, pre.length);
    return b64u(new Uint8Array(await crypto.subtle.digest('SHA-256', all)).subarray(0, 12));
  }
  const isEnc = (s) => typeof s === 'string' && s.startsWith('e1:');
  async function seal(bytes) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: AAD }, key, bytes));
    const out = new Uint8Array(12 + ct.length);
    out.set(iv); out.set(ct, 12);
    return out;
  }
  async function open(u8) {
    return new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: u8.subarray(0, 12), additionalData: AAD }, key, u8.subarray(12)));
  }
  async function encText(s) {
    if (!key || !s) return s;
    return 'e1:' + b64u(await seal(te.encode(s)));
  }
  // Plain text; null when it cannot be read (no key on this phone, other key, or changed on the way).
  async function decText(s) {
    if (!isEnc(s)) return s;
    if (!key) return null;
    try { return td.decode(await open(unb64u(s.slice(3)))); } catch { return null; }
  }
  async function encBlob(buf) {
    if (!key) return buf;
    const ct = await seal(new Uint8Array(buf));
    const out = new Uint8Array(3 + ct.length);
    out.set(MAGIC); out.set(ct, 3);
    return out.buffer;
  }
  const isEncBlob = (u8) => u8.length > 3 && u8[0] === MAGIC[0] && u8[1] === MAGIC[1] && u8[2] === MAGIC[2];
  // Plain bytes; a blob without the magic is returned as it is (older, unencrypted data). null if unreadable.
  async function decBlob(buf) {
    const u8 = new Uint8Array(buf);
    if (!isEncBlob(u8)) return buf;
    if (!key) return null;
    try { return (await open(u8.subarray(3))).buffer; } catch { return null; }
  }
  return { load, set, create, exportKey, fp, has: () => !!key, isEnc, isEncBlob, encText, decText, encBlob, decBlob };
})();

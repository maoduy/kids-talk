// Gọi điện - Nhắn tin con — relay Worker.
// Parent PWA (passkey session)  <->  D1  <->  agent on the child's laptop (bearer token).
import {
  generateAuthenticationOptions,
  generateRegistrationOptions,
  verifyAuthenticationResponse,
  verifyRegistrationResponse,
} from '@simplewebauthn/server';

export interface Env {
  DB: D1Database;
  KV: KVNamespace;
  ASSETS: Fetcher;
  AGENT_TOKEN?: string;   // optional: token of the first computer (others: add-device.sh)
  SETUP_CODE: string;
  SESSION_KEY: string;
  VAPID_PUBLIC: string;
  VAPID_PRIVATE_JWK: string;
  VAPID_SUBJECT: string;
  TURN_KEY_ID?: string;
  TURN_KEY_TOKEN?: string;
  TURN_SECRET?: string;   // your own coturn's use-auth-secret (TURN REST credentials) …
  TURN_HOST?: string;     // … and its host name
  FIRST_DEVICE?: string;  // id of the computer that uses AGENT_TOKEN (default 'kid1')
}

const SESSION_DAYS = 180;
const VOICE_DAYS = 30;
const VOICE_MAX = 8_000_000;
const enc = new TextEncoder();

// ---------- helpers ----------
const json = (data: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...headers },
  });
const bad = (msg: string, status = 400) => json({ error: msg }, status);

function b64url(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function unb64url(s: string): Uint8Array<ArrayBuffer> {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  const out = new Uint8Array(new ArrayBuffer(bin.length));
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function randomId(n = 16): string {
  return b64url(crypto.getRandomValues(new Uint8Array(n)));
}
function timingSafeEq(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
function cookies(req: Request): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (req.headers.get('cookie') || '').split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}
async function hmac(key: string, data: string): Promise<string> {
  const k = await crypto.subtle.importKey('raw', enc.encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return b64url(await crypto.subtle.sign('HMAC', k, enc.encode(data)));
}
// A parent is 'bo' or 'me'. The session cookie carries who signed in: "<exp>~<who>.<hmac>".
type Who = 'bo' | 'me';
const PARENTS = "('bo','me')";
const asWho = (v: unknown): Who => (v === 'me' ? 'me' : 'bo');
async function makeSession(env: Env, who: Who): Promise<string> {
  const exp = Date.now() + SESSION_DAYS * 86400_000;
  const body = `${exp}~${who}`;
  return `${body}.${await hmac(env.SESSION_KEY, body)}`;
}
async function getSession(req: Request, env: Env): Promise<{ who: Who } | null> {
  const sid = cookies(req)['bo_sid'];
  if (!sid) return null;
  const [body, sig] = sid.split('.');
  if (!body || !sig) return null;
  if (!timingSafeEq(sig, await hmac(env.SESSION_KEY, body))) return null;
  const [exp, who] = body.split('~');
  return Number(exp) > Date.now() ? { who: asWho(who) } : null; // sessions from before Mẹ existed are Bố's
}
async function hasSession(req: Request, env: Env): Promise<boolean> {
  return !!(await getSession(req, env));
}
const sessionCookie = (v: string, maxAge: number) =>
  `bo_sid=${v}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`;

async function kvGet<T>(env: Env, k: string): Promise<T | null> {
  const r = await env.DB.prepare('SELECT v FROM kv WHERE k=?').bind(k).first<{ v: string }>();
  return r ? (JSON.parse(r.v) as T) : null;
}
async function kvSet(env: Env, k: string, v: unknown) {
  await env.DB.prepare('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v')
    .bind(k, JSON.stringify(v))
    .run();
}

// Long-poll: every parent-side change for a computer bumps ver:<dev>; /agent/wait holds until it moves.
const WAIT_MAX_S = 25;
const ONLINE_MS = 45_000;   // a waiting agent checks in at least every WAIT_MAX_S
const bump = (env: Env, dev: string) => kvSet(env, `ver:${dev}`, Date.now());
const verOf = async (env: Env, dev: string) => (await kvGet<number>(env, `ver:${dev}`)) || 0;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ---------- voice clips (WAV in KV, kept as recorded: no speech-to-text) ----------
async function putVoice(env: Env, key: string, buf: ArrayBuffer) {
  await env.KV.put(`voice:${key}`, buf, { expirationTtl: VOICE_DAYS * 86400 });
}
async function getVoice(env: Env, key: string): Promise<Response> {
  const buf = /^[\w-]{4,64}$/.test(key) ? await env.KV.get(`voice:${key}`, 'arrayBuffer') : null;
  if (!buf) return bad('Ghi âm đã hết hạn', 404);
  return new Response(buf, { headers: { 'content-type': 'audio/wav', 'cache-control': 'private, max-age=86400' } });
}

// ---------- Web Push (no payload: the service worker fetches /api/notify-preview) ----------
async function vapidJwt(env: Env, aud: string): Promise<string> {
  const header = b64url(enc.encode(JSON.stringify({ typ: 'JWT', alg: 'ES256' })));
  const claims = b64url(
    enc.encode(JSON.stringify({ aud, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: env.VAPID_SUBJECT })),
  );
  const key = await crypto.subtle.importKey(
    'jwk',
    JSON.parse(env.VAPID_PRIVATE_JWK),
    { name: 'ECDSA', namedCurve: 'P-256' },
    false,
    ['sign'],
  );
  const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, key, enc.encode(`${header}.${claims}`));
  return `${header}.${claims}.${b64url(sig)}`;
}
async function notifyParent(env: Env, title: string, body: string) {
  await kvSet(env, 'last_push', { title, body, at: Date.now() });
  if (!env.VAPID_PRIVATE_JWK) return;
  const { results } = await env.DB.prepare('SELECT endpoint FROM push_subs').all<{ endpoint: string }>();
  await Promise.all(
    results.map(async ({ endpoint }) => {
      try {
        const jwt = await vapidJwt(env, new URL(endpoint).origin);
        const r = await fetch(endpoint, {
          method: 'POST',
          headers: { Authorization: `vapid t=${jwt}, k=${env.VAPID_PUBLIC}`, TTL: '86400', Urgency: 'high' },
        });
        if (r.status === 404 || r.status === 410)
          await env.DB.prepare('DELETE FROM push_subs WHERE endpoint=?').bind(endpoint).run();
        else if (!r.ok) console.log('push failed', r.status, await r.text());
      } catch (e) {
        console.log('push error', String(e));
      }
    }),
  );
}

// ---------- children's computers (devices) ----------
// Each child's computer has its own agent token (sha-256 in `devices`); AGENT_TOKEN belongs to the first one.
// Per-device kv keys: the first computer keeps the plain names ('status', 'last_seen', 'lock'), others get ':<id>'.
type Dev = { id: string; name: string };
let FIRST = 'kid1';   // set from env.FIRST_DEVICE on every request
const dk = (k: string, dev: string) => (dev === FIRST ? k : `${k}:${dev}`);
async function sha256hex(s: string) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(s)))].map((b) => b.toString(16).padStart(2, '0')).join('');
}
async function devices(env: Env): Promise<Dev[]> {
  const { results } = await env.DB.prepare('SELECT id, name FROM devices ORDER BY created_at').all<Dev>();
  return results.length ? results : [{ id: FIRST, name: 'Con' }];
}
async function agentDevice(req: Request, env: Env): Promise<Dev | null> {
  const auth = req.headers.get('authorization') || '';
  const tok = auth.startsWith('Bearer ') ? auth.slice(7) : '';
  if (!tok) return null;
  if (env.AGENT_TOKEN && timingSafeEq(auth, `Bearer ${env.AGENT_TOKEN}`)) return (await devices(env)).find((d) => d.id === FIRST) || { id: FIRST, name: 'Con' };
  return await env.DB.prepare('SELECT id, name FROM devices WHERE token_hash=?').bind(await sha256hex(tok)).first<Dev>();
}
// The devices a parent request is about: ?dev= / body.devices — 'all', one id, or a list.
async function pickDevices(env: Env, v: unknown): Promise<Dev[]> {
  const all = await devices(env);
  if (v === undefined || v === null || v === '' || v === 'all') return all;
  const want = Array.isArray(v) ? v.map(String) : String(v).split(',');
  return all.filter((d) => want.includes(d.id));
}

// ---------- live calls (WebRTC voice; this Worker only relays the SDP) ----------
const RING_MS = 60_000;   // unanswered after this -> missed (time to log in or unlock)
const LOST_MS = 20_000;   // a side that stops polling an active call -> lost
const WHO_NAME: Record<string, string> = { bo: 'Bố', me: 'Mẹ', con: 'Con' };
type Call = {
  id: string; dir: 'down' | 'up'; caller: string; answerer: string | null; state: string; reason: string | null;
  ended_by: string | null; offer: string; answer: string | null; created_at: number; answered_at: number | null;
  ended_at: number | null; p_seen: number | null; k_seen: number | null; auto: number; device: string;
};
const CALL_PUBLIC = 'id,device,dir,caller,answerer,state,reason,auto,created_at,answered_at,ended_at';

async function iceServers(env: Env): Promise<RTCIceServerLike[]> {
  const stun = [{ urls: ['stun:stun.cloudflare.com:3478'] }];
  if (env.TURN_SECRET && env.TURN_HOST) {
    // coturn REST credentials: username "<expiry>:<label>", password base64(HMAC-SHA1(secret, username)).
    const host = env.TURN_HOST;
    const username = `${Math.floor(Date.now() / 1000) + 12 * 3600}:kids-talk`;
    const k = await crypto.subtle.importKey('raw', enc.encode(env.TURN_SECRET), { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']);
    const sig = new Uint8Array(await crypto.subtle.sign('HMAC', k, enc.encode(username)));
    const credential = btoa(String.fromCharCode(...sig));
    return [
      { urls: [`stun:${host}:3478`] },
      { urls: [`turn:${host}:3478?transport=udp`, `turn:${host}:3478?transport=tcp`], username, credential },
    ];
  }
  if (!env.TURN_KEY_ID || !env.TURN_KEY_TOKEN) return stun;
  const cached = await env.KV.get<{ servers: RTCIceServerLike[]; exp: number }>('ice', 'json');
  if (cached && cached.exp > Date.now() + 3 * 3600_000) return cached.servers;
  try {
    const r = await fetch(`https://rtc.live.cloudflare.com/v1/turn/keys/${env.TURN_KEY_ID}/credentials/generate-ice-servers`, {
      method: 'POST',
      headers: { authorization: `Bearer ${env.TURN_KEY_TOKEN}`, 'content-type': 'application/json' },
      body: JSON.stringify({ ttl: 86400 }),
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    const j: any = await r.json();
    const list: any[] = Array.isArray(j.iceServers) ? j.iceServers : [j.iceServers];
    // Browsers refuse port 53 (Cloudflare's docs say to drop those URLs).
    const servers = list
      .map((s) => ({ ...s, urls: (Array.isArray(s.urls) ? s.urls : [s.urls]).filter((u: string) => !/:53\b/.test(u)) }))
      .filter((s) => s.urls.length);
    await env.KV.put('ice', JSON.stringify({ servers, exp: Date.now() + 86400_000 }), { expirationTtl: 80000 });
    return servers;
  } catch (e) {
    console.log('turn credentials failed', String(e));
    return stun;
  }
}
type RTCIceServerLike = { urls: string[]; username?: string; credential?: string };

const mmss = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s} giây` : `${Math.floor(s / 60)} phút${s % 60 ? ` ${s % 60} giây` : ''}`;
};
function callNote(c: Call, reason: string, by: string, now: number): string {
  const P = WHO_NAME[c.caller] || 'Bố';
  const lost = reason === 'lost' ? ' · mất kết nối' : '';
  if (c.answered_at) {
    const len = mmss(now - c.answered_at);
    return c.dir === 'down' ? `📞 ${P} gọi cho con${c.auto ? ' (máy tự trả lời)' : ''} · ${len}${lost}` : `📞 Con gọi · ${WHO_NAME[c.answerer || ''] || 'bố mẹ'} nghe · ${len}${lost}`;
  }
  if (c.dir === 'down') {
    if (c.auto) return reason === 'cancelled' ? `📞 ${P} gọi tự trả lời rồi tắt máy` : `📞 ${P} gọi tự trả lời nhưng máy không kết nối được`;
    if (reason === 'declined') return `📞 Con từ chối cuộc gọi của ${P}`;
    if (reason === 'cancelled') return `📞 ${P} gọi rồi tắt máy`;
    if (reason === 'locked') return `📞 ${P} gọi nhưng máy đang khóa, con không nghe được`;
    if (reason === 'nologin') return `📞 ${P} gọi nhưng con chưa đăng nhập máy`;
    return `📞 ${P} gọi, con chưa nghe máy`;
  }
  if (reason === 'declined') return `📞 ${WHO_NAME[by] || 'Bố mẹ'} từ chối cuộc gọi của con`;
  if (reason === 'cancelled') return '📞 Con gọi rồi tắt máy';
  return '📞 Con gọi, bố mẹ chưa nghe máy';
}
async function endCall(env: Env, ctx: ExecutionContext, c: Call, reason: string, by: string) {
  const now = Date.now();
  const r = await env.DB.prepare("UPDATE calls SET state='ended', reason=?, ended_by=?, ended_at=? WHERE id=? AND state!='ended'")
    .bind(reason, by, now, c.id)
    .run();
  if (!r.meta.changes) return;
  const note = callNote(c, reason, by, now);
  await env.DB.prepare("INSERT INTO messages(sender,body,speak,created_at,device) VALUES('he',?,0,?,?)").bind(note, now, c.device).run();
  await bump(env, c.device);
  if (c.dir === 'up' && !c.answered_at && reason !== 'declined') {
    const name = (await devices(env)).find((d) => d.id === c.device)?.name || 'Con';
    ctx.waitUntil(notifyParent(env, `📞 Cuộc gọi nhỡ từ ${name}`, note));
  }
}
async function sweepCalls(env: Env, ctx: ExecutionContext) {
  const now = Date.now();
  const { results } = await env.DB.prepare("SELECT * FROM calls WHERE state!='ended'").all<Call>();
  for (const c of results) {
    if (c.state === 'ringing' && now - c.created_at > RING_MS) await endCall(env, ctx, c, 'missed', 'timeout');
    else if (c.state === 'ringing' && c.dir === 'up' && now - (c.k_seen || c.created_at) > LOST_MS)
      await endCall(env, ctx, c, 'cancelled', 'timeout');
    else if (c.state === 'active' && now - Math.min(c.p_seen || 0, c.k_seen || 0) > LOST_MS)
      await endCall(env, ctx, c, 'lost', 'timeout');
  }
}
async function openCall(env: Env, ctx: ExecutionContext, dev: string) {
  await sweepCalls(env, ctx);
  return await env.DB.prepare(`SELECT ${CALL_PUBLIC} FROM calls WHERE state!='ended' AND device=? ORDER BY created_at DESC LIMIT 1`).bind(dev).first();
}
async function openCalls(env: Env, ctx: ExecutionContext) {
  await sweepCalls(env, ctx);
  return (await env.DB.prepare(`SELECT ${CALL_PUBLIC} FROM calls WHERE state!='ended' ORDER BY created_at DESC`).all()).results;
}
const getCall = (env: Env, id: string) => env.DB.prepare('SELECT * FROM calls WHERE id=?').bind(id).first<Call>();
const sdpOk = (s: unknown) => typeof s === 'string' && (s.startsWith('v=0') || s.startsWith('e1:')) && s.length < 60000;
// End-to-end encryption: the relay only knows the key's fingerprint (kv 'key_fp'). Once it is set, parent
// content must arrive encrypted ("e1:" text, "KT1" blobs); the relay cannot read or check it.
const isEnc = (s: unknown) => typeof s === 'string' && s.startsWith('e1:');
const NEED_KEY = 'Điện thoại này chưa có khóa gia đình (Cài đặt → Mã hóa)';

// ---------- WebAuthn ----------
async function passkeyCount(env: Env): Promise<number> {
  const r = await env.DB.prepare('SELECT COUNT(*) AS n FROM passkeys').first<{ n: number }>();
  return r?.n ?? 0;
}
async function putChallenge(env: Env, challenge: string, extra: Record<string, unknown> = {}): Promise<string> {
  const id = randomId();
  await env.KV.put(`chal:${id}`, JSON.stringify({ challenge, ...extra }), { expirationTtl: 300 });
  return id;
}
async function takeChallenge(env: Env, id: string): Promise<{ challenge: string; who?: Who } | null> {
  if (!id) return null;
  const v = await env.KV.get(`chal:${id}`);
  if (!v) return null;
  await env.KV.delete(`chal:${id}`);
  return JSON.parse(v);
}

async function handleAuth(req: Request, env: Env, path: string): Promise<Response> {
  const url = new URL(req.url);
  const rpID = url.hostname;
  const origin = url.origin;
  const body: any = req.method === 'POST' ? await req.json().catch(() => ({})) : {};

  if (path === '/auth/status') {
    return json({ loggedIn: await hasSession(req, env), hasPasskeys: (await passkeyCount(env)) > 0 });
  }

  if (path === '/auth/register/options') {
    const n = await passkeyCount(env);
    let allowed = await hasSession(req, env);
    const code = String(body.code || '').trim();
    if (!allowed && code) {
      if (n === 0 && timingSafeEq(code, env.SETUP_CODE)) allowed = true;
      else if (await env.KV.get(`invite:${code}`)) {
        allowed = true;
        await env.KV.delete(`invite:${code}`);
      }
    }
    if (!allowed) return bad('Mã không đúng hoặc đã hết hạn', 403);
    const who = asWho(body.who);
    const opts = await generateRegistrationOptions({
      rpName: 'Gọi điện - Nhắn tin con',
      rpID,
      userName: who,
      userDisplayName: who === 'me' ? 'Mẹ' : 'Bố',
      userID: enc.encode(who),
      attestationType: 'none',
      authenticatorSelection: { residentKey: 'required', userVerification: 'preferred' },
    });
    const cid = await putChallenge(env, opts.challenge, { kind: 'reg', who });
    return json({ options: opts, cid });
  }

  if (path === '/auth/register/verify') {
    const ch = await takeChallenge(env, body.cid);
    if (!ch) return bad('Hết thời gian, thử lại');
    const v = await verifyRegistrationResponse({
      response: body.response,
      expectedChallenge: ch.challenge,
      expectedOrigin: origin,
      expectedRPID: rpID,
      requireUserVerification: false,
    }).catch((e) => ({ verified: false, error: String(e) }) as any);
    if (!v.verified || !v.registrationInfo) return bad('Không tạo được passkey: ' + (v.error || ''));
    const cred = v.registrationInfo.credential;
    const who = asWho(ch.who);
    await env.DB.prepare('INSERT INTO passkeys(id,public_key,counter,name,who,created_at) VALUES(?,?,?,?,?,?)')
      .bind(cred.id, b64url(cred.publicKey), cred.counter, String(body.name || 'Thiết bị').slice(0, 60), who, Date.now())
      .run();
    const sid = await makeSession(env, who);
    return json({ ok: true }, 200, { 'set-cookie': sessionCookie(sid, SESSION_DAYS * 86400) });
  }

  if (path === '/auth/login/options') {
    const opts = await generateAuthenticationOptions({ rpID, userVerification: 'preferred' });
    const cid = await putChallenge(env, opts.challenge, { kind: 'auth' });
    return json({ options: opts, cid });
  }

  if (path === '/auth/login/verify') {
    const ch = await takeChallenge(env, body.cid);
    if (!ch) return bad('Hết thời gian, thử lại');
    const id = body.response?.id;
    const pk = await env.DB.prepare('SELECT * FROM passkeys WHERE id=?')
      .bind(id)
      .first<{ id: string; public_key: string; counter: number; who: string | null }>();
    if (!pk) return bad('Passkey không có trong danh sách', 403);
    const v = await verifyAuthenticationResponse({
      response: body.response,
      expectedChallenge: ch.challenge,
      expectedOrigin: origin,
      expectedRPID: rpID,
      credential: { id: pk.id, publicKey: unb64url(pk.public_key), counter: pk.counter },
      requireUserVerification: false,
    }).catch((e) => ({ verified: false, error: String(e) }) as any);
    if (!v.verified) return bad('Đăng nhập không thành công', 403);
    await env.DB.prepare('UPDATE passkeys SET counter=? WHERE id=?')
      .bind(v.authenticationInfo.newCounter, pk.id)
      .run();
    const sid = await makeSession(env, asWho(pk.who));
    return json({ ok: true }, 200, { 'set-cookie': sessionCookie(sid, SESSION_DAYS * 86400) });
  }

  if (path === '/auth/logout') return json({ ok: true }, 200, { 'set-cookie': sessionCookie('', 0) });

  return bad('not found', 404);
}

// ---------- parent API ----------
async function handleApi(req: Request, env: Env, ctx: ExecutionContext, path: string): Promise<Response> {
  const sess = await getSession(req, env);
  if (!sess) return bad('Chưa đăng nhập', 401);
  const who = sess.who;
  const now = Date.now();
  const body: any =
    req.method === 'POST' && path !== '/api/voice' ? await req.json().catch(() => ({})) : {};
  const keyFp = await kvGet<string>(env, 'key_fp');

  if (path === '/api/state' && req.method === 'GET') {
    const qs = new URL(req.url).searchParams;
    const since = Number(qs.get('since') || 0);
    const sel = await pickDevices(env, qs.get('dev'));
    const ids = sel.map((d) => d.id);
    const IN = `device IN (${ids.map(() => '?').join(',') || "''"})`;
    const msgs = since
      ? await env.DB.prepare(`SELECT * FROM messages WHERE id>? AND ${IN} ORDER BY id`).bind(since, ...ids).all()
      : await env.DB.prepare(`SELECT * FROM (SELECT * FROM messages WHERE ${IN} ORDER BY id DESC LIMIT 200) ORDER BY id`).bind(...ids).all();
    // Receipts change on old rows: always resend the receipt state of the last 50 parent messages.
    const receipts = await env.DB.prepare(
      `SELECT id, delivered_at, read_at FROM messages WHERE sender IN ${PARENTS} AND ${IN} ORDER BY id DESC LIMIT 50`,
    ).bind(...ids).all();
    const tasks = await env.DB.prepare(
      `SELECT * FROM tasks WHERE (deleted=0 OR updated_at>?) AND ${IN} ORDER BY (done_at IS NOT NULL), COALESCE(due_at, 9e15), updated_at`,
    )
      .bind(now - 7 * 86400_000, ...ids)
      .all();
    const all = await devices(env);
    const devs = [];
    for (const d of all) {
      const shots = await env.DB.prepare("SELECT * FROM commands WHERE kind='shot' AND device=? ORDER BY id DESC LIMIT 3").bind(d.id).all();
      devs.push({
        ...d,
        status: await kvGet(env, dk('status', d.id)),
        lastSeen: await kvGet(env, dk('last_seen', d.id)),
        lock: await kvGet(env, dk('lock', d.id)),
        shots: shots.results,
      });
    }
    const calls = await openCalls(env, ctx);
    return json({
      messages: msgs.results,
      receipts: receipts.results,
      tasks: tasks.results.filter((t: any) => !t.deleted),
      devices: devs,
      calls,
      call: calls[0] || null,
      me: who,
      keyFp,
      now,
    });
  }

  if (path === '/api/message' && req.method === 'POST') {
    const text = String(body.body || '').trim().slice(0, 8000);
    if (!text) return bad('Tin nhắn trống');
    if (keyFp && !isEnc(text)) return bad(NEED_KEY, 409);
    const rp = body.repeat === true ? 1 : 0;
    const to = await pickDevices(env, body.devices);
    if (!to.length) return bad('Chưa chọn máy');
    const ids = [];
    for (const d of to) {
      const r = await env.DB.prepare(
        'INSERT INTO messages(sender,body,speak,repeat_play,created_at,device) VALUES(?,?,?,?,?,?) RETURNING id',
      )
        .bind(who, text, body.speak === false && !rp ? 0 : 1, rp, now, d.id)
        .first<{ id: number }>();
      ids.push(r?.id);
      await bump(env, d.id);
    }
    return json({ ok: true, id: ids[0], ids });
  }

  if (path === '/api/voice' && req.method === 'POST') {
    const buf = await req.arrayBuffer();
    if (buf.byteLength < 1000) return bad('Ghi âm trống');
    if (buf.byteLength > VOICE_MAX) return bad('Ghi âm quá dài', 413);
    if (keyFp && new TextDecoder().decode(new Uint8Array(buf, 0, 3)) !== 'KT1') return bad(NEED_KEY, 409);
    const qs = new URL(req.url).searchParams;
    const key = randomId(12);
    await putVoice(env, key, buf);
    const rp = qs.get('repeat') === '1' ? 1 : 0;
    const to = await pickDevices(env, qs.get('dev'));
    const ids = [];
    for (const d of to) {
      const r = await env.DB.prepare(
        'INSERT INTO messages(sender,body,speak,repeat_play,audio,audio_ms,created_at,device) VALUES(?,?,?,?,?,?,?,?) RETURNING id',
      )
        .bind(who, '', qs.get('speak') === '0' && !rp ? 0 : 1, rp, key, Number(qs.get('ms')) || null, now, d.id)
        .first<{ id: number }>();
      ids.push(r?.id);
      await bump(env, d.id);
    }
    return json({ ok: true, id: ids[0], ids });
  }
  const vm = path.match(/^\/api\/voice\/([\w-]+)$/);
  if (vm && req.method === 'GET') return getVoice(env, vm[1]);

  if (path === '/api/task' && req.method === 'POST') {
    const title = String(body.title || '').trim().slice(0, 2000);
    if (!title) return bad('Chưa nhập việc');
    if (keyFp && !isEnc(title)) return bad(NEED_KEY, 409);
    const due = body.due_at ? Number(body.due_at) : null;
    const to = await pickDevices(env, body.devices);
    if (!to.length) return bad('Chưa chọn máy');
    for (const d of to) {
      await env.DB.prepare('INSERT INTO tasks(id,title,due_at,created_by,updated_at,device) VALUES(?,?,?,?,?,?)')
        .bind(randomId(9), title, due, who, now, d.id)
        .run();
      await bump(env, d.id);
    }
    return json({ ok: true });
  }

  const tm = path.match(/^\/api\/task\/([\w-]+)$/);
  if (tm && req.method === 'POST') {
    const t = await env.DB.prepare('SELECT * FROM tasks WHERE id=?').bind(tm[1]).first<any>();
    if (!t) return bad('Không thấy việc', 404);
    if (keyFp && body.title !== undefined && !isEnc(body.title)) return bad(NEED_KEY, 409);
    const title = body.title !== undefined ? String(body.title).trim().slice(0, 2000) || t.title : t.title;
    const due = body.due_at !== undefined ? (body.due_at ? Number(body.due_at) : null) : t.due_at;
    const done = body.done !== undefined ? (body.done ? t.done_at || now : null) : t.done_at;
    const del = body.deleted ? 1 : t.deleted;
    const renotify = due !== t.due_at ? 0 : t.overdue_notified;
    await env.DB.prepare(
      'UPDATE tasks SET title=?, due_at=?, done_at=?, deleted=?, overdue_notified=?, updated_at=? WHERE id=?',
    )
      .bind(title, due, done, del, renotify, now, t.id)
      .run();
    await bump(env, t.device);
    return json({ ok: true });
  }

  // Hard lock: the wanted state lives here; the agent makes the laptop match it (and unlocks itself at `until`).
  if (path === '/api/lock' && req.method === 'POST') {
    const to = await pickDevices(env, body.devices);
    if (!to.length) return bad('Chưa chọn máy');
    const min = Math.max(0, Math.min(24 * 60, Number(body.minutes) || 0));
    const lockMsg = String(body.msg || '').trim().slice(0, 2000);
    if (keyFp && lockMsg && !isEnc(lockMsg)) return bad(NEED_KEY, 409);
    for (const d of to) {
      await kvSet(
        env,
        dk('lock', d.id),
        body.on
          ? { on: true, until: min ? now + min * 60_000 : null, msg: lockMsg, by: who, at: now }
          : { on: false, by: who, at: now },
      );
      await bump(env, d.id);
    }
    return json({ ok: true });
  }


  if (path === '/api/ice') return json({ iceServers: await iceServers(env) });
  if (path === '/api/call' && req.method === 'POST') {
    if (!sdpOk(body.offer)) return bad('offer?');
    if (keyFp && !isEnc(body.offer)) return bad(NEED_KEY, 409);
    const [d] = await pickDevices(env, body.device || FIRST);
    if (!d) return bad('Không có máy này', 404);
    const seen = await kvGet<number>(env, dk('last_seen', d.id));
    if (!seen || now - seen > ONLINE_MS) return bad(`Máy của ${d.name} đang tắt hoặc mất mạng`, 409);
    const lock = await kvGet<any>(env, dk('lock', d.id));
    const auto = body.auto === true ? 1 : 0;   // auto-answer works on a locked laptop too
    if (!auto && lock?.on && (!lock.until || lock.until > now)) return bad(`Máy của ${d.name} đang khóa, mở khóa trước rồi gọi`, 409);
    if (await openCall(env, ctx, d.id)) return bad(`${d.name} đang có cuộc gọi khác`, 409);
    const id = randomId(9);
    await env.DB.prepare("INSERT INTO calls(id,dir,caller,state,offer,created_at,p_seen,auto,device) VALUES(?,'down',?,'ringing',?,?,?,?,?)")
      .bind(id, who, body.offer, now, now, auto, d.id)
      .run();
    await bump(env, d.id);
    return json({ ok: true, id });
  }
  const cm = path.match(/^\/api\/call\/([\w-]+)(\/answer|\/end)?$/);
  if (cm) {
    await sweepCalls(env, ctx);
    const c = await getCall(env, cm[1]);
    if (!c) return bad('Không thấy cuộc gọi', 404);
    if (!cm[2] && req.method === 'GET') {
      if (c.state !== 'ended' && (c.dir === 'down' || c.answerer === who))
        await env.DB.prepare('UPDATE calls SET p_seen=? WHERE id=?').bind(now, c.id).run();
      return json({ ...c, offer: c.dir === 'up' ? c.offer : undefined, answer: c.dir === 'down' ? c.answer : undefined });
    }
    if (cm[2] === '/answer' && req.method === 'POST') {
      if (!sdpOk(body.answer)) return bad('answer?');
      if (keyFp && !isEnc(body.answer)) return bad(NEED_KEY, 409);
      const r = await env.DB.prepare(
        "UPDATE calls SET state='active', answer=?, answerer=?, answered_at=?, p_seen=?, k_seen=? WHERE id=? AND state='ringing' AND dir='up'",
      )
        .bind(body.answer, who, now, now, now, c.id)
        .run();
      if (!r.meta.changes) return bad(c.answerer ? `${WHO_NAME[c.answerer]} đã nghe máy` : 'Cuộc gọi đã kết thúc', 409);
      return json({ ok: true });
    }
    if (cm[2] === '/end' && req.method === 'POST') {
      if (c.state !== 'ended') await endCall(env, ctx, c, c.state === 'ringing' ? (c.dir === 'down' ? 'cancelled' : 'declined') : 'hangup', who);
      return json({ ok: true });
    }
  }

  if (path === '/api/command' && req.method === 'POST') {
    if (body.kind !== 'shot') return bad('Lệnh không hợp lệ');
    const [d] = await pickDevices(env, body.device || FIRST);
    if (!d) return bad('Không có máy này', 404);
    const pending = await env.DB.prepare(
      "SELECT id FROM commands WHERE kind='shot' AND done_at IS NULL AND created_at>? AND device=?",
    )
      .bind(now - 120_000, d.id)
      .first<{ id: number }>();
    if (pending) return json({ ok: true, id: pending.id });
    const r = await env.DB.prepare('INSERT INTO commands(kind,created_at,device) VALUES(?,?,?) RETURNING id')
      .bind('shot', now, d.id)
      .first<{ id: number }>();
    await bump(env, d.id);
    return json({ ok: true, id: r?.id });
  }

  const sm = path.match(/^\/api\/shot\/(\d+)$/);
  if (sm && req.method === 'GET') {
    const img = await env.KV.get(`shot:${sm[1]}`, 'arrayBuffer');
    if (!img) return bad('Ảnh đã hết hạn', 404);
    return new Response(img, { headers: { 'content-type': 'image/jpeg', 'cache-control': 'private, max-age=86400' } });
  }

  if (path === '/api/push/key') return json({ key: env.VAPID_PUBLIC || null });
  if (path === '/api/push/subscribe' && req.method === 'POST') {
    const ep = String(body.endpoint || '');
    if (!/^https:\/\//.test(ep)) return bad('endpoint?');
    await env.DB.prepare('INSERT OR IGNORE INTO push_subs(endpoint,created_at) VALUES(?,?)').bind(ep, now).run();
    return json({ ok: true });
  }
  if (path === '/api/push/test' && req.method === 'POST') {
    ctx.waitUntil(notifyParent(env, 'Gọi điện - Nhắn tin con', 'Thông báo thử — đã bật thành công'));
    return json({ ok: true });
  }
  if (path === '/api/notify-preview') return json((await kvGet(env, 'last_push')) || { title: 'Gọi điện - Nhắn tin con', body: 'Có tin mới' });

  if (path === '/api/invite' && req.method === 'POST') {
    const code = String(Math.floor(100000 + Math.random() * 900000));
    await env.KV.put(`invite:${code}`, '1', { expirationTtl: 600 });
    return json({ code });
  }
  if (path === '/api/devices') return json({ devices: await devices(env) });
  // Pairing a child's computer from the app: the phone makes the token (and keeps the family key); the relay
  // stores only the token's sha-256.
  if (path === '/api/device' && req.method === 'POST') {
    const id = String(body.id || '');
    const name = String(body.name || '').trim().slice(0, 40);
    if (!/^[a-z0-9-]{1,20}$/.test(id) || !name || !/^[0-9a-f]{64}$/.test(String(body.token_hash))) return bad('Tên máy không hợp lệ');
    const r = await env.DB.prepare('INSERT OR IGNORE INTO devices(id,name,token_hash,created_at) VALUES(?,?,?,?)')
      .bind(id, name, body.token_hash, now)
      .run();
    if (!r.meta.changes) return bad('Đã có máy tên này', 409);
    return json({ ok: true });
  }
  // The family key's fingerprint: set once by the first phone; `replace` starts over with a new key.
  if (path === '/api/key' && req.method === 'POST') {
    const fp = String(body.fp || '');
    if (!/^[\w-]{16}$/.test(fp)) return bad('fp?');
    if (keyFp && keyFp !== fp && !body.replace) return bad('Gia đình đã có khóa khác: nhập khóa đó', 409);
    await kvSet(env, 'key_fp', fp);
    return json({ ok: true });
  }
  if (path === '/api/passkeys') {
    const { results } = await env.DB.prepare('SELECT id,name,who,created_at FROM passkeys ORDER BY created_at').all();
    return json({ passkeys: results });
  }

  return bad('not found', 404);
}

// ---------- agent API ----------
async function handleAgent(req: Request, env: Env, ctx: ExecutionContext, path: string): Promise<Response> {
  const dev = await agentDevice(req, env);
  if (!dev) return bad('unauthorized', 401);
  const D = dev.id;
  const now = Date.now();

  if (path === '/agent/sync' && req.method === 'POST') {
    const body: any = await req.json().catch(() => ({}));
    const applied: string[] = [];
    const pushes: [string, string][] = [];
    for (const op of Array.isArray(body.outbox) ? body.outbox.slice(0, 200) : []) {
      try {
        const at = Math.min(Number(op.at) || now, now);
        if (op.op === 'msg') {
          const text = String(op.text || '').trim().slice(0, 8000);
          if (text) {
            const r = await env.DB.prepare(
              'INSERT OR IGNORE INTO messages(sender,body,speak,cid,created_at,device) VALUES(?,?,0,?,?,?)',
            )
              .bind(op.sender === 'he' ? 'he' : 'con', text, String(op.cid), at, D)
              .run();
            if (r.meta.changes) pushes.push([op.sender === 'he' ? `Máy của ${dev.name}` : `${dev.name} nhắn`, text]);
          }
        } else if (op.op === 'task_add') {
          const title = String(op.title || '').trim().slice(0, 2000);
          if (title) {
            const r = await env.DB.prepare(
              'INSERT OR IGNORE INTO tasks(id,title,due_at,created_by,updated_at,device) VALUES(?,?,?,?,?,?)',
            )
              .bind(String(op.id), title, op.due_at ? Number(op.due_at) : null, 'con', at, D)
              .run();
            if (r.meta.changes) pushes.push([`${dev.name} thêm việc`, title]);
          }
        } else if (op.op === 'task_done') {
          const t = await env.DB.prepare('SELECT * FROM tasks WHERE id=? AND deleted=0 AND device=?').bind(String(op.id), D).first<any>();
          if (t && !!t.done_at !== !!op.done) {
            await env.DB.prepare('UPDATE tasks SET done_at=?, updated_at=? WHERE id=?')
              .bind(op.done ? at : null, now, t.id)
              .run();
            pushes.push([op.done ? `${dev.name} đã xong ✓` : `${dev.name} bỏ dấu xong`, t.title]);
          }
        } else if (op.op === 'task_del') {
          // The child may delete only the tasks he added himself.
          await env.DB.prepare("UPDATE tasks SET deleted=1, updated_at=? WHERE id=? AND created_by='con' AND device=?")
            .bind(now, String(op.id), D)
            .run();
        } else if (op.op === 'delivered') {
          await env.DB.prepare(
            `UPDATE messages SET delivered_at=COALESCE(delivered_at, ?) WHERE sender IN ${PARENTS} AND id<=? AND device=?`,
          )
            .bind(at, Number(op.upto), D)
            .run();
        } else if (op.op === 'read') {
          await env.DB.prepare(
            `UPDATE messages SET read_at=COALESCE(read_at, ?), delivered_at=COALESCE(delivered_at, ?) WHERE sender IN ${PARENTS} AND id<=? AND device=?`,
          )
            .bind(at, at, Number(op.upto), D)
            .run();
        }
        applied.push(String(op.cid));
      } catch (e) {
        console.log('op failed', JSON.stringify(op), String(e));
      }
    }
    if (body.status) await kvSet(env, dk('status', D), { ...body.status, at: now });
    await kvSet(env, dk('last_seen', D), now);
    for (const [t, b] of pushes.slice(-3)) ctx.waitUntil(notifyParent(env, t, b));

    const ver = await verOf(env, D);   // read before the data, so a change after this is never missed
    const since = Number(body.msg_since || 0);
    const msgs = await env.DB.prepare(
      since
        ? 'SELECT * FROM messages WHERE id>? AND device=? ORDER BY id LIMIT 200'
        : 'SELECT * FROM (SELECT * FROM messages WHERE device=? ORDER BY id DESC LIMIT 100) ORDER BY id',
    )
      .bind(...(since ? [since, D] : [D]))
      .all();
    const tasks = await env.DB.prepare('SELECT * FROM tasks WHERE deleted=0 AND device=?').bind(D).all();
    const cmds = await env.DB.prepare(
      'SELECT id, kind FROM commands WHERE done_at IS NULL AND created_at>? AND device=? ORDER BY id',
    )
      .bind(now - 300_000, D)
      .all();
    return json({
      applied,
      messages: msgs.results,
      tasks: tasks.results,
      commands: cmds.results,
      lock: (await kvGet(env, dk('lock', D))) || { on: false },
      call: await openCall(env, ctx, D),
      device: dev,
      ver,
      now,
    });
  }

  // Long-poll: hold until something for this computer changes (or WAIT_MAX_S), then the agent runs /agent/sync.
  if (path === '/agent/wait' && req.method === 'GET') {
    const qs = new URL(req.url).searchParams;
    const known = Number(qs.get('ver') || 0);
    const end = now + Math.max(0, Math.min(WAIT_MAX_S, Number(qs.get('t')) || WAIT_MAX_S)) * 1000;
    await kvSet(env, dk('last_seen', D), now);
    let v = await verOf(env, D);
    while (v === known && Date.now() < end) {
      await sleep(1000);
      v = await verOf(env, D);
    }
    return json({ ver: v, changed: v !== known });
  }

  if (path === '/agent/ice') return json({ iceServers: await iceServers(env) });
  if (path === '/agent/call' && req.method === 'POST') {
    const body: any = await req.json().catch(() => ({}));
    if (!sdpOk(body.offer)) return bad('offer?');
    if (await openCall(env, ctx, D)) return bad('Đang có cuộc gọi khác', 409);
    const id = randomId(9);
    await env.DB.prepare("INSERT INTO calls(id,dir,caller,state,offer,created_at,k_seen,device) VALUES(?,'up','con','ringing',?,?,?,?)")
      .bind(id, body.offer, now, now, D)
      .run();
    ctx.waitUntil(notifyParent(env, `📞 ${dev.name} đang gọi`, 'Mở app để nghe máy'));
    return json({ ok: true, id });
  }
  const ac = path.match(/^\/agent\/call\/([\w-]+)(\/answer|\/end)?$/);
  if (ac) {
    await sweepCalls(env, ctx);
    const c = await getCall(env, ac[1]);
    if (!c || c.device !== D) return bad('no call', 404);
    if (!ac[2] && req.method === 'GET') {
      if (c.state !== 'ended') await env.DB.prepare('UPDATE calls SET k_seen=? WHERE id=?').bind(now, c.id).run();
      return json({ ...c, offer: c.dir === 'down' ? c.offer : undefined, answer: c.dir === 'up' ? c.answer : undefined });
    }
    const body: any = await req.json().catch(() => ({}));
    if (ac[2] === '/answer' && req.method === 'POST') {
      if (!sdpOk(body.answer)) return bad('answer?');
      const r = await env.DB.prepare(
        "UPDATE calls SET state='active', answer=?, answerer='con', answered_at=?, p_seen=?, k_seen=? WHERE id=? AND state='ringing' AND dir='down'",
      )
        .bind(body.answer, now, now, now, c.id)
        .run();
      if (!r.meta.changes) return bad('Cuộc gọi đã kết thúc', 409);
      return json({ ok: true });
    }
    if (ac[2] === '/end' && req.method === 'POST') {
      let reason = 'hangup';
      if (c.state === 'ringing') reason = c.dir === 'up' ? 'cancelled' : ['locked', 'nologin'].includes(body.reason) ? body.reason : 'declined';
      if (c.state !== 'ended') await endCall(env, ctx, c, reason, 'con');
      return json({ ok: true });
    }
  }

  if (path === '/agent/voice' && req.method === 'PUT') {
    const qs = new URL(req.url).searchParams;
    const cid = String(qs.get('cid') || '');
    if (!/^[\w-]{4,40}$/.test(cid)) return bad('cid?');
    const dup = await env.DB.prepare('SELECT id FROM messages WHERE cid=? AND device=?').bind(cid, D).first<{ id: number }>();
    if (dup) return json({ ok: true, id: dup.id });
    const buf = await req.arrayBuffer();
    if (buf.byteLength < 1000 || buf.byteLength > VOICE_MAX) return bad('size', 413);
    const key = `c-${cid}`;
    await putVoice(env, key, buf);
    const at = Math.min(Number(qs.get('at')) || now, now);
    const r = await env.DB.prepare(
      'INSERT OR IGNORE INTO messages(sender,body,speak,audio,audio_ms,cid,created_at,device) VALUES(?,?,0,?,?,?,?,?) RETURNING id',
    )
      .bind('con', '', key, Number(qs.get('ms')) || null, cid, at, D)
      .first<{ id: number }>();
    if (r) {
      const secs = Math.round((Number(qs.get('ms')) || 0) / 1000);
      ctx.waitUntil(notifyParent(env, `${dev.name} trả lời 🎤`, `Tin nhắn thoại${secs ? ` ${secs} giây` : ''} — mở để nghe`));
    }
    return json({ ok: true, id: r?.id });
  }
  const av = path.match(/^\/agent\/voice\/([\w-]+)$/);
  if (av && req.method === 'GET') return getVoice(env, av[1]);

  const shot = path.match(/^\/agent\/shot\/(\d+)$/);
  if (shot && req.method === 'PUT') {
    const id = Number(shot[1]);
    if (!(await env.DB.prepare('SELECT 1 FROM commands WHERE id=? AND device=?').bind(id, D).first())) return bad('no command', 404);
    const err = req.headers.get('x-error');
    if (err) {
      let msg = err;
      try { msg = decodeURIComponent(err); } catch {}
      await env.DB.prepare('UPDATE commands SET done_at=?, result=? WHERE id=?').bind(now, msg.slice(0, 300), id).run();
      return json({ ok: true });
    }
    const buf = await req.arrayBuffer();
    if (buf.byteLength > 10_000_000) return bad('too big', 413);
    await env.KV.put(`shot:${id}`, buf, { expirationTtl: 2 * 86400 });
    await env.DB.prepare('UPDATE commands SET done_at=?, result=? WHERE id=?').bind(now, 'ok', id).run();
    return json({ ok: true });
  }

  return bad('not found', 404);
}

// ---------- cron: overdue tasks + agent gone quiet ----------
async function cron(env: Env) {
  const now = Date.now();
  const { results } = await env.DB.prepare(
    'SELECT * FROM tasks WHERE deleted=0 AND done_at IS NULL AND due_at IS NOT NULL AND due_at<? AND overdue_notified=0',
  )
    .bind(now)
    .all<any>();
  for (const t of results) {
    await env.DB.prepare('UPDATE tasks SET overdue_notified=1 WHERE id=?').bind(t.id).run();
    const name = (await devices(env)).find((d) => d.id === t.device)?.name || 'con';
    await notifyParent(env, `Quá hạn, ${name} chưa xong`, t.title);
  }
}

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    FIRST = env.FIRST_DEVICE || 'kid1';
    const path = new URL(req.url).pathname;
    try {
      if (path.startsWith('/auth/')) return await handleAuth(req, env, path);
      if (path.startsWith('/api/')) return await handleApi(req, env, ctx, path);
      if (path.startsWith('/agent/')) return await handleAgent(req, env, ctx, path);
    } catch (e) {
      console.log('error', path, String(e), (e as Error).stack);
      return bad('Lỗi máy chủ', 500);
    }
    return env.ASSETS.fetch(req);
  },
  async scheduled(_ev: ScheduledEvent, env: Env, ctx: ExecutionContext) {
    FIRST = env.FIRST_DEVICE || 'kid1';
    ctx.waitUntil(cron(env));
  },
};

"""Gọi điện - Nhắn tin con agent for Windows.

Two processes, one file:
  kids_talk_win.py service   SYSTEM (scheduled task at startup): syncs with the relay, keeps the local state,
                      makes the Vietnamese speech (Piper), serves the child's page on http://127.0.0.1:8765
                      and hands work to the helper.
  kids_talk_win.py helper    the child's own session (scheduled task at his logon, pythonw): popups, sound,
                      Edge windows for calls, which app is in front, screenshots. It long-polls the service.
If the child kills the helper, the service starts it again (schtasks /run).
"""
import ctypes
import ctypes.wintypes as W
import datetime as dt
import http.server
import json
import math
import os
import queue
import random
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import wave

VERSION = 'w4'
APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('KT_DATA', r'C:\ProgramData\KidsTalk')
AUDIO = os.path.join(DATA, 'audio')          # readable by the child (helper plays these)
SHOTS = os.path.join(DATA, 'shots')          # writable by the child (helper saves screenshots)
try:   # the helper (child) cannot read agent.json: it holds the relay token
    CONF = json.load(open(os.path.join(DATA, 'agent.json'), encoding='utf-8-sig'))
except OSError:
    CONF = {}
URL = CONF.get('url', '').rstrip('/')
TOKEN = CONF.get('token', '')
KID = CONF.get('kid_user', '')   # the child's local Windows account (required, set by install.ps1)
PORT = int(CONF.get('port', 8765))
VOICE = CONF.get('voice', os.path.join(DATA, 'voices', 'vi_VN-vais1000-medium.onnx'))
EDGE = CONF.get('edge', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
HELPER_TASK = 'KidsTalk Helper'
SYNC_EVERY = 60     # full sync for the status; changes arrive through the long-poll (/agent/wait)
WAIT_S = 25
SAMPLE_EVERY = 10
IDLE_AWAY = 120
RING_MAX = 58
PARENT = {'bo': 'Bố', 'me': 'Mẹ'}
PARENTS_SQL = "('bo','me')"
CALL_GAIN = float(CONF.get('call_gain', 1.5))
VOLUME = float(CONF.get('volume', 0.85))   # speakers are raised to at least this while we talk
LISTEN_WAIT = float(CONF.get('listen_wait', 10))      # s to start talking, else nothing is sent
LISTEN_SILENCE = float(CONF.get('listen_silence', 2))  # s of silence that ends the answer
LISTEN_MAX = float(CONF.get('listen_max', 60))
MIC_GAIN = float(CONF.get('mic_gain', 2.0))
SPEECH_P = float(CONF.get('speech_p', 0.7))
REPEAT_GAP = 10
REPEAT_MAX = 3600
VAD_MODEL = os.path.join(DATA, 'voices', 'silero_vad.onnx')
BASE = f'http://127.0.0.1:{PORT}'

# ---------------------------------------------------------------- end-to-end encryption
# One family key (AES-256-GCM, 32 bytes, base64url in the config as "key") is shared by the parents'
# phones and the children's computers. It never goes to the relay: the relay stores only "e1:…" text and
# "KT1…" blobs. With a key set, content a parent "sent" in plaintext is refused (only the relay could
# have written it). Same format as worker/public/crypto.js.
FKEY = CONF.get('key') or ''
E2E_AAD = b'kids-talk/1'
E2E_MAGIC = b'KT1'
_AES = None
if FKEY:
    import base64 as _b64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # pip install cryptography
    _AES = AESGCM(_b64.urlsafe_b64decode(FKEY + '=' * (-len(FKEY) % 4)))
    assert len(_b64.urlsafe_b64decode(FKEY + '=' * (-len(FKEY) % 4))) == 32, 'family key must be 32 bytes'


def is_enc(s):
    return isinstance(s, str) and s.startswith('e1:')


def enc_text(s):
    if not _AES or not s:
        return s
    iv = os.urandom(12)
    return 'e1:' + _b64.urlsafe_b64encode(iv + _AES.encrypt(iv, s.encode(), E2E_AAD)).rstrip(b'=').decode()


def dec_text(s):
    """Plain text of an "e1:" string; None when it cannot be read (no key, wrong key, tampered)."""
    if not is_enc(s):
        return s
    if not _AES:
        return None
    try:
        b = _b64.urlsafe_b64decode(s[3:] + '=' * (-len(s[3:]) % 4))
        return _AES.decrypt(b[:12], b[12:], E2E_AAD).decode()
    except Exception:  # noqa: BLE001
        return None


def open_text(s, from_parent=True):
    """Decrypt; with a family key, plaintext that claims to come from a parent is refused (None)."""
    if is_enc(s):
        return dec_text(s)
    if _AES and from_parent and s:
        return None
    return s


def enc_blob(b):
    if not _AES or not b:
        return b
    iv = os.urandom(12)
    return E2E_MAGIC + iv + _AES.encrypt(iv, b, E2E_AAD)


def dec_blob(b, from_parent=True):
    """Plain bytes of a "KT1" blob; None if unreadable, or plaintext from a parent while a key is set."""
    if not b or b[:3] != E2E_MAGIC:
        return None if (_AES and from_parent) else b
    if not _AES:
        return None
    try:
        return _AES.decrypt(b[3:15], b[15:], E2E_AAD)
    except Exception:  # noqa: BLE001
        return None


def seal_op(op):
    op = dict(op)
    if op.get('op') == 'msg' and op.get('text'):
        op['text'] = enc_text(op['text'])
    elif op.get('op') == 'task_add' and op.get('title'):
        op['title'] = enc_text(op['title'])
    return op


def seal_status(st):
    """Everything but the agent version and the lock state goes inside the encrypted "e" field."""
    if not _AES:
        return st
    plain = {k: st[k] for k in ('agent', 'lock') if k in st}
    plain['e'] = enc_text(json.dumps({k: v for k, v in st.items() if k not in plain}))
    return plain


UNSEALED = '🔓 Tin không mã hoá hoặc không đọc được: đã bỏ qua'


def open_message(m):
    m = dict(m)
    parent = m.get('sender') in ('bo', 'me')
    body = open_text(m.get('body') or '', parent)
    if body is None:
        m.update(body=UNSEALED, speak=0, repeat_play=0, audio=None)
    else:
        m['body'] = body
    return m


def open_task(t):
    t = dict(t)
    title = open_text(t.get('title') or '', t.get('created_by') != 'con')
    t['title'] = '🔓 (việc không mã hoá)' if title is None else title
    return t


def open_lock(L):
    L = dict(L or {})
    if L.get('msg'):
        L['msg'] = open_text(L['msg']) or ''
    return L


def open_call(j):
    """SDP from the relay: decrypt; a plaintext offer/answer is refused while a key is set (no MITM)."""
    if isinstance(j, dict):
        j = dict(j)
        for k in ('offer', 'answer'):
            if j.get(k):
                j[k] = open_text(j[k])
    return j



def log(*a):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a)
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001  (pythonw has no console)
        pass
    try:
        d = os.path.join(os.environ.get('LOCALAPPDATA', DATA), 'KidsTalk') if MODE == 'helper' else os.path.join(DATA, 'logs')
        with open(os.path.join(d, f'{MODE}.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def now_ms():
    return int(time.time() * 1000)


MODE = sys.argv[1] if len(sys.argv) > 1 else 'service'


# ==================================================================================== SERVICE
# ------------------------------------------------------------ speakers + media (Windows Core Audio, media keys API)
def _endpoint():
    """The default speakers' volume control (pycaw). Works from the child's session and from SYSTEM."""
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    d = AudioUtilities.GetSpeakers()
    ev = getattr(d, 'EndpointVolume', None)
    if ev is not None:
        return ev
    return ctypes.cast(d.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None), ctypes.POINTER(IAudioEndpointVolume))


def loud():
    """Unmute and raise the speakers to VOLUME at least. Returns what to put back (or None)."""
    try:
        ev = _endpoint()
        saved = (float(ev.GetMasterVolumeLevelScalar()), int(ev.GetMute()))
        ev.SetMute(0, None)
        if saved[0] < VOLUME:
            ev.SetMasterVolumeLevelScalar(VOLUME, None)
        return saved
    except Exception as e:  # noqa: BLE001
        log('volume failed', repr(e))
        return None


def unloud(saved):
    if not saved:
        return
    try:
        ev = _endpoint()
        ev.SetMasterVolumeLevelScalar(saved[0], None)
        ev.SetMute(saved[1], None)
    except Exception as e:  # noqa: BLE001
        log('volume restore failed', repr(e))


def _media(fn):
    import asyncio
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M

    async def go():
        return await fn(await M.request_async())
    return asyncio.run(go())


def media_pause():
    """Pause every player that is playing (YouTube in Edge/Chrome, Spotify…). Returns the apps to resume."""
    async def f(m):
        out = []
        for x in m.get_sessions():
            if int(x.get_playback_info().playback_status) == 4 and await x.try_pause_async():   # 4 = Playing
                out.append(x.source_app_user_model_id)
        return out
    try:
        r = _media(f) or []
        if r:
            log('paused', r)
        return r
    except Exception as e:  # noqa: BLE001
        log('media pause failed', repr(e))
        return []


def media_resume(apps):
    if not apps:
        return

    async def f(m):
        for x in m.get_sessions():
            if x.source_app_user_model_id in apps and int(x.get_playback_info().playback_status) == 5:   # 5 = Paused
                await x.try_play_async()
    try:
        _media(f)
    except Exception as e:  # noqa: BLE001
        log('media resume failed', repr(e))


class Hold:
    """While anyone holds it, the speakers are loud and the child's media is paused. The last release puts
    things back after `linger` s, so a repeated message or play-then-listen does not toggle the music."""

    def __init__(self, pause=True, linger=12):
        self.n, self.saved, self.apps, self.pause, self.linger = 0, None, [], pause, linger
        self.mx, self.gen = threading.Lock(), 0

    def hold(self):
        with self.mx:
            self.n += 1
            self.gen += 1
            if self.n == 1 and self.saved is None and not self.apps:
                self.apps = media_pause() if self.pause else []
                self.saved = loud()

    def release(self, now=False):
        with self.mx:
            self.n = max(0, self.n - 1)
            gen = self.gen

        def back():
            with self.mx:
                if self.n or self.gen != gen:
                    return
                saved, apps, self.saved, self.apps = self.saved, self.apps, None, []
            unloud(saved)
            media_resume(apps)
        if now:
            back()
        else:
            threading.Timer(self.linger, back).start()


def console_spawn(argv):
    """CreateProcessAsUser with our SYSTEM token moved to the console session. Returns the process handle."""
    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    k32.GetCurrentProcess.restype = W.HANDLE
    tok, dup = W.HANDLE(), W.HANDLE()
    if not adv.OpenProcessToken(W.HANDLE(k32.GetCurrentProcess()), 0xF01FF, ctypes.byref(tok)):   # TOKEN_ALL_ACCESS
        raise ctypes.WinError()
    try:
        if not adv.DuplicateTokenEx(tok, 0x02000000, None, 2, 1, ctypes.byref(dup)):   # MAXIMUM_ALLOWED, impersonation, primary
            raise ctypes.WinError()
    finally:
        k32.CloseHandle(tok)
    try:
        sid = W.DWORD(k32.WTSGetActiveConsoleSessionId())
        if not adv.SetTokenInformation(dup, 12, ctypes.byref(sid), 4):   # TokenSessionId (needs SeTcbPrivilege)
            raise ctypes.WinError()

        class SI(ctypes.Structure):
            _fields_ = [('cb', W.DWORD), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR), ('lpTitle', W.LPWSTR),
                        ('dwX', W.DWORD), ('dwY', W.DWORD), ('dwXSize', W.DWORD), ('dwYSize', W.DWORD),
                        ('dwXCountChars', W.DWORD), ('dwYCountChars', W.DWORD), ('dwFillAttribute', W.DWORD),
                        ('dwFlags', W.DWORD), ('wShowWindow', W.WORD), ('cbReserved2', W.WORD),
                        ('lpReserved2', ctypes.c_void_p), ('hStdInput', W.HANDLE), ('hStdOutput', W.HANDLE),
                        ('hStdError', W.HANDLE)]

        class PI(ctypes.Structure):
            _fields_ = [('hProcess', W.HANDLE), ('hThread', W.HANDLE), ('dwProcessId', W.DWORD), ('dwThreadId', W.DWORD)]
        si, pi = SI(), PI()
        si.cb = ctypes.sizeof(SI)
        si.lpDesktop = 'winsta0\\winlogon'
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        if not adv.CreateProcessAsUserW(dup, None, cmd, None, None, False, 0x08000000, None, APP,   # CREATE_NO_WINDOW
                                        ctypes.byref(si), ctypes.byref(pi)):
            raise ctypes.WinError()
        k32.CloseHandle(pi.hThread)
        return pi.hProcess
    finally:
        k32.CloseHandle(dup)


def play_main(paths):
    """`kids_talk_win.py play a.wav b.wav`: started in the console session by the service (login screen)."""
    import winsound
    saved = loud()
    try:
        for p in paths:
            winsound.PlaySound(p, winsound.SND_FILENAME)
    finally:
        unloud(saved)


def service_main():
    import sqlite3
    os.makedirs(os.path.join(DATA, 'logs'), exist_ok=True)
    os.makedirs(os.path.join(AUDIO, 'voice'), exist_ok=True)
    os.makedirs(SHOTS, exist_ok=True)
    db = sqlite3.connect(os.path.join(DATA, 'state.db'), check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    dblock = threading.RLock()

    def q(sql, *args):
        with dblock:
            return db.execute(sql, args).fetchall()

    def q1(sql, *args):
        r = q(sql, *args)
        return r[0] if r else None

    with dblock:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, sender TEXT, body TEXT, speak INTEGER,
          created_at INTEGER, delivered_at INTEGER, read_at INTEGER, audio TEXT, audio_ms INTEGER, repeat_play INTEGER);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE IF NOT EXISTS outbox(cid TEXT PRIMARY KEY, op TEXT, at INTEGER);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS activity(day TEXT, app TEXT, title TEXT, sec INTEGER, PRIMARY KEY(day, app, title));
        CREATE TABLE IF NOT EXISTS recent(at INTEGER, app TEXT, title TEXT);
        CREATE TABLE IF NOT EXISTS reminded(task_id TEXT, due_at INTEGER, PRIMARY KEY(task_id, due_at));
        CREATE TABLE IF NOT EXISTS voice_out(cid TEXT PRIMARY KEY, path TEXT, ms INTEGER, at INTEGER);
        ''')
    os.makedirs(os.path.join(DATA, 'voice-out'), exist_ok=True)

    def meta(k, default=None):
        r = q1('SELECT v FROM meta WHERE k=?', k)
        return json.loads(r['v']) if r else default

    def set_meta(k, v):
        q('INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', k, json.dumps(v))

    WAKE = threading.Event()

    def enqueue(op):
        cid = op.get('cid') or secrets.token_urlsafe(9)
        op['cid'] = cid
        op.setdefault('at', now_ms())
        q('INSERT OR REPLACE INTO outbox(cid, op, at) VALUES(?,?,?)', cid, json.dumps(op), op['at'])
        WAKE.set()
        return cid

    # ------------------------------------------------------------ relay
    def relay(method, path, body=None, timeout=15, raw=None, ctype='application/json'):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(URL + path, data=data, method=method,
                                     headers={'Authorization': f'Bearer {TOKEN}', 'content-type': ctype,
                                              'user-agent': 'kids-talk-win/' + VERSION})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                return r.status, (json.loads(b) if b[:1] in (b'{', b'[') else b)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except ValueError:
                return e.code, {'error': str(e.code)}

    # ------------------------------------------------------------ helper channel
    H = {'q': [], 'cv': threading.Condition(), 'seen': 0.0, 'status': {}, 'events': queue.Queue(), 'started': 0.0}

    def to_helper(**action):
        with H['cv']:
            H['q'].append(action)
            H['cv'].notify_all()

    def helper_alive():
        return time.time() - H['seen'] < 25

    RESP = {}

    def ask_helper(timeout, stop=None, **action):
        """Give the helper a job and wait for its answer (None on timeout, stop, or no helper)."""
        rid = secrets.token_urlsafe(6)
        ev = threading.Event()
        RESP[rid] = {'ev': ev}
        to_helper(rid=rid, **action)
        end = time.time() + timeout
        while not ev.wait(0.2):
            if time.time() > end or (stop is not None and stop.is_set()) or not helper_alive():
                to_helper(kind='stop', target=rid)
                break
        return RESP.pop(rid, {}).get('result')

    # ------------------------------------------------------------ the child's session (WTS)
    wts = ctypes.WinDLL('wtsapi32')

    class WTS_SESSION_INFO(ctypes.Structure):
        _fields_ = [('SessionId', W.DWORD), ('pWinStationName', W.LPWSTR), ('State', ctypes.c_int)]

    def kid_sessions():
        """[(session id, state)] of the child's logons; state 0 = on the screen, 4 = disconnected."""
        out = []
        p, n = ctypes.POINTER(WTS_SESSION_INFO)(), W.DWORD()
        if not wts.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(p), ctypes.byref(n)):
            return out
        try:
            for i in range(n.value):
                s = p[i]
                buf, ln = W.LPWSTR(), W.DWORD()
                if wts.WTSQuerySessionInformationW(None, s.SessionId, 5, ctypes.byref(buf), ctypes.byref(ln)):  # WTSUserName
                    user = buf.value or ''
                    wts.WTSFreeMemory(buf)
                    if user.lower() == KID.lower():
                        out.append((s.SessionId, s.State))
        finally:
            wts.WTSFreeMemory(p)
        return out

    def kid_session():
        ss = kid_sessions()
        return ss[0][0] if ss else None

    # ------------------------------------------------------------ hard lock
    # The relay holds the wanted state {on, until, msg, by}; we make the PC match it on every sync and sample
    # tick, from the last state we saw, so `until` unlocks him even offline or after a reboot.
    # Locking = his account is disabled (Windows refuses his password at login) + his session is sent back to
    # the login screen (disconnected: his apps keep running, he cannot get back in until it is enabled).
    # The admin account is never touched; as admin: `Get-LocalUser bot | Enable-LocalUser` is the manual way out.
    LOCKMX = threading.Lock()

    def lock_wanted(L):
        return bool(L and L.get('on') and (not L.get('until') or now_ms() < L['until']))

    def hhmm(ms):
        return time.strftime('%H:%M', time.localtime(ms / 1000))

    def set_account(enabled):
        cmd = f"Get-LocalUser -Name '{KID}' | {'Enable' if enabled else 'Disable'}-LocalUser -ErrorAction Stop"
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', cmd], capture_output=True,
                           text=True, timeout=90, creationflags=0x08000000)
        if r.returncode:
            log('account', 'enable' if enabled else 'disable', 'failed', (r.stderr or r.stdout).strip()[:300])
        return r.returncode == 0

    def kick():
        """Send his on-screen session back to the login screen."""
        for sid, state in kid_sessions():
            if state == 0:
                ok = wts.WTSDisconnectSession(None, sid, False)
                log('session', sid, 'disconnected' if ok else f'disconnect failed {ctypes.GetLastError()}')

    def reconcile_lock(L=None):
        with LOCKMX:
            if L is not None:
                set_meta('lock_want', L)
            L = meta('lock_want', {}) or {}
            want, applied = lock_wanted(L), meta('lock_applied', False)
            if want and not applied:
                if not set_account(False):
                    return
                set_meta('lock_applied', True)
                name = PARENT.get(L.get('by'), 'Bố')
                until = f'đến {hhmm(L["until"])}' if L.get('until') else f'đến khi {name.lower()} mở'
                log('locked', until)
                stop_repeat('locked')
                if helper_alive():
                    to_helper(kind='notify', title=f'🔒 {name} đã khóa máy', body=(L.get('msg') or 'Máy khóa') + f' — {until}.',
                              buttons=[])
                    to_helper(kind='play', paths=[chime()])
                    time.sleep(4)
                kick()
                enqueue({'op': 'msg', 'sender': 'he', 'text': f'🔒 Máy của con đã khóa {until}'})
            elif not want and applied:
                if not set_account(True):
                    return
                set_meta('lock_applied', False)
                expired = L.get('on') and L.get('until')
                log('unlocked', 'time up' if expired else 'by parent')
                enqueue({'op': 'msg', 'sender': 'he',
                         'text': '🔓 Hết giờ khóa, máy đã mở' if expired else '🔓 Máy của con đã mở khóa'})
            elif want and applied and any(st == 0 for _, st in kid_sessions()):
                kick()   # he got back in somehow (or the disconnect failed): out again

    def keep_helper():
        """Start the helper in his session if it is not talking to us."""
        if kid_session() is not None and not helper_alive() and time.time() - H['started'] > 30:
            H['started'] = time.time()
            r = subprocess.run(['schtasks', '/run', '/tn', HELPER_TASK], capture_output=True, text=True)
            log('helper (re)start', r.returncode, (r.stdout + r.stderr).strip()[:200])

    # ------------------------------------------------------------ speech
    tts_lock = threading.Lock()
    voice_holder = {}

    def tts(text, fname='say.wav'):
        try:
            with tts_lock:
                if 'v' not in voice_holder:
                    from piper import PiperVoice
                    voice_holder['v'] = PiperVoice.load(VOICE)
                p = os.path.join(AUDIO, fname)
                with wave.open(p, 'wb') as w:
                    voice_holder['v'].synthesize_wav(text, w)
                return p
        except Exception as e:  # noqa: BLE001
            log('tts failed', repr(e))
            return None

    def tone(name, parts):
        p = os.path.join(AUDIO, name)
        if os.path.exists(p):
            return p
        rate, frames = 22050, bytearray()
        for f, ms in parts:
            n = int(rate * ms / 1000)
            for i in range(n):
                env = min(1, i / 200, (n - i) / 400)
                v = int(0.55 * 32767 * env * math.sin(2 * math.pi * f * i / rate)) if f else 0
                frames += struct.pack('<h', v)
        with wave.open(p, 'wb') as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(bytes(frames))
        return p

    chime = lambda: tone('chime.wav', ((880, 180), (0, 40), (1175, 260)))
    ring = lambda: tone('ring.wav', ((988, 400), (0, 120), (784, 400), (0, 1100)))
    beep = lambda: tone('beep.wav', ((1046, 220),))

    def voice_file(key):
        if not re.fullmatch(r'[\w-]{4,64}', key or ''):
            return None
        p = os.path.join(AUDIO, 'voice', key + '.wav')
        if os.path.exists(p):
            return p
        st, data = relay('GET', f'/agent/voice/{key}', timeout=60)
        if st != 200 or not isinstance(data, bytes):
            log('voice get failed', key, st)
            return None
        data = dec_blob(data, from_parent=not key.startswith('c-'))
        if data is None:
            log('voice refused (not encrypted with the family key)', key)
            return None
        with open(p + '.tmp', 'wb') as f:
            f.write(data)
        os.replace(p + '.tmp', p)
        return p

    # ------------------------------------------------------------ messages read aloud
    SAY = queue.Queue()
    QUICK = {'ok': 'Dạ, con nghe rồi ạ', 'later': 'Con xin 5 phút nữa ạ'}

    def mark_read_local(upto):
        q(f"UPDATE messages SET read_at=COALESCE(read_at, ?) WHERE sender IN {PARENTS_SQL} AND id<=?", now_ms(), upto)

    REPEAT = {}

    def add_kid_message(text):
        text = text.strip()[:1000]
        if text:
            enqueue({'op': 'msg', 'text': text})
            stop_repeat('answered')

    def stop_repeat(why='stopped'):
        if REPEAT.get('stop') and not REPEAT['stop'].is_set():
            REPEAT['why'] = why
            REPEAT['stop'].set()

    def screen_locked():
        return bool(H['status'].get('locked'))

    def play_wait(paths, repeat=1, stop=None):
        paths = [x for x in paths if x]
        ask_helper(30 + 120 * repeat, stop=stop, kind='play', paths=paths, repeat=repeat)

    def listen_and_send(stop=None):
        """Beep, record his spoken answer (in his session) and queue it for the parent. True when sent."""
        r = ask_helper(LISTEN_WAIT + LISTEN_MAX + 20, stop=stop, kind='listen', beep=beep(), wait=LISTEN_WAIT,
                       silence=LISTEN_SILENCE, max=LISTEN_MAX, gain=MIC_GAIN, speech_p=SPEECH_P)
        return bool(r)

    def save_answer(data, ms):
        cid = 'v' + secrets.token_urlsafe(9)
        path = os.path.join(DATA, 'voice-out', cid + '.wav')
        with open(path, 'wb') as f:
            f.write(data)
        with open(os.path.join(AUDIO, 'voice', f'c-{cid}.wav'), 'wb') as f:   # same key the relay uses
            f.write(data)
        q('INSERT INTO voice_out(cid,path,ms,at) VALUES(?,?,?,?)', cid, path, ms, now_ms())
        log('listen: answer', ms, 'ms')
        WAKE.set()

    def upload_voices():
        for r in q('SELECT * FROM voice_out ORDER BY at LIMIT 3'):
            try:
                data = open(r['path'], 'rb').read()
            except OSError:
                q('DELETE FROM voice_out WHERE cid=?', r['cid'])
                continue
            qs = urllib.parse.urlencode({'cid': r['cid'], 'ms': r['ms'], 'at': r['at']})
            try:
                st, j = relay('PUT', f'/agent/voice?{qs}', raw=enc_blob(data), ctype='audio/wav', timeout=90)
            except Exception as e:  # noqa: BLE001
                log('voice upload failed', repr(e))
                return
            if st != 200:
                log('voice upload http', st)
                if 400 <= st < 500 and st not in (401, 408, 429):
                    q('DELETE FROM voice_out WHERE cid=?', r['cid'])
                continue
            q('DELETE FROM voice_out WHERE cid=?', r['cid'])
            os.unlink(r['path'])

    def repeat_loop(speech, stop):
        end = time.time() + REPEAT_MAX
        reason = 'timeout'
        while True:
            play_wait([chime(), speech], stop=stop)
            REPEAT['n'] += 1
            if stop.is_set():
                reason = REPEAT.get('why', 'stopped')
                break
            if screen_locked():
                stop.wait(REPEAT_GAP)
            elif listen_and_send(stop):
                reason = 'answered'
                break
            if stop.is_set():
                reason = REPEAT.get('why', 'stopped')
            elif not SAY.empty():
                reason = 'new'
            elif not helper_alive():
                reason = 'logout'
            elif time.time() > end:
                reason = 'timeout'
            else:
                continue
            break
        n = REPEAT['n']
        log('repeat ended', reason, n)
        note = {
            'stopped': f'Con đã bấm dừng phát lặp lại (nghe {n} lần)',
            'answered': f'Con đã trả lời, dừng phát lặp lại (nghe {n} lần)',
            'new': f'Dừng phát lặp lại vì có tin mới (đã phát {n} lần)',
            'logout': f'Con đã đăng xuất, dừng phát lặp lại (đã phát {n} lần)',
            'locked': f'Máy đã khóa, dừng phát lặp lại (đã phát {n} lần)',
            'timeout': f'Đã phát lặp lại 1 giờ ({n} lần), con chưa dừng',
        }.get(reason, f'Dừng phát lặp lại (đã phát {n} lần)')
        enqueue({'op': 'msg', 'text': note, 'sender': 'he'})

    def announcer():
        while True:
            kind, text, msg_id, audio, repeat, sender = SAY.get()
            name = PARENT.get(sender, 'Bố')
            try:
                if not helper_alive():
                    continue
                if kind in ('msg', 'quiet'):
                    buttons = ([['stop', '⏸ Dừng phát'], ['ok', QUICK['ok']], ['open', 'Mở trả lời']] if repeat and kind == 'msg'
                               else [['ok', QUICK['ok']], ['later', QUICK['later']], ['open', 'Mở trả lời']])
                    to_helper(kind='notify', title=f'{name} nhắn' + (' 🔁' if repeat and kind == 'msg' else ''),
                              body=text or f'🎤 {name} gửi tin nhắn thoại', msg_id=msg_id, buttons=buttons)
                if kind == 'msg':
                    clip = voice_file(audio) if audio else None
                    speech = clip or tts(f'{name} nhắn: ' + (text or f'{name.lower()} gửi tin nhắn thoại, con mở chương trình nhắn tin để nghe'))
                    if repeat:
                        stop = threading.Event()
                        REPEAT.update(id=msg_id, body=text or '🎤 tin nhắn thoại', n=0, stop=stop, name=name)
                        try:
                            repeat_loop(speech, stop)
                        finally:
                            REPEAT.clear()
                    else:
                        away = (H['status'].get('idle_s') or 0) > 60 or screen_locked()
                        play_wait([chime(), speech], repeat=2 if away else 1)
                        if not screen_locked() and helper_alive():
                            listen_and_send()
                elif kind == 'remind':
                    to_helper(kind='notify', title='Đến giờ rồi con', body=text, buttons=[['open', 'Mở']])
                    play_wait([chime(), tts('Đến giờ rồi con ơi: ' + text)], repeat=2)
            except Exception:  # noqa: BLE001
                log('announce failed', traceback.format_exc())

    # ------------------------------------------------------------ calls
    CALL = {}
    RANG = set()

    def call_end_local(cid):
        if CALL.get('id') == cid:
            log('call over', cid, CALL.get('state'))
            CALL.clear()
            to_helper(kind='call_end', id=cid)

    def sys_play(paths):
        """Nobody logged in: session 0 (the service) has no audible sound, so play from a small process
        started as SYSTEM in the console session (the login screen). Waits until it is done."""
        paths = [p for p in paths if p and os.path.exists(p)]
        if not paths:
            return True
        try:
            proc = console_spawn([sys.executable, os.path.abspath(__file__), 'play'] + paths)
        except OSError as e:
            log('login-screen sound failed', repr(e))
            return False
        k32 = ctypes.windll.kernel32
        try:
            k32.WaitForSingleObject(proc, 60000)
        finally:
            k32.CloseHandle(proc)
        return True

    def ring_logon(cid, name):
        """Nobody logged in: ring and say who calls at the login screen; when he logs in, the call window opens."""
        CALL.clear()
        CALL.update(id=cid, state='ringing', at=time.time())
        say = tts(f'{name} đang gọi. Con đăng nhập để nghe máy.', 'call-logon.wav')
        log('call ringing at the login screen', cid, name)
        end = time.time() + RING_MAX
        try:
            while time.time() < end and CALL.get('id') == cid and CALL.get('state') == 'ringing':
                if helper_alive():
                    log('he logged in: call window', cid)
                    to_helper(kind='ring', id=cid, name=name, url=f'{BASE}/call?id={urllib.parse.quote(cid)}', ring=ring(),
                              max=max(5, int(end - time.time())))
                    return
                if not sys_play([ring(), ring()] + ([say] if say else [])):
                    time.sleep(2)
                keep_helper()
            if CALL.get('id') == cid and CALL.get('state') == 'ringing':
                relay('POST', f'/agent/call/{cid}/end', {'reason': 'nologin'})
                call_end_local(cid)
        except Exception:  # noqa: BLE001
            log('ring at the login screen failed', traceback.format_exc())

    def ringer(c):
        cid, name = c['id'], PARENT.get(c.get('caller'), 'Bố')
        locked = meta('lock_applied', False)
        # Edge refuses to run as SYSTEM (exit 1002), so auto-answer needs his session: at the login screen
        # an auto call rings like a normal one.
        if locked:
            relay('POST', f'/agent/call/{cid}/end', {'reason': 'locked'})
            return
        if not helper_alive():
            return ring_logon(cid, name)
        CALL.clear()
        CALL.update(id=cid, state='ringing', at=time.time())
        if c.get('auto'):
            log('call auto-answer', cid, name)
            say = tts(f'{name} gọi, máy tự nghe máy.', 'call-say.wav')
            to_helper(kind='call_auto', id=cid, name=name, url=f'{BASE}/call?id={urllib.parse.quote(cid)}&auto=1',
                      paths=[chime()] + ([say] if say else []))
            time.sleep(30)
            if CALL.get('id') == cid and CALL.get('state') == 'ringing':
                log('call auto-answer did not connect', cid)
                call_ui('/api/call/end', {'id': cid})
            return
        log('call ringing', cid, name)
        to_helper(kind='ring', id=cid, name=name, url=f'{BASE}/call?id={urllib.parse.quote(cid)}', ring=ring(), max=RING_MAX)

    def on_call_sync(c):
        if c and c.get('dir') == 'down' and c.get('state') == 'ringing' and c['id'] not in RANG:
            RANG.add(c['id'])
            threading.Thread(target=ringer, args=(c,), daemon=True).start()
        cur = CALL.get('id')
        if cur and (not c or c['id'] != cur) and time.time() - CALL.get('at', 0) > 8:
            call_end_local(cur)

    def call_ui(path, b):
        if path == '/api/call/ice':
            st, j = relay('GET', '/agent/ice')
            if st != 200:
                return st, j
            return 200, {'iceServers': j.get('iceServers') or [], 'gain': CALL_GAIN, 'relay': False}
        if path.startswith('/api/call/info'):
            cid = urllib.parse.parse_qs(urllib.parse.urlparse(path).query).get('id', [''])[0]
            if not re.fullmatch(r'[\w-]{4,40}', cid):
                return 400, {'error': 'id'}
            st, j = relay('GET', f'/agent/call/{cid}')
            if st == 200 and j.get('state') == 'ended':
                call_end_local(cid)
            return st, open_call(j)
        if path == '/api/call/start':
            if meta('lock_applied', False):
                return 409, {'error': 'Máy đang khóa'}
            st, j = relay('POST', '/agent/call', {'offer': enc_text(b.get('offer'))})
            if st == 200:
                CALL.clear()
                CALL.update(id=j['id'], state='ringing', at=time.time(), dir='up')
                log('call out', j['id'])
            return st, j
        cid = str(b.get('id', ''))
        if not re.fullmatch(r'[\w-]{4,40}', cid):
            return 400, {'error': 'id'}
        if path == '/api/call/answer':
            st, j = relay('POST', f'/agent/call/{cid}/answer', {'answer': enc_text(b.get('answer'))})
            if st == 200 and CALL.get('id') == cid:
                CALL['state'] = 'active'
                to_helper(kind='ring_stop', id=cid)
            return st, j
        if path == '/api/call/end':
            st, j = relay('POST', f'/agent/call/{cid}/end', {})
            call_end_local(cid)
            return st, j
        return 404, {'error': 'not found'}

    # ------------------------------------------------------------ activity
    CUR = {'key': None}

    def sample():
        st = H['status'] if helper_alive() else {}
        if not st:
            return
        if st.get('locked') or (st.get('idle_s') or 0) > IDLE_AWAY:
            CUR['key'] = None
            return
        app, title = st.get('app') or '?', (st.get('title') or '')[:160]
        day = dt.date.today().isoformat()
        q('INSERT INTO activity(day,app,title,sec) VALUES(?,?,?,?) ON CONFLICT(day,app,title) DO UPDATE SET sec=sec+excluded.sec',
          day, app, title, SAMPLE_EVERY)
        if (app, title) != CUR['key']:
            CUR['key'] = (app, title)
            q('INSERT INTO recent(at,app,title) VALUES(?,?,?)', now_ms(), app, title)

    def summary():
        day = dt.date.today().isoformat()
        apps = []
        for r in q('SELECT app, SUM(sec) s FROM activity WHERE day=? GROUP BY app ORDER BY s DESC LIMIT 12', day):
            titles = [{'title': t['title'], 'sec': t['sec']} for t in
                      q('SELECT title, sec FROM activity WHERE day=? AND app=? ORDER BY sec DESC LIMIT 3', day, r['app'])]
            apps.append({'app': r['app'], 'sec': r['s'], 'titles': titles})
        total = q1('SELECT COALESCE(SUM(sec),0) s FROM activity WHERE day=?', day)['s']
        recent = [dict(r) for r in q('SELECT * FROM recent ORDER BY at DESC LIMIT 15')]
        return apps, total, recent

    def cur_status():
        if not helper_alive():
            return {'session': 'active' if kid_session() is not None else 'off', 'helper': False}
        st = H['status']
        return {'session': 'locked' if st.get('locked') else 'active', 'idle_s': st.get('idle_s', 0),
                'focus': {'app': st.get('app') or '?', 'title': st.get('title') or ''}, 'volume': st.get('volume')}

    def sampler():
        last_prune = 0
        while True:
            try:
                reconcile_lock()
                keep_helper()
                sample()
                if time.time() - last_prune > 3600:
                    last_prune = time.time()
                    q('DELETE FROM activity WHERE day<?', (dt.date.today() - dt.timedelta(days=30)).isoformat())
                    q('DELETE FROM recent WHERE at<?', now_ms() - 2 * 86400_000)
            except Exception:  # noqa: BLE001
                log('sample failed', traceback.format_exc())
            time.sleep(SAMPLE_EVERY)

    # ------------------------------------------------------------ screenshot
    def take_shot(cmd_id):
        err, data = None, b''
        try:
            if not helper_alive():
                raise RuntimeError('con chưa đăng nhập')
            path = os.path.join(SHOTS, f'{cmd_id}.jpg')
            to_helper(kind='shot', path=path)
            for _ in range(40):
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    time.sleep(0.5)
                    data = open(path, 'rb').read()
                    os.unlink(path)
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError('không chụp được màn hình')
        except Exception as e:  # noqa: BLE001
            err = str(e)
        req = urllib.request.Request(f'{URL}/agent/shot/{cmd_id}', data=enc_blob(data), method='PUT', headers={
            'Authorization': f'Bearer {TOKEN}', 'content-type': 'image/jpeg', 'user-agent': 'kids-talk-win/' + VERSION,
            **({'x-error': urllib.parse.quote(err[:300])} if err else {})})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:  # noqa: BLE001
            log('shot upload failed', repr(e))

    # ------------------------------------------------------------ sync
    DONE_CMDS = set()

    def sync_once():
        upload_voices()
        ops = [seal_op(json.loads(r['op'])) for r in q('SELECT op FROM outbox ORDER BY at LIMIT 100')]
        apps, total, recent = summary()
        L = meta('lock_want', {}) or {}
        status = dict(cur_status(), today=apps, screen_s=total, recent=recent, agent=VERSION,
                      lock={'on': bool(meta('lock_applied', False)), 'until': L.get('until')})
        msg_since = meta('msg_since', 0)
        st, res = relay('POST', '/agent/sync', {'outbox': ops, 'status': seal_status(status), 'msg_since': msg_since},
                        timeout=20)
        if st != 200:
            raise RuntimeError(f'sync http {st} {res}')
        set_meta('last_ok', time.time())
        res['messages'] = [open_message(m) for m in res.get('messages', [])]
        res['tasks'] = [open_task(t) for t in res.get('tasks', [])]
        if 'lock' in res:
            res['lock'] = open_lock(res['lock'])
        for cid in res.get('applied', []):
            q('DELETE FROM outbox WHERE cid=?', cid)
        if 'lock' in res:
            reconcile_lock(res['lock'] or {})
        if 'call' in res:
            on_call_sync(res['call'])
        present = helper_alive()
        fresh = []
        for m in res.get('messages', []):
            old = q1('SELECT read_at FROM messages WHERE id=?', m['id'])
            q('INSERT OR REPLACE INTO messages(id,sender,body,speak,created_at,delivered_at,read_at,audio,audio_ms,repeat_play) '
              'VALUES(?,?,?,?,?,?,?,?,?,?)', m['id'], m['sender'], m['body'], m['speak'], m['created_at'], m['delivered_at'],
              (old['read_at'] if old and old['read_at'] else m['read_at']), m.get('audio'), m.get('audio_ms'),
              m.get('repeat_play') or 0)
            msg_since = max(msg_since, m['id'])
            if m['sender'] in PARENT and not m['delivered_at']:
                fresh.append(m)
        if fresh and not present:
            msg_since = min(m['id'] for m in fresh) - 1   # keep them for when he logs in
        elif fresh:
            for m in fresh[-3:]:
                SAY.put(('msg' if m['speak'] else 'quiet', m['body'], m['id'], m.get('audio'), bool(m.get('repeat_play')),
                         m['sender']))
            enqueue({'op': 'delivered', 'upto': fresh[-1]['id']})
        set_meta('msg_since', msg_since)
        q('DELETE FROM tasks')
        for t in res.get('tasks', []):
            q('INSERT INTO tasks(id,data) VALUES(?,?)', t['id'], json.dumps(t))
        if present:
            now = now_ms()
            for t in res.get('tasks', []):
                due = t.get('due_at')
                if due and not t.get('done_at') and now - 6 * 3600_000 < due <= now \
                        and not q1('SELECT 1 FROM reminded WHERE task_id=? AND due_at=?', t['id'], due):
                    q('INSERT INTO reminded(task_id,due_at) VALUES(?,?)', t['id'], due)
                    SAY.put(('remind', t['title'], None, None, False, t.get('created_by')))
        for c in res.get('commands', []):
            if c['kind'] == 'shot' and c['id'] not in DONE_CMDS:
                DONE_CMDS.add(c['id'])
                threading.Thread(target=take_shot, args=(c['id'],), daemon=True).start()

    VER = {'v': 0}

    def syncer():
        backoff = 4
        while True:
            WAKE.clear()
            try:
                sync_once()
                backoff, pause = 4, SYNC_EVERY
            except Exception as e:  # noqa: BLE001
                log('sync failed', repr(e))
                backoff = pause = min(backoff * 2, 60)
            WAKE.wait(pause)

    # Long-poll: the relay holds this request until something for this computer changes (max WAIT_S),
    # then a full sync fetches it. A dead connection is noticed within WAIT_S + a few seconds.
    def waiter():
        backoff = 2
        while True:
            try:
                st, res = relay('GET', f"/agent/wait?ver={VER['v']}&t={WAIT_S}", timeout=WAIT_S + 15)
                if st != 200:
                    raise RuntimeError(f'wait http {st} {res}')
                backoff = 2
                if res.get('changed'):
                    VER['v'] = res.get('ver') or 0
                    WAKE.set()
            except Exception as e:  # noqa: BLE001
                log('wait failed', repr(e))
                time.sleep(backoff + random.random() * 2)   # spread reconnects after a relay restart
                backoff = min(backoff * 2, 60)

    def helper_events():
        while True:
            ev = H['events'].get()
            try:
                act, msg_id = ev.get('act'), ev.get('msg_id')
                if act == 'done':
                    r = RESP.get(ev.get('rid'))
                    if r:
                        r['result'] = ev.get('result')
                        r['ev'].set()
                    continue
                if msg_id and REPEAT.get('id') == msg_id and act:
                    stop_repeat('stopped')
                if msg_id:
                    enqueue({'op': 'read', 'upto': msg_id})
                    mark_read_local(msg_id)
                if act in QUICK:
                    add_kid_message(QUICK[act])
                if act == 'end_call' and ev.get('id'):
                    call_ui('/api/call/end', {'id': ev['id']})
            except Exception:  # noqa: BLE001
                log('event failed', traceback.format_exc())

    # ------------------------------------------------------------ child's page
    def local_tasks():
        tasks = {r['id']: json.loads(r['data']) for r in q('SELECT * FROM tasks')}
        for r in q('SELECT op FROM outbox ORDER BY at'):
            op = json.loads(r['op'])
            if op['op'] == 'task_add':
                tasks[op['id']] = {'id': op['id'], 'title': op['title'], 'due_at': op.get('due_at'), 'created_by': 'con',
                                   'done_at': None, 'pending': True}
            elif op['op'] == 'task_done' and op['id'] in tasks:
                tasks[op['id']]['done_at'] = op['at'] if op['done'] else None
            elif op['op'] == 'task_del':
                tasks.pop(op['id'], None)
        return sorted(tasks.values(), key=lambda t: (bool(t.get('done_at')), t.get('due_at') or 9e15, t['title']))

    def ui_state():
        msgs = [dict(r) for r in q('SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT 200) ORDER BY id')]
        for r in q("SELECT op FROM outbox WHERE op LIKE '%\"op\": \"msg\"%' ORDER BY at"):
            op = json.loads(r['op'])
            msgs.append({'id': None, 'sender': op.get('sender', 'con'), 'body': op['text'], 'created_at': op['at'],
                         'pending': True})
        for r in q('SELECT * FROM voice_out ORDER BY at'):
            msgs.append({'id': None, 'sender': 'con', 'body': '', 'audio': 'c-' + r['cid'], 'audio_ms': r['ms'],
                         'created_at': r['at'], 'pending': True})
        rp = ({'id': REPEAT['id'], 'body': REPEAT['body'], 'n': REPEAT['n'], 'name': REPEAT.get('name', 'Bố')}
              if REPEAT.get('stop') else None)
        return {'messages': msgs, 'repeat': rp, 'tasks': local_tasks(), 'online': time.time() - meta('last_ok', 0) < 30,
                'now': now_ms()}

    class UI(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('content-type', ctype)
            self.send_header('cache-control', 'no-store')
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            return self.headers.get('host', '') in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')

        def body(self):
            n = int(self.headers.get('content-length') or 0)
            return json.loads(self.rfile.read(min(n, 65536)) or b'{}')

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, {'error': 'host'})
            p = self.path
            if p in ('/', '/index.html'):
                return self.send(200, open(os.path.join(APP, 'kid.html'), 'rb').read(), 'text/html; charset=utf-8')
            if p.split('?')[0] == '/call':
                return self.send(200, open(os.path.join(APP, 'call.html'), 'rb').read(), 'text/html; charset=utf-8')
            if p == '/icon.png':
                return self.send(200, open(os.path.join(APP, 'icon.png'), 'rb').read(), 'image/png')
            m = re.fullmatch(r'/voice/([\w-]{4,64})', p)
            if m:
                f = voice_file(m.group(1))
                return self.send(200, open(f, 'rb').read(), 'audio/wav') if f else self.send(404, {'error': 'voice'})
            if p.startswith('/api/call/'):
                return self.send(*call_ui(p, {}))
            if p == '/api/state':
                return self.send(200, ui_state())
            self.send(404, {'error': 'not found'})

        def do_POST(self):
            if not self.host_ok():
                return self.send(403, {'error': 'forbidden'})
            p = self.path
            if p == '/helper/voice':
                if self.headers.get('x-key') != HELPER_KEY:
                    return self.send(403, {'error': 'key'})
                n = int(self.headers.get('content-length') or 0)
                if not 0 < n <= 8 * 1024 * 1024:
                    return self.send(400, {'error': 'size'})
                save_answer(self.rfile.read(n), int(self.headers.get('x-ms') or 0))
                return self.send(200, {'ok': True})
            try:
                b = self.body()
            except ValueError:
                return self.send(400, {'error': 'json'})
            if p.startswith('/helper/'):
                return self.helper(p, b)
            if self.headers.get('x-bo') != '1':
                return self.send(403, {'error': 'forbidden'})
            if p.startswith('/api/call/'):
                return self.send(*call_ui(p, b))
            if p == '/api/send':
                add_kid_message(str(b.get('text', '')))
            elif p == '/api/read':
                last = q1(f"SELECT MAX(id) m FROM messages WHERE sender IN {PARENTS_SQL} AND read_at IS NULL")
                if last and last['m']:
                    mark_read_local(last['m'])
                    enqueue({'op': 'read', 'upto': last['m']})
            elif p == '/api/task':
                title = str(b.get('title', '')).strip()[:200]
                if not title:
                    return self.send(400, {'error': 'Chưa nhập việc'})
                due = b.get('due_at')
                enqueue({'op': 'task_add', 'id': 'c' + secrets.token_urlsafe(8), 'title': title,
                         'due_at': int(due) if due else None})
            elif p == '/api/task/done':
                enqueue({'op': 'task_done', 'id': str(b.get('id')), 'done': bool(b.get('done'))})
            elif p == '/api/task/del':
                enqueue({'op': 'task_del', 'id': str(b.get('id'))})
            elif p == '/api/repeat/stop':
                stop_repeat('stopped')
            else:
                return self.send(404, {'error': 'not found'})
            self.send(200, ui_state())

        def helper(self, p, b):
            # Only the helper (the child's own process) knows this secret file's content.
            if b.get('key') != HELPER_KEY:
                return self.send(403, {'error': 'key'})
            if p == '/helper/sync':
                H['seen'] = time.time()
                if isinstance(b.get('status'), dict):
                    H['status'] = b['status']
                for ev in b.get('events') or []:
                    H['events'].put(ev)
                with H['cv']:
                    if not H['q']:
                        H['cv'].wait(timeout=min(20, float(b.get('wait', 10))))
                    acts, H['q'] = H['q'], []
                H['seen'] = time.time()
                return self.send(200, {'actions': acts})
            if p == '/helper/event':
                H['seen'] = time.time()
                for ev in b.get('events') or []:
                    H['events'].put(ev)
                return self.send(200, {'ok': True})
            return self.send(404, {'error': 'not found'})

    # The helper proves it runs on this machine by reading a key file only the child + SYSTEM can read.
    keyfile = os.path.join(DATA, 'helper.key')
    if not os.path.exists(keyfile):
        open(keyfile, 'w').write(secrets.token_urlsafe(24))
    HELPER_KEY = open(keyfile).read().strip()

    log('Gọi điện - Nhắn tin con (Windows) service', VERSION, 'kid', KID, 'relay', URL)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), UI)
    srv.daemon_threads = True
    for fn in (announcer, sampler, syncer, waiter, helper_events):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    srv.serve_forever()


# ===================================================================================== HELPER
def helper_main():
    import tkinter as tk
    import winsound
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    os.makedirs(os.path.join(os.environ.get('LOCALAPPDATA', DATA), 'KidsTalk'), exist_ok=True)
    key = open(os.path.join(DATA, 'helper.key')).read().strip()
    EV = []            # events for the service
    ACT = queue.Queue()  # actions for the Tk thread
    play_lock = threading.Lock()
    AUD = Hold()       # loud speakers + his media paused while we talk to him
    calls = {}         # call id -> {'proc', 'ringing'}
    STOP = set()       # job ids the service asked to stop
    UIQ = queue.Queue()  # callables to run on the Tk thread
    popups = []

    def send_event(ev):
        """Tell the service at once (the long poll may be waiting); keep it for the next poll on failure."""
        def go():
            try:
                req = urllib.request.Request(BASE + '/helper/event', method='POST',
                                             data=json.dumps({'key': key, 'events': [ev]}).encode(),
                                             headers={'content-type': 'application/json'})
                urllib.request.urlopen(req, timeout=10).read()
            except Exception:  # noqa: BLE001
                EV.append(ev)
        threading.Thread(target=go, daemon=True).start()

    def done(rid, result=None):
        STOP.discard(rid)
        if rid:
            send_event({'act': 'done', 'rid': rid, 'result': result})

    # ---------------- what is in front
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize', W.UINT), ('dwTime', W.DWORD)]

    def proc_name(pid):
        h = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ''
        try:
            buf, n = ctypes.create_unicode_buffer(520), W.DWORD(520)
            return os.path.basename(buf.value) if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ''
        finally:
            kernel32.CloseHandle(h)

    FRIENDLY = {'msedge.exe': 'Microsoft Edge', 'chrome.exe': 'Google Chrome', 'firefox.exe': 'Firefox',
                'explorer.exe': 'File Explorer', 'winword.exe': 'Word', 'excel.exe': 'Excel', 'powerpnt.exe': 'PowerPoint',
                'code.exe': 'VS Code', 'robloxplayerbeta.exe': 'Roblox', 'minecraft.windows.exe': 'Minecraft',
                'applicationframehost.exe': 'Ứng dụng Windows'}
    STRIP = re.compile(r'\s+[-–—]\s+(?:[^-–—]*\s[-–—]\s)?(Microsoft\u200b? Edge|Google Chrome|Mozilla Firefox)$')

    def status():
        lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        user32.GetLastInputInfo(ctypes.byref(lii))
        idle = max(0, (kernel32.GetTickCount() - lii.dwTime) // 1000)
        hwnd = user32.GetForegroundWindow()
        pid = W.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = proc_name(pid.value).lower() if hwnd else ''
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        locked = exe in ('lockapp.exe', 'logonui.exe') or not hwnd
        return {'idle_s': idle, 'locked': locked, 'app': FRIENDLY.get(exe, exe[:-4] if exe.endswith('.exe') else exe or '?'),
                'title': STRIP.sub('', buf.value)[:160]}

    # ---------------- sound
    def play(paths, repeat=1, rid=None):
        """Play WAVs one after another; stops at once when the service stops job `rid`."""
        stopped = lambda: rid is not None and rid in STOP
        with play_lock:
            for i in range(repeat):
                if i:
                    t = time.time() + 1.5
                    while time.time() < t:
                        if stopped():
                            return
                        time.sleep(0.1)
                for p in paths:
                    if stopped():
                        return
                    if not (p and os.path.exists(p)):
                        continue
                    try:
                        with wave.open(p) as w:
                            dur = w.getnframes() / w.getframerate()
                    except Exception:  # noqa: BLE001
                        dur = 5
                    winsound.PlaySound(p, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    end = time.time() + dur + 0.15
                    while time.time() < end:
                        if stopped():
                            winsound.PlaySound(None, 0)
                            return
                        time.sleep(0.05)

    # ---------------- his spoken answer (Silero VAD, like the Ubuntu agent)
    class Vad:
        FRAME = 512

        def __init__(self):
            import numpy as np
            self.np = np
            self.sess = None
            try:
                import onnxruntime as ort
                o = ort.SessionOptions()
                o.inter_op_num_threads = o.intra_op_num_threads = 1
                self.sess = ort.InferenceSession(VAD_MODEL, o, providers=['CPUExecutionProvider'])
            except Exception as e:  # noqa: BLE001
                log('silero vad unavailable, using loudness', repr(e))
            self.reset()

        def reset(self):
            np = self.np
            self.state = np.zeros((2, 1, 128), dtype=np.float32)
            self.ctx = np.zeros((1, 64), dtype=np.float32)
            self.floor = None

        def prob(self, x):
            np = self.np
            if self.sess is None:
                rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
                self.floor = rms if self.floor is None else min(self.floor * 1.002, rms) if rms < self.floor * 3 else self.floor
                return 1.0 if rms > max(0.01, self.floor * 4) else 0.0
            inp = np.concatenate([self.ctx, x[None, :]], axis=1)
            out, self.state = self.sess.run(None, {'input': inp, 'state': self.state, 'sr': np.array(16000, dtype=np.int64)})
            self.ctx = inp[:, -64:]
            return float(out[0][0])

    VAD = {}

    def record(a, rid):
        """Record until `silence` s of quiet. Returns (wav bytes, ms) or (None, 0)."""
        import io
        import numpy as np
        import sounddevice as sd
        vad = VAD.get('v') or VAD.setdefault('v', Vad())
        vad.reset()
        fps = 16000 / Vad.FRAME
        gain, sp = float(a.get('gain', 2)), float(a.get('speech_p', 0.7))
        pre, frames = [], []
        started, run, quiet, n, speech = False, 0, 0, 0, 0
        with sd.InputStream(samplerate=16000, channels=1, dtype='int16', blocksize=Vad.FRAME) as st:
            while True:
                if rid in STOP:
                    return None, 0
                data, _ = st.read(Vad.FRAME)
                x = np.clip(data[:, 0].astype(np.float32) * gain, -32767, 32767)
                n += 1
                p = vad.prob(x / 32768.0)
                if not started:
                    pre = (pre + [x])[-10:]
                    run = run + 1 if p > sp else 0
                    if run >= 5:
                        started, frames, pre, speech = True, pre, [], run
                    elif n > float(a.get('wait', 10)) * fps:
                        break
                    continue
                frames.append(x)
                quiet = 0 if p > 0.35 else quiet + 1
                if p > sp:
                    speech += 1
                if quiet > float(a.get('silence', 2)) * fps or len(frames) > float(a.get('max', 60)) * fps:
                    break
        if not started or speech < 0.5 * fps:
            return None, 0
        keep = len(frames) - max(0, quiet - int(0.4 * fps))
        y = np.concatenate(frames[:keep])
        peak = float(np.percentile(np.abs(y), 99.9)) or 1.0
        g = min(4.0, 0.85 * 32767 / peak)
        if g > 1.05:
            y = np.clip(y * g, -32767, 32767)
        pcm = y.astype(np.int16).tobytes()
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(pcm)
        return buf.getvalue(), int(len(pcm) / 2 / 16)

    LISTENING = {}

    def listen(a, note):
        rid, sent = a.get('rid'), False
        LISTENING['rid'] = rid
        AUD.hold()
        try:
            play([a.get('beep')], rid=rid)
            data, ms = record(a, rid)
            if data:
                req = urllib.request.Request(BASE + '/helper/voice', data=data, method='POST',
                                             headers={'content-type': 'audio/wav', 'x-key': key, 'x-ms': str(ms)})
                urllib.request.urlopen(req, timeout=30).read()
                sent = True
                UIQ.put(lambda: popup('Đã gửi cho bố ✓', f'Ghi âm {round(ms / 1000)} giây', [], close_after=4000))
        except Exception:  # noqa: BLE001
            log('listen failed', traceback.format_exc())
        finally:
            AUD.release()
            UIQ.put(lambda: close(note))
            done(rid, sent)

    def close(w):
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            pass

    def edge(args):
        return subprocess.Popen([EDGE] + args, creationflags=0x08000000)   # CREATE_NO_WINDOW

    # ---------------- popups (Tk main thread)
    root = tk.Tk()
    root.withdraw()

    def popup(title, body, buttons, msg_id=None, call_id=None, close_after=None):
        w = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes('-topmost', True)
        w.configure(bg='#1f6feb', padx=2, pady=2)
        f = tk.Frame(w, bg='white', padx=14, pady=12)
        f.pack(fill='both', expand=True)
        tk.Label(f, text=title, font=('Segoe UI', 13, 'bold'), bg='white', fg='#1b1f24', anchor='w').pack(fill='x')
        tk.Label(f, text=body, font=('Segoe UI', 12), bg='white', fg='#1b1f24', wraplength=360, justify='left',
                 anchor='w').pack(fill='x', pady=(4, 10))
        row = tk.Frame(f, bg='white')
        row.pack(fill='x')

        def press(act):
            close(w)
            if act == 'stop_listen':
                STOP.add(LISTENING.get('rid'))
                return
            send_event({'act': act, 'msg_id': msg_id, 'id': call_id})
            if act == 'open':
                edge([f'--app={BASE}/'])

        for act, label in buttons:
            tk.Button(row, text=label, font=('Segoe UI', 10), relief='flat', bg='#e9ecef', activebackground='#d0d7de',
                      padx=8, pady=4, command=lambda a=act: press(a)).pack(side='left', padx=(0, 6))
        if not buttons and not call_id:
            tk.Button(row, text='Đóng', font=('Segoe UI', 10), relief='flat', bg='#e9ecef', padx=8, pady=4,
                      command=lambda: close(w)).pack(side='left')
        w.update_idletasks()
        # stack above the popups still open
        popups[:] = [x for x in popups if x.winfo_exists()]
        y = w.winfo_screenheight() - 72
        for x in popups:
            y -= x.winfo_height() + 10
        w.geometry(f'+{w.winfo_screenwidth() - w.winfo_reqwidth() - 24}+{max(10, y - w.winfo_reqheight())}')
        popups.append(w)
        w.lift()
        if close_after:
            w.after(close_after, lambda: close(w))
        return w

    # ---------------- actions
    def ring_loop(cid, path, maxs):
        c = calls[cid]
        end = time.time() + maxs
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        while time.time() < end and c.get('ringing'):
            time.sleep(0.2)
        if c.get('ringing') or calls.get(cid) is not c:
            pass
        winsound.PlaySound(None, 0)
        c['ringing'] = False

    def do(a):
        k = a.get('kind')
        if k == 'notify':
            popup(a['title'], a['body'], a.get('buttons') or [], a.get('msg_id'))
        elif k == 'play':
            def go():
                AUD.hold()
                try:
                    play(a['paths'], a.get('repeat', 1), a.get('rid'))
                finally:
                    AUD.release()
                done(a.get('rid'))
            threading.Thread(target=go, daemon=True).start()
        elif k == 'stop':
            STOP.add(a.get('target'))
            winsound.PlaySound(None, 0)
        elif k == 'listen':
            note = popup('🎤 Bố đang nghe con nói…', 'Con nói đi. Im 2 giây là tự gửi cho bố.', [['stop_listen', 'Thôi']])
            threading.Thread(target=listen, args=(a, note), daemon=True).start()
        elif k == 'ring':
            cid = a['id']
            calls[cid] = {'ringing': True}
            threading.Thread(target=AUD.hold, daemon=True).start()
            edge([f'--app={a["url"]}'])
            calls[cid]['note'] = popup(f'📞 {a["name"]} đang gọi', 'Bấm Nghe trên cửa sổ cuộc gọi', [], call_id=cid)
            threading.Thread(target=ring_loop, args=(cid, a['ring'], a.get('max', 58)), daemon=True).start()
        elif k == 'ring_stop' and a['id'] in calls:
            calls[a['id']]['ringing'] = False
            winsound.PlaySound(None, 0)
            n = calls[a['id']].pop('note', None)
            if n:
                n.destroy()
        elif k == 'call_auto':
            cid = a['id']

            def go():
                play(a.get('paths') or [])
                d = os.path.join(os.environ['LOCALAPPDATA'], 'KidsTalk', 'edgecall')
                calls[cid]['proc'] = edge(['--headless=new', f'--user-data-dir={d}', '--no-first-run',
                                           '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required',
                                           a['url']])
            calls[cid] = {}
            threading.Thread(target=AUD.hold, daemon=True).start()
            calls[cid]['note'] = popup(f'📞 {a["name"]} đang nói chuyện với con', 'Máy tự nghe máy. Bấm Kết thúc để dừng.',
                                       [['end_call', 'Kết thúc']], call_id=cid)
            threading.Thread(target=go, daemon=True).start()
        elif k == 'call_end' and a['id'] in calls:
            c = calls.pop(a['id'])
            c['ringing'] = False
            threading.Thread(target=AUD.release, kwargs={'now': True}, daemon=True).start()
            winsound.PlaySound(None, 0)
            if c.get('note'):
                try:
                    c['note'].destroy()
                except tk.TclError:
                    pass
            p = c.get('proc')
            if p and p.poll() is None:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True,
                               creationflags=0x08000000)
        elif k == 'shot':
            def grab():
                try:
                    from PIL import ImageGrab
                    im = ImageGrab.grab(all_screens=True).convert('RGB')
                    im.thumbnail((1600, 1600))
                    im.save(a['path'] + '.tmp', 'JPEG', quality=72)
                    os.replace(a['path'] + '.tmp', a['path'])
                except Exception:  # noqa: BLE001
                    log('shot failed', traceback.format_exc())
            threading.Thread(target=grab, daemon=True).start()

    def pump():
        try:
            while True:
                UIQ.get_nowait()()
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            log('ui failed', traceback.format_exc())
        try:
            while True:
                a = ACT.get_nowait()
                try:
                    do(a)
                except Exception:  # noqa: BLE001
                    log('action failed', a.get('kind'), traceback.format_exc())
        except queue.Empty:
            pass
        root.after(200, pump)

    def net():
        while True:
            try:
                evs = EV[:]
                del EV[:len(evs)]
                body = json.dumps({'key': key, 'status': status(), 'events': evs, 'wait': 10}).encode()
                req = urllib.request.Request(BASE + '/helper/sync', data=body, method='POST',
                                             headers={'content-type': 'application/json', 'host': f'127.0.0.1:{PORT}'})
                with urllib.request.urlopen(req, timeout=30) as r:
                    for a in json.loads(r.read()).get('actions', []):
                        ACT.put(a)
            except Exception as e:  # noqa: BLE001
                log('helper sync failed', repr(e))
                time.sleep(3)

    log('helper start', os.environ.get('USERNAME'))
    threading.Thread(target=net, daemon=True).start()
    root.after(200, pump)
    root.mainloop()


if __name__ == '__main__':
    if MODE == 'helper':
        helper_main()
    elif MODE == 'play':
        play_main(sys.argv[2:])
    else:
        service_main()

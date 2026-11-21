#!/opt/kids-talk/venv/bin/python3
"""Gọi điện - Nhắn tin con agent — runs as root on the child's laptop.

- syncs with the relay Worker (messages, tasks, commands, status)
- shows critical notifications and reads parent messages aloud in the child's session
- serves the child's reply/task page on http://127.0.0.1:8765
- samples the focused window (GNOME extension app.kidstalk.Agent) for the activity summary
- voice: plays the parent's voice messages; after a spoken message it pauses media, listens on the
  mic (Silero VAD), sends the child's answer after 2 s of silence, then resumes media
"""
import datetime as dt
import http.server
import io
import json
import math
import os
import pwd
import queue
import random
import re
import secrets
import signal
import sqlite3
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

CONF_PATH = os.environ.get('BO_CONF', '/etc/kids-talk/agent.json')
CONF = json.load(open(CONF_PATH))
URL = CONF['url'].rstrip('/')
TOKEN = CONF['token']
KID = CONF.get('kid_user', '')   # the child's Linux account (required, set by install.sh)
PORT = int(CONF.get('port', 8765))
VOLUME = float(CONF.get('volume', 0.85))
VOICE = CONF.get('voice', '/opt/kids-talk/voices/vi_VN-vais1000-medium.onnx')
STATE_DIR = CONF.get('state_dir', '/var/lib/kids-talk')
HERE = os.path.dirname(os.path.abspath(__file__))

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

# Audio is played by the child's own user: keep it out of the root-only state dir.
AUDIO_DIR = CONF.get('audio_dir', '/var/cache/kids-talk')
SYNC_EVERY = 60     # full sync for the status; changes arrive through the long-poll (/agent/wait)
WAIT_S = 25
SAMPLE_EVERY = 10
IDLE_AWAY = 120
VERSION = '7'
VAD_MODEL = CONF.get('vad_model', '/opt/kids-talk/silero_vad.onnx')
LISTEN_WAIT = float(CONF.get('listen_wait', 10))     # s to start talking, else nothing is sent
LISTEN_SILENCE = float(CONF.get('listen_silence', 2))  # s of silence that ends the answer
LISTEN_MAX = float(CONF.get('listen_max', 60))
# The Dell's digital mic has no hardware gain and is quiet (speech peaks ~-30 dBFS): boost in software.
MIC_GAIN = float(CONF.get('mic_gain', 4.0))
SPEECH_P = float(CONF.get('speech_p', 0.7))           # Silero probability that counts as speech
REPEAT_GAP = 10          # s between plays of a repeating message (spent listening for an answer)
REPEAT_MAX = 3600        # a repeating message stops after 1 hour
REPEAT = {}              # the message being repeated now: id, body, n, stop (Event)
PARENT = {'bo': 'Bố', 'me': 'Mẹ'}
PARENTS_SQL = "('bo','me')"
LOCKMX = threading.Lock()
VOICE_DIR = os.path.join(AUDIO_DIR, 'voice')
CALL_GAIN = float(CONF.get('call_gain', 2.5))      # software mic boost in the call window
CALL_RELAY = bool(CONF.get('call_relay', True))    # the child's filter only lets TCP 443 out: TURN only
RING_MAX = 58      # the relay gives up at 60 s
CALL = {}                # the call on this laptop: id, dir, caller, state, paused, vol
CALLMX = threading.Lock()
RANG = set()

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.chmod(AUDIO_DIR, 0o755)
os.makedirs(VOICE_DIR, exist_ok=True)
os.chmod(VOICE_DIR, 0o755)
os.makedirs(os.path.join(STATE_DIR, 'voice-out'), exist_ok=True)
DB = sqlite3.connect(os.path.join(STATE_DIR, 'state.db'), check_same_thread=False, isolation_level=None)
DB.row_factory = sqlite3.Row
LOCK = threading.RLock()
WAKE = threading.Event()   # wake the sync loop early (local change)
SAY = queue.Queue()        # serialized announcements


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def q(sql, *args):
    with LOCK:
        return DB.execute(sql, args).fetchall()


def q1(sql, *args):
    r = q(sql, *args)
    return r[0] if r else None


with LOCK:
    DB.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, sender TEXT, body TEXT, speak INTEGER,
      created_at INTEGER, delivered_at INTEGER, read_at INTEGER);
    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, data TEXT);
    CREATE TABLE IF NOT EXISTS outbox(cid TEXT PRIMARY KEY, op TEXT, at INTEGER);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS activity(day TEXT, app TEXT, title TEXT, sec INTEGER, PRIMARY KEY(day, app, title));
    CREATE TABLE IF NOT EXISTS recent(at INTEGER, app TEXT, title TEXT);
    CREATE TABLE IF NOT EXISTS reminded(task_id TEXT, due_at INTEGER, PRIMARY KEY(task_id, due_at));
    CREATE TABLE IF NOT EXISTS voice_out(cid TEXT PRIMARY KEY, path TEXT, ms INTEGER, at INTEGER, tries INTEGER DEFAULT 0);
    ''')
    _cols = {r[1] for r in DB.execute('PRAGMA table_info(messages)')}
    for _c, _t in (('audio', 'TEXT'), ('audio_ms', 'INTEGER'), ('repeat_play', 'INTEGER')):
        if _c not in _cols:
            DB.execute(f'ALTER TABLE messages ADD COLUMN {_c} {_t}')


def meta(k, default=None):
    r = q1('SELECT v FROM meta WHERE k=?', k)
    return json.loads(r['v']) if r else default


def set_meta(k, v):
    q('INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', k, json.dumps(v))


def now_ms():
    return int(time.time() * 1000)


def enqueue(op):
    cid = op.get('cid') or secrets.token_urlsafe(9)
    op['cid'] = cid
    op.setdefault('at', now_ms())
    q('INSERT OR REPLACE INTO outbox(cid, op, at) VALUES(?,?,?)', cid, json.dumps(op), op['at'])
    WAKE.set()
    return cid


# ---------------------------------------------------------------- child session
class Kid:
    def __init__(self, name):
        p = pwd.getpwnam(name)
        self.name, self.uid, self.gid, self.home = name, p.pw_uid, p.pw_gid, p.pw_dir
        self.run = f'/run/user/{self.uid}'

    def env(self):
        return {
            'HOME': self.home, 'USER': self.name, 'LOGNAME': self.name, 'PATH': '/usr/local/bin:/usr/bin:/bin',
            'XDG_RUNTIME_DIR': self.run, 'DBUS_SESSION_BUS_ADDRESS': f'unix:path={self.run}/bus',
            'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0', 'LANG': 'vi_VN.UTF-8',
            'XDG_SESSION_TYPE': 'wayland', 'XAUTHORITY': self.xauth(),
        }

    def xauth(self):
        try:
            for f in os.listdir(self.run):
                if f.startswith('.mutter-Xwaylandauth.'):
                    return f'{self.run}/{f}'
        except OSError:
            pass
        return f'{self.home}/.Xauthority'

    def sh(self, cmd, timeout=10, wait=True, **kw):
        args = dict(user=self.uid, group=self.gid, extra_groups=[], env=self.env(), cwd=self.home,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        args.update(kw)
        if not wait:
            return subprocess.Popen(cmd, start_new_session=True, **args)
        try:
            return subprocess.run(cmd, timeout=timeout, **args)
        except subprocess.TimeoutExpired:
            return None

    def graphical(self):
        """True when the child has a graphical session (locked counts)."""
        if not os.path.exists(f'{self.run}/bus'):
            return False
        r = subprocess.run(['loginctl', 'list-sessions', '--no-legend', '-o', 'json'], capture_output=True, text=True)
        try:
            sessions = json.loads(r.stdout or '[]')
        except ValueError:
            return False
        for s in sessions:
            if s.get('user') == self.name and s.get('seat'):
                return True
        return False

    def call(self, dest, path, iface, method, *sig_args):
        cmd = ['busctl', '--user', '--json=short', 'call', dest, path, iface, method, *map(str, sig_args)]
        r = self.sh(cmd, timeout=8)
        if not r or r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)['data']
        except (ValueError, KeyError):
            return None

    def focus(self):
        d = self.call('app.kidstalk.Agent', '/app/kidstalk/Agent', 'app.kidstalk.Agent', 'Status')
        if d:
            return json.loads(d[0])
        # Extension not loaded yet (needs a fresh login): idle + lock only.
        st = {'ext': False}
        idle = self.call('org.gnome.Mutter.IdleMonitor', '/org/gnome/Mutter/IdleMonitor/Core',
                         'org.gnome.Mutter.IdleMonitor', 'GetIdletime')
        if idle:
            st['idle_ms'] = idle[0]
        lk = self.call('org.gnome.ScreenSaver', '/org/gnome/ScreenSaver', 'org.gnome.ScreenSaver', 'GetActive')
        if lk is not None:
            st['locked'] = bool(lk[0])
        return st

    def volume(self):
        r = self.sh(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'])
        if not r or r.returncode:
            return None, None
        m = re.search(r'Volume:\s*([\d.]+)', r.stdout)
        return (float(m.group(1)) if m else None), ('MUTED' in r.stdout)


KIDU = Kid(KID)


# ---------------------------------------------------------------- sound
def chime_path():
    p = os.path.join(AUDIO_DIR, 'chime.wav')
    if os.path.exists(p):
        return p
    rate, frames = 22050, bytearray()
    for f, ms in ((880, 180), (0, 40), (1175, 260)):
        n = int(rate * ms / 1000)
        for i in range(n):
            env = min(1, i / 300, (n - i) / 900)
            v = int(0.55 * 32767 * env * math.sin(2 * math.pi * f * i / rate)) if f else 0
            frames += struct.pack('<h', v)
    with wave.open(p, 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(bytes(frames))
    os.chmod(p, 0o644)
    return p


_voice = None


def tts(text, fname='say.wav'):
    """Synthesize Vietnamese speech to a wav readable by the child. Returns path or None."""
    global _voice
    try:
        if _voice is None:
            from piper import PiperVoice
            _voice = PiperVoice.load(VOICE)
        p = os.path.join(AUDIO_DIR, fname)
        with wave.open(p, 'wb') as w:
            _voice.synthesize_wav(text, w)
        os.chmod(p, 0o644)
        return p
    except Exception as e:  # noqa: BLE001
        log('tts failed', e)
        return None


def play_loud(paths, repeat=1):
    vol, muted = KIDU.volume()
    KIDU.sh(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0'])
    KIDU.sh(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{VOLUME:.2f}'])
    try:
        for i in range(repeat):
            if i:
                time.sleep(1.5)
            for p in paths:
                if p:
                    r = KIDU.sh(['pw-play', p], timeout=120)
                    if not r or r.returncode:
                        log('pw-play failed', p, r and r.stderr.strip()[:200])
    finally:
        if vol is not None:
            KIDU.sh(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{vol:.2f}'])
        if muted:
            KIDU.sh(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '1'])


QUICK = {'ok': 'Dạ, con nghe rồi ạ', 'later': 'Con xin 5 phút nữa ạ'}


def notify(title, body, msg_id=None, on_action=None):
    """Critical notification that stays until clicked; buttons answer the parent.
    on_action (repeating message): called on any button, and the buttons offer ⏸ Dừng phát."""
    def run():
        cmd = ['notify-send', '-u', 'critical', '-a', 'Gọi điện - Nhắn tin con', '-i', '/opt/kids-talk/icon.png', '--wait']
        if msg_id and on_action:
            cmd += ['--action=stop=⏸ Dừng phát', '--action=ok=' + QUICK['ok'], '--action=open=Mở trả lời']
        elif msg_id:
            cmd += ['--action=ok=' + QUICK['ok'], '--action=later=' + QUICK['later'], '--action=open=Mở trả lời']
        else:
            cmd += ['--action=open=Mở']
        r = KIDU.sh(cmd + [title, body[:400]], timeout=6 * 3600)
        act = (r.stdout.strip() if r else '')
        if not act:
            return
        if on_action:
            on_action()
        if msg_id:
            enqueue({'op': 'read', 'upto': msg_id})
            mark_read_local(msg_id)
        if act in QUICK:
            add_kid_message(QUICK[act])
        if act == 'open':
            open_ui()
    threading.Thread(target=run, daemon=True).start()


def open_ui():
    KIDU.sh(['google-chrome', '--ozone-platform=wayland', f'--app=http://127.0.0.1:{PORT}/'], wait=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def announcer():
    while True:
        kind, text, msg_id, audio, repeat, sender = SAY.get()
        name = PARENT.get(sender, 'Bố')
        paused = []
        try:
            if kind == 'msg':
                paused = pause_media()
                stop = threading.Event()
                if repeat:
                    REPEAT.update(id=msg_id, body=text or '🎤 tin nhắn thoại', n=0, stop=stop, name=name)
                notify(f'{name} nhắn' + (' 🔁' if repeat else ''), text or f'🎤 {name} gửi tin nhắn thoại', msg_id,
                       on_action=stop.set if repeat else None)
                clip = voice_file(audio) if audio else None
                if audio and not clip:
                    log('voice download failed, reading the text instead', audio)
                speech = clip or tts(f'{name} nhắn: ' + (text or f'{name.lower()} gửi tin nhắn thoại, con mở chương trình nhắn tin để nghe'))
                if repeat:
                    repeat_loop(speech, stop, paused)
                else:
                    st = KIDU.focus() or {}
                    away = (st.get('idle_ms') or 0) > 60_000 or st.get('locked')
                    play_loud([chime_path(), speech], repeat=2 if away else 1)
                    if not (KIDU.focus() or {}).get('locked'):
                        listen_and_send()
            elif kind == 'remind':
                notify('Đến giờ rồi con', text)
                play_loud([chime_path(), tts('Đến giờ rồi con ơi: ' + text)], repeat=2)
            elif kind == 'quiet':  # parent unticked "Đọc to": notification only, no sound
                notify(f'{name} nhắn', text or f'🎤 {name} gửi tin nhắn thoại', msg_id)
        except Exception:  # noqa: BLE001
            log('announce failed', traceback.format_exc())
        finally:
            REPEAT.clear()
            resume_media(paused)


def repeat_loop(speech, stop, paused):
    """Play, then listen REPEAT_GAP s for an answer, again and again until the child stops it,
    answers, a new message comes, he logs out, or REPEAT_MAX passes. Tells the parent how it ended."""
    end = time.time() + REPEAT_MAX
    reason = 'timeout'
    while True:
        paused += [p for p in pause_media() if p not in paused]   # he may have restarted the video
        play_loud([chime_path(), speech])
        REPEAT['n'] += 1
        if stop.is_set():
            reason = REPEAT.get('why', 'stopped')
            break
        if (KIDU.focus() or {}).get('locked'):
            stop.wait(REPEAT_GAP)
        elif listen_and_send(stop):
            reason = 'answered'
            break
        if stop.is_set():
            reason = REPEAT.get('why', 'stopped')
        elif not SAY.empty():
            reason = 'new'
        elif not KIDU.graphical():
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
        'timeout': f'Đã phát lặp lại 1 giờ ({n} lần), con chưa dừng',
    }[reason]
    enqueue({'op': 'msg', 'text': note, 'sender': 'he'})


def stop_repeat(why='stopped'):
    if REPEAT.get('stop') and not REPEAT['stop'].is_set():
        REPEAT['why'] = why
        REPEAT['stop'].set()


# ---------------------------------------------------------------- hard lock
# The relay holds the wanted state {on, until, msg, by}; we make the laptop match it on every sync and every
# sample tick, from the last state we saw, so `until` unlocks him even offline or after a reboot.
# Locking = his password is locked (passwd -l: the lock screen and login reject it) + his session is locked.
# The admin account is never touched; `passwd -u <child>` as admin is the manual way out.
def lock_wanted(L):
    return bool(L and L.get('on') and (not L.get('until') or now_ms() < L['until']))


def hhmm(ms):
    return time.strftime('%H:%M', time.localtime(ms / 1000))


def kid_sessions():
    r = subprocess.run(['loginctl', 'list-sessions', '--no-legend', '-o', 'json'], capture_output=True, text=True)
    try:
        return [x['session'] for x in json.loads(r.stdout or '[]') if x.get('user') == KID]
    except ValueError:
        return []


def lock_sessions():
    for sid in kid_sessions():
        subprocess.run(['loginctl', 'lock-session', str(sid)], capture_output=True)


def reconcile_lock(L=None):
    """Make the laptop match the wanted lock state (L from the relay, else the last one we saw)."""
    with LOCKMX:
        if L is not None:
            set_meta('lock_want', L)
        L = meta('lock_want', {}) or {}
        want, applied = lock_wanted(L), meta('lock_applied', False)
        if want and not applied:
            r = subprocess.run(['passwd', '-l', KID], capture_output=True, text=True)
            if r.returncode:
                log('passwd -l failed', r.stderr.strip())
                return
            set_meta('lock_applied', True)
            name = PARENT.get(L.get('by'), 'Bố')
            until = f'đến {hhmm(L["until"])}' if L.get('until') else f'đến khi {name.lower()} mở'
            log('locked', until)
            if KIDU.graphical():
                pause_media()
                notify(f'🔒 {name} đã khóa máy', (L.get('msg') or 'Máy khóa') + f' — {until}.')
                time.sleep(1.5)
            lock_sessions()
            enqueue({'op': 'msg', 'sender': 'he', 'text': f'🔒 Máy của con đã khóa {until}'})
        elif not want and applied:
            r = subprocess.run(['passwd', '-u', KID], capture_output=True, text=True)
            if r.returncode:
                log('passwd -u failed', r.stderr.strip())
                return
            set_meta('lock_applied', False)
            expired = L.get('on') and L.get('until')
            log('unlocked', 'time up' if expired else 'by parent')
            if KIDU.graphical():
                notify('🔓 Máy đã mở khóa', 'Con dùng mật khẩu của con để mở máy nhé.')
            enqueue({'op': 'msg', 'sender': 'he',
                     'text': '🔓 Hết giờ khóa, máy đã mở' if expired else '🔓 Máy của con đã mở khóa'})
        elif want and applied and KIDU.graphical() and not (KIDU.focus() or {}).get('locked'):
            lock_sessions()   # his session got unlocked somehow (or locking it failed): lock again


# ---------------------------------------------------------------- voice
def voice_file(key):
    """Local WAV of a voice message (downloaded once, readable by the child). None on failure."""
    if not re.fullmatch(r'[\w-]{4,64}', key or ''):
        return None
    p = os.path.join(VOICE_DIR, key + '.wav')
    if os.path.exists(p):
        return p
    req = urllib.request.Request(f'{URL}/agent/voice/{key}', headers={
        'Authorization': f'Bearer {TOKEN}', 'user-agent': 'kids-talk-agent/' + VERSION})
    try:
        data = urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:  # noqa: BLE001
        log('voice get failed', key, repr(e))
        return None
    data = dec_blob(data, from_parent=not key.startswith('c-'))
    if data is None:
        log('voice refused (not encrypted with the family key)', key)
        return None
    tmp = p + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)
    return p


def mpris_players():
    d = KIDU.call('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'ListNames')
    return [n for n in (d[0] if d else []) if n.startswith('org.mpris.MediaPlayer2.')]


def pause_media():
    """Pause every player that is playing (YouTube in Chrome, Spotify, Firefox…). Returns what to resume."""
    paused = []
    try:
        for name in mpris_players():
            r = KIDU.sh(['busctl', '--user', '--json=short', 'get-property', name, '/org/mpris/MediaPlayer2',
                         'org.mpris.MediaPlayer2.Player', 'PlaybackStatus'], timeout=5)
            try:
                playing = r and r.returncode == 0 and json.loads(r.stdout)['data'] == 'Playing'
            except (ValueError, KeyError):
                playing = False
            if playing:
                KIDU.call(name, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player', 'Pause')
                paused.append(name)
        if paused:
            log('paused', paused)
    except Exception:  # noqa: BLE001
        log('pause failed', traceback.format_exc())
    return paused


def resume_media(names):
    for name in names:
        KIDU.call(name, '/org/mpris/MediaPlayer2', 'org.mpris.MediaPlayer2.Player', 'Play')


def beep_path():
    p = os.path.join(AUDIO_DIR, 'listen.wav')
    if os.path.exists(p):
        return p
    rate, frames = 22050, bytearray()
    for f, ms in ((1320, 110), (0, 60), (1320, 110)):
        n = int(rate * ms / 1000)
        for i in range(n):
            env = min(1, i / 200, (n - i) / 400)
            v = int(0.45 * 32767 * env * math.sin(2 * math.pi * f * i / rate)) if f else 0
            frames += struct.pack('<h', v)
    with wave.open(p, 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(bytes(frames))
    os.chmod(p, 0o644)
    return p


class Vad:
    """Silero VAD v5 (ONNX, 16 kHz, 512-sample frames). Falls back to a loudness gate."""
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

    def prob(self, pcm):
        np = self.np
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)[None, :] / 32768.0
        if self.sess is None:
            rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
            self.floor = rms if self.floor is None else min(self.floor * 1.002, rms) if rms < self.floor * 3 else self.floor
            return 1.0 if rms > max(0.01, self.floor * 4) else 0.0
        inp = np.concatenate([self.ctx, x], axis=1)
        out, self.state = self.sess.run(None, {'input': inp, 'state': self.state, 'sr': np.array(16000, dtype=np.int64)})
        self.ctx = inp[:, -64:]
        return float(out[0][0])


_vad = None


def amplify(pcm, gain):
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * gain
    return np.clip(x, -32767, 32767).astype(np.int16).tobytes()


def normalize(pcm, target=0.85, max_gain=4.0):
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    peak = float(np.percentile(np.abs(x), 99.9)) or 1.0
    g = min(max_gain, target * 32767 / peak)
    return amplify(pcm, g) if g > 1.05 else pcm


def wav_bytes(pcm, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(pcm)
    return buf.getvalue()


def show_note(title, body, urgency='normal'):
    r = KIDU.sh(['notify-send', '-p', '-u', urgency, '-a', 'Gọi điện - Nhắn tin con', '-i', '/opt/kids-talk/icon.png', title, body])
    try:
        return int(r.stdout.strip()) if r and r.returncode == 0 else None
    except ValueError:
        return None


def close_note(nid):
    if nid:
        KIDU.call('org.freedesktop.Notifications', '/org/freedesktop/Notifications',
                  'org.freedesktop.Notifications', 'CloseNotification', 'u', nid)


def record_answer(stop=None):
    """Record from the child's default mic until LISTEN_SILENCE s of silence. Returns (pcm, ms) or (None, 0).
    A set `stop` event (child pressed Dừng) ends it with nothing sent."""
    global _vad
    if _vad is None:
        _vad = Vad()
    vad = _vad
    vad.reset()
    frame_b = Vad.FRAME * 2
    fps = 16000 / Vad.FRAME
    proc = KIDU.sh(['pw-record', '--rate', '16000', '--channels', '1', '--format', 's16', '-'], wait=False,
                   text=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pre = []           # ~0.3 s before speech starts, so the first word is not cut
    frames = []
    started = False
    run = quiet = n = 0
    speech_frames = 0
    try:
        while True:
            pcm = proc.stdout.read(frame_b)
            if not pcm or len(pcm) < frame_b:
                log('pw-record ended early')
                break
            n += 1
            if stop is not None and stop.is_set():
                return None, 0
            if MIC_GAIN != 1:
                pcm = amplify(pcm, MIC_GAIN)
            p = vad.prob(pcm)
            if not started:
                pre = (pre + [pcm])[-10:]
                run = run + 1 if p > SPEECH_P else 0
                if run >= 5:                       # ~0.16 s of clear speech
                    started, frames, pre = True, pre, []
                    speech_frames = run
                elif n > LISTEN_WAIT * fps:
                    break
                continue
            frames.append(pcm)
            if p > 0.35:
                quiet = 0
            else:
                quiet += 1
            if p > SPEECH_P:
                speech_frames += 1
            if quiet > LISTEN_SILENCE * fps or len(frames) > LISTEN_MAX * fps:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not started or speech_frames < 0.5 * fps:
        if started:
            log('listen: too little clear speech', speech_frames, 'frames')
        return None, 0
    keep = len(frames) - max(0, quiet - int(0.4 * fps))   # trim the trailing silence, keep 0.4 s
    pcm = normalize(b''.join(frames[:keep]))
    return pcm, int(len(pcm) / 2 / 16)


def listen_and_send(stop=None):
    """Beep, record the child's spoken answer and queue it for the parent. True when something was sent."""
    nid = show_note('🎤 Bố đang nghe con nói…', 'Con nói đi. Im 2 giây là tự gửi cho bố.')
    try:
        play_loud([beep_path()])
        pcm, ms = record_answer(stop)
    finally:
        close_note(nid)
    if not pcm:
        log('listen: no answer')
        return False
    cid = 'v' + secrets.token_urlsafe(9)
    data = wav_bytes(pcm)
    path = os.path.join(STATE_DIR, 'voice-out', cid + '.wav')
    with open(path, 'wb') as f:
        f.write(data)
    local = os.path.join(VOICE_DIR, f'c-{cid}.wav')   # same key the relay uses: c-<cid>
    with open(local, 'wb') as f:
        f.write(data)
    os.chmod(local, 0o644)
    q('INSERT INTO voice_out(cid,path,ms,at) VALUES(?,?,?,?)', cid, path, ms, now_ms())
    log('listen: answer', ms, 'ms')
    show_note('Đã gửi cho bố ✓', f'Ghi âm {round(ms / 1000)} giây')
    WAKE.set()
    return True


def upload_voices():
    for r in q('SELECT * FROM voice_out ORDER BY at LIMIT 3'):
        try:
            data = open(r['path'], 'rb').read()
        except OSError:
            q('DELETE FROM voice_out WHERE cid=?', r['cid'])
            continue
        qs = urllib.parse.urlencode({'cid': r['cid'], 'ms': r['ms'], 'at': r['at']})
        req = urllib.request.Request(f'{URL}/agent/voice?{qs}', data=enc_blob(data), method='PUT', headers={
            'Authorization': f'Bearer {TOKEN}', 'content-type': 'audio/wav', 'user-agent': 'kids-talk-agent/' + VERSION})
        try:
            urllib.request.urlopen(req, timeout=90).read()
        except urllib.error.HTTPError as e:
            log('voice upload http', e.code)
            if 400 <= e.code < 500 and e.code not in (401, 408, 429):
                q('DELETE FROM voice_out WHERE cid=?', r['cid'])   # rejected for good
            continue
        except Exception as e:  # noqa: BLE001
            log('voice upload failed', repr(e))
            return
        q('DELETE FROM voice_out WHERE cid=?', r['cid'])
        os.unlink(r['path'])


def prune_voice():
    old = time.time() - 30 * 86400
    for f in os.listdir(VOICE_DIR):
        p = os.path.join(VOICE_DIR, f)
        if os.path.getmtime(p) < old:
            os.unlink(p)


# ---------------------------------------------------------------- local data
def mark_read_local(upto):
    q(f"UPDATE messages SET read_at=COALESCE(read_at, ?) WHERE sender IN {PARENTS_SQL} AND id<=?", now_ms(), upto)


def add_kid_message(text):
    text = text.strip()[:1000]
    if text:
        enqueue({'op': 'msg', 'text': text})
        stop_repeat('answered')   # answering a repeating message stops it


def local_tasks():
    tasks = {r['id']: json.loads(r['data']) for r in q('SELECT * FROM tasks')}
    for r in q('SELECT op FROM outbox ORDER BY at'):  # show pending changes immediately
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
    return {'messages': msgs, 'repeat': rp, 'tasks': local_tasks(), 'online': time.time() - meta('last_ok', 0) < 30, 'now': now_ms()}


# ---------------------------------------------------------------- activity
CUR = {'key': None, 'status': {}}
STRIP = re.compile(r'\s+[-–—]\s+(Google Chrome|Chromium|Mozilla Firefox|Visual Studio Code)$')


def sample():
    if not KIDU.graphical():
        CUR['status'] = {'session': 'off'}
        return
    st = KIDU.focus() or {}
    idle_s = int((st.get('idle_ms') or 0) / 1000)
    locked = bool(st.get('locked'))
    app = st.get('app') or st.get('wmClass') or ('?' if st.get('ext', True) else 'chưa có extension')
    title = STRIP.sub('', st.get('title') or '')[:160]
    vol, muted = KIDU.volume()
    CUR['status'] = {
        'session': 'locked' if locked else 'active', 'idle_s': idle_s,
        'focus': {'app': app, 'title': title}, 'ext': st.get('ext', True),
        'volume': None if vol is None else (f'{int(vol * 100)}%' + (' (tắt tiếng)' if muted else '')),
    }
    if locked or idle_s > IDLE_AWAY:
        CUR['key'] = None
        return
    day = dt.date.today().isoformat()
    q('INSERT INTO activity(day,app,title,sec) VALUES(?,?,?,?) ON CONFLICT(day,app,title) DO UPDATE SET sec=sec+excluded.sec',
      day, app, title, SAMPLE_EVERY)
    key = (app, title)
    if key != CUR['key']:
        CUR['key'] = key
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


def sampler():
    last_prune = 0
    while True:
        try:
            reconcile_lock()
            sample()
            if time.time() - last_prune > 3600:
                last_prune = time.time()
                old = (dt.date.today() - dt.timedelta(days=30)).isoformat()
                q('DELETE FROM activity WHERE day<?', old)
                q('DELETE FROM recent WHERE at<?', now_ms() - 2 * 86400_000)
                prune_voice()
        except Exception:  # noqa: BLE001
            log('sample failed', traceback.format_exc())
        time.sleep(SAMPLE_EVERY)


# ---------------------------------------------------------------- screenshot
def take_shot(cmd_id):
    err = None
    data = b''
    try:
        if not KIDU.graphical():
            raise RuntimeError('con chưa đăng nhập')
        path = f'{KIDU.run}/kids-talk-shot.png'
        d = KIDU.call('app.kidstalk.Agent', '/app/kidstalk/Agent', 'app.kidstalk.Agent', 'Screenshot', 's', path)
        if not d or not d[0] or not os.path.exists(path):
            raise RuntimeError('chưa có extension (con cần đăng xuất/đăng nhập lại một lần)')
        from PIL import Image
        im = Image.open(path).convert('RGB')
        im.thumbnail((1600, 1600))
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=72)
        data = buf.getvalue()
        os.unlink(path)
    except Exception as e:  # noqa: BLE001
        err = str(e)
        log('shot failed', err)
    headers = {'Authorization': f'Bearer {TOKEN}', 'content-type': 'image/jpeg',
               'user-agent': 'kids-talk-agent/' + VERSION}
    if err:
        headers['x-error'] = urllib.request.quote(err[:300])
    req = urllib.request.Request(f'{URL}/agent/shot/{cmd_id}', data=enc_blob(data), method='PUT', headers=headers)
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:  # noqa: BLE001
        log('shot upload failed', repr(e))


# ---------------------------------------------------------------- live calls
def relay(method, path, body=None, timeout=15):
    req = urllib.request.Request(URL + path, data=None if body is None else json.dumps(body).encode(), method=method,
                                 headers={'Authorization': f'Bearer {TOKEN}', 'content-type': 'application/json',
                                          'user-agent': 'kids-talk-agent/' + VERSION})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {'error': str(e.code)}


def ring_path():
    p = os.path.join(AUDIO_DIR, 'ring.wav')
    if os.path.exists(p):
        return p
    rate, frames = 22050, bytearray()
    for f, ms in ((988, 400), (0, 120), (784, 400), (0, 1100)):   # ~2 s: two tones then a pause
        n = int(rate * ms / 1000)
        for i in range(n):
            env = min(1, i / 200, (n - i) / 400)
            v = int(0.6 * 32767 * env * (math.sin(2 * math.pi * f * i / rate) + 0.3 * math.sin(4 * math.pi * f * i / rate)) / 1.3) if f else 0
            frames += struct.pack('<h', v)
    with wave.open(p, 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(bytes(frames))
    os.chmod(p, 0o644)
    return p


def open_call_window(query):
    KIDU.sh(['google-chrome', '--ozone-platform=wayland', f'--app=http://127.0.0.1:{PORT}/call?{query}'], wait=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def call_begin(cid, direction, caller, state):
    """Pause media and turn the speaker up for the whole call."""
    with CALLMX:
        if CALL.get('id') == cid:
            return False
        if CALL.get('id'):
            call_cleanup_locked()
        vol, muted = KIDU.volume()
        CALL.update(id=cid, dir=direction, caller=caller, state=state, paused=pause_media(), vol=vol, muted=muted,
                    at=time.time())
        KIDU.sh(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0'])
        KIDU.sh(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{max(VOLUME, vol or 0):.2f}'])
        return True


def call_cleanup_locked():
    if not CALL:
        return
    log('call over', CALL.get('id'), CALL.get('state'))
    if CALL.get('vol') is not None:
        KIDU.sh(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f"{CALL['vol']:.2f}"])
    if CALL.get('muted'):
        KIDU.sh(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '1'])
    close_note(CALL.get('note'))
    proc = CALL.get('proc')
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    paused = CALL.get('paused') or []
    CALL.clear()
    threading.Thread(target=resume_media, args=(paused,), daemon=True).start()


def call_cleanup(cid=None):
    with CALLMX:
        if CALL and (cid is None or CALL.get('id') == cid):
            call_cleanup_locked()


def greeter_env():
    """The login screen's user (gdm) with a running PipeWire: (uid, gid, env) or None."""
    for d in sorted(os.listdir('/run/user')):
        if not d.isdigit() or int(d) in (0, KIDU.uid) or not os.path.exists(f'/run/user/{d}/pipewire-0'):
            continue
        uid = int(d)
        try:
            gid = pwd.getpwuid(uid).pw_gid
        except KeyError:
            continue
        env = {'XDG_RUNTIME_DIR': f'/run/user/{uid}', 'PATH': '/usr/bin:/bin', 'HOME': f'/tmp/bocall-{uid}'}
        if os.path.exists(f'/run/user/{uid}/bus'):
            env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path=/run/user/{uid}/bus'
        return uid, gid, env
    return None


def greeter_player():
    """At the login screen nobody's session is there to play sound: use the greeter's PipeWire
    (gdm's /run/user/<uid>/pipewire-0), else plain ALSA as root. Returns a function path -> Popen."""
    g = greeter_env()
    if g:
        uid, gid, env = g
        kw = dict(user=uid, group=gid, extra_groups=[], env=env, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0'], timeout=5, **kw)
        subprocess.run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{VOLUME:.2f}'], timeout=5, **kw)
        return lambda path: subprocess.Popen(['pw-play', path], **kw)
    return lambda path: subprocess.Popen(['aplay', '-q', path], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def headless_call(url, uid, gid, env):
    """Answer the call in a hidden Chrome (it has the echo canceller) as `uid`, with its audio."""
    d = f'/tmp/bocall-{uid}'
    os.makedirs(d, exist_ok=True)
    os.chown(d, uid, gid)
    os.chmod(d, 0o700)
    cmd = ['google-chrome', '--headless=new', f'--user-data-dir={d}', '--no-first-run', '--no-default-browser-check',
           '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required', '--disable-gpu', url]
    logf = open(os.path.join(STATE_DIR, 'call-chrome.log'), 'ab')
    return subprocess.Popen(cmd, user=uid, group=gid, extra_groups=[], env=dict(env, HOME=env.get('HOME', d)),
                            cwd=d, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)


def auto_answer(c):
    """"Gọi tự trả lời": beep + say who calls, then a hidden Chrome answers, logged in or not."""
    cid, name = c['id'], PARENT.get(c.get('caller'), 'Bố')
    url = f'http://127.0.0.1:{PORT}/call?id={urllib.parse.quote(cid)}&auto=1'
    say = tts(f'{name} gọi, máy tự nghe máy.', 'call-say.wav')
    if KIDU.graphical():
        if not call_begin(cid, 'down', c.get('caller'), 'ringing'):
            return
        log('call auto-answer in his session', cid, name)
        for p in (chime_path(), say):
            if p:
                KIDU.sh(['pw-play', p], timeout=15)
        CALL['proc'] = headless_call(url, KIDU.uid, KIDU.gid, KIDU.env())
        threading.Thread(target=auto_note, args=(cid, name), daemon=True).start()
    else:
        g = greeter_env()
        if not g:
            log('call auto-answer: no login-screen audio')
            relay('POST', f'/agent/call/{cid}/end', {'reason': 'nologin'})
            return
        log('call auto-answer at the login screen', cid, name)
        play = greeter_player()
        for p in (chime_path(), say):
            if p:
                try:
                    play(p).wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
        with CALLMX:
            if CALL.get('id'):
                call_cleanup_locked()
            CALL.update(id=cid, dir='down', caller=c.get('caller'), state='ringing', paused=[], vol=None,
                        at=time.time(), proc=headless_call(url, *g))
    time.sleep(25)
    if CALL.get('id') == cid and CALL.get('state') == 'ringing':
        log('call auto-answer did not connect', cid)
        call_ui('/api/call/end', {'id': cid})


def auto_note(cid, name):
    """While an auto-answered call runs in his session: a notification with Kết thúc."""
    p = KIDU.sh(['notify-send', '-p', '--wait', '-u', 'critical', '-a', 'Gọi điện - Nhắn tin con', '-i', '/opt/kids-talk/icon.png',
                 '--action=end=Kết thúc', f'📞 {name} đang nói chuyện với con', 'Máy tự nghe máy. Bấm Kết thúc để dừng.'],
                wait=False)
    try:
        nid = p.stdout.readline().strip()
        if CALL.get('id') == cid and nid.isdigit():
            CALL['note'] = int(nid)
        act = p.stdout.readline().strip()
        if act == 'end' and CALL.get('id') == cid:
            call_ui('/api/call/end', {'id': cid})
    except Exception:  # noqa: BLE001
        pass
    finally:
        if p.poll() is None:
            p.kill()


def ringer(c):
    """A parent is calling: ring until he answers, declines, or the parent gives up.
    Not logged in: ring + say who calls at the login screen; the call window opens once he logs in.
    Screen locked: the window opens behind the lock and the voice asks him to unlock."""
    cid, name = c['id'], PARENT.get(c.get('caller'), 'Bố')
    if meta('lock_applied', False):
        relay('POST', f'/agent/call/{cid}/end', {'reason': 'locked'})
        return
    ring, end = ring_path(), time.time() + RING_MAX

    def still_ringing():
        st, j = relay('GET', f'/agent/call/{cid}', timeout=8)
        return st == 200 and j.get('state') == 'ringing'

    def play_until(pr, ok):
        while pr.poll() is None and ok():
            time.sleep(0.15)
        if pr.poll() is None:
            pr.kill()   # answered or hung up mid-ring: stop at once

    # 1) nobody logged in: ring at the login screen
    if not KIDU.graphical():
        log('call ringing at the login screen', cid, name)
        play = greeter_player()
        say = tts(f'{name} đang gọi. Con đăng nhập máy để nghe máy.', 'call-say.wav')
        n = 0
        while time.time() < end and not KIDU.graphical():
            if not still_ringing():
                return
            for p in ([say] if say and n % 3 == 0 else []) + [ring]:
                play_until(play(p), lambda: time.time() < end and not KIDU.graphical())
            n += 1
        if not KIDU.graphical():
            relay('POST', f'/agent/call/{cid}/end', {'reason': 'nologin'})
            return
        time.sleep(4)   # let the desktop come up before opening a window
        if not still_ringing():
            return
    # 2) his session (maybe locked)
    if not call_begin(cid, 'down', c.get('caller'), 'ringing'):
        return
    log('call ringing', cid, name)
    open_call_window('id=' + urllib.parse.quote(cid))
    CALL['note'] = show_note(f'📞 {name} đang gọi', 'Bấm Nghe trên cửa sổ cuộc gọi', 'critical')
    ringing = lambda: time.time() < end and CALL.get('id') == cid and CALL.get('state') == 'ringing'
    unlock = None
    n = 0
    while ringing():
        if (KIDU.focus() or {}).get('locked') and n % 3 == 0:
            unlock = unlock or tts(f'{name} đang gọi. Con mở khóa máy để nghe máy.', 'call-say.wav')
            if unlock:
                play_until(KIDU.sh(['pw-play', unlock], wait=False, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL), ringing)
        play_until(KIDU.sh(['pw-play', ring], wait=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), ringing)
        n += 1
    if CALL.get('id') == cid and CALL.get('state') == 'ringing':
        call_cleanup(cid)


def on_call_sync(c):
    """The relay's open call (or None), seen on every sync."""
    if c and c.get('dir') == 'down' and c.get('state') == 'ringing' and c['id'] not in RANG:
        RANG.add(c['id'])
        threading.Thread(target=auto_answer if c.get('auto') else ringer, args=(c,), daemon=True).start()
    cur = CALL.get('id')
    if cur and (not c or c['id'] != cur) and time.time() - CALL.get('at', 0) > 8:
        call_cleanup(cur)   # ended elsewhere (parent hung up, timeout) and the window did not tell us


def call_ui(path, b):
    """Local endpoints for call.html. Returns (status, body)."""
    if path == '/api/call/ice':
        st, j = relay('GET', '/agent/ice')
        if st != 200:
            return st, j
        return 200, {'iceServers': j.get('iceServers') or [], 'gain': CALL_GAIN,
                     'relay': CALL_RELAY and any('turn' in u for s in j.get('iceServers') or [] for u in s.get('urls') or [])}
    if path.startswith('/api/call/info'):
        cid = urllib.parse.parse_qs(urllib.parse.urlparse(path).query).get('id', [''])[0]
        if not re.fullmatch(r'[\w-]{4,40}', cid):
            return 400, {'error': 'id'}
        st, j = relay('GET', f'/agent/call/{cid}')
        if st == 200 and j.get('state') == 'ended':
            call_cleanup(cid)
        return st, open_call(j)
    if path == '/api/call/start':
        if meta('lock_applied', False):
            return 409, {'error': 'Máy đang khóa'}
        st, j = relay('POST', '/agent/call', {'offer': enc_text(b.get('offer'))})
        if st == 200:
            call_begin(j['id'], 'up', 'con', 'ringing')
            log('call out', j['id'])
        return st, j
    cid = str(b.get('id', ''))
    if not re.fullmatch(r'[\w-]{4,40}', cid):
        return 400, {'error': 'id'}
    if path == '/api/call/answer':
        st, j = relay('POST', f'/agent/call/{cid}/answer', {'answer': enc_text(b.get('answer'))})
        if st == 200 and CALL.get('id') == cid:
            CALL['state'] = 'active'
            close_note(CALL.pop('note', None))
        return st, j
    if path == '/api/call/end':
        if CALL.get('id') == cid:
            CALL['state'] = 'ending'
        st, j = relay('POST', f'/agent/call/{cid}/end', {})
        call_cleanup(cid)
        return st, j
    return 404, {'error': 'not found'}


# ---------------------------------------------------------------- sync
def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method='POST',
                                 headers={'Authorization': f'Bearer {TOKEN}', 'content-type': 'application/json',
                                          'user-agent': 'kids-talk-agent/' + VERSION})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def sync_once():
    upload_voices()
    ops = [seal_op(json.loads(r['op'])) for r in q('SELECT op FROM outbox ORDER BY at LIMIT 100')]
    apps, total, recent = summary()
    L = meta('lock_want', {}) or {}
    status = dict(CUR['status'], today=apps, screen_s=total, recent=recent, agent=VERSION,
                  lock={'on': bool(meta('lock_applied', False)), 'until': L.get('until')})
    msg_since = meta('msg_since', 0)
    res = post('/agent/sync', {'outbox': ops, 'status': seal_status(status), 'msg_since': msg_since})
    set_meta('last_ok', time.time())
    res['messages'] = [open_message(m) for m in res.get('messages', [])]
    res['tasks'] = [open_task(t) for t in res.get('tasks', [])]
    if 'lock' in res:
        reconcile_lock(open_lock(res['lock']))
    if 'call' in res:
        on_call_sync(res['call'])
    for cid in res.get('applied', []):
        q('DELETE FROM outbox WHERE cid=?', cid)

    present = KIDU.graphical()
    fresh = []
    for m in res.get('messages', []):
        old = q1('SELECT read_at FROM messages WHERE id=?', m['id'])
        q('INSERT OR REPLACE INTO messages(id,sender,body,speak,created_at,delivered_at,read_at,audio,audio_ms,repeat_play) '
          'VALUES(?,?,?,?,?,?,?,?,?,?)',
          m['id'], m['sender'], m['body'], m['speak'], m['created_at'], m['delivered_at'],
          (old['read_at'] if old and old['read_at'] else m['read_at']), m.get('audio'), m.get('audio_ms'),
          m.get('repeat_play') or 0)
        msg_since = max(msg_since, m['id'])
        if m['sender'] in PARENT and not m['delivered_at']:
            fresh.append(m)
    if fresh and not present:
        # Nobody to tell: keep them undelivered and fetch them again next time.
        msg_since = min(m['id'] for m in fresh) - 1
    elif fresh:
        for m in fresh[-3:]:  # after a long offline spell, read only the last three
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
            if due and not t.get('done_at') and now - 6 * 3600_000 < due <= now:
                if not q1('SELECT 1 FROM reminded WHERE task_id=? AND due_at=?', t['id'], due):
                    q('INSERT INTO reminded(task_id,due_at) VALUES(?,?)', t['id'], due)
                    SAY.put(('remind', t['title'], None, None, False, t.get('created_by')))

    for c in res.get('commands', []):
        if c['kind'] == 'shot' and c['id'] not in DONE_CMDS:
            DONE_CMDS.add(c['id'])
            threading.Thread(target=take_shot, args=(c['id'],), daemon=True).start()


DONE_CMDS = set()


def syncer():
    backoff = 4
    while True:
        WAKE.clear()
        try:
            sync_once()
            backoff, pause = 4, SYNC_EVERY
        except urllib.error.HTTPError as e:
            log('sync http', e.code, e.read()[:200])
            backoff = pause = min(backoff * 2, 120)
        except Exception as e:  # noqa: BLE001
            log('sync failed', repr(e))
            backoff = pause = min(backoff * 2, 60)
        WAKE.wait(pause)


VER = {'v': 0}


def waiter():
    """Long-poll: the relay holds this until something for this computer changes (max WAIT_S), then sync."""
    backoff = 2
    while True:
        try:
            req = urllib.request.Request(f"{URL}/agent/wait?ver={VER['v']}&t={WAIT_S}",
                                         headers={'Authorization': f'Bearer {TOKEN}',
                                                  'user-agent': 'kids-talk-agent/' + VERSION})
            with urllib.request.urlopen(req, timeout=WAIT_S + 15) as r:
                res = json.loads(r.read())
            backoff = 2
            if res.get('changed'):
                VER['v'] = res.get('ver') or 0
                WAKE.set()
        except Exception as e:  # noqa: BLE001
            log('wait failed', repr(e))
            time.sleep(backoff + random.random() * 2)   # spread reconnects after a relay restart
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------- child UI
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

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {'error': 'host'})
        if self.path in ('/', '/index.html'):
            return self.send(200, open(os.path.join(HERE, 'kid.html'), 'rb').read(), 'text/html; charset=utf-8')
        if self.path.split('?')[0] == '/call':
            return self.send(200, open(os.path.join(HERE, 'call.html'), 'rb').read(), 'text/html; charset=utf-8')
        if self.path.startswith('/api/call/'):
            return self.send(*call_ui(self.path, {}))
        if self.path == '/icon.png':
            return self.send(200, open('/opt/kids-talk/icon.png', 'rb').read(), 'image/png')
        m = re.fullmatch(r'/voice/([\w-]{4,64})', self.path)
        if m:
            p = voice_file(m.group(1))
            return self.send(200, open(p, 'rb').read(), 'audio/wav') if p else self.send(404, {'error': 'voice'})
        if self.path == '/api/state':
            return self.send(200, ui_state())
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        if not self.host_ok() or self.headers.get('x-bo') != '1':
            return self.send(403, {'error': 'forbidden'})
        n = int(self.headers.get('content-length') or 0)
        try:
            b = json.loads(self.rfile.read(min(n, 65536)) or b'{}')
        except ValueError:
            return self.send(400, {'error': 'json'})
        p = self.path
        if p.startswith('/api/call/'):
            return self.send(*call_ui(p, b))
        if p == '/api/send':
            add_kid_message(str(b.get('text', '')))
        elif p == '/api/repeat/stop':
            stop_repeat()
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
        else:
            return self.send(404, {'error': 'not found'})
        self.send(200, ui_state())


def serve_ui():
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), UI)
    srv.daemon_threads = True
    srv.serve_forever()


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log('Gọi điện - Nhắn tin con agent', VERSION, 'kid', KID, 'relay', URL)
    for fn in (serve_ui, announcer, sampler, syncer, waiter):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    main()

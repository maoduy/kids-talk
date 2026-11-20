-- Gọi điện - Nhắn tin con: relay between the parent's PWA and the agent on the child's laptop.
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender TEXT NOT NULL,            -- 'bo' | 'me' | 'con' | 'he' (system note)
  body TEXT NOT NULL,
  speak INTEGER NOT NULL DEFAULT 1,
  audio TEXT,                      -- KV key voice:<audio> (WAV), NULL for text messages
  audio_ms INTEGER,
  repeat_play INTEGER NOT NULL DEFAULT 0,  -- 1: the laptop repeats it (10 s apart, max 1 h) until the child stops it
  cid TEXT UNIQUE,                 -- client id for idempotent agent uploads
  created_at INTEGER NOT NULL,
  delivered_at INTEGER,
  read_at INTEGER,
  device TEXT NOT NULL DEFAULT 'kid1'  -- which child's computer
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  due_at INTEGER,
  created_by TEXT NOT NULL,        -- 'bo' | 'me' | 'con'
  done_at INTEGER,
  deleted INTEGER NOT NULL DEFAULT 0,
  overdue_notified INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  device TEXT NOT NULL DEFAULT 'kid1'
);
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,              -- 'shot'
  created_at INTEGER NOT NULL,
  done_at INTEGER,
  result TEXT,
  device TEXT NOT NULL DEFAULT 'kid1'
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS passkeys (
  id TEXT PRIMARY KEY,
  public_key TEXT NOT NULL,
  counter INTEGER NOT NULL DEFAULT 0,
  name TEXT,
  who TEXT NOT NULL DEFAULT 'bo',  -- 'bo' | 'me': whose phone this passkey is
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, created_at INTEGER NOT NULL);
-- Live voice calls (WebRTC). Signalling only: SDP with all ICE candidates, polled by both sides.
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY,
  dir TEXT NOT NULL,               -- 'down' parent -> child, 'up' child -> parents
  caller TEXT NOT NULL,            -- 'bo' | 'me' | 'con'
  answerer TEXT,
  state TEXT NOT NULL,             -- 'ringing' | 'active' | 'ended'
  reason TEXT,                     -- hangup | missed | declined | cancelled | lost | locked | nologin
  ended_by TEXT,
  offer TEXT NOT NULL,
  answer TEXT,
  created_at INTEGER NOT NULL,
  answered_at INTEGER,
  ended_at INTEGER,
  p_seen INTEGER,                  -- last poll from the parent side (active calls)
  k_seen INTEGER,                  -- last poll from the child's laptop
  auto INTEGER NOT NULL DEFAULT 0, -- 1: the laptop answers by itself (logged in or not)
  device TEXT NOT NULL DEFAULT 'kid1'
);
-- Children's computers. token_hash = sha-256 hex of that agent's bearer token (the first computer uses AGENT_TOKEN: NULL).
CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS messages_device ON messages(device, id);

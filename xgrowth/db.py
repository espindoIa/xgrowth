"""Estado persistente em SQLite (um arquivo só, fácil de versionar no GitHub Actions)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date TEXT NOT NULL,          -- data local AAAA-MM-DD
    scheduled_at TEXT NOT NULL,       -- ISO UTC
    hour INTEGER NOT NULL,            -- hora local planejada
    posted_hour INTEGER,              -- hora local real da publicação
    hour_explored INTEGER DEFAULT 0,
    pillar TEXT, format TEXT, tone TEXT, hook TEXT,
    content_explored INTEGER DEFAULT 0,
    topic TEXT,
    tweets TEXT,                      -- JSON: lista de textos (1 = post único, >1 = thread)
    status TEXT NOT NULL,             -- planned|awaiting_approval|approved|drafted|posted|rejected|expired|skipped|failed
    tweet_id TEXT,
    thread_ids TEXT,                  -- JSON
    posted_at TEXT,
    followers_at_post INTEGER,
    attempts INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_tweet ON posts(tweet_id);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    age_hours REAL NOT NULL,
    impressions INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    quotes INTEGER DEFAULT 0,
    bookmarks INTEGER DEFAULT 0,
    profile_clicks INTEGER DEFAULT 0,
    url_clicks INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_metrics_post ON metrics(post_id);

CREATE TABLE IF NOT EXISTS mentions (
    tweet_id TEXT PRIMARY KEY,
    author_id TEXT,
    author_username TEXT,
    text TEXT,
    created_at TEXT,
    in_reply_to_tweet_id TEXT,
    mentioned_ids TEXT,               -- JSON
    status TEXT NOT NULL,             -- new|skipped|optout|awaiting_approval|approved|replied|rejected|expired|failed
    reason TEXT,
    reply_text TEXT,
    reply_tweet_id TEXT,
    handled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mentions_status ON mentions(status);

CREATE TABLE IF NOT EXISTS optouts (user_id TEXT PRIMARY KEY, username TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS followers (at TEXT NOT NULL, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS log (at TEXT NOT NULL, level TEXT NOT NULL, msg TEXT NOT NULL);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- helpers genéricos ----------------------------------------------
    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, args))

    def one(self, sql: str, *args):
        r = self.conn.execute(sql, args).fetchone()
        return r

    def x(self, sql: str, *args) -> int:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur.lastrowid

    def get(self, k: str, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", k)
        return json.loads(r["v"]) if r else default

    def set(self, k: str, v) -> None:
        self.x("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, json.dumps(v))

    def log(self, level: str, msg: str, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.x("INSERT INTO log(at, level, msg) VALUES(?, ?, ?)", iso(now), level, msg)
        print(f"[{level}] {msg}", flush=True)

    # ---- posts --------------------------------------------------------------
    def update_post(self, post_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in fields.values()]
        self.x(f"UPDATE posts SET {cols} WHERE id=?", *vals, post_id)

    def post_by_tweet(self, tweet_id: str):
        r = self.one("SELECT * FROM posts WHERE tweet_id=?", tweet_id)
        if r:
            return r
        for row in self.q("SELECT * FROM posts WHERE thread_ids IS NOT NULL AND status='posted'"):
            if tweet_id in json.loads(row["thread_ids"] or "[]"):
                return row
        return None

    def update_mention(self, tweet_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE mentions SET {cols} WHERE tweet_id=?", *fields.values(), tweet_id)

    def latest_followers(self) -> int | None:
        r = self.one("SELECT count FROM followers ORDER BY at DESC LIMIT 1")
        return r["count"] if r else None

"""SQLite：去重、已排時段、發佈意圖（pending）。

live 紀錄（status 為 pending / scheduled）對 norm_key、slot、url 有 UNIQUE（partial index）。
pending = 已打算送出或結果不明，必須人工對帳，期間 slot/url/話題都不得重用。
dry-run 只留稽核紀錄（mode='dry'），不受唯一約束。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .common import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  norm_key TEXT NOT NULL,
  keyword TEXT,
  urls TEXT,
  slot TEXT,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  fb_id TEXT,
  path TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS used_urls(
  url TEXT NOT NULL,
  post_id INTEGER,
  mode TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_posts_key ON posts(norm_key, mode);
CREATE INDEX IF NOT EXISTS ix_urls ON used_urls(url, mode);
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_key ON posts(norm_key)
  WHERE mode='live' AND status IN ('pending','scheduled');
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_slot ON posts(slot)
  WHERE mode='live' AND status IN ('pending','scheduled') AND slot IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_url ON used_urls(url) WHERE mode='live';
"""

LIVE_ACTIVE = "mode='live' AND status IN ('pending','scheduled')"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30)
    con.executescript(SCHEMA)
    return con


def is_duplicate(con, key: str, urls: list[str]) -> str | None:
    """回重複原因或 None（只比對 live 的 pending/scheduled）。"""
    if con.execute(f"SELECT 1 FROM posts WHERE norm_key=? AND {LIVE_ACTIVE}", (key,)).fetchone():
        return f"關鍵字已發過: {key}"
    for u in urls:
        if con.execute(
                f"SELECT 1 FROM used_urls WHERE url=? AND mode='live' AND post_id IN "
                f"(SELECT id FROM posts WHERE {LIVE_ACTIVE})", (u,)).fetchone():
            return f"來源網址已用過: {u}"
    return None


def taken_slots(con) -> set[str]:
    rows = con.execute(f"SELECT slot FROM posts WHERE {LIVE_ACTIVE} AND slot IS NOT NULL").fetchall()
    return {r[0] for r in rows}


def reserve(con, key, keyword, urls, slot, mode, path=None) -> int | None:
    """原子保留 話題 + 網址 + 時段。live 衝突（UNIQUE）回 None。live 初始 status='pending'，dry 為 'dry'。"""
    status = "pending" if mode == "live" else "dry"
    try:
        with con:
            cur = con.execute(
                "INSERT INTO posts(norm_key,keyword,urls,slot,mode,status,path) VALUES(?,?,?,?,?,?,?)",
                (key, keyword, "\n".join(urls), slot, mode, status, path))
            pid = cur.lastrowid
            con.executemany("INSERT INTO used_urls(url,post_id,mode) VALUES(?,?,?)", [(u, pid, mode) for u in urls])
        return pid
    except sqlite3.IntegrityError:
        return None


def mark(con, post_id: int, status: str, fb_id: str | None = None) -> None:
    with con:
        con.execute("UPDATE posts SET status=?, fb_id=COALESCE(?,fb_id) WHERE id=?", (status, fb_id, post_id))


def release(con, post_id: int) -> None:
    """確定失敗（FB 明確拒絕、未送出）：標 failed 並釋出網址，讓 slot/話題可重用。"""
    with con:
        con.execute("UPDATE posts SET status='failed' WHERE id=?", (post_id,))
        con.execute("DELETE FROM used_urls WHERE post_id=?", (post_id,))


def pending_posts(con) -> list[tuple]:
    return con.execute("SELECT id, keyword, slot, path FROM posts WHERE mode='live' AND status='pending'").fetchall()

"""One SQLite file shared by every worker process: retry dedupe, the 48-hour window,
handover flags, daily AI caps, the access_token cache, the conversation log and the queue
of customer-service messages still to send. WAL mode, short transactions, one connection
per call, so several uvicorn workers can use it at once."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  dedupe_key TEXT PRIMARY KEY, openid TEXT NOT NULL, received_at REAL NOT NULL,
  state TEXT NOT NULL,            -- working | passive | async | done | failed | ignored
  answer TEXT, delivered_by TEXT, -- passive | customer_service | none
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  openid TEXT PRIMARY KEY, last_inbound_at REAL, subscribed INTEGER NOT NULL DEFAULT 1,
  handover INTEGER NOT NULL DEFAULT 0, handover_at REAL, lang TEXT,
  ai_day TEXT, ai_calls INTEGER NOT NULL DEFAULT 0,
  cs_sent INTEGER NOT NULL DEFAULT 0  -- customer-service messages since their last message
);
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, openid TEXT NOT NULL,
  direction TEXT NOT NULL,        -- in | out | event | note
  channel TEXT, lang TEXT, text TEXT, status TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
  appid TEXT PRIMARY KEY, access_token TEXT NOT NULL, expires_at REAL NOT NULL, fetched_at REAL NOT NULL,
  source TEXT
);
CREATE TABLE IF NOT EXISTS cs_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT UNIQUE, openid TEXT NOT NULL, text TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL, last_error TEXT,
  state TEXT NOT NULL DEFAULT 'pending'  -- pending | sent | dead
);
CREATE TABLE IF NOT EXISTS disclosed (openid TEXT PRIMARY KEY, at REAL NOT NULL);  -- AI disclosure shown
CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, ai_calls INTEGER NOT NULL DEFAULT 0);
"""


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self):
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # ---- retries -----------------------------------------------------------------
    def claim(self, key: str, openid: str, now: Optional[float] = None) -> Optional[sqlite3.Row]:
        """Record a new message. Returns None if it is new (we own it), else the earlier row."""
        now = now or time.time()
        with self.tx() as c:
            row = c.execute("SELECT * FROM messages WHERE dedupe_key=?", (key,)).fetchone()
            if row:
                return row
            c.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (key, openid, now, "working", None, None, now))
            return None

    def get_message(self, key: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM messages WHERE dedupe_key=?", (key,)).fetchone()

    def set_message(self, key: str, state: str, answer: Optional[str] = None, delivered_by: Optional[str] = None,
                    only_if: Optional[str] = None) -> bool:
        """Move a message to a new state; with only_if, only from that state (a compare-and-set,
        so the request thread and the background answer can never both deliver it)."""
        with self.tx() as c:
            sql = "UPDATE messages SET state=?, answer=COALESCE(?, answer), delivered_by=COALESCE(?, delivered_by), " \
                  "updated_at=? WHERE dedupe_key=?"
            args = [state, answer, delivered_by, time.time(), key]
            if only_if:
                sql += " AND state=?"
                args.append(only_if)
            return c.execute(sql, args).rowcount == 1

    def prune_messages(self, older_than: float) -> int:
        with self.tx() as c:
            return c.execute("DELETE FROM messages WHERE received_at < ?", (older_than,)).rowcount

    # ---- users -------------------------------------------------------------------
    def touch_user(self, openid: str, at: float, lang: Optional[str] = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO users(openid, last_inbound_at, lang) VALUES (?,?,?) "
                      "ON CONFLICT(openid) DO UPDATE SET last_inbound_at=MAX(COALESCE(last_inbound_at,0), excluded.last_inbound_at), "
                      "lang=COALESCE(excluded.lang, lang), subscribed=1, "
                      "cs_sent=CASE WHEN excluded.last_inbound_at > COALESCE(last_inbound_at,0) THEN 0 ELSE cs_sent END", (openid, at, lang))

    def take_cs_slot(self, openid: str, now: float, window: float, per_window: int) -> Optional[str]:
        """WeChat allows a few customer-service messages (5) within 48 hours of the follower's
        last message. Returns None and counts one if there is room, else why not."""
        with self.tx() as c:
            u = c.execute("SELECT last_inbound_at, cs_sent, subscribed FROM users WHERE openid=?", (openid,)).fetchone()
            if not u or not u["last_inbound_at"]:
                return "no_inbound"
            if not u["subscribed"]:
                return "unsubscribed"
            if now - u["last_inbound_at"] > window:
                return "window_closed"
            if per_window and u["cs_sent"] >= per_window:
                return "budget_used"
            c.execute("UPDATE users SET cs_sent=cs_sent+1 WHERE openid=?", (openid,))
            return None

    def give_back_cs_slot(self, openid: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE users SET cs_sent=MAX(cs_sent-1,0) WHERE openid=?", (openid,))

    def set_subscribed(self, openid: str, subscribed: bool) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO users(openid, subscribed) VALUES (?,?) "
                      "ON CONFLICT(openid) DO UPDATE SET subscribed=excluded.subscribed", (openid, int(subscribed)))

    def user(self, openid: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM users WHERE openid=?", (openid,)).fetchone()

    def take_disclosure(self, openid: str) -> bool:
        """True exactly once per follower (across workers): the first message to them must say it is an AI."""
        with self.tx() as c:
            return c.execute("INSERT OR IGNORE INTO disclosed VALUES (?,?)", (openid, time.time())).rowcount == 1

    def forget_disclosure(self, openid: str) -> None:
        """The message that carried the disclosure never arrived: show it again next time."""
        with self.tx() as c:
            c.execute("DELETE FROM disclosed WHERE openid=?", (openid,))

    def set_handover(self, openid: str, on: bool) -> bool:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO users(openid) VALUES (?)", (openid,))
            return c.execute("UPDATE users SET handover=?, handover_at=? WHERE openid=?",
                             (int(on), time.time() if on else None, openid)).rowcount == 1

    def handovers(self) -> List[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM users WHERE handover=1 ORDER BY handover_at").fetchall()

    def take_ai_call(self, openid: str, day: str, per_user: int, per_day: int) -> Optional[str]:
        """Count one model call against both caps. Returns None if allowed, else which cap is full."""
        with self.tx() as c:
            total = c.execute("SELECT ai_calls FROM daily WHERE day=?", (day,)).fetchone()
            if per_day and total and total["ai_calls"] >= per_day:
                return "daily"
            c.execute("INSERT OR IGNORE INTO users(openid) VALUES (?)", (openid,))
            u = c.execute("SELECT ai_day, ai_calls FROM users WHERE openid=?", (openid,)).fetchone()
            used = u["ai_calls"] if u["ai_day"] == day else 0
            if per_user and used >= per_user:
                return "user"
            c.execute("UPDATE users SET ai_day=?, ai_calls=? WHERE openid=?", (day, used + 1, openid))
            c.execute("INSERT INTO daily(day, ai_calls) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET ai_calls=ai_calls+1",
                      (day,))
            return None

    # ---- log ---------------------------------------------------------------------
    def log(self, openid: str, direction: str, text: str = "", channel: Optional[str] = None,
            lang: Optional[str] = None, status: Optional[str] = None, detail: Optional[Dict] = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO log(at, openid, direction, channel, lang, text, status, detail) VALUES (?,?,?,?,?,?,?,?)",
                      (time.time(), openid, direction, channel, lang, text, status,
                       json.dumps(detail, ensure_ascii=False) if detail else None))

    def history(self, openid: Optional[str] = None, limit: int = 200) -> List[sqlite3.Row]:
        with self._conn() as c:
            if openid:
                return c.execute("SELECT * FROM log WHERE openid=? ORDER BY id DESC LIMIT ?", (openid, limit)).fetchall()[::-1]
            return c.execute("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()[::-1]

    # ---- access_token cache ------------------------------------------------------
    def get_token(self, appid: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM tokens WHERE appid=?", (appid,)).fetchone()

    def put_token(self, appid: str, token: str, expires_at: float, source: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO tokens VALUES (?,?,?,?,?) ON CONFLICT(appid) DO UPDATE SET "
                      "access_token=excluded.access_token, expires_at=excluded.expires_at, "
                      "fetched_at=excluded.fetched_at, source=excluded.source",
                      (appid, token, expires_at, time.time(), source))

    def drop_token(self, appid: str, token: str) -> None:
        """Forget a token WeChat refused, unless another worker already replaced it."""
        with self.tx() as c:
            c.execute("DELETE FROM tokens WHERE appid=? AND access_token=?", (appid, token))

    # ---- customer-service queue --------------------------------------------------
    def enqueue_cs(self, key: str, openid: str, text: str, at: Optional[float] = None) -> bool:
        with self.tx() as c:
            return c.execute("INSERT OR IGNORE INTO cs_queue(dedupe_key, openid, text, next_at) VALUES (?,?,?,?)",
                             (key, openid, text, at or time.time())).rowcount == 1

    def due_cs(self, now: Optional[float] = None, limit: int = 20) -> List[sqlite3.Row]:
        """Rows to send now: pending ones that are due, and 'sending' ones whose lease ran out
        (the worker sending them died)."""
        with self._conn() as c:
            return c.execute("SELECT * FROM cs_queue WHERE state IN ('pending','sending') AND next_at<=? "
                             "ORDER BY id LIMIT ?", (now or time.time(), limit)).fetchall()

    def claim_cs(self, row_id: int, now: float, lease: float = 60) -> Optional[sqlite3.Row]:
        """Take one queued message for sending. Only one caller can win (the request that just
        queued it, or the background sweep), so a message is never sent twice at the same time."""
        with self.tx() as c:
            n = c.execute("UPDATE cs_queue SET state='sending', next_at=? WHERE id=? AND state IN ('pending','sending') "
                          "AND next_at<=?", (now + lease, row_id, now)).rowcount
            return c.execute("SELECT * FROM cs_queue WHERE id=?", (row_id,)).fetchone() if n == 1 else None

    def cs_result(self, row_id: int, state: str, error: Optional[str] = None, next_at: Optional[float] = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE cs_queue SET state=?, last_error=?, attempts=attempts+1, next_at=COALESCE(?, next_at) "
                      "WHERE id=?", (state, error, next_at, row_id))

    def cs_rows(self) -> List[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM cs_queue ORDER BY id").fetchall()

    def counts(self) -> Dict[str, int]:
        with self._conn() as c:
            out = {f"messages_{r['state']}": r["n"] for r in
                   c.execute("SELECT state, COUNT(*) n FROM messages GROUP BY state")}
            out.update({f"delivered_{r['delivered_by']}": r["n"] for r in
                        c.execute("SELECT delivered_by, COUNT(*) n FROM messages WHERE delivered_by IS NOT NULL "
                                  "GROUP BY delivered_by")})
            out.update({f"cs_{r['state']}": r["n"] for r in
                        c.execute("SELECT state, COUNT(*) n FROM cs_queue GROUP BY state")})
            out["handovers_open"] = c.execute("SELECT COUNT(*) FROM users WHERE handover=1").fetchone()[0]
            return out

"""SQLite storage. One shared connection guarded by a lock; small and fast enough for one user."""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, prompt TEXT, workdir TEXT,
    agent_pref TEXT DEFAULT 'auto',       -- auto | claude | local
    verify_cmd TEXT DEFAULT '',
    status TEXT DEFAULT 'queued',         -- queued|running|needs_input|review|done|failed|cancelled
    agent TEXT, account TEXT, model TEXT, session_id TEXT,
    category TEXT, complexity INTEGER, route_reason TEXT,
    progress REAL DEFAULT 0, todos TEXT, report TEXT, result_text TEXT,
    verify TEXT, changed_files TEXT, error TEXT,
    cost_usd REAL DEFAULT 0, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0, num_turns INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
    pending_message TEXT, feedback TEXT, rating INTEGER,
    last_event_at REAL, created_at REAL, started_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER, ts REAL, kind TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, task_id INTEGER, kind TEXT, name TEXT, model TEXT,
    cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER, duration_ms INTEGER,
    ok INTEGER, category TEXT
);
CREATE TABLE IF NOT EXISTS account_state (
    name TEXT PRIMARY KEY, cooldown_until REAL DEFAULT 0, last_error TEXT,
    limits TEXT, limits_at REAL
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT,             -- 'global' or a workdir path
    text TEXT, tags TEXT, source_task INTEGER,
    score REAL DEFAULT 1, uses INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, created_at REAL
);
CREATE TABLE IF NOT EXISTS permission_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule TEXT UNIQUE, count INTEGER DEFAULT 1, status TEXT DEFAULT 'pending', last_task INTEGER
);
"""

JSON_FIELDS = {"todos", "report", "verify", "changed_files", "data", "tags", "limits"}


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    # -- helpers -------------------------------------------------------------
    def _row(self, row):
        if row is None:
            return None
        d = dict(row)
        for k in JSON_FIELDS & d.keys():
            if d[k]:
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d

    def query(self, sql, args=()):
        with self.lock:
            return [self._row(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            return self._row(self.conn.execute(sql, args).fetchone())

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    @staticmethod
    def _enc(v):
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v

    # -- tasks ---------------------------------------------------------------
    def create_task(self, **fields):
        fields.setdefault("created_at", time.time())
        fields.setdefault("title", (fields.get("prompt") or "")[:80])
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})",
                            [self._enc(v) for v in fields.values()])

    def update_task(self, task_id, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE tasks SET {sets} WHERE id=?",
                     [self._enc(v) for v in fields.values()] + [task_id])

    def task(self, task_id):
        return self.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def tasks(self, limit=200):
        return self.query("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))

    def add_event(self, task_id, kind, data):
        ts = time.time()
        eid = self.execute("INSERT INTO events (task_id, ts, kind, data) VALUES (?,?,?,?)",
                           (task_id, ts, kind, json.dumps(data, ensure_ascii=False)))
        return {"id": eid, "task_id": task_id, "ts": ts, "kind": kind, "data": data}

    def events(self, task_id, after=0, limit=2000):
        return self.query("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
                          (task_id, after, limit))

    # -- usage ---------------------------------------------------------------
    def add_usage(self, **u):
        u.setdefault("ts", time.time())
        cols = ", ".join(u)
        self.execute(f"INSERT INTO usage ({cols}) VALUES ({', '.join('?' for _ in u)})",
                     list(u.values()))

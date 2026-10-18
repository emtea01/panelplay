"""SQLite store: settings, the arc catalogue, render jobs and rendered episodes."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS arcs (
    id         INTEGER PRIMARY KEY,
    strip      TEXT NOT NULL,
    title      TEXT NOT NULL,
    progs      TEXT NOT NULL,          -- JSON list of prog numbers, in reading order
    specials   TEXT NOT NULL DEFAULT '',  -- year-end progs ("Prog 2007") the archive can't hold
    episodes   INTEGER,
    credits    TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    season     INTEGER,                -- Plex season override; NULL = position within the strip
    source     TEXT NOT NULL DEFAULT 'manual',  -- 'index' or 'manual'
    hidden     INTEGER NOT NULL DEFAULT 0,
    created    REAL NOT NULL,
    UNIQUE (strip, title, progs)
);
CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY,
    arc_id    INTEGER NOT NULL REFERENCES arcs(id),
    mode      TEXT NOT NULL,           -- 'render' (reuse readings and voices) or 'reread' (ask Gemini again)
    only_prog INTEGER,                 -- NULL = whole arc
    status    TEXT NOT NULL,           -- queued, running, done, failed, cancelled
    progress  TEXT NOT NULL DEFAULT '',
    log       TEXT NOT NULL DEFAULT '',
    error     TEXT NOT NULL DEFAULT '',
    created   REAL NOT NULL,
    started   REAL,
    finished  REAL
);
CREATE TABLE IF NOT EXISTS episodes (
    arc_id      INTEGER NOT NULL REFERENCES arcs(id),
    prog        INTEGER NOT NULL,
    episode     INTEGER NOT NULL,
    status      TEXT NOT NULL,         -- rendered, failed, not_found
    path        TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    job_id      INTEGER,
    rendered_at REAL,
    PRIMARY KEY (arc_id, prog)
);
CREATE INDEX IF NOT EXISTS arcs_strip ON arcs(strip);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        # One short-lived connection per operation; the lock keeps the job thread and requests from colliding.
        with self._lock:
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            finally:
                c.close()

    # ---------- settings ----------
    def get_settings(self) -> dict:
        with self.conn() as c:
            return {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key, value FROM settings")}

    def set_settings(self, values: dict):
        with self.conn() as c:
            c.executemany("INSERT INTO settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          [(k, json.dumps(v)) for k, v in values.items()])

    # ---------- arcs ----------
    def upsert_arc(self, c, arc: dict) -> bool:
        """Insert an index arc unless it's already there. Returns True if new."""
        cur = c.execute(
            "INSERT OR IGNORE INTO arcs (strip, title, progs, specials, episodes, credits, notes, source, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (arc["strip"], arc["title"], json.dumps(arc["progs"]), arc.get("specials", ""), arc.get("episodes"),
             arc.get("credits", ""), arc.get("notes", ""), arc.get("source", "manual"), time.time()))
        return cur.rowcount > 0

    def arc(self, arc_id: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM arcs WHERE id = ?", (arc_id,)).fetchone()
        return _arc(r) if r else None

    def strips(self) -> list:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT strip FROM arcs WHERE hidden = 0 ORDER BY strip")]

    def arcs(self, strip=None, q=None, include_hidden=False) -> list:
        sql, args = "SELECT * FROM arcs WHERE 1=1", []
        if not include_hidden:
            sql += " AND hidden = 0"
        if strip:
            sql += " AND strip = ?"
            args.append(strip)
        if q:
            sql += " AND (title LIKE ? OR strip LIKE ? OR notes LIKE ? OR credits LIKE ?)"
            args += [f"%{q}%"] * 4
        with self.conn() as c:
            return [_arc(r) for r in c.execute(sql, args)]

    def strip_arcs(self, strip: str) -> list:
        with self.conn() as c:
            return [_arc(r) for r in c.execute("SELECT * FROM arcs WHERE strip = ?", (strip,))]

    def update_arc(self, arc_id: int, **fields):
        if "progs" in fields:
            fields["progs"] = json.dumps(fields["progs"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE arcs SET {sets} WHERE id = ?", (*fields.values(), arc_id))

    def add_arc(self, arc: dict) -> int:
        with self.conn() as c:
            self.upsert_arc(c, arc)
            return c.execute("SELECT id FROM arcs WHERE strip = ? AND title = ? AND progs = ?",
                             (arc["strip"], arc["title"], json.dumps(arc["progs"]))).fetchone()[0]

    # ---------- jobs ----------
    def add_job(self, arc_id: int, mode: str, only_prog=None) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO jobs (arc_id, mode, only_prog, status, created) VALUES (?, ?, ?, 'queued', ?)",
                             (arc_id, mode, only_prog, time.time())).lastrowid

    def job(self, job_id: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(r) if r else None

    def next_queued_job(self):
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        return dict(r) if r else None

    def jobs(self, limit=100, arc_id=None) -> list:
        sql = ("SELECT jobs.*, arcs.strip, arcs.title FROM jobs JOIN arcs ON arcs.id = jobs.arc_id"
               + (" WHERE arc_id = ?" if arc_id else "") + " ORDER BY jobs.id DESC LIMIT ?")
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, ((arc_id, limit) if arc_id else (limit,)))]

    def active_jobs_by_arc(self) -> dict:
        with self.conn() as c:
            return {r["arc_id"]: r["status"] for r in
                    c.execute("SELECT arc_id, status FROM jobs WHERE status IN ('queued', 'running')")}

    def update_job(self, job_id: int, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))

    def append_log(self, job_id: int, text: str, keep=20000):
        with self.conn() as c:
            c.execute("UPDATE jobs SET log = substr(log || ?, -?) WHERE id = ?", (text, keep, job_id))

    def fail_interrupted_jobs(self):
        with self.conn() as c:
            c.execute("UPDATE jobs SET status = 'failed', error = 'Interrupted (the portal was restarted)', "
                      "finished = ? WHERE status = 'running'", (time.time(),))

    # ---------- episodes ----------
    def set_episode(self, arc_id, prog, episode, status, path="", detail="", job_id=None):
        with self.conn() as c:
            c.execute("INSERT INTO episodes (arc_id, prog, episode, status, path, detail, job_id, rendered_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(arc_id, prog) DO UPDATE SET "
                      "episode = excluded.episode, status = excluded.status, path = excluded.path, "
                      "detail = excluded.detail, job_id = excluded.job_id, rendered_at = excluded.rendered_at",
                      (arc_id, prog, episode, status, path, detail, job_id, time.time()))

    def episodes(self, arc_id: int) -> dict:
        with self.conn() as c:
            return {r["prog"]: dict(r) for r in c.execute("SELECT * FROM episodes WHERE arc_id = ?", (arc_id,))}

    def rendered_counts(self) -> dict:
        with self.conn() as c:
            return {r[0]: r[1] for r in
                    c.execute("SELECT arc_id, COUNT(*) FROM episodes WHERE status = 'rendered' GROUP BY arc_id")}


def _arc(r) -> dict:
    a = dict(r)
    a["progs"] = json.loads(a["progs"])
    return a

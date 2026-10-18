"""SQLite store: settings, the library (series and issues), the arc catalogue, render jobs and episodes."""
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
CREATE TABLE IF NOT EXISTS series (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,   -- folder relative to the comics root, or "komga:<id>"
    name          TEXT NOT NULL,
    year          INTEGER,
    publisher     TEXT NOT NULL DEFAULT '',
    cv_id         INTEGER,                -- Comic Vine volume id (from Mylar's series.json)
    description   TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'auto',  -- auto, anthology, single (one story per issue)
    present       INTEGER NOT NULL DEFAULT 1,    -- 0 once a scan no longer finds it
    index_status  TEXT NOT NULL DEFAULT '',      -- '', queued, running, done, failed
    index_message TEXT NOT NULL DEFAULT '',
    indexed_at    REAL,
    scanned_at    REAL
);
CREATE TABLE IF NOT EXISTS issues (
    series_id INTEGER NOT NULL REFERENCES series(id),
    number    INTEGER NOT NULL,
    year      INTEGER,
    date      TEXT,                        -- cover date from Comic Vine (YYYY-MM-DD), when known
    file      TEXT NOT NULL,
    PRIMARY KEY (series_id, number)
);
CREATE TABLE IF NOT EXISTS arcs (
    id         INTEGER PRIMARY KEY,
    series_id  INTEGER REFERENCES series(id),  -- the title it ran in; NULL = 2000 AD (from before the library)
    strip      TEXT NOT NULL,
    title      TEXT NOT NULL,
    progs      TEXT NOT NULL,          -- JSON list of issue (prog) numbers, in reading order
    specials   TEXT NOT NULL DEFAULT '',  -- year-end progs ("Prog 2007") the archive can't hold
    episodes   INTEGER,
    credits    TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    season     INTEGER,                -- Plex season override; NULL = position within the strip
    source     TEXT NOT NULL DEFAULT 'manual',  -- 'index', 'comicvine', 'gemini' or 'manual'
    hidden     INTEGER NOT NULL DEFAULT 0,
    created    REAL NOT NULL,
    UNIQUE (series_id, strip, title, progs)
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
CREATE INDEX IF NOT EXISTS arcs_strip ON arcs(strip COLLATE NOCASE);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self.conn() as c:
            self._migrate(c)
            c.executescript(SCHEMA)

    @staticmethod
    def _migrate(c):
        """Databases from before the library: give arcs a series_id (and the new unique key)."""
        cols = [r[1] for r in c.execute("PRAGMA table_info(arcs)")]
        if cols and "series_id" not in cols:
            c.execute("PRAGMA legacy_alter_table = ON")  # leave jobs/episodes pointing at "arcs", not "arcs_old"
            c.execute("ALTER TABLE arcs RENAME TO arcs_old")
            c.execute("DROP INDEX IF EXISTS arcs_strip")
            c.executescript(SCHEMA)
            c.execute("INSERT INTO arcs (id, strip, title, progs, specials, episodes, credits, notes, season, source, "
                      "hidden, created) SELECT id, strip, title, progs, specials, episodes, credits, notes, season, "
                      "source, hidden, created FROM arcs_old")
            c.execute("DROP TABLE arcs_old")

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

    # ---------- library ----------
    def save_scan(self, found: list[dict]):
        """Store a library scan: add or update each series and replace its issue list; mark the rest absent."""
        now = time.time()
        with self.conn() as c:
            c.execute("UPDATE series SET present = 0")
            for sr in found:
                c.execute("INSERT INTO series (key, name, year, publisher, cv_id, description, present, scanned_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, 1, ?) ON CONFLICT(key) DO UPDATE SET name = excluded.name, "
                          "year = excluded.year, publisher = excluded.publisher, cv_id = excluded.cv_id, "
                          "description = excluded.description, present = 1, scanned_at = excluded.scanned_at",
                          (sr["key"], sr["name"], sr["year"], sr["publisher"], sr["cv_id"], sr["description"], now))
                sid = c.execute("SELECT id FROM series WHERE key = ?", (sr["key"],)).fetchone()[0]
                dates = {r[0]: r[1] for r in c.execute("SELECT number, date FROM issues WHERE series_id = ?", (sid,))}
                c.execute("DELETE FROM issues WHERE series_id = ?", (sid,))
                c.executemany("INSERT INTO issues (series_id, number, year, date, file) VALUES (?, ?, ?, ?, ?)",
                              [(sid, n, i["year"], dates.get(n), i["file"]) for n, i in sr["issues"].items()])
            # Stories imported before the library existed are all 2000 AD.
            main = c.execute("SELECT id FROM series WHERE present = 1 AND (cv_id = 19752 OR name = '2000 AD') "
                             "ORDER BY cv_id = 19752 DESC LIMIT 1").fetchone()
            if main:
                c.execute("UPDATE arcs SET series_id = ? WHERE series_id IS NULL", (main[0],))

    def series_list(self, present_only=True) -> list:
        sql = ("SELECT series.*, (SELECT COUNT(*) FROM issues WHERE series_id = series.id) AS issue_count, "
               "(SELECT COUNT(*) FROM arcs WHERE series_id = series.id AND hidden = 0) AS arc_count FROM series"
               + (" WHERE present = 1" if present_only else "") + " ORDER BY name COLLATE NOCASE, year")
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql)]

    def series(self, series_id):
        with self.conn() as c:
            r = c.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
        return dict(r) if r else None

    def main_series(self):
        """The 2000 AD weekly, which the Wikimedia indexes describe."""
        with self.conn() as c:
            r = c.execute("SELECT * FROM series WHERE present = 1 AND (cv_id = 19752 OR name = '2000 AD') "
                          "ORDER BY cv_id = 19752 DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def update_series(self, series_id: int, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE series SET {sets} WHERE id = ?", (*fields.values(), series_id))

    def issues(self, series_id=None) -> dict:
        """{series_id: {number: {"year", "date", "file"}}} (just one series if given)."""
        sql, args = "SELECT * FROM issues", ()
        if series_id is not None:
            sql, args = sql + " WHERE series_id = ?", (series_id,)
        out = {}
        with self.conn() as c:
            for r in c.execute(sql, args):
                out.setdefault(r["series_id"], {})[r["number"]] = {"year": r["year"], "date": r["date"],
                                                                    "file": r["file"]}
        return out

    def set_issue_dates(self, series_id: int, dates: dict):
        with self.conn() as c:
            c.executemany("UPDATE issues SET date = ? WHERE series_id = ? AND number = ?",
                          [(d, series_id, n) for n, d in dates.items()])

    # ---------- arcs ----------
    def upsert_arc(self, c, arc: dict) -> bool:
        """Insert an index arc unless it's already there. Returns True if new."""
        cur = c.execute(
            "INSERT OR IGNORE INTO arcs (series_id, strip, title, progs, specials, episodes, credits, notes, source, "
            "created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (arc.get("series_id"), arc["strip"], arc["title"], json.dumps(arc["progs"]), arc.get("specials", ""),
             arc.get("episodes"), arc.get("credits", ""), arc.get("notes", ""), arc.get("source", "manual"),
             time.time()))
        return cur.rowcount > 0

    def arc(self, arc_id: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM arcs WHERE id = ?", (arc_id,)).fetchone()
        return _arc(r) if r else None

    def strips(self) -> list:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT strip FROM arcs WHERE hidden = 0 GROUP BY strip COLLATE NOCASE "
                                            "ORDER BY strip COLLATE NOCASE")]

    def arcs(self, strip=None, q=None, include_hidden=False, series_id=None) -> list:
        sql, args = "SELECT * FROM arcs WHERE 1=1", []
        if not include_hidden:
            sql += " AND hidden = 0"
        if strip:
            sql += " AND strip = ? COLLATE NOCASE"
            args.append(strip)
        if series_id:
            sql += " AND series_id = ?"
            args.append(series_id)
        if q:
            sql += " AND (title LIKE ? OR strip LIKE ? OR notes LIKE ? OR credits LIKE ?)"
            args += [f"%{q}%"] * 4
        with self.conn() as c:
            return [_arc(r) for r in c.execute(sql, args)]

    def strip_arcs(self, strip: str) -> list:
        """Every story of a strip (one Plex show), whichever title it ran in."""
        with self.conn() as c:
            return [_arc(r) for r in c.execute("SELECT * FROM arcs WHERE strip = ? COLLATE NOCASE", (strip,))]

    def delete_arcs(self, series_id: int, source: str):
        """Forget a series' stories from one source, except ones that have been rendered or queued."""
        with self.conn() as c:
            c.execute("DELETE FROM arcs WHERE series_id = ? AND source = ? AND id NOT IN (SELECT arc_id FROM jobs) "
                      "AND id NOT IN (SELECT arc_id FROM episodes)", (series_id, source))

    def update_arc(self, arc_id: int, **fields):
        if "progs" in fields:
            fields["progs"] = json.dumps(fields["progs"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE arcs SET {sets} WHERE id = ?", (*fields.values(), arc_id))

    def add_arc(self, arc: dict) -> int:
        with self.conn() as c:
            self.upsert_arc(c, arc)
            return c.execute("SELECT id FROM arcs WHERE series_id IS ? AND strip = ? AND title = ? AND progs = ?",
                             (arc.get("series_id"), arc["strip"], arc["title"], json.dumps(arc["progs"]))).fetchone()[0]

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

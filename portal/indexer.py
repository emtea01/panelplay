"""Finding the stories in a series that has no published index: Comic Vine's story arcs first, then Gemini
reading each issue's contents (one call per issue, cached), linking the parts of a story across issues.

Runs one series at a time on a background thread. A series waiting or being indexed has index_status
'queued' or 'running'; Gemini's daily limit pauses it, and indexing it again carries on from the cache.
"""
import json
import re
import threading
import time
from pathlib import Path

import requests

from . import catalog, settings as S

CV_API = "https://comicvine.gamespot.com/api"
CV_GAP = 18.5  # Comic Vine allows 200 requests an hour per resource
CONTENTS_SIDE = 640  # pages sent to Gemini for the contents: big enough to read the title logos


class Stop(Exception):
    pass


class Indexer:
    def __init__(self, db):
        self.db = db
        self._wake = threading.Event()
        self._stop_id = None
        self._last_cv = 0.0

    def start(self):
        with self.db.conn() as c:  # carry on with whatever a restart interrupted
            c.execute("UPDATE series SET index_status = 'queued' WHERE index_status = 'running'")
        threading.Thread(target=self._loop, daemon=True).start()

    def queue(self, series_id: int):
        self.db.update_series(series_id, index_status="queued", index_message="Waiting…")
        self._wake.set()

    def stop(self, series_id: int):
        sr = self.db.series(series_id)
        if sr and sr["index_status"] == "queued":
            self.db.update_series(series_id, index_status="", index_message="Stopped.")
        self._stop_id = series_id

    def _loop(self):
        while True:
            with self.db.conn() as c:
                r = c.execute("SELECT id FROM series WHERE index_status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if not r:
                self._wake.wait(30)
                self._wake.clear()
                continue
            sid = r[0]
            self._stop_id = None
            self.db.update_series(sid, index_status="running", index_message="Starting…")
            try:
                msg = self._index(self.db.series(sid))
                self.db.update_series(sid, index_status="done", index_message=msg, indexed_at=time.time())
            except Stop as e:
                self.db.update_series(sid, index_status="", index_message=str(e) or "Stopped.")
            except Exception as e:  # noqa: BLE001 -- shown on the Library page
                self.db.update_series(sid, index_status="failed", index_message=f"Failed: {e}"[:400])

    def _say(self, sid, msg):
        if self._stop_id == sid:
            raise Stop("Stopped. Find stories again to carry on.")
        self.db.update_series(sid, index_message=msg)

    # ---------- one series ----------

    def _index(self, series: dict) -> str:
        sid = series["id"]
        s = S.current(self.db)
        issues = self.db.issues(sid).get(sid, {})
        if not issues:
            return "No issues to look through."
        notes = []

        cv_arcs = []
        if s.get("comicvine_api_key") and series.get("cv_id"):
            cv_arcs = self._comicvine(series, issues, s)
            notes.append(f"{len(cv_arcs)} from Comic Vine")
        elif series.get("cv_id"):
            notes.append("no Comic Vine key, so Gemini only")

        if not s.get("gemini_api_key"):
            stories = cv_arcs
            notes.append("add a Gemini key to find the rest")
            paused = ""
        else:
            stories, paused = self._gemini(series, issues, s, cv_arcs)

        self.db.delete_arcs(sid, "comicvine")
        self.db.delete_arcs(sid, "gemini")
        with self.db.conn() as c:
            for a in stories:
                self.db.upsert_arc(c, {**a, "series_id": sid})
        msg = f"{len(stories)} stories ({', '.join(notes)})." if notes else f"{len(stories)} stories."
        return msg + (" " + paused if paused else "")

    # ---------- Comic Vine ----------

    def _cv_get(self, path, s, cache: Path | None = None, **params):
        if cache and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        wait = self._last_cv + CV_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_cv = time.time()
        r = requests.get(f"{CV_API}/{path}", params={"api_key": s["comicvine_api_key"], "format": "json", **params},
                         headers={"User-Agent": catalog.CV_USER_AGENT}, timeout=60)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Comic Vine HTTP {r.status_code}: not a JSON reply")
        if data.get("status_code") != 1:
            raise RuntimeError(f"Comic Vine: {data.get('error', 'error')}")
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data), encoding="utf-8")
        return data

    def _comicvine(self, series, issues, s) -> list[dict]:
        sid, cache = series["id"], S.data_dir() / "comicvine"
        self._say(sid, "Comic Vine: listing issues…")
        cv_issues, offset = {}, 0
        while True:
            data = self._cv_get("issues/", s, filter=f"volume:{series['cv_id']}", offset=offset, limit=100,
                                field_list="id,issue_number,cover_date")
            for it in data["results"]:
                try:
                    n = int(float(it["issue_number"]))
                except (TypeError, ValueError):
                    continue
                cv_issues[n] = it
            offset += 100
            if offset >= data.get("number_of_total_results", 0):
                break
        self.db.set_issue_dates(sid, {n: it["cover_date"] for n, it in cv_issues.items()
                                      if n in issues and it.get("cover_date")})

        arcs: dict[int, dict] = {}
        held = sorted(n for n in issues if n in cv_issues)
        for k, n in enumerate(held, 1):
            self._say(sid, f"Comic Vine: story arcs, issue {n} ({k} of {len(held)})")
            it = cv_issues[n]
            data = self._cv_get(f"issue/4000-{it['id']}/", s, cache=cache / f"issue_{it['id']}.json",
                                field_list="id,story_arc_credits")
            for arc in (data["results"] or {}).get("story_arc_credits") or []:
                arcs.setdefault(arc["id"], {"name": arc["name"], "progs": []})["progs"].append(n)
        out = []
        for a in arcs.values():
            strip, title = _split_arc_name(a["name"], series)
            out.append({"strip": strip, "title": title, "progs": sorted(set(a["progs"])), "source": "comicvine"})
        return out

    # ---------- Gemini ----------

    def _gemini(self, series, issues, s, cv_arcs) -> tuple[list[dict], str]:
        from reader import Reader
        from sources import FolderSource, KomgaSource, _thumb

        sid = series["id"]
        cfg = S.renderer_config(s, series)
        src = KomgaSource(cfg["komga"]) if cfg["source"] == "komga" else FolderSource(cfg["folder"])
        reader = Reader({"api_key": s["gemini_api_key"], "tag_model": s["gemini_tag_model"],
                         "read_model": s["gemini_read_model"], "requests_per_minute": s["gemini_rpm"]})
        cdir = contents_dir(s["work_path"], series)
        cdir.mkdir(parents=True, exist_ok=True)
        known = self.db.strips()

        covered = {n for a in cv_arcs for n in a["progs"]}
        nums = sorted(issues)
        kind = series["kind"]
        paused = ""

        def wanted(n, anthology):
            return anthology or n not in covered

        anthology = kind == "anthology"
        contents = {}
        todo = [n for n in nums if wanted(n, anthology)]
        k = 0
        while k < len(todo):
            n = todo[k]
            k += 1
            f = cdir / f"issue_{n:04d}.json"
            if not f.exists():
                self._say(sid, f"Gemini: reading the contents of #{n} ({k} of {len(todo)})")
                try:
                    book = src.prog(n)
                    thumbs = [_thumb(src.page(book, i)[0], CONTENTS_SIDE) for i in range(1, src.page_count(book) + 1)]
                    result = reader.contents(thumbs, series["name"], n, known)
                except RuntimeError as e:  # quota: keep what we have
                    paused = (f"Paused at #{n}: {e} Find stories again later to carry on "
                              f"({len(contents)} issues read so far).")
                    break
                except SystemExit as e:
                    contents[n] = []
                    self._say(sid, f"Couldn't open #{n}: {e}")
                    continue
                f.write_text(json.dumps(result.model_dump(), indent=1, ensure_ascii=False), encoding="utf-8")
            contents[n] = json.loads(f.read_text(encoding="utf-8"))["stories"]
            if kind == "auto" and not anthology and len(contents) >= 3:
                # Several strips per issue: an anthology, so every issue needs reading, covered or not.
                if sum(len(v) >= 2 for v in contents.values()) * 2 > len(contents):
                    anthology = True
                    self.db.update_series(sid, kind="anthology")
                    todo = todo + [x for x in nums if x not in todo]

        found = link_stories(series, nums, contents, known)
        return merge_with_comicvine(cv_arcs, found), paused


def contents_dir(work_path: str, series: dict) -> Path:
    from narrate import slug

    return Path(work_path) / "contents" / slug(catalog.series_label(series))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def canonical_strip(name: str, known: list[str]) -> str:
    """Gemini's name for a strip, matched to one the catalogue already uses ("Anderson" -> "Anderson, Psi-Division")."""
    n = _norm(name)
    for k in known:
        if _norm(k) == n:
            return k
    if len(n) >= 4:
        for k in sorted(known, key=len):
            kn = _norm(k)
            if kn.startswith(n + " ") or n.startswith(kn + " "):
                return k
    return name.strip()


def _split_arc_name(name: str, series: dict) -> tuple[str, str]:
    """Comic Vine arc names are often "Strip: Story"; otherwise the story belongs to the series' own strip."""
    if ":" in name:
        strip, title = (x.strip() for x in name.split(":", 1))
        if strip and title:
            return strip, title
    return series["name"], name.strip()


def link_stories(series: dict, nums: list[int], contents: dict, known: list[str]) -> list[dict]:
    """Join each issue's stories into arcs. The same strip and title in nearby issues, with part numbers (when
    printed) running on, is one story. Serials often print the title on part 1 only, so an untitled episode in
    the next issue or two carries on the strip's latest story, unless it says it's a part 1."""
    known = list(known)
    by_title: dict[tuple, dict] = {}
    latest: dict[str, dict] = {}  # strip -> its most recent story
    arcs = []
    index = {n: i for i, n in enumerate(nums)}

    def near(a, n):
        return a is not None and 0 < index[n] - index[a["last"]] <= 2

    for n in nums:
        for st in contents.get(n) or []:
            strip = canonical_strip(st["strip"], known)
            if strip not in known:
                known.append(strip)
            title, part = (st.get("title") or "").strip() or None, st.get("part")
            sk = _norm(strip)
            if title:
                a = by_title.get((sk, _norm(title)))
                if a is not None and a["last"] == n:
                    continue  # the same story split across pages of one issue
                cont = near(a, n) and part != 1 and (part is None or a["part"] is None or part == a["part"] + 1)
            else:
                a = latest.get(sk)
                if a is not None and a["last"] == n:
                    continue
                cont = near(a, n) and part != 1 and (part is None or a["part"] is None or part == a["part"] + 1)
            if cont:
                a["progs"].append(n)
                a["last"], a["part"] = n, part or (a["part"] + 1 if a["part"] else None)
            else:
                a = {"strip": strip, "title": title, "progs": [n], "last": n, "part": part}
                arcs.append(a)
            if title and not a["title"]:
                a["title"] = title
            if a["title"]:
                by_title[(sk, _norm(a["title"]))] = a
            latest[sk] = a
    out = []
    for a in arcs:
        title = a["title"] or f"Untitled ({catalog.issue_label(series, a['progs'][0])})"
        out.append({"strip": a["strip"], "title": title, "progs": a["progs"], "source": "gemini",
                    "episodes": len(a["progs"])})
    return out


def merge_with_comicvine(cv_arcs: list[dict], found: list[dict]) -> list[dict]:
    """Comic Vine's arcs win where Gemini found the same story (sharing issues and a similar title); the
    Gemini match supplies the strip, which Comic Vine often doesn't say."""
    def similar(a, b):
        x, y = set(_norm(a).split()), set(_norm(b).split())
        return bool(x and y) and (x <= y or y <= x or len(x & y) / len(x | y) >= 0.5)

    out, used = [], set()
    for cv in cv_arcs:
        for i, g in enumerate(found):
            if i not in used and set(cv["progs"]) & set(g["progs"]) and similar(cv["title"], g["title"]):
                used.add(i)
                cv = {**cv, "strip": g["strip"]}
                break
        out.append(cv)
    return out + [g for i, g in enumerate(found) if i not in used]

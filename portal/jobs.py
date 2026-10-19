"""Render queue: one job at a time, each a narrate.py subprocess whose @@ progress lines update the database."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

from . import brief, catalog, settings as S

PANEL_LINE = re.compile(r"^\s*panel \d+\s*$")


class Runner:
    def __init__(self, db):
        self.db = db
        self.proc = None
        self.current = None
        self._cancel = set()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="render-queue", daemon=True)

    def start(self):
        self.db.fail_interrupted_jobs()
        self._thread.start()

    def poke(self):
        self._wake.set()

    def cancel(self, job_id: int):
        job = self.db.job(job_id)
        if not job:
            return
        if job["status"] == "queued":
            self.db.update_job(job_id, status="cancelled", finished=time.time())
        elif job["status"] == "running" and self.current == job_id:
            self._cancel.add(job_id)
            if self.proc:
                self.proc.terminate()

    # ---------- the loop ----------
    def _loop(self):
        while True:
            job = self.db.next_queued_job()
            if not job:
                self._wake.wait(3)
                self._wake.clear()
                continue
            self.current = job["id"]
            try:
                self._run(job)
            except Exception as e:  # never let one bad job stop the queue
                self.db.update_job(job["id"], status="failed", error=f"{type(e).__name__}: {e}", finished=time.time())
            finally:
                self.current, self.proc = None, None

    def _run(self, job):
        db, jid = self.db, job["id"]
        s = S.current(db)
        arc = db.arc(job["arc_id"])
        missing = S.problems(s)
        if missing:
            db.update_job(jid, status="failed", error=" ".join(missing), finished=time.time())
            return
        progs = arc["progs"] if not job["only_prog"] else [job["only_prog"]]
        rendered = {p for p, e in db.episodes(arc["id"]).items() if e["status"] == "rendered"}
        if job["mode"] == "rest":
            progs = [p for p in arc["progs"] if p not in rendered]
            if not progs:
                db.update_job(jid, status="done", progress="Every episode was already rendered.", finished=time.time())
                return
        if job["mode"] == "art":
            progs = [p for p in progs if p in rendered]
            if not progs:
                db.update_job(jid, status="done", progress="No rendered episodes to redo the artwork of.",
                              finished=time.time())
                return
        if not arc["progs"]:
            db.update_job(jid, status="failed", error="This arc has no issue numbers the library can hold.",
                          finished=time.time())
            return
        series = db.series(arc["series_id"]) if arc["series_id"] else None
        if not series:
            db.update_job(jid, status="failed", error="This story's title isn't in the library (Library → Rescan).",
                          finished=time.time())
            return

        numbers = catalog.plex_numbers(arc, db.strip_arcs(arc["strip"]), catalog.issue_order(db.issues()))
        arc_dir = catalog.work_dir(s["work_path"], arc, series)
        if job["mode"] == "reread":
            # Forget what Gemini read, so the pages are found and read again. Voices stay cached.
            for p in progs:
                pdir = arc_dir / f"prog_{p:04d}"
                for f in [pdir / "tag.json", *pdir.glob("page_*.json")]:
                    f.unlink(missing_ok=True)
            (arc_dir / brief.BRIEF_FILE).unlink(missing_ok=True)
        else:
            _pages_from_contents(s, series, arc, progs, arc_dir)

        run_dir = S.data_dir() / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = run_dir / f"job_{jid}.yaml"
        cfg_file.write_text(yaml.safe_dump(S.renderer_config(s, series)), encoding="utf-8")
        cmd = [sys.executable, "-u", str(S.ROOT / "narrate.py"), "--events",
               "--strip", arc["strip"], "--arc", arc["title"], "--series", series["name"], "--work", str(arc_dir),
               "--progs", catalog.format_progs(arc["progs"]).replace(" ", ""),
               "--plex", ",".join(f"{p}={se}x{e}" for p, (se, e) in numbers.items()),
               "--config", str(cfg_file), "--cast", str(S.ROOT / s["cast_file"])]
        if job["mode"] == "art":
            cmd += ["--stage", "art"]
        if job["only_prog"] or job["mode"] in ("rest", "art"):
            cmd += ["--only", catalog.format_progs(progs).replace(" ", "")]

        db.update_job(jid, status="running", started=time.time(), progress="starting", log="", error="")
        db.append_log(jid, catalog.series_label(series) + ": " + ", ".join(
            catalog.issue_label(series, p) + (f" (s{numbers[p][0]:02d}e{numbers[p][1]:02d})" if p in numbers else "")
            for p in progs) + "\n")
        if job["mode"] != "art":
            db.update_job(jid, progress="casting: who speaks in this story, and how they sound")
            brief.make(s, series, arc, arc_dir, lambda msg: db.append_log(jid, msg))
        self.proc = subprocess.Popen(cmd, cwd=S.ROOT, env={**os.environ, **S.renderer_env(s)},
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace")
        current_prog, done, skipped, tail = None, 0, 0, []
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("@@"):
                try:
                    ev = json.loads(line[2:])
                except ValueError:
                    continue
                kind = ev.get("event")
                if kind == "prog_start":
                    current_prog = ev["prog"]
                    db.update_job(jid, progress=f"{catalog.issue_label(series, current_prog)} "
                                                f"(episode {ev['episode']} of {len(arc['progs'])})")
                elif kind == "stage":
                    db.update_job(jid, progress=f"{catalog.issue_label(series, ev['prog'])}: {ev['detail']}")
                elif kind == "episode_done":
                    done += 1
                    db.set_episode(arc["id"], ev["prog"], ev["episode"], "rendered", path=ev["path"], job_id=jid)
                    self._tell_plex(s, jid, ev["path"])
                elif kind == "art_done":
                    done += 1
                    self._tell_plex(s, jid, ev["path"], refresh=True)
                elif kind == "prog_skip":
                    skipped += 1
                    db.set_episode(arc["id"], ev["prog"], ev["episode"], ev["reason"], detail=ev.get("detail", ""),
                                   job_id=jid)
                continue
            if PANEL_LINE.match(line) or "HF_TOKEN" in line or not line.strip():
                continue
            tail = (tail + [line])[-15:]
            db.append_log(jid, line + "\n")
        rc = self.proc.wait()

        if jid in self._cancel:
            self._cancel.discard(jid)
            status, error = "cancelled", ""
        elif rc == 0:
            status, error = "done", ""
        else:
            status, error = "failed", _error_from(tail) or f"The renderer stopped (exit code {rc})."
            if current_prog is not None and current_prog not in db.episodes(arc["id"]):
                ep = arc["progs"].index(current_prog) + 1 if current_prog in arc["progs"] else 0
                db.set_episode(arc["id"], current_prog, ep, "failed", detail=error, job_id=jid)
        summary = (f"{done} episode{'s' if done != 1 else ''} "
                   + ("given new artwork" if job["mode"] == "art" else "rendered")) + (f", {skipped} skipped" if skipped else "")
        db.update_job(jid, status=status, error=error, progress=summary, finished=time.time())
        cfg_file.unlink(missing_ok=True)

    def _tell_plex(self, s, jid, path, refresh=False):
        if not (s["plex_url"] and s["plex_token"]):
            return
        try:
            from . import plex

            folder = plex.scan(s, path, refresh)
            self.db.append_log(jid, f"Asked Plex to {'refresh' if refresh else 'scan'} {folder}\n")
        except Exception as e:  # the episode is fine either way
            self.db.append_log(jid, f"Couldn't ask Plex to scan: {e}\n")


def _error_from(tail: list[str]) -> str:
    """The most useful line from the end of a failed run's output."""
    for line in reversed(tail):
        t = line.strip()
        if t and not t.startswith(("File ", "Traceback", "^", "~")):
            return t[:500]
    return ""


def _pages_from_contents(s, series, arc, progs, arc_dir):
    """If Gemini already listed an issue's contents while finding stories, take the story's pages from there
    instead of asking again."""
    from .indexer import _norm, canonical_strip, contents_dir

    cdir = contents_dir(s["work_path"], series)
    for p in progs:
        tag, f = arc_dir / f"prog_{p:04d}" / "tag.json", cdir / f"issue_{p:04d}.json"
        if tag.exists() or not f.exists():
            continue
        stories = json.loads(f.read_text(encoding="utf-8"))["stories"]
        mine = [st for st in stories if _norm(canonical_strip(st["strip"], [arc["strip"]])) == _norm(arc["strip"])]
        titled = [st for st in mine if _norm(st.get("title") or "") == _norm(arc["title"])]
        pick = (titled or mine)[:1]
        if pick and len(titled or mine) == 1:
            tag.parent.mkdir(parents=True, exist_ok=True)
            tag.write_text(json.dumps({"pages": pick[0]["pages"], "episode_title": None, "part": pick[0].get("part")},
                                      indent=2), encoding="utf-8")

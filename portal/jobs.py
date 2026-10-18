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

from . import catalog, settings as S

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
        if job["mode"] == "rest":
            rendered = {p for p, e in db.episodes(arc["id"]).items() if e["status"] == "rendered"}
            progs = [p for p in arc["progs"] if p not in rendered]
            if not progs:
                db.update_job(jid, status="done", progress="Every episode was already rendered.", finished=time.time())
                return
        if not arc["progs"]:
            db.update_job(jid, status="failed", error="This arc has no prog numbers the archive can hold.",
                          finished=time.time())
            return

        season = catalog.season_for(arc, db.strip_arcs(arc["strip"]))
        work = Path(s["work_path"])
        from narrate import slug

        arc_dir = work / f"{slug(arc['strip'])}--{slug(arc['title'])}"
        if job["mode"] == "reread":
            # Forget what Gemini read, so the pages are found and read again. Voices stay cached.
            for p in progs:
                pdir = arc_dir / f"prog_{p:04d}"
                for f in [pdir / "tag.json", *pdir.glob("page_*.json")]:
                    f.unlink(missing_ok=True)

        run_dir = S.data_dir() / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = run_dir / f"job_{jid}.yaml"
        cfg_file.write_text(yaml.safe_dump(S.renderer_config(s)), encoding="utf-8")
        cmd = [sys.executable, "-u", str(S.ROOT / "narrate.py"), "--events",
               "--strip", arc["strip"], "--arc", arc["title"],
               "--progs", catalog.format_progs(arc["progs"]).replace(" ", ""),
               "--season", str(season), "--config", str(cfg_file), "--cast", str(S.ROOT / s["cast_file"])]
        if job["only_prog"] or job["mode"] == "rest":
            cmd += ["--only", catalog.format_progs(progs).replace(" ", "")]

        db.update_job(jid, status="running", started=time.time(), progress="starting", log="", error="")
        db.append_log(jid, f"Season {season}, progs {catalog.format_progs(progs)}\n")
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
                    db.update_job(jid, progress=f"Prog {current_prog} (episode {ev['episode']} of {len(arc['progs'])})")
                elif kind == "stage":
                    db.update_job(jid, progress=f"Prog {ev['prog']}: {ev['detail']}")
                elif kind == "episode_done":
                    done += 1
                    db.set_episode(arc["id"], ev["prog"], ev["episode"], "rendered", path=ev["path"], job_id=jid)
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
        summary = f"{done} episode{'s' if done != 1 else ''} rendered" + (f", {skipped} skipped" if skipped else "")
        db.update_job(jid, status=status, error=error, progress=summary, finished=time.time())
        cfg_file.unlink(missing_ok=True)


def _error_from(tail: list[str]) -> str:
    """The most useful line from the end of a failed run's output."""
    for line in reversed(tail):
        t = line.strip()
        if t and not t.startswith(("File ", "Traceback", "^", "~")):
            return t[:500]
    return ""

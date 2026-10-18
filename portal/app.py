"""PanelPlay portal: browse story arcs, queue renders, watch progress, manage settings."""
import html
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import requests
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import catalog, settings as S
from .db import DB
from .jobs import Runner

HERE = Path(__file__).resolve().parent
PAGE_SIZE = 50

db = DB(S.data_dir() / "panelplay.db")
S.seed_from_dev_setup(db)
runner = Runner(db)


@asynccontextmanager
async def lifespan(_app):
    runner.start()
    yield


app = FastAPI(title="PanelPlay", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ---------- helpers ----------

def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%d %b %Y, %H:%M") if ts else ""


def _duration(a, b):
    if not a:
        return ""
    secs = int((b or time.time()) - a)
    return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"


templates.env.filters["when"] = _when
templates.env.globals["duration"] = _duration
templates.env.globals["format_progs"] = catalog.format_progs


class Archive:
    """Which progs the archive holds, with their years. Scanning a network share takes a moment, so it's cached."""

    def __init__(self):
        self.progs, self.checked, self.error, self._key = {}, 0.0, "", None

    def get(self, s, force=False) -> dict:
        key = (s["source"], s["comics_path"], s["komga_url"], s["komga_series"])
        if force or key != self._key or time.time() - self.checked > 900:
            try:
                self.progs, self.error = catalog.archive_progs(s), ""
            except Exception as e:
                self.progs, self.error = {}, f"Couldn't read the archive: {e}"
            self.checked, self._key = time.time(), key
        return self.progs


archive = Archive()
index_import = {"running": False, "message": ""}


def _arc_row(arc, held, rendered, active):
    total = len(arc["progs"])
    missing = [p for p in arc["progs"] if p not in held]
    done = rendered.get(arc["id"], 0)
    if arc["id"] in active:
        status = ("Rendering", "running") if active[arc["id"]] == "running" else ("Queued", "queued")
    elif total and done >= total:
        status = ("Rendered", "done")
    elif done:
        status = (f"{done} of {total} rendered", "partial")
    else:
        status = ("Not rendered", "none")
    available = bool(total) and not missing and not arc["specials"]
    return {**arc, "total": total, "missing": missing, "available": available, "status": status, "done": done}


def _render_page(request, name, **ctx):
    return templates.TemplateResponse(request, name, {"nav": name.split(".")[0], **ctx})


# ---------- arcs ----------

@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/arcs")


@app.get("/arcs", response_class=HTMLResponse)
def arcs(request: Request, strip: str = "", q: str = "", available: str = "", status: str = "", page: int = 1):
    s = S.current(db)
    held = archive.get(s)
    rendered, active = db.rendered_counts(), db.active_jobs_by_arc()
    rows = [_arc_row(a, held, rendered, active) for a in db.arcs(strip or None, q.strip() or None)]
    if available:
        rows = [r for r in rows if r["available"]]
    if status == "rendered":
        rows = [r for r in rows if r["done"]]
    elif status == "unrendered":
        rows = [r for r in rows if not r["done"]]
    elif status == "active":
        rows = [r for r in rows if r["id"] in active]
    rows.sort(key=lambda r: (r["strip"].lower(), r["progs"][0] if r["progs"] else 10**6, r["title"]))
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    ctx = dict(rows=rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], count=len(rows), page=page, pages=pages,
               strip=strip, q=q, available=available, status=status, strips=db.strips(),
               problems=S.problems(s), archive_error=archive.error, archive_count=len(held),
               index_import=index_import, any_arcs=bool(db.strips()))
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "arc-table":
        return templates.TemplateResponse(request, "_arc_table.html", ctx)
    return _render_page(request, "arcs.html", **ctx)


@app.get("/arcs/new", response_class=HTMLResponse)
def new_arc_form(request: Request):
    return _render_page(request, "arc_new.html", strips=db.strips(), error="", form={})


@app.post("/arcs/new")
def new_arc(request: Request, strip: str = Form(...), title: str = Form(...), progs: str = Form(...),
            notes: str = Form("")):
    nums, specials = catalog.parse_progs(progs)
    if not nums:
        return _render_page(request, "arc_new.html", strips=db.strips(), form=dict(strip=strip, title=title,
                            progs=progs, notes=notes), error="Couldn't read any prog numbers from that.")
    arc_id = db.add_arc({"strip": strip.strip(), "title": title.strip(), "progs": nums,
                         "specials": ", ".join(specials), "notes": notes.strip(), "source": "manual"})
    return RedirectResponse(f"/arcs/{arc_id}", status_code=303)


@app.get("/arcs/{arc_id}", response_class=HTMLResponse)
def arc_detail(request: Request, arc_id: int):
    arc = db.arc(arc_id)
    if not arc:
        raise HTTPException(404)
    s = S.current(db)
    held = archive.get(s)
    row = _arc_row(arc, held, db.rendered_counts(), db.active_jobs_by_arc())
    eps = db.episodes(arc_id)
    from narrate import slug

    arc_dir = Path(s["work_path"]) / f"{slug(arc['strip'])}--{slug(arc['title'])}"
    numbers = catalog.plex_numbers(arc, db.strip_arcs(arc["strip"]), held)
    episodes = []
    for i, p in enumerate(arc["progs"], 1):
        e = eps.get(p, {})
        se = numbers.get(p)
        episodes.append({"prog": p, "episode": i, "held": p in held, "year": held.get(p),
                         "plex": f"S{se[0]:02d}E{se[1]:02d}" if se else "", "status": e.get("status", ""),
                         "path": e.get("path", ""), "detail": e.get("detail", ""), "when": e.get("rendered_at"),
                         "transcript": (arc_dir / f"prog_{p:04d}" / "transcript.txt").exists(),
                         "read": (arc_dir / f"prog_{p:04d}" / "tag.json").exists()})
    seasons = sorted({se for se, _ in numbers.values()})
    return _render_page(request, "arc.html", arc=row, episodes=episodes, seasons=seasons,
                        jobs=db.jobs(20, arc_id=arc_id), problems=S.problems(s))


@app.post("/arcs/{arc_id}/render")
def queue_render(arc_id: int, mode: str = Form("render"), prog: int | None = Form(None)):
    if not db.arc(arc_id):
        raise HTTPException(404)
    if mode not in ("render", "rest", "reread"):
        raise HTTPException(400)
    db.add_job(arc_id, mode, prog)
    runner.poke()
    return RedirectResponse(f"/arcs/{arc_id}", status_code=303)


@app.post("/arcs/{arc_id}/edit")
def edit_arc(arc_id: int, title: str = Form(...), progs: str = Form(...), season: str = Form(""),
             notes: str = Form(""), hidden: str = Form("")):
    nums, specials = catalog.parse_progs(progs)
    fields = {"title": title.strip(), "notes": notes.strip(), "hidden": 1 if hidden else 0,
              "season": int(season) if season.strip().isdigit() else None}
    if nums or specials:
        fields.update(progs=nums, specials=", ".join(specials))
    db.update_arc(arc_id, **fields)
    return RedirectResponse(f"/arcs/{arc_id}", status_code=303)


@app.get("/arcs/{arc_id}/transcript/{prog}", response_class=PlainTextResponse)
def transcript(arc_id: int, prog: int):
    arc = db.arc(arc_id)
    if not arc:
        raise HTTPException(404)
    from narrate import slug

    f = Path(S.current(db)["work_path"]) / f"{slug(arc['strip'])}--{slug(arc['title'])}" / f"prog_{prog:04d}" / "transcript.txt"
    if not f.exists():
        raise HTTPException(404)
    return f.read_text(encoding="utf-8")


# ---------- renders ----------

@app.get("/renders", response_class=HTMLResponse)
def renders(request: Request):
    ctx = dict(jobs=db.jobs(200))
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_jobs_table.html", ctx)
    return _render_page(request, "renders.html", **ctx)


@app.get("/renders/{job_id}", response_class=HTMLResponse)
def render_detail(request: Request, job_id: int):
    job = db.job(job_id)
    if not job:
        raise HTTPException(404)
    arc = db.arc(job["arc_id"])
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_job_live.html", dict(job=job, arc=arc))
    return _render_page(request, "render.html", job=job, arc=arc)


@app.post("/renders/{job_id}/cancel")
def cancel(job_id: int):
    runner.cancel(job_id)
    return RedirectResponse(f"/renders/{job_id}", status_code=303)


# ---------- settings ----------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = ""):
    s = S.current(db)
    return _render_page(request, "settings.html", s=s, fields=S.FIELDS, groups=S.GROUPS, casts=S.cast_files(),
                        saved=bool(saved), index_import=index_import, arc_count=len(db.arcs(include_hidden=True)))


@app.post("/settings")
async def save_settings(request: Request):
    form = dict(await request.form())
    db.set_settings(S.parse_form(form, S.current(db)))
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/import-index", response_class=HTMLResponse)
def import_index(request: Request, refresh: str = Form("")):
    if not index_import["running"]:
        index_import.update(running=True, message="Fetching the story indexes…")
        threading.Thread(target=_import_index, args=(bool(refresh),), daemon=True).start()
    return templates.TemplateResponse(request, "_import_status.html", dict(index_import=index_import))


@app.get("/settings/import-index", response_class=HTMLResponse)
def import_status(request: Request):
    return templates.TemplateResponse(request, "_import_status.html", dict(index_import=index_import))


def _import_index(refresh=False):
    try:
        held = archive.get(S.current(db), force=True)
        arcs = catalog.download_and_parse(S.data_dir() / "index", latest=max(held) if held else None,
                                          prefer_cache=not refresh)
        with db.conn() as c:
            new = sum(db.upsert_arc(c, a) for a in arcs)
        index_import.update(message=f"Imported {len(arcs)} stories ({new} new).")
    except Exception as e:
        index_import.update(message=f"Import failed: {e}")
    finally:
        index_import["running"] = False


@app.post("/settings/test/{what}", response_class=HTMLResponse)
def test_setting(what: str):
    s = S.current(db)
    ok, msg = False, ""
    key = {"gemini": "gemini_api_key", "elevenlabs": "elevenlabs_api_key", "comicvine": "comicvine_api_key"}.get(what)
    if key and not s.get(key):
        return '<span class="test bad">✗ No key saved yet.</span>'
    try:
        if what == "archive":
            n = len(archive.get(s, force=True))
            ok, msg = n > 0, (f"Found {n} progs." if n else (archive.error or "No progs found there."))
        elif what == "output":
            p = Path(s["output_path"])
            probe = p / ".panelplay_write_test"
            probe.mkdir(parents=True, exist_ok=True)
            probe.rmdir()
            ok, msg = True, "Writable."
        elif what == "gemini":
            from google import genai

            client = genai.Client(api_key=s["gemini_api_key"])  # keep a reference, or it closes mid-request
            r = client.models.generate_content(model=s["gemini_tag_model"], contents="Reply with just: ok")
            ok, msg = True, f"Working ({s['gemini_tag_model']} replied “{r.text.strip()[:20]}”)."
        elif what == "elevenlabs":
            r = requests.get("https://api.elevenlabs.io/v2/voices", params={"page_size": 100},
                             headers={"xi-api-key": s["elevenlabs_api_key"]}, timeout=20)
            r.raise_for_status()
            mine = [v["name"] for v in r.json()["voices"] if v.get("category") != "premade"]
            ok, msg = True, f"Working. {len(r.json()['voices'])} voices" + (f", yours: {', '.join(mine)}" if mine else "") + "."
        elif what == "comicvine":
            r = requests.get("https://comicvine.gamespot.com/api/search/",
                             params={"api_key": s["comicvine_api_key"], "format": "json", "query": "2000 AD",
                                     "resources": "volume", "limit": 1},
                             headers={"User-Agent": catalog.USER_AGENT}, timeout=20)
            data = r.json()
            ok = data.get("status_code") == 1
            msg = "Working." if ok else data.get("error", f"HTTP {r.status_code}")
        elif what == "komga":
            n = len(catalog.archive_progs({**s, "source": "komga"}))
            ok, msg = n > 0, f"Found {n} progs in “{s['komga_series']}”."
        else:
            raise HTTPException(404)
    except HTTPException:
        raise
    except Exception as e:
        ok, msg = False, str(e)[:300]
    cls = "ok" if ok else "bad"
    return f'<span class="test {cls}">{"✓" if ok else "✗"} {html.escape(msg)}</span>'

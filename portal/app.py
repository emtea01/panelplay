"""PanelPlay portal: the comics library, its story arcs, render queue and progress, and settings."""
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
from .indexer import Indexer
from .jobs import Runner

HERE = Path(__file__).resolve().parent
PAGE_SIZE = 50

db = DB(S.data_dir() / "panelplay.db")
S.seed_from_dev_setup(db)
runner = Runner(db)
indexer = Indexer(db)


@asynccontextmanager
async def lifespan(_app):
    runner.start()
    indexer.start()
    if not db.series_list():
        start_scan()  # first start: find what's in the library
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
templates.env.globals["issue_label"] = catalog.issue_label
templates.env.globals["series_label"] = catalog.series_label
templates.env.globals["is_main_series"] = catalog.is_main_series


library_scan = {"running": False, "message": ""}
index_import = {"running": False, "message": ""}


def start_scan():
    if library_scan["running"]:
        return
    library_scan.update(running=True, message="Scanning the library…")
    threading.Thread(target=_scan, daemon=True).start()


def _scan():
    import library

    try:
        found = library.scan(S.current(db))
        db.save_scan(found)
        n = sum(len(f["issues"]) for f in found)
        library_scan.update(message=f"Found {len(found)} series, {n} issues. Looking in the Plex folder…")
        from portal import plexscan

        try:
            plex = plexscan.find_episodes(db, S.current(db)["output_path"])
        except OSError as e:
            plex = f"Couldn't look in the Plex folder: {e}"
        library_scan.update(message=f"Found {len(found)} series, {n} issues. {plex}".strip())
    except BaseException as e:  # SystemExit from the scanner carries a readable message
        library_scan.update(message=f"Couldn't scan the library: {e}")
    finally:
        library_scan["running"] = False


def _arc_row(arc, held, rendered, active, series=None):
    """held: {series_id: {issue number: ...}} for the whole library."""
    total = len(arc["progs"])
    have = held.get(arc["series_id"], {})
    missing = [p for p in arc["progs"] if p not in have]
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
    return {**arc, "total": total, "missing": missing, "available": available, "status": status, "done": done,
            "series": (series or {}).get(arc["series_id"])}


def _render_page(request, name, **ctx):
    return templates.TemplateResponse(request, name, {"nav": name.split(".")[0], **ctx})


# ---------- arcs ----------

@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/arcs")


def _series_by_id():
    return {sr["id"]: sr for sr in db.series_list(present_only=False)}


@app.get("/arcs", response_class=HTMLResponse)
def arcs(request: Request, strip: str = "", q: str = "", available: str = "", status: str = "", series: str = "",
         page: int = 1):
    s = S.current(db)
    held, by_id = db.issues(), _series_by_id()
    rendered, active = db.rendered_counts(), db.active_jobs_by_arc()
    sid = int(series) if series.isdigit() else None
    rows = [_arc_row(a, held, rendered, active, by_id) for a in db.arcs(strip or None, q.strip() or None, series_id=sid)]
    if available:
        rows = [r for r in rows if r["available"]]
    if status == "rendered":
        rows = [r for r in rows if r["done"]]
    elif status == "unrendered":
        rows = [r for r in rows if not r["done"]]
    elif status == "active":
        rows = [r for r in rows if r["id"] in active]
    order = catalog.issue_order(held)
    rows.sort(key=lambda r: (r["strip"].lower(),
                             order.get((r["series_id"], r["progs"][0]), (9999, 0)) if r["progs"] else (9999, 0),
                             r["title"]))
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    ctx = dict(rows=rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], count=len(rows), page=page, pages=pages,
               strip=strip, q=q, available=available, status=status, series=series, strips=db.strips(),
               series_list=[sr for sr in by_id.values() if sr["present"]], problems=S.problems(s),
               library_scan=library_scan, any_series=bool(by_id), index_import=index_import,
               any_arcs=bool(db.strips()))
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "arc-table":
        return templates.TemplateResponse(request, "_arc_table.html", ctx)
    return _render_page(request, "arcs.html", **ctx)


@app.get("/arcs/new", response_class=HTMLResponse)
def new_arc_form(request: Request, series: str = ""):
    return _render_page(request, "arc_new.html", strips=db.strips(), series_list=db.series_list(), error="",
                        form={"series": series})


@app.post("/arcs/new")
def new_arc(request: Request, series: int = Form(...), strip: str = Form(...), title: str = Form(...),
            progs: str = Form(...), notes: str = Form("")):
    nums, specials = catalog.parse_progs(progs)
    if not nums or not db.series(series):
        return _render_page(request, "arc_new.html", strips=db.strips(), series_list=db.series_list(),
                            form=dict(series=str(series), strip=strip, title=title, progs=progs, notes=notes),
                            error="Couldn't read any issue numbers from that.")
    arc_id = db.add_arc({"series_id": series, "strip": strip.strip(), "title": title.strip(), "progs": nums,
                         "specials": ", ".join(specials), "notes": notes.strip(), "source": "manual"})
    return RedirectResponse(f"/arcs/{arc_id}", status_code=303)


@app.get("/arcs/{arc_id}", response_class=HTMLResponse)
def arc_detail(request: Request, arc_id: int):
    arc = db.arc(arc_id)
    if not arc:
        raise HTTPException(404)
    s = S.current(db)
    held, by_id = db.issues(), _series_by_id()
    row = _arc_row(arc, held, db.rendered_counts(), db.active_jobs_by_arc(), by_id)
    series = by_id.get(arc["series_id"])
    have = held.get(arc["series_id"], {})
    eps = db.episodes(arc_id)
    arc_dir = catalog.work_dir(s["work_path"], arc, series)
    numbers = catalog.plex_numbers(arc, db.strip_arcs(arc["strip"]), catalog.issue_order(held))
    episodes = []
    for i, p in enumerate(arc["progs"], 1):
        e = eps.get(p, {})
        se = numbers.get(p)
        episodes.append({"prog": p, "label": catalog.issue_label(series, p), "episode": i, "held": p in have,
                         "year": (have.get(p) or {}).get("year"),
                         "plex": f"S{se[0]:02d}E{se[1]:02d}" if se else "", "status": e.get("status", ""),
                         "path": e.get("path", ""), "detail": e.get("detail", ""), "when": e.get("rendered_at"),
                         "transcript": (arc_dir / f"prog_{p:04d}" / "transcript.txt").exists(),
                         "read": (arc_dir / f"prog_{p:04d}" / "tag.json").exists()})
    seasons = sorted({se for se, _ in numbers.values()})
    return _render_page(request, "arc.html", arc=row, episodes=episodes, seasons=seasons, series=series,
                        jobs=db.jobs(20, arc_id=arc_id), problems=S.problems(s))


@app.post("/arcs/{arc_id}/render")
def queue_render(arc_id: int, mode: str = Form("render"), prog: int | None = Form(None)):
    if not db.arc(arc_id):
        raise HTTPException(404)
    if mode not in ("render", "rest", "reread", "art"):
        raise HTTPException(400)
    db.add_job(arc_id, mode, prog)
    runner.poke()
    return RedirectResponse(f"/arcs/{arc_id}", status_code=303)


@app.post("/arcs/{arc_id}/edit")
def edit_arc(arc_id: int, title: str = Form(...), progs: str = Form(...), season: str = Form(""),
             notes: str = Form(""), hidden: str = Form(""), strip: str = Form("")):
    nums, specials = catalog.parse_progs(progs)
    fields = {"title": title.strip(), "notes": notes.strip(), "hidden": 1 if hidden else 0,
              "season": int(season) if season.strip().isdigit() else None}
    if nums or specials:
        fields.update(progs=nums, specials=", ".join(specials))
    if strip.strip():
        fields["strip"] = strip.strip()
    db.update_arc(arc_id, **fields)
    return RedirectResponse(f"/arcs/{arc_id}", status_code=303)


@app.get("/arcs/{arc_id}/transcript/{prog}", response_class=PlainTextResponse)
def transcript(arc_id: int, prog: int):
    arc = db.arc(arc_id)
    if not arc:
        raise HTTPException(404)
    series = db.series(arc["series_id"]) if arc["series_id"] else None
    f = catalog.work_dir(S.current(db)["work_path"], arc, series) / f"prog_{prog:04d}" / "transcript.txt"
    if not f.exists():
        raise HTTPException(404)
    return f.read_text(encoding="utf-8")


# ---------- library ----------

@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    ctx = dict(series_list=db.series_list(), library_scan=library_scan, problems=S.problems(S.current(db)),
               main=db.main_series(), index_import=index_import)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_series_table.html", ctx)
    return _render_page(request, "library.html", **ctx)


@app.post("/library/scan", response_class=HTMLResponse)
def scan_library(request: Request):
    start_scan()
    return templates.TemplateResponse(request, "_scan_status.html", dict(library_scan=library_scan))


@app.get("/library/scan", response_class=HTMLResponse)
def scan_status(request: Request):
    return templates.TemplateResponse(request, "_scan_status.html", dict(library_scan=library_scan))


@app.post("/library/{series_id}/index")
def index_series(series_id: int):
    if not db.series(series_id):
        raise HTTPException(404)
    indexer.queue(series_id)
    return RedirectResponse("/library", status_code=303)


@app.post("/library/{series_id}/stop")
def stop_index(series_id: int):
    indexer.stop(series_id)
    return RedirectResponse("/library", status_code=303)


@app.post("/library/{series_id}/kind")
def set_kind(series_id: int, kind: str = Form(...)):
    if kind not in ("auto", "anthology", "single"):
        raise HTTPException(400)
    db.update_series(series_id, kind=kind)
    return RedirectResponse("/library", status_code=303)


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
        index_import.update(running=True, message="Starting…")
        threading.Thread(target=_import_index, args=(bool(refresh),), daemon=True).start()
    return templates.TemplateResponse(request, "_import_status.html", dict(index_import=index_import))


@app.get("/settings/import-index", response_class=HTMLResponse)
def import_status(request: Request):
    return templates.TemplateResponse(request, "_import_status.html", dict(index_import=index_import))


def _import_index(refresh=False):
    try:
        main = db.main_series()
        if not main:
            index_import.update(message="No 2000 AD series in the library yet (Library → Rescan).")
            return
        held = db.issues(main["id"]).get(main["id"], {})
        arcs = catalog.download_and_parse(S.data_dir() / "index", latest=max(held) if held else None,
                                          prefer_cache=not refresh,
                                          say=lambda m: index_import.update(message=m))
        index_import.update(message="Saving the stories…")
        with db.conn() as c:
            new = sum(db.upsert_arc(c, {**a, "series_id": main["id"]}) for a in arcs)
        # Rows from earlier imports that were notes, not stories.
        db.delete_unused_arcs([a["id"] for a in db.arcs(include_hidden=True, series_id=main["id"])
                               if a["source"] == "index" and catalog.NOTE_ROW.search(a["title"])])
        index_import.update(message=f"Imported {len(arcs)} stories ({new} new).")
    except Exception as e:
        index_import.update(message=f"Import failed: {e}")
    finally:
        index_import["running"] = False


@app.post("/settings/test/{what}", response_class=HTMLResponse)
def test_setting(what: str):
    s = S.current(db)
    ok, msg = False, ""
    key = {"gemini": "gemini_api_key", "elevenlabs": "elevenlabs_api_key", "comicvine": "comicvine_api_key",
           "plex": "plex_token"}.get(what)
    if key and not s.get(key):
        return '<span class="test bad">✗ No key saved yet.</span>'
    try:
        if what == "archive":
            import library

            found = library.scan(s)
            db.save_scan(found)
            n = sum(len(f["issues"]) for f in found)
            ok, msg = n > 0, f"Found {len(found)} series, {n} issues." if n else "No comics found there."
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
            if not r.ok:
                try:
                    detail = r.json().get("detail")
                    detail = detail.get("message") or detail.get("status") if isinstance(detail, dict) else detail
                except ValueError:
                    detail = r.text[:200]
                raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {detail}")
            mine = [v["name"] for v in r.json()["voices"] if v.get("category") != "premade"]
            ok, msg = True, f"Working. {len(r.json()['voices'])} voices" + (f", yours: {', '.join(mine)}" if mine else "") + "."
            sub = requests.get("https://api.elevenlabs.io/v1/user/subscription",
                               headers={"xi-api-key": s["elevenlabs_api_key"]}, timeout=20)
            if sub.ok:
                sub = sub.json()
                msg += (f" {sub['character_limit'] - sub['character_count']:,} of {sub['character_limit']:,} "
                        "credits left this month.")
            else:
                msg += " (Can't see the credit balance: give the key User: read access, so renders can check it.)"
        elif what == "comicvine":
            r = requests.get("https://comicvine.gamespot.com/api/search/",
                             params={"api_key": s["comicvine_api_key"], "format": "json", "query": "2000 AD",
                                     "resources": "volume", "limit": 1},
                             headers={"User-Agent": catalog.CV_USER_AGENT}, timeout=20)
            try:
                data = r.json()
            except ValueError:
                data = {"error": f"HTTP {r.status_code}: Comic Vine didn't send a JSON reply"}
            ok = data.get("status_code") == 1
            msg = "Working." if ok else data.get("error", f"HTTP {r.status_code}")
        elif what == "plex":
            from . import plex

            if not s["plex_url"]:
                raise RuntimeError("Set the Plex server URL first.")
            _, title, folder = plex.target(s)
            ok, msg = True, f"Working. New episodes will be scanned into “{title}” (Plex sees the folder as {folder})."
        elif what == "komga":
            import library

            found = library.scan({**s, "source": "komga"})
            n = sum(len(f["issues"]) for f in found)
            ok, msg = n > 0, f"Found {len(found)} series, {n} issues."
        else:
            raise HTTPException(404)
    except HTTPException:
        raise
    except Exception as e:
        ok, msg = False, str(e)[:300]
    cls = "ok" if ok else "bad"
    return f'<span class="test {cls}">{"✓" if ok else "✗"} {html.escape(msg)}</span>'

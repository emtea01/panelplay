"""The arc catalogue: import story indexes (Wikimedia Commons PDFs), parse prog ranges, check the archive.

Two indexes, both compiled by 2000 AD fans and hosted on Wikimedia Commons:
- every series except Judge Dredd: series heading in capitals, then "Story title   prog range" rows (2 columns)
- Judge Dredd: one block per story with Prog(s): / Episodes: / Script: / Art: / Dated: / Note: fields (3 columns)
"""
import io
import re
from pathlib import Path

import requests

INDEX_URLS = {
    "series": "https://upload.wikimedia.org/wikipedia/commons/6/65/Index_of_stories_published_in_British_comic_2000AD.pdf",
    "dredd": "https://upload.wikimedia.org/wikipedia/commons/5/56/Index_of_Judge_Dredd_stories_in_British_comic_2000AD.pdf",
}
# Wikimedia asks automated clients to identify themselves.
USER_AGENT = "PanelPlay/0.1 (https://github.com/emtea01/panelplay; self-hosted home media tool) python-requests"
# Comic Vine blocks any user agent naming python-requests as a scraper.
CV_USER_AGENT = USER_AGENT.removesuffix(" python-requests")

# ---------- prog ranges ----------

_RANGE = re.compile(r"^(\d{1,4})(?:\s*-\s*(\d{1,4}|ongoing))?$", re.I)


def parse_progs(spec: str, latest: int | None = None) -> tuple[list[int], list[str]]:
    """'750-757, 780-784' -> ([750..757, 780..784], []). Year-end specials ('Prog 2011') come back separately,
    because their numbers clash with the regular progs 2000-2015 of 2016-17. 'NNNN-ongoing' runs to `latest`."""
    progs, specials = [], []
    spec = spec.replace("–", "-").replace("&", ",").replace(" and ", ",")
    for part in re.split(r"[,;]", spec):
        part = part.strip().strip(".")
        if not part:
            continue
        m = re.match(r"(?i)^prog\s+(\d{4})$", part)
        if m:
            specials.append(f"Prog {m.group(1)}")
            continue
        m = _RANGE.match(part)
        if not m:
            continue
        a = int(m.group(1))
        if m.group(2) is None:
            progs.append(a)
        elif m.group(2).lower() == "ongoing":
            progs.extend(range(a, max(a, latest or a) + 1))
        else:
            b = int(m.group(2))
            if b >= a and b - a < 200:
                progs.extend(range(a, b + 1))
    return progs, specials


def format_progs(progs: list[int]) -> str:
    """[10, 11, 12, 20] -> '10-12, 20' (also the --progs argument for narrate.py, minus the spaces)."""
    out, i = [], 0
    while i < len(progs):
        j = i
        while j + 1 < len(progs) and progs[j + 1] == progs[j] + 1:
            j += 1
        out.append(str(progs[i]) if i == j else f"{progs[i]}-{progs[j]}")
        i = j + 1
    return ", ".join(out)


def smart_title(s: str) -> str:
    """'ANDERSON, PSI-DIVISION' -> 'Anderson, Psi-Division'; 'THE 86ERS' -> 'The 86ers'; keeps 'A.B.C.'."""
    small = {"of", "the", "and", "a", "an", "in", "on", "to", "for", "at", "by"}

    def word(w, first):
        if re.fullmatch(r"([A-Z]\.)+", w):
            return w
        parts = w.split("-")
        out = []
        for k, p in enumerate(parts):
            low = p.lower()
            if not first and k == 0 and low in small:
                out.append(low)
            else:
                out.append(re.sub(r"^([^a-z0-9]*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), low, count=1))
        return "-".join(out)

    return " ".join(word(w, i == 0) for i, w in enumerate(s.split()))


# ---------- PDF reading ----------

def _lines(page, columns: list[float], tol=2.5):
    """Words on a page grouped into lines per column, top to bottom. Yields (column, top, [words])."""
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2, extra_attrs=["fontname", "size"])
    for ci in range(len(columns)):
        lo = columns[ci]
        hi = columns[ci + 1] if ci + 1 < len(columns) else page.width + 1
        col = sorted((w for w in words if lo <= w["x0"] < hi), key=lambda w: (w["top"], w["x0"]))
        line, top = [], None
        for w in col:
            if top is not None and abs(w["top"] - top) > tol:
                yield ci, top, sorted(line, key=lambda w: w["x0"])
                line = []
            if not line:
                top = w["top"]
            line.append(w)
        if line:
            yield ci, top, sorted(line, key=lambda w: w["x0"])


def _text(words) -> str:
    t = " ".join(w["text"] for w in words)
    return (t.replace("�", "'").replace("’", "'").replace("‘", "'")
             .replace("“", '"').replace("”", '"').replace(" ,", ","))


def _is_caps(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return len(letters) >= 3 and sum(ch.isupper() for ch in letters) / len(letters) > 0.85


# Index rows that point elsewhere ("Strontium Dog first appeared in Starlord no. 1") rather than list a story.
NOTE_ROW = re.compile(r"(?i)\b(first appeared in|see also|episode in|story features)\b|\b(no\.|vol\.|in)$")
_PROG_WORD = re.compile(r"(?i)^(prog|\d{1,4}(-(\d{1,4}|ongoing))?,?|-|and|&)$")
_NOTE_START = re.compile(r"(?i)^(note|notes|see also|story continues|characters?|spin-off|also|continued|"
                         r"[a-z]+ (first|also) appeared|this|these|published|reprinted|\()")


def parse_series_index(pdf, latest=None) -> list[dict]:
    """Every series except Judge Dredd (which has its own, more detailed index)."""
    arcs, series, pending, last, heading_open = [], None, None, None, False
    for page in pdf.pages:
        # Two columns; each has a title area and a prog-range area about 108pt to its right.
        for ci, top, words in _lines(page, [0, page.width / 2]):
            col_x = 89 if ci == 0 else 315
            prog_x = col_x + 100
            # Series headings are bold; story titles (even ones in capitals, like "FAQ") are not.
            bold = "bold" in words[0].get("fontname", "").lower()
            # Trailing words in the prog column that look like prog numbers.
            k = len(words)
            while k > 0 and words[k - 1]["x0"] >= prog_x and _PROG_WORD.match(words[k - 1]["text"]):
                k -= 1
            title, spec = _text(words[:k]).strip(), _text(words[k:])
            gap = (top - last[1]) if last and last[0] == (page.page_number, ci) else 99
            last = ((page.page_number, ci), top)
            if spec and not re.search(r"\d", spec):
                title, spec = _text(words), ""
            if bold and title.startswith(("2000 AD Story Index", "Prog 20")):
                continue
            if not spec:
                if bold and _is_caps(title):
                    # A wrapped heading continues on the next bold line.
                    series = f"{series} {smart_title(title)}" if heading_open and gap <= 10 else smart_title(title)
                    pending, heading_open = None, True
                elif title and not _NOTE_START.match(title) and series:
                    pending, heading_open = (title, top), False  # might be the first half of a wrapped title
                else:
                    pending, heading_open = None, False
                continue
            heading_open = False
            if bold and _is_caps(title):
                # A heading with its own progs: a series that is a single story.
                series, pending = smart_title(title), None
                title = series
            if not series:
                continue
            if not title and pending:
                title, pending = pending[0], None
            elif pending and gap <= 10:
                title, pending = f"{pending[0]} {title}", None
            else:
                pending = None
            if not title or series.upper().startswith("JUDGE DREDD"):
                continue
            progs, specials = parse_progs(spec, latest)
            if (progs or specials) and not NOTE_ROW.search(title):
                arcs.append({"strip": series, "title": title.strip(" -"), "progs": progs,
                             "specials": ", ".join(specials), "source": "index"})
    return arcs


_FIELD = re.compile(r"^(Progs?|Episodes|Pages|Script|Scripts|Art|Dated|Notes?|Events|Letters|Colours|Writer)s?:")


def parse_dredd_index(pdf, latest=None) -> list[dict]:
    lines = []
    for page in pdf.pages:
        # Three columns starting at about x = 90, 240 and 391.
        for ci, top, words in _lines(page, [0, 230, 385]):
            lines.append({"key": (page.page_number, ci), "top": top, "text": _text(words)})
    for i, ln in enumerate(lines):
        prev = lines[i - 1] if i else None
        ln["gap"] = ln["top"] - prev["top"] if prev and prev["key"] == ln["key"] else 99

    arcs = []
    for i, ln in enumerate(lines):
        m = re.match(r"^Progs?:\s*(.+)$", ln["text"])
        if not m:
            continue
        # Title: the lines just above, back to the blank gap that separates stories.
        title_lines, j = [], i - 1
        while j >= 0 and not _FIELD.match(lines[j]["text"]):
            title_lines.insert(0, lines[j]["text"])
            if lines[j]["gap"] > 13 or len(title_lines) >= 3:
                break
            j -= 1
        if not title_lines or title_lines[0].startswith("Judge Dredd stories in"):
            continue
        # Fields: until the next story's title (a line after a blank gap that isn't a field).
        fields, cur, k = {}, None, i + 1
        while k < len(lines) and not re.match(r"^Progs?:", lines[k]["text"]):
            t = lines[k]["text"]
            f = _FIELD.match(t)
            if f:
                cur = f.group(1).rstrip("s").lower()
                fields[cur] = t[f.end():].strip()
            elif lines[k]["gap"] > 13:
                break
            elif cur:
                fields[cur] += " " + t
            k += 1
        progs, specials = parse_progs(m.group(1), latest)
        if not (progs or specials):
            continue
        credits = "; ".join(f"{name}: {fields[key]}" for key, name in (("script", "Script"), ("art", "Art"))
                            if fields.get(key))
        ep = re.match(r"\d+", fields.get("episode", ""))
        arcs.append({"strip": "Judge Dredd", "title": " ".join(title_lines).strip(), "progs": progs,
                     "specials": ", ".join(specials), "episodes": int(ep.group()) if ep else None,
                     "credits": credits, "notes": fields.get("note", ""), "source": "index"})
    return arcs


def _fetch(url: str, cache_file, timeout=120) -> bytes:
    """Download an index, keeping a copy. Wikimedia rate-limits bots, so retry politely and fall back to the copy."""
    import time

    error = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            if r.status_code == 429 and attempt < 2:
                wait = r.headers.get("Retry-After", "")
                time.sleep(min(60, int(wait) if wait.isdigit() else 20))
                continue
            r.raise_for_status()
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(r.content)
            return r.content
        except requests.RequestException as e:
            error = e
            time.sleep(5)
    if cache_file.exists():
        return cache_file.read_bytes()
    raise RuntimeError(f"Couldn't download the story index ({error})")


def download_and_parse(cache_dir, latest=None, prefer_cache=True) -> list[dict]:
    import pdfplumber

    arcs = []
    for name, url in INDEX_URLS.items():
        cache_file = cache_dir / f"{name}.pdf"
        data = cache_file.read_bytes() if prefer_cache and cache_file.exists() else _fetch(url, cache_file)
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            arcs += parse_dredd_index(pdf, latest) if name == "dredd" else parse_series_index(pdf, latest)
    return arcs


# ---------- series, seasons, working folders ----------

def is_main_series(series: dict | None) -> bool:
    """The 2000 AD weekly (what the Wikimedia indexes describe, and where the strips are called progs)."""
    return bool(series) and (series.get("cv_id") == 19752 or series.get("name") == "2000 AD")


def issue_label(series: dict | None, n: int) -> str:
    return f"Prog {n}" if is_main_series(series) else f"#{n}"


def series_label(series: dict | None) -> str:
    if not series:
        return ""
    return series["name"] + (f" ({series['year']})" if series.get("year") else "")


def work_dir(work_path: str, arc: dict, series: dict | None) -> Path:
    """Where a story's page scripts and renders live. 2000 AD keeps the original layout, so earlier readings
    are reused; other titles get a folder each, so stories with the same name in two titles don't collide."""
    from narrate import slug

    base = Path(work_path)
    if series and not is_main_series(series):
        base = base / slug(series_label(series))
    return base / f"{slug(arc['strip'])}--{slug(arc['title'])}"


def issue_order(issues: dict) -> dict:
    """{(series_id, number): (year, position through the year)} for every issue in the library.

    The position puts issues of different titles in publication order: the cover date when Comic Vine gave
    one, otherwise how far through its year the issue falls (a weekly's 26th prog of 52 sits mid-year, beside
    a monthly's 6th issue of 12)."""
    from datetime import date

    out = {}
    for sid, nums in issues.items():
        per_year: dict[int, list] = {}
        for n, i in nums.items():
            if i["year"]:
                per_year.setdefault(i["year"], []).append(n)
        for year, ns in per_year.items():
            ns.sort()
            for k, n in enumerate(ns):
                d = nums[n].get("date") or ""
                try:
                    frac = (date.fromisoformat(d[:10]).timetuple().tm_yday - 0.5) / 366 if d[:4] == str(year) else None
                except ValueError:
                    frac = None
                out[(sid, n)] = (year, frac if frac is not None else (k + 0.5) / len(ns))
    return out


def plex_numbers(arc: dict, strip_arcs: list[dict], order: dict) -> dict[int, tuple[int, int]]:
    """Plex (season, episode) for each of the arc's issues.

    One Plex show per strip, whichever titles it ran in. A season is a year of publication (the strip's
    first year is season 1), and its episodes are every episode of the strip that year in publication
    order, one-offs and multi-part stories together. A season set by hand on the story overrides this: the
    story becomes that season on its own. Issues not in the library get no number."""
    if arc.get("season"):
        return {p: (arc["season"], i) for i, p in enumerate(arc["progs"], 1)}
    by_year: dict[int, list] = {}
    for a in strip_arcs:
        if a.get("hidden") or a.get("season"):
            continue
        for p in a["progs"]:
            o = order.get((a["series_id"], p))
            if o:
                by_year.setdefault(o[0], []).append((o[1], a["series_id"] or 0, p, a["progs"][0], a["id"]))
    if not by_year:
        return {}
    first = min(by_year)
    out = {}
    for year, entries in by_year.items():
        for i, (_, _, p, _, aid) in enumerate(sorted(entries), 1):
            if aid == arc["id"]:
                out[p] = (year - first + 1, i)
    return out

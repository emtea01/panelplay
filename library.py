"""The comics library: every series under the root comics folder (or in Komga), and the issues in each.

A series is a folder that directly holds comic files or a Mylar series.json; folders that hold neither
are searched for series inside them. series.json (written by Mylar) gives the name, year, publisher and
Comic Vine volume id; otherwise the folder name ("Warlord (1974)") is used.
"""
import json
import re
from pathlib import Path

from sources import COMIC_EXT, _year, comic_files


def scan_folder(root: str, max_depth: int = 3) -> list[dict]:
    root_p = Path(root)
    if not root_p.is_dir():
        raise SystemExit(f"{root} isn't a folder this machine can see")
    found = []

    def visit(folder: Path, depth: int):
        try:
            entries = list(folder.iterdir())
        except OSError:
            return
        has_comics = any(e.suffix.lower() in COMIC_EXT for e in entries if e.is_file())
        meta_file = folder / "series.json"
        if has_comics or meta_file.exists():
            found.append(_folder_series(root_p, folder, meta_file if meta_file.exists() else None))
        if depth < max_depth:
            for e in sorted(entries):
                if e.is_dir() and not e.name.startswith((".", "@", "#")):
                    visit(e, depth + 1)

    visit(root_p, 0)
    return found


def _folder_series(root: Path, folder: Path, meta_file: Path | None) -> dict:
    m = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", folder.name)
    name, year = (m.group(1), int(m.group(2))) if m else (folder.name, None)
    s = {"key": folder.relative_to(root).as_posix(), "name": name, "year": year, "publisher": "",
         "cv_id": None, "description": ""}
    if meta_file:
        try:
            md = json.loads(meta_file.read_text(encoding="utf-8")).get("metadata", {})
            s.update(name=md.get("name") or name, year=md.get("year") or year, publisher=md.get("publisher") or "",
                     cv_id=md.get("comicid"), description=(md.get("description_text") or "")[:2000])
        except (ValueError, OSError):
            pass
    s["issues"] = {n: {"file": f.name, "year": _year(f.stem)} for n, f in comic_files(folder).items()}
    return s


def scan_komga(cfg: dict) -> list[dict]:
    """Every series in Komga (or just the one named in cfg['series'])."""
    from sources import KomgaSource, _prog_number

    src = KomgaSource({**cfg, "series": cfg.get("series", "")})
    params = {"unpaged": "true"}
    if cfg.get("series"):
        params["search"] = cfg["series"]
    out = []
    for sr in src._get("/api/v1/series", **params).json()["content"]:
        md = sr.get("metadata", {})
        books = src._get(f"/api/v1/series/{sr['id']}/books", unpaged="true").json()["content"]
        issues = {}
        for b in books:
            n = _prog_number(b.get("metadata", {}).get("number"), b.get("name", ""))
            if n is not None:
                issues.setdefault(n, {"file": b["id"],
                                      "year": _year(b.get("metadata", {}).get("releaseDate") or "", b.get("name", ""))})
        years = [i["year"] for i in issues.values() if i["year"]]
        out.append({"key": f"komga:{sr['id']}", "name": md.get("title") or sr["name"],
                    "year": min(years) if years else None, "publisher": md.get("publisher", ""),
                    "cv_id": None, "description": (md.get("summary") or "")[:2000], "issues": issues})
    return out


def scan(settings: dict) -> list[dict]:
    if settings["source"] == "komga":
        return scan_komga({"url": settings["komga_url"], "api_key": settings["komga_api_key"],
                           "user": settings["komga_user"], "password": settings["komga_password"],
                           "series": settings.get("komga_series", "")})
    return scan_folder(settings["comics_path"])

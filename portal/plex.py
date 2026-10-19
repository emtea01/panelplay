"""Telling Plex to scan the folder a new episode went into, so it shows up straight away. Plex's own
"scan automatically" rarely notices changes on network shares."""
from pathlib import Path, PurePosixPath, PureWindowsPath

import requests


def _get(s: dict, endpoint: str, **params):
    r = requests.get(s["plex_url"].rstrip("/") + endpoint, params=params, timeout=20,
                     headers={"X-Plex-Token": s["plex_token"], "Accept": "application/json"})
    if r.status_code == 401:
        raise RuntimeError("Plex says the token is wrong (HTTP 401).")
    r.raise_for_status()
    return r


def _pure(p: str):
    return PureWindowsPath(p) if (len(p) > 1 and p[1] == ":") or p.startswith("\\\\") else PurePosixPath(p)


def target(s: dict) -> tuple[str, str, str]:
    """(library key, library title, the episodes folder as Plex sees it) for the TV library that holds the
    output folder. Plex may see the folder under another path (its own mount), so unless Settings says where,
    match the library folder with the same name as ours."""
    libs = [d for d in _get(s, "/library/sections").json()["MediaContainer"].get("Directory", [])
            if d["type"] == "show"]
    if not libs:
        raise RuntimeError("Plex has no TV libraries.")
    if s.get("plex_folder"):
        want = _pure(s["plex_folder"])
        for lib in libs:
            for loc in lib.get("Location", []):
                lp = _pure(loc["path"])
                if want == lp or lp in want.parents:
                    return lib["key"], lib["title"], s["plex_folder"]
        raise RuntimeError(f"No Plex TV library contains {s['plex_folder']}.")
    name = Path(s["output_path"]).name.lower()
    hits = [(lib["key"], lib["title"], loc["path"]) for lib in libs for loc in lib.get("Location", [])
            if _pure(loc["path"]).name.lower() == name]
    if len(hits) != 1:
        found = "; ".join(f"{lib['title']}: {', '.join(l['path'] for l in lib.get('Location', []))}" for lib in libs)
        raise RuntimeError(f"Can't tell which Plex folder is {s['output_path']}. Set 'The episodes folder as Plex "
                           f"sees it' to one of these (or a folder inside one): {found}")
    return hits[0]


def scan(s: dict, episode_file: str) -> str:
    """Ask Plex to scan the season folder of a newly written episode. Returns the folder, as Plex sees it."""
    key, _, plex_root = target(s)
    rel = Path(episode_file).parent.relative_to(Path(s["output_path"]))
    folder = str(_pure(plex_root).joinpath(*rel.parts))
    _get(s, f"/library/sections/{key}/refresh", path=folder)
    return folder

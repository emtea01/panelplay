"""Find episodes already in the Plex folder (rendered earlier, elsewhere, or before a reinstall) and record them."""
import re
from pathlib import Path

from portal import catalog

# narrate.py names episodes "<Show>/Season NN/<Show> - sNNeNN - <Story> - Part N.mp4".
EPISODE = re.compile(r"^(?P<show>.+?) - s(?P<season>\d+)e(?P<ep>\d+) - (?P<title>.+) - Part (?P<part>\d+)\.mp4$", re.I)


def _safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", s or "").strip().lower()


def find_episodes(db, output_path: str) -> str:
    """Mark episodes whose file is already in the Plex folder as rendered. Only folders named after a known
    strip are looked in, so a Plex library shared with other shows isn't trawled. Returns a summary."""
    root = Path(output_path) if output_path else None
    if not root or not root.is_dir():
        return ""
    arcs = db.arcs(include_hidden=True)
    by_name: dict[tuple, list] = {}
    for a in arcs:
        by_name.setdefault((_safe(a["strip"]), _safe(a["title"])), []).append(a)
    shows = {_safe(a["strip"]) for a in arcs}
    order = catalog.issue_order(db.issues())
    numbers: dict[int, dict] = {}  # arc id -> {prog: (season, episode)}, worked out when needed

    found = new = unmatched = 0
    for show_dir in root.iterdir():
        if not show_dir.is_dir() or show_dir.name.lower() not in shows:
            continue
        for f in show_dir.glob("Season */*.mp4"):
            m = EPISODE.match(f.name)
            if not m:
                continue
            found += 1
            candidates = by_name.get((_safe(m["show"]), _safe(m["title"])), [])
            season, ep, part = int(m["season"]), int(m["ep"]), int(m["part"])
            hit = None
            for a in candidates:  # the Plex numbers say which issue it is, even if the part numbering doesn't
                if a["id"] not in numbers:
                    numbers[a["id"]] = catalog.plex_numbers(a, db.strip_arcs(a["strip"]), order)
                prog = next((p for p, n in numbers[a["id"]].items() if n == (season, ep)), None)
                if prog is not None:
                    hit = (a, prog)
                    break
            if hit is None and len(candidates) == 1 and 1 <= part <= len(candidates[0]["progs"]):
                hit = (candidates[0], candidates[0]["progs"][part - 1])
            if hit is None:
                unmatched += 1
                continue
            a, prog = hit
            known = db.episodes(a["id"]).get(prog)
            if known and known["status"] == "rendered" and Path(known["path"]).exists():
                continue
            db.set_episode(a["id"], prog, a["progs"].index(prog) + 1, "rendered", path=str(f),
                           detail="Found in the Plex folder")
            new += 1
    if not found:
        return "No PanelPlay episodes in the Plex folder yet."
    msg = f"{found} episode{'s' if found != 1 else ''} in the Plex folder ({new} newly marked rendered)"
    return msg + (f"; {unmatched} didn't match a story." if unmatched else ".")

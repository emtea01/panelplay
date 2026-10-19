#!/usr/bin/env python3
"""
Turn a comic story arc (2000 AD by default) into narrated, character-voiced episodes for Plex.

  python narrate.py --strip "Judge Dredd" --arc "The Robot Wars" --progs 10-17 --season 1

Each prog becomes one episode. Work is cached under work/, so re-runs only redo what's missing:
  --stage read    stop after reading the pages (then check/edit the JSON scripts)
  --stage render  only render from existing scripts (no Gemini calls)
"""
import argparse
import json
import re
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def parse_progs(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", s).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strip", required=True, help='e.g. "Judge Dredd"')
    ap.add_argument("--arc", required=True, help='e.g. "The Robot Wars"')
    ap.add_argument("--progs", required=True, help='issue (prog) numbers, e.g. "10-17" or "10,11,14-16"')
    ap.add_argument("--series", default="2000 AD", help="the comic the story ran in (for Gemini's prompts)")
    ap.add_argument("--work", help="this story's working folder (default: <paths.work>/<strip>--<arc>)")
    ap.add_argument("--season", type=int, default=1, help="Plex season number for this arc")
    ap.add_argument("--plex", help='Plex numbers per prog instead of --season, e.g. "10=1x9,11=1x10" (prog=SxE)')
    ap.add_argument("--stage", choices=["tag", "read", "render", "art", "all"], default="all",
                    help="art: only redo the Plex artwork of episodes already rendered")
    ap.add_argument("--only", help='process just these progs from the range, e.g. "10" or "11-13,15"')
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--cast", default=str(HERE / "cast.yaml"))
    ap.add_argument("--tts", choices=["kokoro", "fake"], help="override tts.engine from config")
    ap.add_argument("--events", action="store_true", help="also print machine-readable @@{json} progress lines")
    args = ap.parse_args()

    def emit(event, **data):
        if args.events:
            print("@@" + json.dumps({"event": event, **data}), flush=True)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    work = Path(cfg["paths"]["work"]).expanduser()
    if not work.is_absolute():
        work = HERE / work
    arc_dir = Path(args.work) if args.work else work / f"{slug(args.strip)}--{slug(args.arc)}"
    progs = parse_progs(args.progs)
    if args.only:
        only = set(parse_progs(args.only))
        progs = [p for p in progs if p in only]

    from sources import make_source
    from render import Renderer, load_image
    from voices import Cast, make_tts

    source = make_source(cfg)
    cast = Cast(Path(args.cast), work / f"{Path(args.cast).stem}_assigned.yaml")
    reader = None

    def get_reader():
        nonlocal reader
        if reader is None:
            from reader import Reader
            reader = Reader(cfg["gemini"])
        return reader

    all_progs = parse_progs(args.progs)
    plex = {}
    for item in (args.plex or "").split(","):
        if item.strip():
            p, se = item.split("=")
            plex[int(p)] = tuple(int(x) for x in se.split("x"))
    for prog in progs:
        ep = all_progs.index(prog) + 1
        pdir = arc_dir / f"prog_{prog:04d}"
        pdir.mkdir(parents=True, exist_ok=True)
        print(f"\nProg {prog}  (episode {ep})")
        emit("prog_start", prog=prog, episode=ep)
        book = None

        def book_():
            nonlocal book
            if book is None:
                book = source.prog(prog)
            return book

        try:
            book_()
        except SystemExit as e:  # prog missing from the archive: carry on with the rest of the arc
            print(f"  {e}")
            emit("prog_skip", prog=prog, episode=ep, reason="not_found", detail=str(e))
            continue

        # 1. Which pages are this strip?
        tag_file = pdir / "tag.json"
        if not tag_file.exists():
            if args.stage in ("render", "art"):
                print("  no tag.json yet, skipping (run without --stage render first)")
                continue
            n = source.page_count(book_())
            print(f"  finding '{args.strip}' in {n} pages")
            emit("stage", prog=prog, stage="tag", detail=f"finding the strip in {n} pages")
            thumbs = [source.thumbnail(book_(), i) for i in range(1, n + 1)]
            tag = get_reader().tag_pages(thumbs, args.strip, args.arc, args.series).model_dump()
            tag_file.write_text(json.dumps(tag, indent=2), encoding="utf-8")
        tag = json.loads(tag_file.read_text(encoding="utf-8"))
        pages = tag["pages"]
        if not pages:
            print(f"  '{args.strip}' not found in this prog; edit {tag_file} if that's wrong")
            emit("prog_skip", prog=prog, episode=ep, reason="strip_not_found",
                 detail=f"'{args.strip}' wasn't found in this prog")
            continue
        print(f"  pages {pages}")
        if args.stage == "tag":
            continue

        # 2. Read each page into a script.
        for pg in pages:
            sfile = pdir / f"page_{pg:03d}.json"
            if sfile.exists() or args.stage in ("render", "art"):
                continue
            print(f"  reading page {pg}")
            emit("stage", prog=prog, stage="read", detail=f"reading page {pg} ({pages.index(pg) + 1} of {len(pages)})")
            img, mime = source.page(book_(), pg)
            script = get_reader().read_page(img, mime, args.strip, args.arc, cast.names, args.series)
            sfile.write_text(json.dumps(script.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
        if args.stage == "read":
            continue

        missing = [pg for pg in pages if not (pdir / f"page_{pg:03d}.json").exists()]
        if missing:
            print(f"  missing scripts for pages {missing}, skipping render")
            continue

        # 3. Render.
        scripts = [json.loads((pdir / f"page_{pg:03d}.json").read_text(encoding="utf-8")) for pg in pages]
        _write_transcript(pdir / "transcript.txt", scripts)
        images = [load_image(source.page(book_(), pg)[0]) for pg in pages]

        engine = args.tts or cfg["tts"]["engine"]
        tts = None if args.stage == "art" else make_tts(engine, cfg.get("elevenlabs"), work / "tts_cache")
        renderer = Renderer(cfg["render"], tts, cast)
        # The arc's prog list is the authority; Gemini's printed part number is only a cross-check (it has
        # mistaken 2000 AD's "Thrill 3" corner badge for "Part 3").
        part = ep
        if tag.get("part") and tag["part"] != ep:
            print(f"  note: page says part {tag['part']}, using {ep} from the arc's prog list")
        part_label = f"Part {part}" + (f": {tag['episode_title']}" if tag.get("episode_title") else "")
        show = safe(args.strip)
        season, plex_ep = plex.get(prog, (args.season, ep))
        season_dir = Path(cfg["paths"]["plex"]) / show / f"Season {season:02d}"
        out = season_dir / f"{show} - s{season:02d}e{plex_ep:02d} - {safe(args.arc)} - Part {part}.mp4"
        if args.stage == "art":
            if not out.exists():
                print(f"  {out.name} isn't rendered yet, skipping")
                continue
            _write_artwork(renderer, images[0], scripts[0], out)
            print(f"  artwork redone for {out.name}")
            emit("art_done", prog=prog, episode=ep, path=str(out))
            continue
        season_dir.mkdir(parents=True, exist_ok=True)  # fail now, not after a long render, if it isn't writable
        title_line = f"{args.strip}. {args.arc}. Part {part}."
        _check_credits(tts, cast, renderer.spoken_lines(scripts, title_line), prog)
        print(f"  rendering -> {out}")
        total = sum(len(s["panels"]) for s in scripts)
        renderer.episode(images, scripts, args.strip, args.arc, part_label, title_line, pdir / "segments", out,
                         progress=lambda n: emit("stage", prog=prog, stage="render", detail=f"panel {n} of {total}"))

        _write_artwork(renderer, images[0], scripts[0], out)
        print("  done")
        emit("episode_done", prog=prog, episode=ep, path=str(out))


def _check_credits(tts, cast, lines, prog):
    """Stop before voicing an episode the ElevenLabs account can't pay for, rather than run out halfway."""
    need, el = tts.elevenlabs_needed(cast, lines)
    if not need:
        return
    left = el.credits_left()
    if left is None:
        print(f"  ElevenLabs: about {need} credits for this episode (the API key can't read the balance; "
              "give it User: read access to have it checked)")
        return
    print(f"  ElevenLabs: about {need} credits for this episode, {left} left this month")
    if need > left:
        raise SystemExit(f"Not enough ElevenLabs credits for prog {prog}: it needs about {need}, the account has "
                         f"{left} left this month. Nothing was spent on it.")


def _write_artwork(renderer, first_page, first_script, out: Path):
    """Artwork for Plex, from the strip itself (an issue's cover is usually another strip's): the episode's
    still, and the show poster, which is the first page of the show's earliest episode."""
    try:
        renderer.thumbnail(first_page, first_script).save(out.with_suffix(".jpg"), quality=90)
        show_dir = out.parent.parent
        order = lambda f: tuple(int(n) for n in re.search(r"s(\d+)e(\d+)", f.name, re.I).groups())
        episodes = sorted((f for f in show_dir.glob("Season */*.mp4") if re.search(r"s\d+e\d+", f.name, re.I)),
                          key=order)
        if not (show_dir / "poster.jpg").exists() or (episodes and episodes[0].name == out.name):
            first_page.convert("RGB").save(show_dir / "poster.jpg", quality=90)
    except Exception as e:  # artwork is a nice-to-have
        print(f"  (couldn't save artwork: {e})")


def _write_transcript(path: Path, scripts):
    lines = []
    for s in scripts:
        for p in s["panels"]:
            for ln in p["lines"]:
                who = "NARRATOR" if ln["kind"] == "caption" else ln["speaker"].upper()
                if ln["kind"] == "sfx":
                    who = "SFX"
                lines.append(f"{who}: {ln['text']}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()

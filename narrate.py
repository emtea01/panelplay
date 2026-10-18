#!/usr/bin/env python3
"""
Turn a 2000 AD story arc into narrated, character-voiced episodes for Plex.

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
    ap.add_argument("--progs", required=True, help='e.g. "10-17" or "10,11,14-16"')
    ap.add_argument("--season", type=int, default=1, help="Plex season number for this arc")
    ap.add_argument("--stage", choices=["tag", "read", "render", "all"], default="all")
    ap.add_argument("--only", type=int, help="process just this prog number from the range")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--cast", default=str(HERE / "cast.yaml"))
    ap.add_argument("--tts", choices=["kokoro", "fake"], help="override tts.engine from config")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    work = Path(cfg["paths"]["work"]).expanduser()
    if not work.is_absolute():
        work = HERE / work
    arc_dir = work / f"{slug(args.strip)}--{slug(args.arc)}"
    progs = parse_progs(args.progs)
    if args.only:
        progs = [p for p in progs if p == args.only]

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
    for prog in progs:
        ep = all_progs.index(prog) + 1
        pdir = arc_dir / f"prog_{prog:04d}"
        pdir.mkdir(parents=True, exist_ok=True)
        print(f"\nProg {prog}  (episode {ep})")
        book = None

        def book_():
            nonlocal book
            if book is None:
                book = source.prog(prog)
            return book

        # 1. Which pages are this strip?
        tag_file = pdir / "tag.json"
        if not tag_file.exists():
            if args.stage == "render":
                print("  no tag.json yet, skipping (run without --stage render first)")
                continue
            n = source.page_count(book_())
            print(f"  finding '{args.strip}' in {n} pages")
            thumbs = [source.thumbnail(book_(), i) for i in range(1, n + 1)]
            tag = get_reader().tag_pages(thumbs, args.strip, args.arc).model_dump()
            tag_file.write_text(json.dumps(tag, indent=2), encoding="utf-8")
        tag = json.loads(tag_file.read_text(encoding="utf-8"))
        pages = tag["pages"]
        if not pages:
            print(f"  '{args.strip}' not found in this prog; edit {tag_file} if that's wrong")
            continue
        print(f"  pages {pages}")
        if args.stage == "tag":
            continue

        # 2. Read each page into a script.
        for pg in pages:
            sfile = pdir / f"page_{pg:03d}.json"
            if sfile.exists() or args.stage == "render":
                continue
            print(f"  reading page {pg}")
            img, mime = source.page(book_(), pg)
            script = get_reader().read_page(img, mime, args.strip, args.arc, cast.names)
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
        renderer = Renderer(cfg["render"], make_tts(engine, cfg.get("elevenlabs"), work / "tts_cache"), cast)
        part = tag.get("part") or ep
        part_label = f"Part {part}" + (f": {tag['episode_title']}" if tag.get("episode_title") else "")
        show = safe(args.strip)
        season_dir = Path(cfg["paths"]["plex"]) / show / f"Season {args.season:02d}"
        out = season_dir / f"{show} - s{args.season:02d}e{ep:02d} - {safe(args.arc)} - Part {part}.mp4"
        season_dir.mkdir(parents=True, exist_ok=True)  # fail now, not after a long render, if it isn't writable
        print(f"  rendering -> {out}")
        renderer.episode(images, scripts, args.strip, args.arc, part_label,
                         f"{args.strip}. {args.arc}. Part {part}.", pdir / "segments", out)

        # Artwork for Plex (used when the library is set to prefer local assets).
        try:
            cover = source.cover(book_())
            out.with_suffix(".jpg").write_bytes(cover)
            poster = season_dir.parent / "poster.jpg"
            if not poster.exists():
                poster.write_bytes(cover)
        except Exception as e:  # artwork is a nice-to-have
            print(f"  (couldn't save cover art: {e})")
        print("  done")


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

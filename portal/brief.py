"""A story's casting brief: who speaks in it and how they should sound, made once before its pages are read.
Lettering only shows an accent when it's spelt out ("Ze robot!"); Sláine's Irish voice comes from knowing
the strip. Comic Vine's character credits for the story's issues, when there are any, ground Gemini's list."""
import json
import time
from pathlib import Path
from typing import Literal, Optional

import requests
from pydantic import BaseModel, Field

from . import catalog, settings as S

CV_API = "https://comicvine.gamespot.com/api"
BRIEF_FILE = "characters.json"


class Character(BaseModel):
    name: str = Field(description="The name as the comic spells it")
    gender: Literal["male", "female", "robot", "unknown"]
    accent: Optional[str] = Field(None, description="e.g. 'Irish', 'Scottish', 'German', or null for none in particular")
    notes: str = Field(description="A few words on how they sound: age, manner")


class Word(BaseModel):
    word: str = Field(description="A name as the comic spells it")
    say_as: str = Field(description="Respelt in plain English syllables, stressed syllable first, e.g. 'Slawn-ya'")


class Brief(BaseModel):
    characters: list[Character]
    pronounce: list[Word] = Field(default_factory=list)


PROMPT = """We're making an audio drama of the {strip} story "{title}" from the comic "{series}" ({issues}).
List the characters likely to speak in it, from what you know of the comic.{cv}
For each give:
- name: as the comic spells it.
- gender.
- accent: the accent their voice should have when the comic establishes or clearly implies one through their
  nationality, culture or homeland (e.g. Irish, Scottish, German, Russian). Give people of one culture the same
  accent. Null when there's no particular accent (most Mega-City One citizens, for instance).
- notes: a few words on how they sound (age, manner).
Only list characters you're confident appear in this story or are regulars of the strip at the time.
If you don't know the story, list the strip's regulars only; return an empty list rather than guess.
Also list under pronounce the names in the story (characters, places, peoples, gods) a voice actor would likely
say wrong from the spelling, each respelt the way the comic's creators say it, in plain English syllables a
text-to-speech voice will read right (e.g. Sláine -> "Slawn-ya", Tir-Nan-Og -> "Teer-nan-ogue"). Leave out
names any English reader says right."""


def _cv_characters(s: dict, series: dict, progs: list[int]) -> list[str]:
    """Characters Comic Vine credits in the story's issues (cached), most often credited first."""
    cache = S.data_dir() / "comicvine"
    counts = {}
    for n in progs[:12]:
        f = cache / f"chars_{series['cv_id']}_{n}.json"
        if f.exists():
            names = json.loads(f.read_text(encoding="utf-8"))
        else:
            names = []
            found = _cv_get(s, "issues/", filter=f"volume:{series['cv_id']},issue_number:{n}", field_list="id")
            for it in found["results"][:1]:
                credits = _cv_get(s, f"issue/4000-{it['id']}/", field_list="character_credits")
                names = [c["name"] for c in (credits["results"] or {}).get("character_credits") or []]
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(names), encoding="utf-8")
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts, key=lambda k: -counts[k])


def _cv_get(s: dict, path: str, **params) -> dict:
    time.sleep(1)  # Comic Vine asks for no more than about one request a second
    r = requests.get(f"{CV_API}/{path}", params={"api_key": s["comicvine_api_key"], "format": "json", **params},
                     headers={"User-Agent": catalog.CV_USER_AGENT}, timeout=60)
    data = r.json()
    if data.get("status_code") != 1:
        raise RuntimeError(f"Comic Vine: {data.get('error', 'error')}")
    return data


def make(s: dict, series: dict, arc: dict, arc_dir: Path, log) -> None:
    """Write arc_dir/characters.json unless it's there already. log(str) reports progress; failures are only
    logged, since the story can still be read and voiced without a brief."""
    out = arc_dir / BRIEF_FILE
    if out.exists():
        return
    cv_names = []
    if s.get("comicvine_api_key") and series.get("cv_id"):
        try:
            cv_names = _cv_characters(s, series, arc["progs"])
            log(f"Comic Vine credits {len(cv_names)} characters in these issues.\n")
        except Exception as e:
            log(f"Couldn't get characters from Comic Vine: {e}\n")
    from reader import Reader

    issues = ", ".join(catalog.issue_label(series, p) for p in arc["progs"][:12])
    cv = ("\nComic Vine credits these characters in those issues (an anthology's issues include other strips' "
          "characters, so leave those out): " + ", ".join(cv_names[:80]) + ".") if cv_names else ""
    try:
        reader = Reader({"api_key": s["gemini_api_key"], "paid_api_key": s.get("gemini_paid_api_key"),
                         "tag_model": s["gemini_tag_model"], "read_model": s["gemini_read_model"]})
        brief = reader.ask(s["gemini_read_model"],
                           PROMPT.format(strip=arc["strip"], title=arc["title"], series=series["name"],
                                         issues=issues, cv=cv), Brief)
    except (Exception, SystemExit) as e:  # Reader raises SystemExit when there's no key at all
        log(f"Couldn't make a casting brief: {e}\n")
        return
    arc_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(brief.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    log("Casting brief: " + (", ".join(c.name + (f" ({c.accent})" if c.accent else "") for c in brief.characters)
                             or "no characters known") + "\n")
    if brief.pronounce:
        log("Saying: " + ", ".join(f"{w.word} as {w.say_as}" for w in brief.pronounce) + "\n")

"""The voice cast, kept in the portal's database so voices can be changed on the Voices page. The cast file in
the repo (Settings → Voices) seeds it once and still supplies the engine and the pools that voice characters
nobody has cast; each render gets a cast file built from both."""
import re
from pathlib import Path

import requests
import yaml

from . import settings as S

NARRATOR = "Narrator"


def seed(db):
    """First run: copy the cast file's narrator, characters and pronunciations into the database."""
    s = S.current(db)
    if s.get("cast_seeded"):
        return
    f = S.ROOT / s["cast_file"]
    if f.exists():
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        notes = _comments(f)
        entries = {NARRATOR: data.get("narrator") or {}, **(data.get("characters") or {})}
        for name, v in entries.items():
            if v.get("voice"):
                voice_name, note = notes.get(name, ("", ""))
                db.set_voice(name, v["voice"], voice_name, v.get("accent", ""), v.get("fx", ""),
                             float(v.get("speed", 1.0)), note)
        for word, say_as in (data.get("pronounce") or {}).items():
            db.set_pronunciation(word, say_as)
    db.set_settings({"cast_seeded": True})


def _comments(f: Path) -> dict:
    """{name: (voice name, note)} from the cast file's comments ("# Callum - husky trickster")."""
    out, section = {}, None
    for line in f.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\w", line):
            section = line.split(":")[0]
        m = re.match(r"^\s+(?:voice:\s*\S+\s*#\s*(.+)|([^:#]+):\s*\{.*\}\s*#\s*(.+))$", line)
        if not m:
            continue
        name, comment = (NARRATOR, m.group(1)) if m.group(1) and section == "narrator" else (m.group(2), m.group(3))
        if name and comment:
            voice_name, _, note = comment.partition(" - ")
            out[name.strip()] = (voice_name.strip(), note.strip())
    return out


def build(db, s: dict, out_dir: Path) -> Path:
    """A cast file for one render: the base cast file with the database's voices and pronunciations. It keeps
    the base file's name, so the automatic picks (<name>_assigned.yaml) carry over from render to render."""
    base = yaml.safe_load((S.ROOT / s["cast_file"]).read_text(encoding="utf-8")) or {}
    characters = {}
    for v in db.voices():
        entry = {"voice": v["voice"]}
        if v["fx"]:
            entry["fx"] = v["fx"]
        if v["accent"]:
            entry["accent"] = v["accent"]
        if v["native_accent"]:
            entry["native_accent"] = v["native_accent"].lower()
        if v["speed"] != 1.0:
            entry["speed"] = v["speed"]
        if v["name"].lower() == NARRATOR.lower():
            base["narrator"] = entry
        else:
            characters[v["name"]] = entry
    base["characters"] = characters
    base["pronounce"] = {p["word"]: p["say_as"] for p in db.pronunciations()}
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / s["cast_file"]
    out.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return out


def native_accent(s: dict, voice: str, voice_name: str = "") -> str | None:
    """The voice's own accent as ElevenLabs labels it ('irish'), '' when it has none (a designed voice), or None
    when it can't be found. Your own and built-in voices answer by ID; a library voice may only be found by
    searching the library for its name."""
    if not s.get("elevenlabs_api_key") or not voice:
        return None
    headers = {"xi-api-key": s["elevenlabs_api_key"]}
    try:
        r = requests.get(f"https://api.elevenlabs.io/v1/voices/{voice}", headers=headers, timeout=20)
        if r.ok:
            return ((r.json().get("labels") or {}).get("accent") or "").lower()
        if voice_name:
            r = requests.get("https://api.elevenlabs.io/v1/shared-voices", headers=headers, timeout=20,
                             params={"search": voice_name, "page_size": 100})
            for v in r.json().get("voices", []) if r.ok else []:
                if v["voice_id"] == voice:
                    return (v.get("accent") or "").lower()
    except requests.RequestException:
        pass
    return None


def fill_native_accents(db, s: dict):
    """Look up the accent of cast voices that haven't been looked up yet."""
    for v in db.voices():
        if v["native_accent"] is None:
            found = native_accent(s, v["voice"], v["voice_name"])
            if found is not None:
                db.set_native_accent(v["name"], found)


def assigned(s: dict) -> list[dict]:
    """Voices the renderer picked by itself for characters nobody has cast."""
    f = Path(s["work_path"]) / f"{Path(s['cast_file']).stem}_assigned.yaml"
    if not f.exists():
        return []
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return sorted(({"name": k, **v} for k, v in data.items()), key=lambda v: v["name"])


def find(s: dict, accent="", gender="", age="", q="") -> list[dict]:
    """Voices in ElevenLabs' shared library."""
    params = {"page_size": 30, "sort": "usage_character_count_1y"}
    params.update({k: v for k, v in (("accent", accent.lower()), ("gender", gender), ("age", age), ("search", q)) if v})
    r = requests.get("https://api.elevenlabs.io/v1/shared-voices", params=params, timeout=30,
                     headers={"xi-api-key": s["elevenlabs_api_key"]})
    r.raise_for_status()
    return [{"voice": v["voice_id"], "name": v["name"], "short": v["name"].split(" - ")[0].strip(),
             "accent": v.get("accent") or "", "age": v.get("age") or "", "gender": v.get("gender") or "",
             "use": (v.get("use_case") or "").replace("_", " "), "description": v.get("description") or "",
             "preview": v.get("preview_url") or ""} for v in r.json().get("voices", [])]

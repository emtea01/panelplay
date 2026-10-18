"""Admin settings: what they are, their defaults, and turning them into a renderer config."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent  # the panelplay folder (renderer + portal)

# key: (label, default, kind). kinds: text, path, secret, bool, int, choice:<a|b|c>
FIELDS = {
    # Library
    "source":            ("Comics come from", "folder", "choice:folder|komga"),
    "comics_path":       ("Comics folder (the 2000 AD progs)", "", "path"),
    "komga_url":         ("Komga URL", "http://komga.lan:25600", "text"),
    "komga_api_key":     ("Komga API key", "", "secret"),
    "komga_user":        ("Komga user (if no API key)", "", "text"),
    "komga_password":    ("Komga password", "", "secret"),
    "komga_series":      ("Komga series name", "2000 AD", "text"),
    "output_path":       ("Write finished episodes to (a Plex TV library folder)", "", "path"),
    "work_path":         ("Working folder (page scripts, voice cache)", "", "path"),
    # Keys
    "gemini_api_key":     ("Gemini API key", "", "secret"),
    "elevenlabs_api_key": ("ElevenLabs API key", "", "secret"),
    "comicvine_api_key":  ("Comic Vine API key", "", "secret"),
    # Reading
    "gemini_tag_model":  ("Gemini model for finding the strip's pages", "gemini-flash-lite-latest", "text"),
    "gemini_read_model": ("Gemini model for reading pages", "gemini-flash-latest", "text"),
    "gemini_rpm":        ("Gemini requests per minute", 8, "int"),
    # Voices
    "cast_file":         ("Voice cast", "cast-elevenlabs.yaml", "cast"),
    "tts_engine":        ("Default voice engine (for cast entries that don't name one)", "kokoro",
                          "choice:kokoro|fake"),
    "elevenlabs_model":  ("ElevenLabs model", "eleven_v3", "text"),
    # Picture
    "render_width":      ("Width", 1920, "int"),
    "render_height":     ("Height", 1080, "int"),
    "render_fps":        ("Frames per second", 25, "int"),
    "motion":            ("Slow zoom on panels", True, "bool"),
    "punch":             ("Punch-in zoom into each panel", True, "bool"),
    "page_intro":        ("Show each full page before its panels", False, "bool"),
    "read_sfx":          ("Read sound effects aloud", False, "bool"),
}

GROUPS = [
    ("Library", ["source", "comics_path", "komga_url", "komga_api_key", "komga_user", "komga_password",
                 "komga_series", "output_path", "work_path"]),
    ("API keys", ["gemini_api_key", "elevenlabs_api_key", "comicvine_api_key"]),
    ("Reading the pages", ["gemini_tag_model", "gemini_read_model", "gemini_rpm"]),
    ("Voices", ["cast_file", "tts_engine", "elevenlabs_model"]),
    ("Picture", ["render_width", "render_height", "render_fps", "motion", "punch", "page_intro", "read_sfx"]),
]


def data_dir() -> Path:
    return Path(os.environ.get("PANELPLAY_DATA", ROOT / "data")).resolve()


def defaults() -> dict:
    d = {k: v[1] for k, v in FIELDS.items()}
    d["work_path"] = str(data_dir() / "work")
    return d


def cast_files() -> list[str]:
    return sorted(p.name for p in ROOT.glob("cast*.yaml"))


def current(db) -> dict:
    return {**defaults(), **db.get_settings()}


def seed_from_dev_setup(db):
    """First run: borrow the command-line renderer's config.yaml if there is one, and keys and folders from
    the environment (PANELPLAY_COMICS_PATH / PANELPLAY_OUTPUT_PATH, optional)."""
    if db.get_settings():
        return
    seed = {}
    cfg_file = ROOT / "config.yaml"
    if cfg_file.exists():
        cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        folder, paths = cfg.get("folder") or {}, cfg.get("paths") or {}
        if folder.get("path"):
            seed["comics_path"] = folder["path"]
        if paths.get("plex"):
            seed["output_path"] = str(Path(paths["plex"]))
        if paths.get("work"):
            w = Path(paths["work"])
            seed["work_path"] = str(w if w.is_absolute() else (ROOT / w).resolve())
        seed["source"] = cfg.get("source", "folder")
    for env, key in (("GEMINI_API_KEY", "gemini_api_key"), ("ELEVENLABS_API_KEY", "elevenlabs_api_key"),
                     ("COMICVINE_API_KEY", "comicvine_api_key"),
                     ("PANELPLAY_COMICS_PATH", "comics_path"), ("PANELPLAY_OUTPUT_PATH", "output_path")):
        if os.environ.get(env):
            seed[key] = os.environ[env]
    db.set_settings(seed or {"source": "folder"})


def parse_form(form: dict, existing: dict) -> dict:
    """Form values -> settings. Blank secrets keep the stored value; unticked checkboxes are False."""
    out = {}
    for key, (_, default, kind) in FIELDS.items():
        raw = form.get(key)
        if kind == "bool":
            out[key] = raw == "on"
        elif kind == "secret":
            if raw == "__clear__":
                out[key] = ""
            elif raw:
                out[key] = raw.strip()
        elif kind == "int":
            try:
                out[key] = int(raw)
            except (TypeError, ValueError):
                out[key] = existing.get(key, default)
        elif raw is not None:
            out[key] = raw.strip()
    return out


def renderer_config(s: dict) -> dict:
    """The config.yaml the renderer expects. API keys are passed separately, as environment variables."""
    return {
        "source": s["source"],
        "komga": {"url": s["komga_url"], "api_key": s["komga_api_key"], "user": s["komga_user"],
                  "password": s["komga_password"], "series": s["komga_series"]},
        "folder": {"path": s["comics_path"], "prog_regex": ""},
        "gemini": {"api_key": "", "tag_model": s["gemini_tag_model"], "read_model": s["gemini_read_model"],
                   "requests_per_minute": s["gemini_rpm"]},
        "tts": {"engine": s["tts_engine"]},
        "elevenlabs": {"api_key": "", "model": s["elevenlabs_model"]},
        "render": {"width": s["render_width"], "height": s["render_height"], "fps": s["render_fps"],
                   "motion": s["motion"], "punch": s["punch"], "page_intro": s["page_intro"],
                   "read_sfx": s["read_sfx"]},
        "paths": {"work": s["work_path"], "plex": s["output_path"]},
    }


def renderer_env(s: dict) -> dict:
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONWARNINGS": "ignore", "PYTHONUNBUFFERED": "1"}
    if s.get("gemini_api_key"):
        env["GEMINI_API_KEY"] = s["gemini_api_key"]
    if s.get("elevenlabs_api_key"):
        env["ELEVENLABS_API_KEY"] = s["elevenlabs_api_key"]
    return env


def problems(s: dict) -> list[str]:
    """What still needs setting before a render can run."""
    out = []
    if s["source"] == "folder" and not (s["comics_path"] and Path(s["comics_path"]).is_dir()):
        out.append("Set the comics folder (Settings → Library).")
    if s["source"] == "komga" and not s["komga_url"]:
        out.append("Set the Komga URL (Settings → Library).")
    if not s["output_path"]:
        out.append("Set where finished episodes go (Settings → Library).")
    if not s["gemini_api_key"]:
        out.append("Add a Gemini API key (Settings → API keys).")
    return out

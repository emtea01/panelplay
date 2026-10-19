"""Text-to-speech with a cast list. Kokoro runs on CPU, ElevenLabs in the cloud (per character); 'fake' makes tones."""
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from pathlib import Path

import numpy as np
import yaml

SR = 24000
DELIVERY_SPEED = {"normal": 1.0, "shout": 1.08, "whisper": 0.92, "radio": 1.0}
DELIVERY_GAIN = {"normal": 1.0, "shout": 1.15, "whisper": 0.7, "radio": 0.95}


def _name_key(name: str) -> str:
    """Sláine and SLAINE are one character."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch)).lower().strip()


_VARIANTS = {"a": "áàâäãå", "e": "éèêë", "i": "íìîï", "o": "óòôöõø", "u": "úùûü", "n": "ñ", "c": "ç", "y": "ýÿ"}


def _loose_name(key: str) -> re.Pattern:
    """The name as a whole word, with or without its accents, in any case."""
    return re.compile(r"(?<!\w)" + "".join(f"[{re.escape(ch)}{_VARIANTS.get(ch, '')}]" if ch.isalpha() else re.escape(ch)
                                            for ch in key) + r"(?!\w)", re.I)


class Cast:
    def __init__(self, cast_file: Path, assigned_file: Path):
        data = yaml.safe_load(cast_file.read_text(encoding="utf-8"))
        self.engine = data.get("engine")  # default engine for every entry that doesn't name one
        self.narrator = data["narrator"]
        self.characters = {_name_key(k): v for k, v in (data.get("characters") or {}).items()}
        self.names = list((data.get("characters") or {}).keys())
        # Names respelt as said ("Sláine: Slawn-ya"), so every line says them the same way.
        self.pronounce = {_name_key(k): v for k, v in (data.get("pronounce") or {}).items()}
        self.story_accents, self.story_ages = {}, {}
        self.world_accent = None    # the accent of anyone in the story the brief doesn't list
        self.story_narrator = None  # (name, gender) of the character whose voice the captions are in
        self.library = None         # VoiceLibrary: native-accent voices from ElevenLabs' shared library
        self.used = set()           # voices given to this story's characters, so no two share one
        self.pools = data["pools"]
        self.assigned_file = assigned_file
        self.assigned = {}
        if assigned_file.exists():
            self.assigned = {k.lower(): v for k, v in (yaml.safe_load(assigned_file.read_text(encoding="utf-8")) or {}).items()}

    def use_brief(self, brief: dict):
        """Accents, ages, the narrator and pronunciations from the story's casting brief (portal/brief.py),
        where the cast file doesn't give them."""
        characters = brief.get("characters", [])
        self.story_accents = {_name_key(c["name"]): c["accent"] for c in characters if c.get("accent")}
        self.story_ages = {_name_key(c["name"]): c["age"] for c in characters if c.get("age")}
        self.world_accent = brief.get("world_accent")
        teller = next((c for c in characters if brief.get("narrator") and _name_key(c["name"]) == _name_key(brief["narrator"])),
                      None)
        self.story_narrator = (teller["name"], teller["gender"]) if teller else None
        # "OOK-koh" -> "Ook-koh": a voice may spell out capitals.
        self.pronounce = {**{_name_key(w["word"]): w["say_as"][:1].upper() + w["say_as"][1:].lower()
                             for w in brief.get("pronounce", [])}, **self.pronounce}

    def precast(self, characters: list[dict]) -> list[str]:
        """Give the brief's characters their voices in its order (leads first, so they get the best match).
        Returns a line per character for the log."""
        out = []
        for c in characters:
            v = self.voice_for(c["name"], c["gender"], "speech")
            out.append(f"{c['name']}: {v.get('voice_name') or v['voice']}"
                       + (f" ({v['native_accent']} voice)" if v.get("native_accent")
                          else f" (+ {v['accent']} accent tag)" if v.get("accent") else ""))
        return out

    def _accent_for(self, speaker: str, speaker_type: str) -> str | None:
        key = _name_key(speaker)
        if key in self.characters:
            return self.characters[key].get("accent") or self.characters[key].get("native_accent")
        return self.story_accents.get(key) or (self.world_accent if speaker_type != "robot" else None)

    def respell(self, text: str) -> str:
        for key, say_as in self.pronounce.items():
            text = _loose_name(key).sub(say_as, text)
        return text

    def voice_for(self, speaker: str, speaker_type: str, kind: str, line_accent: str | None = None) -> dict:
        """line_accent: the accent the reader saw spelt out in this line ("Ze robot!"), used when neither the
        cast file nor the brief gives the character one."""
        if speaker == "Title":  # the episode's title card: always the series narrator
            v = self.narrator
        elif kind == "caption" or speaker.lower() in ("narrator", "caption"):
            if self.story_narrator:  # captions in a character's voice (Ukko tells Sláine's saga)
                return self.voice_for(*self.story_narrator, "speech")
            v = self.narrator
        else:
            accent = self._accent_for(speaker, speaker_type) or line_accent
            v = self._voice_for(speaker, speaker_type, accent)
            accent = accent or v.get("for_accent")
            if accent:
                v = {**v, "accent": accent}
        return {"engine": self.engine, **v} if self.engine else v

    def _voice_for(self, speaker: str, speaker_type: str, accent: str | None) -> dict:
        if _name_key(speaker) in self.characters:
            v = self.characters[_name_key(speaker)]
            self.used.add(v["voice"])
            return v
        key = speaker.lower().strip()
        # A voice picked for another accent (or before accents were known) is picked again. A character keeps
        # their accent in an episode whose lettering doesn't show it.
        if key not in self.assigned or (accent and self.assigned[key].get("for_accent") != accent):
            v = None
            if accent and self.library and speaker_type in ("male", "female"):
                v = self.library.pick(accent, speaker_type, self.story_ages.get(_name_key(speaker)), self.used)
            if not v:
                pool = self.pools.get(speaker_type) or self.pools["unknown"]
                v = {"voice": pool[int(hashlib.md5(key.encode()).hexdigest(), 16) % len(pool)], "speed": 1.0}
                if speaker_type == "robot":
                    v["fx"] = "robot"
            self.assigned[key] = {**v, "for_accent": accent}
            self._save()
        self.used.add(self.assigned[key]["voice"])
        return self.assigned[key]

    def _save(self):
        self.assigned_file.parent.mkdir(parents=True, exist_ok=True)
        self.assigned_file.write_text(
            "# Voices picked automatically for characters not in cast.yaml. Edit freely,\n"
            "# or move a character into cast.yaml to make it permanent.\n"
            + yaml.safe_dump(self.assigned, sort_keys=True),
            encoding="utf-8",
        )


class VoiceLibrary:
    """Native-accent voices from ElevenLabs' shared voice library (usable by ID on a paid plan, no voice slot
    needed). An Irish voice sounds Irish; an American one told "[strong Irish accent]" sounds American-Irish."""
    GOOD_USES = ("narrative_story", "characters_animation", "conversational")
    ACCENTS = {"celtic": "irish", "gaelic": "irish", "northern irish": "irish", "english": "british"}

    def __init__(self, api_key: str, cache_dir: Path):
        self.key, self.cache = api_key, cache_dir

    @classmethod
    def norm(cls, accent: str) -> str:
        """'Irish (Connacht)' -> 'irish', 'Celtic' -> 'irish': the library's accent names."""
        accent = accent.lower().split("(")[0].strip()
        return cls.ACCENTS.get(accent, accent)

    def _voices(self, accent: str, gender: str) -> list[dict]:
        accent = self.norm(accent)
        f = self.cache / f"{re.sub(r'[^a-z]+', '_', accent)}_{gender}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        import requests

        r = requests.get("https://api.elevenlabs.io/v1/shared-voices", headers={"xi-api-key": self.key}, timeout=30,
                         params={"accent": accent, "gender": gender, "page_size": 40, "sort": "usage_character_count_1y"})
        voices = [{k: v.get(k) for k in ("voice_id", "name", "age", "use_case", "accent")}
                  for v in (r.json().get("voices", []) if r.ok else [])]
        if r.ok:
            self.cache.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(voices), encoding="utf-8")
        return voices

    def pick(self, accent: str, gender: str, age: str | None, exclude: set) -> dict | None:
        """The most-used voice of that accent and gender, preferring the right age and storytelling voices,
        that nobody else in the story has."""
        voices = [v for v in self._voices(accent, gender) if v["voice_id"] not in exclude]
        if not voices:
            return None
        best = min(voices, key=lambda v: ((v["age"] != age) if age else 0, v["use_case"] not in self.GOOD_USES,
                                         voices.index(v)))
        return {"voice": best["voice_id"], "voice_name": best["name"].split(" - ")[0], "speed": 1.0,
                "native_accent": self.norm(accent)}


class KokoroTTS:
    def __init__(self):
        from kokoro import KPipeline  # noqa: F401  (fail early if missing)

        self._KPipeline = KPipeline
        self.pipes = {}

    def say(self, text: str, voice: str, speed: float) -> np.ndarray:
        lang = voice[0]  # 'a' American, 'b' British
        if lang not in self.pipes:
            self.pipes[lang] = self._KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M")
        chunks = []
        for _, _, audio in self.pipes[lang](text, voice=voice, speed=speed):
            if audio is not None:
                chunks.append(audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio))
        return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(1, np.float32)


class ElevenLabsTTS:
    """ElevenLabs voices, one request per line. Every line is cached on disk, so re-renders cost no credits."""

    def __init__(self, cfg: dict, cache_dir: Path):
        import requests

        self.key = cfg.get("api_key") or os.environ.get("ELEVENLABS_API_KEY")
        if not self.key:
            raise SystemExit("No ElevenLabs API key: set elevenlabs.api_key in config.yaml or ELEVENLABS_API_KEY")
        self.model = cfg.get("model", "eleven_v3")
        self.cache = cache_dir
        self.cache.mkdir(parents=True, exist_ok=True)
        self.s = requests.Session()
        self.s.headers["xi-api-key"] = self.key

    def _cache_file(self, text: str, voice: str) -> Path:
        return self.cache / f"{hashlib.sha1(f'{self.model}|{voice}|{text}'.encode()).hexdigest()[:20]}.npy"

    def is_cached(self, text: str, voice: str) -> bool:
        return self._cache_file(text, voice).exists()

    def credits_per_char(self) -> float:
        return 0.5 if "flash" in self.model or "turbo" in self.model else 1.0

    def credits_left(self) -> int | None:
        """Credits left this month on the account, or None if the key can't read that (it needs User: read)."""
        r = self.s.get("https://api.elevenlabs.io/v1/user/subscription", timeout=20)
        if not r.ok:
            return None
        sub = r.json()
        return sub["character_limit"] - sub["character_count"]

    def say(self, text: str, voice: str, speed: float) -> np.ndarray:
        f = self._cache_file(text, voice)
        if f.exists():
            return np.load(f)
        r = self.s.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                        params={"output_format": "mp3_44100_128"},
                        json={"text": text, "model_id": self.model}, timeout=180)
        if not r.ok:
            # One line, ElevenLabs' own message first: the portal shows the last output line as the job's error.
            try:
                detail = r.json().get("detail")
                detail = detail.get("message") or detail.get("status") if isinstance(detail, dict) else detail
            except ValueError:
                detail = r.text[:300]
            raise SystemExit(f"ElevenLabs HTTP {r.status_code}: {detail} (lines voiced so far are cached)")
        # Decode the MP3 to 24 kHz mono float with ffmpeg.
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", str(SR), "pipe:1"],
                             input=r.content, capture_output=True, check=True).stdout
        audio = np.frombuffer(pcm, np.float32).copy()
        np.save(f, audio)
        print(f"        elevenlabs: {len(text)} chars", flush=True)
        return audio


class MultiTTS:
    """Routes each line to the engine its cast entry asks for (default Kokoro), loading engines only when used."""

    def __init__(self, default: str, el_cfg: dict, cache_dir: Path):
        self.default, self.el_cfg, self.cache_dir = default, el_cfg, cache_dir
        self.engines = {}

    def engine(self, name: str | None):
        name = "fake" if self.default == "fake" else (name or self.default)
        if name not in self.engines:
            self.engines[name] = {"kokoro": KokoroTTS, "fake": FakeTTS,
                                  "elevenlabs": lambda: ElevenLabsTTS(self.el_cfg, self.cache_dir / "elevenlabs")}[name]()
        return self.engines[name]

    def say(self, text: str, voice: str, speed: float, engine: str | None = None) -> np.ndarray:
        return self.engine(engine).say(text, voice, speed)

    def elevenlabs_needed(self, cast: "Cast", lines: list[dict]) -> tuple[int, "ElevenLabsTTS | None"]:
        """Credits the ElevenLabs lines not yet in the cache will cost, and the engine (None if no line uses it)."""
        todo = set()
        for line in lines:
            v, text, _ = line_request(cast, line)
            if v.get("engine") == "elevenlabs" and self.default != "fake":
                todo.add((text, v["voice"]))
        if not todo:
            return 0, None
        el = self.engine("elevenlabs")
        chars = sum(len(text) for text, voice in todo if not el.is_cached(text, voice))
        return int(chars * el.credits_per_char() + 0.5), el


class FakeTTS:
    """Tone whose length tracks the text, so the render can be tested without models."""

    def say(self, text: str, voice: str, speed: float) -> np.ndarray:
        dur = max(0.6, 0.065 * len(text) / speed)
        t = np.arange(int(SR * dur)) / SR
        f = 140 + int(hashlib.md5(voice.encode()).hexdigest(), 16) % 260
        env = np.minimum(1, np.minimum(t, dur - t) * 20)
        return (0.2 * np.sin(2 * np.pi * f * t) * env).astype(np.float32)


def apply_fx(audio: np.ndarray, fx: str) -> np.ndarray:
    if fx == "robot":
        # Ring modulation: the classic tin-can robot voice, half mixed with the dry voice.
        t = np.arange(len(audio)) / SR
        return (0.45 * audio + 0.75 * audio * np.sin(2 * np.pi * 45 * t)).astype(np.float32)
    if fx == "radio":
        # Crude telephone band-pass using FFT masking.
        spec = np.fft.rfft(audio)
        freqs = np.fft.rfftfreq(len(audio), 1 / SR)
        spec[(freqs < 350) | (freqs > 3200)] = 0
        out = np.fft.irfft(spec, n=len(audio))
        return np.clip(out * 1.6, -1, 1).astype(np.float32)
    return audio


def make_tts(engine: str, el_cfg: dict | None = None, cache_dir: Path | None = None):
    return MultiTTS(engine, el_cfg or {}, cache_dir or Path("work/tts_cache"))


def line_request(cast: Cast, line: dict) -> tuple[dict, str, float]:
    """(cast entry, text to send, speed) for one line."""
    v = cast.voice_for(line["speaker"], line.get("speaker_type", "unknown"), line["kind"], line.get("accent"))
    speed = float(v.get("speed", 1.0)) * DELIVERY_SPEED.get(line.get("delivery", "normal"), 1.0)
    text = cast.respell(line["text"])
    if v.get("engine") == "elevenlabs":  # ElevenLabs v3 audio tags; other engines skip them
        if line.get("tag"):
            text = f"[{line['tag']}] {text}"  # e.g. "screams in agony"
        accent = v.get("accent")
        if accent and VoiceLibrary.norm(accent) != v.get("native_accent"):  # a native voice needs no telling
            text = f"[strong {accent} accent] {text}"
    return v, text, speed


def speak_line(tts, cast: Cast, line: dict) -> np.ndarray:
    v, text, speed = line_request(cast, line)
    delivery = line.get("delivery", "normal")
    audio = tts.say(text, v["voice"], speed, engine=v.get("engine"))
    fx = v.get("fx") or ("radio" if delivery == "radio" else None)
    if fx:
        audio = apply_fx(audio, fx)
    return np.clip(audio * DELIVERY_GAIN.get(delivery, 1.0), -1, 1)

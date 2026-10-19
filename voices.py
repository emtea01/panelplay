"""Text-to-speech with a cast list. Kokoro runs on CPU, ElevenLabs in the cloud (per character); 'fake' makes tones."""
import hashlib
import os
import subprocess
from pathlib import Path

import numpy as np
import yaml

SR = 24000
DELIVERY_SPEED = {"normal": 1.0, "shout": 1.08, "whisper": 0.92, "radio": 1.0}
DELIVERY_GAIN = {"normal": 1.0, "shout": 1.15, "whisper": 0.7, "radio": 0.95}


class Cast:
    def __init__(self, cast_file: Path, assigned_file: Path):
        data = yaml.safe_load(cast_file.read_text(encoding="utf-8"))
        self.engine = data.get("engine")  # default engine for every entry that doesn't name one
        self.narrator = data["narrator"]
        self.characters = {k.lower(): v for k, v in (data.get("characters") or {}).items()}
        self.names = list((data.get("characters") or {}).keys())
        self.pools = data["pools"]
        self.assigned_file = assigned_file
        self.assigned = {}
        if assigned_file.exists():
            self.assigned = {k.lower(): v for k, v in (yaml.safe_load(assigned_file.read_text(encoding="utf-8")) or {}).items()}

    def voice_for(self, speaker: str, speaker_type: str, kind: str) -> dict:
        v = self._voice_for(speaker, speaker_type, kind)
        return {"engine": self.engine, **v} if self.engine else v

    def _voice_for(self, speaker: str, speaker_type: str, kind: str) -> dict:
        if kind == "caption" or speaker.lower() in ("narrator", "caption"):
            return self.narrator
        key = speaker.lower().strip()
        if key in self.characters:
            return self.characters[key]
        if key not in self.assigned:
            pool = self.pools.get(speaker_type) or self.pools["unknown"]
            idx = int(hashlib.md5(key.encode()).hexdigest(), 16) % len(pool)
            v = {"voice": pool[idx], "speed": 1.0}
            if speaker_type == "robot":
                v["fx"] = "robot"
            self.assigned[key] = v
            self._save()
        return self.assigned[key]

    def _save(self):
        self.assigned_file.parent.mkdir(parents=True, exist_ok=True)
        self.assigned_file.write_text(
            "# Voices picked automatically for characters not in cast.yaml. Edit freely,\n"
            "# or move a character into cast.yaml to make it permanent.\n"
            + yaml.safe_dump(self.assigned, sort_keys=True),
            encoding="utf-8",
        )


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
    v = cast.voice_for(line["speaker"], line.get("speaker_type", "unknown"), line["kind"])
    speed = float(v.get("speed", 1.0)) * DELIVERY_SPEED.get(line.get("delivery", "normal"), 1.0)
    text = line["text"]
    if v.get("engine") == "elevenlabs":  # ElevenLabs v3 audio tags; other engines skip them
        if line.get("tag"):
            text = f"[{line['tag']}] {text}"  # e.g. "screams in agony"
        accent = v.get("accent") or line.get("accent")  # a cast entry's accent wins over the reader's
        if accent:
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

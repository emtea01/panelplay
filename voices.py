"""Text-to-speech with a cast list. Kokoro runs on CPU; 'fake' makes tones for testing."""
import hashlib
from pathlib import Path

import numpy as np
import yaml

SR = 24000
DELIVERY_SPEED = {"normal": 1.0, "shout": 1.08, "whisper": 0.92, "radio": 1.0}
DELIVERY_GAIN = {"normal": 1.0, "shout": 1.15, "whisper": 0.7, "radio": 0.95}


class Cast:
    def __init__(self, cast_file: Path, assigned_file: Path):
        data = yaml.safe_load(cast_file.read_text())
        self.narrator = data["narrator"]
        self.characters = {k.lower(): v for k, v in (data.get("characters") or {}).items()}
        self.names = list((data.get("characters") or {}).keys())
        self.pools = data["pools"]
        self.assigned_file = assigned_file
        self.assigned = {}
        if assigned_file.exists():
            self.assigned = {k.lower(): v for k, v in (yaml.safe_load(assigned_file.read_text()) or {}).items()}

    def voice_for(self, speaker: str, speaker_type: str, kind: str) -> dict:
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
            + yaml.safe_dump(self.assigned, sort_keys=True)
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


def make_tts(engine: str):
    return KokoroTTS() if engine == "kokoro" else FakeTTS()


def speak_line(tts, cast: Cast, line: dict) -> np.ndarray:
    v = cast.voice_for(line["speaker"], line.get("speaker_type", "unknown"), line["kind"])
    delivery = line.get("delivery", "normal")
    speed = float(v.get("speed", 1.0)) * DELIVERY_SPEED.get(delivery, 1.0)
    audio = tts.say(line["text"], v["voice"], speed)
    fx = v.get("fx") or ("radio" if delivery == "radio" else None)
    if fx:
        audio = apply_fx(audio, fx)
    return np.clip(audio * DELIVERY_GAIN.get(delivery, 1.0), -1, 1)

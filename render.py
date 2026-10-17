"""Turn page scripts + voices into an episode MP4: panel-by-panel, with a slow zoom."""
import io
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from voices import SR, speak_line

LINE_GAP = 0.30      # silence between balloons
PANEL_LEAD = 0.35    # before the first balloon in a panel
PANEL_TAIL = 0.55    # after the last balloon
SILENT_PANEL = 2.2   # panels with no text
PAGE_INTRO = 1.6     # full-page look before its panels


def _font(size):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
              "C:/Windows/Fonts/arialbd.ttf"):
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def write_wav(path: Path, audio: np.ndarray):
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def silence(sec):
    return np.zeros(int(SR * sec), np.float32)


class Renderer:
    def __init__(self, cfg, tts, cast):
        self.W, self.H = cfg["width"], cfg["height"]
        self.fps = cfg["fps"]
        self.motion = cfg.get("motion", True)
        self.read_sfx = cfg.get("read_sfx", False)
        self.tts, self.cast = tts, cast
        # Render frames at 2x when zooming, so the zoom is smooth rather than jittery.
        self.scale = 2 if self.motion else 1

    # ---------- pictures ----------
    def _canvas(self, page: Image.Image) -> Image.Image:
        W, H = self.W * self.scale, self.H * self.scale
        bg = page.copy()
        r = max(W / bg.width, H / bg.height)
        bg = bg.resize((int(bg.width * r) + 1, int(bg.height * r) + 1), Image.BILINEAR)
        left, top = (bg.width - W) // 2, (bg.height - H) // 2
        bg = bg.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(28 * self.scale))
        return Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.65)

    def _fit(self, canvas: Image.Image, img: Image.Image, margin=0.05) -> Image.Image:
        W, H = canvas.size
        r = min(W * (1 - 2 * margin) / img.width, H * (1 - 2 * margin) / img.height)
        img = img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))), Image.LANCZOS)
        out = canvas.copy()
        out.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
        return out

    def page_frame(self, page):
        return self._fit(self._canvas(page), page, margin=0.03)

    def panel_frame(self, page, box):
        ymin, xmin, ymax, xmax = [max(0, min(1000, v)) for v in box]
        if ymax - ymin < 20 or xmax - xmin < 20:  # bad box from the model: show the whole page
            return self.page_frame(page)
        pad = 12
        crop = page.crop((
            int(max(0, xmin - pad) / 1000 * page.width), int(max(0, ymin - pad) / 1000 * page.height),
            int(min(1000, xmax + pad) / 1000 * page.width), int(min(1000, ymax + pad) / 1000 * page.height),
        ))
        return self._fit(self._canvas(page), crop)

    def title_frame(self, strip, arc, part_label):
        W, H = self.W * self.scale, self.H * self.scale
        im = Image.new("RGB", (W, H), (12, 12, 16))
        d = ImageDraw.Draw(im)
        for text, size, y, colour in (
            (strip.upper(), 110, 0.36, (230, 40, 40)),
            (arc, 70, 0.52, (240, 240, 240)),
            (part_label, 54, 0.64, (180, 180, 180)),
        ):
            f = _font(size * self.scale)
            w = d.textlength(text, font=f)
            d.text(((W - w) / 2, H * y), text, font=f, fill=colour)
        return im

    # ---------- sound ----------
    def panel_audio(self, lines):
        parts = []
        for ln in lines:
            if ln["kind"] == "sfx" and not self.read_sfx:
                continue
            if not ln["text"].strip():
                continue
            if parts:
                parts.append(silence(LINE_GAP))
            parts.append(speak_line(self.tts, self.cast, ln))
        if not parts:
            return silence(SILENT_PANEL)
        return np.concatenate([silence(PANEL_LEAD), *parts, silence(PANEL_TAIL)])

    # ---------- video ----------
    def segment(self, frame: Image.Image, audio: np.ndarray, out: Path, zoom_in=True):
        img, wav = out.with_suffix(".png"), out.with_suffix(".wav")
        frame.save(img)
        write_wav(wav, audio)
        dur = len(audio) / SR
        frames = max(1, round(dur * self.fps))
        dur = frames / self.fps
        if self.motion:
            z = f"1+0.05*on/{frames}" if zoom_in else f"1.05-0.05*on/{frames}"
            vf = (f"[0:v]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                  f":d={frames}:s={self.W}x{self.H}:fps={self.fps},format=yuv420p[v]")
            vin = ["-i", str(img)]
        else:
            vf = f"[0:v]scale={self.W}:{self.H},format=yuv420p[v]"
            vin = ["-loop", "1", "-framerate", str(self.fps), "-i", str(img)]
        af = f"[1:a]apad,atrim=0:{dur:.3f},aresample=48000,aformat=channel_layouts=stereo[a]"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *vin, "-i", str(wav),
               "-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(self.fps),
               "-c:a", "aac", "-b:a", "160k", "-t", f"{dur:.3f}", str(out)]
        subprocess.run(cmd, check=True)
        img.unlink()
        wav.unlink()

    def episode(self, pages, scripts, strip, arc, part_label, title_line, seg_dir: Path, out_file: Path):
        """pages: list of PIL images; scripts: list of page-script dicts (same order)."""
        seg_dir.mkdir(parents=True, exist_ok=True)
        segs = []

        def add(frame, audio, zoom_in=True):
            p = seg_dir / f"seg_{len(segs):04d}.mp4"
            self.segment(frame, audio, p, zoom_in)
            segs.append(p)

        intro = speak_line(self.tts, self.cast, {"kind": "caption", "speaker": "Narrator", "text": title_line})
        add(self.title_frame(strip, arc, part_label), np.concatenate([silence(0.6), intro, silence(1.0)]))

        n = 0
        for page, script in zip(pages, scripts):
            add(self.page_frame(page), silence(PAGE_INTRO), zoom_in=False)
            for panel in script["panels"]:
                n += 1
                print(f"      panel {n}", end="\r", flush=True)
                add(self.panel_frame(page, panel["box_2d"]), self.panel_audio(panel["lines"]), zoom_in=n % 2 == 1)
        print()

        lst = seg_dir / "list.txt"
        lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in segs), encoding="utf-8")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out_file)], check=True)
        for p in segs:
            p.unlink()
        lst.unlink()


def load_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")

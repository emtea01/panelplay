"""Turn page scripts + voices into an episode MP4: panel-by-panel, with a slow zoom."""
import io
import shutil
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
PUNCH = 0.4          # snap-zoom into each panel (from the whole page for a page's first panel)
PUNCH_CONTEXT = 2.2  # later panels punch in from a view this many times the panel's size


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
        self.page_intro = cfg.get("page_intro", False)  # show each full page before its panels
        self.punch = cfg.get("punch", True) and self.motion
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

    @staticmethod
    def panel_rect(page, box):
        """Panel box (0-1000, [ymin, xmin, ymax, xmax]) -> padded pixel rect, or None if the box is unusable."""
        ymin, xmin, ymax, xmax = [max(0, min(1000, v)) for v in box]
        if ymax - ymin < 20 or xmax - xmin < 20:
            return None
        pad = 12
        return (int(max(0, xmin - pad) / 1000 * page.width), int(max(0, ymin - pad) / 1000 * page.height),
                int(min(1000, xmax + pad) / 1000 * page.width), int(min(1000, ymax + pad) / 1000 * page.height))

    def panel_frame(self, page, box, canvas=None):
        rect = self.panel_rect(page, box)
        if rect is None:  # bad box from the model: show the whole page
            return self.page_frame(page)
        return self._fit(canvas or self._canvas(page), page.crop(rect))

    def punch_frames(self, page, canvas, start, end):
        """Camera move from rect start to rect end (pixel rects on the page), easing out, at output size."""
        bg = canvas.resize((self.W, self.H), Image.BILINEAR)
        n = max(2, round(PUNCH * self.fps))
        frames = []
        for i in range(1, n + 1):
            e = 1 - (1 - i / n) ** 3
            rect = tuple(int(a + (b - a) * e) for a, b in zip(start, end))
            frames.append(self._fit(bg, page.crop(rect)))
        return frames

    @staticmethod
    def context_rect(page, rect):
        """A wider view centred on rect, kept inside the page."""
        l, t, r, b = rect
        w = min(page.width, (r - l) * PUNCH_CONTEXT)
        h = min(page.height, (b - t) * PUNCH_CONTEXT)
        cx = min(max((l + r) / 2, w / 2), page.width - w / 2)
        cy = min(max((t + b) / 2, h / 2), page.height - h / 2)
        return (int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2))

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
    def panel_audio(self, lines, lead=PANEL_LEAD):
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
        return np.concatenate([silence(lead), *parts, silence(PANEL_TAIL)])

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

    def moving_segment(self, frames: list, out: Path):
        """Silent segment from pre-rendered frames, encoded to match the still segments so concat can copy."""
        fdir = out.with_suffix("")
        fdir.mkdir(exist_ok=True)
        for i, f in enumerate(frames):
            f.save(fdir / f"{i:03d}.png")
        dur = len(frames) / self.fps
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(self.fps), "-i", str(fdir / "%03d.png"),
               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-map", "0:v", "-map", "1:a", "-vf", "format=yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(self.fps),
               "-c:a", "aac", "-b:a", "160k", "-t", f"{dur:.3f}", str(out)]
        subprocess.run(cmd, check=True)
        shutil.rmtree(fdir)

    def episode(self, pages, scripts, strip, arc, part_label, title_line, seg_dir: Path, out_file: Path,
                progress=None):
        """pages: list of PIL images; scripts: list of page-script dicts (same order).
        progress: optional callback(panel_number) as each panel is rendered."""
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
            if self.page_intro:
                add(self.page_frame(page), silence(PAGE_INTRO), zoom_in=False)
            canvas = self._canvas(page)
            full = (0, 0, page.width, page.height)
            for i, panel in enumerate(script["panels"]):
                n += 1
                print(f"      panel {n}", end="\r", flush=True)
                if progress:
                    progress(n)
                rect = self.panel_rect(page, panel["box_2d"])
                if self.punch and rect:
                    # Snap in (from the whole page on a page's first panel), then keep pushing in slowly.
                    start = full if i == 0 and not self.page_intro else self.context_rect(page, rect)
                    p = seg_dir / f"seg_{len(segs):04d}.mp4"
                    self.moving_segment(self.punch_frames(page, canvas, start, rect), p)
                    segs.append(p)
                    add(self.panel_frame(page, panel["box_2d"], canvas), self.panel_audio(panel["lines"], lead=0.1))
                else:
                    add(self.panel_frame(page, panel["box_2d"], canvas), self.panel_audio(panel["lines"]),
                        zoom_in=n % 2 == 1)
        print()

        lst = seg_dir / "list.txt"
        lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in segs), encoding="utf-8")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        # Build locally, then move: +faststart rewrites the file in place, which network shares can refuse.
        tmp = seg_dir / "episode.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(tmp)], check=True)
        shutil.move(str(tmp), str(out_file))
        for p in segs:
            p.unlink()
        lst.unlink()


def load_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")

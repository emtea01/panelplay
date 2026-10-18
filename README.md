# narrate2000ad — proof of concept

Turns a 2000 AD story arc from your Komga archive into narrated, character-voiced episodes in Plex.
One prog = one episode; one arc = one season.

```
Komga / CBZ ──► Gemini Flash (free tier) ──► page scripts (JSON) ──► Kokoro voices (CPU) ──► ffmpeg ──► Plex
                 1. which pages are the strip   you can review/edit      cast.yaml              panel-by-panel
                 2. panels + who says what                                                     slow zoom
```

No GPU needed. Gemini does the reading in the cloud on the free tier; Kokoro and ffmpeg run on CPU.

## 1. Container

A Debian 12 LXC with 4 cores and 4–6 GB RAM is plenty. Bind-mount:
- the Plex library folder you want episodes written to (read-write)
- optionally the 2000 AD CBZ folder (read-only) if you'd rather skip Komga's API

```bash
apt update && apt install -y python3-venv ffmpeg espeak-ng fonts-dejavu-core
cd /opt && # copy this folder here
cd narrate2000ad
python3 -m venv venv && . venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU-only torch, ~200 MB instead of ~2 GB
pip install -r requirements.txt
cp config.example.yaml config.yaml   # then edit it
```

The first real run downloads the Kokoro model (~330 MB) from Hugging Face once.

## 2. Config

- **Komga**: URL, an API key (or user/password), and the series name that holds the progs.
  Progs are matched by Komga's issue number, falling back to the file name.
- **Gemini**: get a free key at aistudio.google.com and put it in `config.yaml` or `GEMINI_API_KEY`.
  Model names are `*-latest` aliases; if Google retires one, swap in a current Flash model name.
- **paths.plex**: a folder inside a Plex **TV Shows** library.

In Plex, set that library's agent to the personal-media option so it doesn't try to match online TV
metadata, and turn on local media assets so it uses the cover thumbnails written next to each episode.

## 3. Test the render first (no API calls, no model download)

```bash
python narrate.py --strip "Judge Dredd" --arc "The Robot Wars" --progs 10-17 --only 10 --stage tag
python narrate.py --strip "Judge Dredd" --arc "The Robot Wars" --progs 10-17 --only 10 --stage read
python narrate.py --strip "Judge Dredd" --arc "The Robot Wars" --progs 10-17 --only 10 --tts fake
```

The last command renders with beeps instead of voices, so you can check timing and framing in Plex.
Then run it again without `--tts fake` for real voices. Check the prog range against the wiki before a full arc.

## 4. The full workflow

```bash
# Everything for the arc (cached, so re-running only does what's missing):
python narrate.py --strip "Judge Dredd" --arc "The Robot Wars" --progs 10-17 --season 1
```

Per prog, `work/<strip>--<arc>/prog_NNNN/` holds:

| file | what it is | edit it to… |
|---|---|---|
| `tag.json` | which pages are the strip | fix wrong pages (delete it to re-ask Gemini) |
| `page_NNN.json` | panels, boxes, every line with speaker and delivery | fix a misread line or a wrong speaker |
| `transcript.txt` | plain script | feed synopses for the portal later |

Delete an episode's MP4 and run with `--stage render` to re-render after edits (no Gemini calls).

**Free-tier budget**: about 1 call per prog to find the pages plus 1 per strip page, so an 8-part
Dredd arc is roughly 50 calls. If you hit the daily cap it stops cleanly; run it again tomorrow.

## 5. Voices

`cast.yaml` maps characters to Kokoro voices, speed and effects (`robot` ring-modulation, `radio`
band-pass). Anyone not listed gets a voice from a pool, picked consistently by name, and saved to
`work/cast_assigned.yaml`. Move recurring characters into `cast.yaml` as you meet them.
Delivery tags from Gemini (shout/whisper/radio) nudge speed and volume.

## Known limits of the POC

- Panel boxes come from Gemini; odd layouts (inset panels, splash pages) may crop oddly.
  A bad box falls back to showing the whole page.
- Kokoro is clear but not very emotional. If the voices are the weak spot, the TTS layer is
  one small class in `voices.py` to swap.
- Sound effects are skipped by default (`render.read_sfx`).
- Pages are sent to Google's free tier, which may use them to improve its models.

## The portal

A small web app (FastAPI + htmx, no build step) for picking stories and rendering them:

```bash
pip install -r requirements.txt
python -m portal            # http://127.0.0.1:8077   (PANELPLAY_HOST=0.0.0.0 to reach it from the LAN)
```

- **Story arcs**: the catalogue, imported from the fan-compiled 2000 AD story indexes on Wikimedia Commons
  (every series, plus a detailed Judge Dredd index with credits). Filter by strip, search, show only stories
  your archive holds, and render or re-render. Stories can also be added or edited by hand.
- **Renders**: the queue (one render at a time) with live progress, logs and cancel; every render is recorded.
- **Settings**: comics folder or Komga, the Plex output folder, the working folder, API keys (Gemini,
  ElevenLabs, Comic Vine) with a Test button for each, the voice cast, and picture options.

Re-rendering reuses what Gemini read and every voice line already made (cached by voice and text), so it only
spends credits on lines that change, e.g. after re-casting a character. "Re-read" asks Gemini to read the pages again.

State lives in `data/` (`PANELPLAY_DATA` to move it): `panelplay.db` (settings, catalogue, render history),
cached index PDFs, and per-render configs. On first start the portal borrows `config.yaml` and the
`GEMINI_API_KEY` / `ELEVENLABS_API_KEY` environment variables if present. API keys are stored in plain text in
the database, and the portal has no login, so keep it on your LAN.

Plex seasons are years of publication (1977 is season 1), holding every episode of the strip that year in prog
order, one-offs and multi-part stories together: Robot Wars part 1 (prog 10) is Judge Dredd s01e09. Set a season
by hand on a story's page to give that story a season of its own.

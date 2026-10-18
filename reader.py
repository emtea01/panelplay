"""Gemini (free tier) reads the pages: which pages belong to a strip, then the script of each page."""
import json
import os
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field


class StripPages(BaseModel):
    pages: list[int] = Field(description="1-based page numbers belonging to the strip, in order")
    episode_title: Optional[str] = Field(None, description="Episode or chapter title printed on the strip, if any")
    part: Optional[int] = Field(None, description="Part number if printed (e.g. 'Part 3' -> 3)")


class Line(BaseModel):
    kind: Literal["caption", "speech", "thought", "sfx"]
    speaker: str = Field(description="Exact character name, 'Narrator' for captions, or a short label for unnamed people")
    speaker_type: Literal["male", "female", "robot", "unknown"]
    delivery: Literal["normal", "shout", "whisper", "radio"] = "normal"
    text: str


class Panel(BaseModel):
    box_2d: list[int] = Field(description="[ymin, xmin, ymax, xmax] of the panel, normalised 0-1000")
    lines: list[Line]


class PageScript(BaseModel):
    panels: list[Panel]


class Story(BaseModel):
    strip: str = Field(description="The strip's name, as its title logo shows it (e.g. 'Judge Dredd')")
    title: Optional[str] = Field(None, description="This story's own title, if one is printed (e.g. 'The Cursed Earth')")
    part: Optional[int] = Field(None, description="Part or episode number if printed (e.g. 'Part 3' -> 3)")
    pages: list[int] = Field(description="1-based page numbers of this story, in order")


class IssueContents(BaseModel):
    stories: list[Story]


CONTENTS_PROMPT = """These {n} images are every page of issue {issue} of the comic "{series}", in order.
Page numbers start at 1.
List every comic strip story in the issue, in page order. A story usually starts on a page with its title
logo and runs until the next one starts. Leave out covers, adverts, letters pages, editorials, text-only
stories, puzzles and pin-ups.
For the strip's name, use exactly one of these names if it is one of them: {known}.
Give the story's own title and part number only if they are printed; don't invent them."""

TAG_PROMPT = """These {n} images are every page of one issue of the comic "{series}", in order.
Page numbers start at 1.
Which pages contain the strip "{strip}"{arc_hint}? A strip usually starts on a page with its title logo and runs
until the next strip's title page. Ignore covers, adverts, letters pages and other strips.
If the strip is not in this issue, return an empty list."""

READ_PROMPT = """This is a page of the strip "{strip}", story "{arc}", from the comic "{series}".
Write it out as a script for an audio drama.

- List the panels in reading order (left to right, top to bottom, following the gutters).
- For each panel give its bounding box and every caption, speech balloon, thought balloon and sound effect,
  in the order they should be read.
- Use these exact speaker names when they match: {cast}.
- Captions are spoken by "Narrator". For unnamed characters use a short consistent label such as
  "Robot guard" or "Citizen".
- Copy the text exactly as lettered, but in normal sentence case (not ALL CAPS). Keep stylised speech
  (e.g. a robot that says "wobot"). Join words broken across lines. Drop the strip's title logo and credits.
- delivery: "shout" for bold/jagged balloons or exclamations, "whisper" for small/dotted balloons,
  "radio" for zig-zag tails (radios, screens, phones).
- Panels with no text still get an entry with an empty lines list."""


class Reader:
    def __init__(self, cfg):
        from google import genai  # imported here so render-only runs don't need it

        key = cfg.get("api_key") or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise SystemExit("No Gemini API key: set gemini.api_key in config.yaml or GEMINI_API_KEY")
        self.client = genai.Client(api_key=key)
        self.tag_model = cfg["tag_model"]
        self.read_model = cfg["read_model"]
        self.min_gap = 60.0 / max(1, cfg.get("requests_per_minute", 8))
        self._last = 0.0

    def _call(self, model, parts, schema):
        from google.genai import errors, types

        conf = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
        )
        for attempt in range(6):
            wait = self._last + self.min_gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                resp = self.client.models.generate_content(model=model, contents=parts, config=conf)
                if resp.parsed is not None:
                    return resp.parsed
                return schema.model_validate(json.loads(resp.text))
            except errors.ClientError as e:
                if getattr(e, "code", None) == 429:
                    delay = 30 * (attempt + 1)
                    print(f"    rate limited, waiting {delay}s")
                    time.sleep(delay)
                    continue
                raise
            except errors.ServerError as e:
                print(f"    Gemini server error ({e}), retrying")
                time.sleep(10 * (attempt + 1))
        raise RuntimeError("Gemini kept failing; free-tier daily quota may be used up. Re-run later, cached pages are kept.")

    def tag_pages(self, thumbs: list[bytes], strip: str, arc: str, series: str = "2000 AD") -> StripPages:
        from google.genai import types

        parts = [TAG_PROMPT.format(n=len(thumbs), series=series, strip=strip,
                                   arc_hint=f' (story "{arc}")' if arc else "")]
        for i, t in enumerate(thumbs, 1):
            parts.append(f"Page {i}:")
            parts.append(types.Part.from_bytes(data=t, mime_type="image/jpeg"))
        return self._call(self.tag_model, parts, StripPages)

    def contents(self, thumbs: list[bytes], series: str, issue, known_strips: list[str]) -> IssueContents:
        from google.genai import types

        parts = [CONTENTS_PROMPT.format(n=len(thumbs), issue=issue, series=series,
                                        known=", ".join(known_strips) or "(none yet)")]
        for i, t in enumerate(thumbs, 1):
            parts.append(f"Page {i}:")
            parts.append(types.Part.from_bytes(data=t, mime_type="image/jpeg"))
        return self._call(self.tag_model, parts, IssueContents)

    def read_page(self, image: bytes, mime: str, strip: str, arc: str, cast_names: list[str],
                  series: str = "2000 AD") -> PageScript:
        from google.genai import types

        prompt = READ_PROMPT.format(series=series, strip=strip, arc=arc, cast=", ".join(cast_names) or "(none)")
        parts = [types.Part.from_bytes(data=image, mime_type=mime), prompt]
        return self._call(self.read_model, parts, PageScript)

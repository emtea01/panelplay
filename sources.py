"""Where the comic pages come from: Komga's API, or CBZ files in a folder."""
import io
import re
import zipfile
from pathlib import Path

import requests
from PIL import Image

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _thumb(data: bytes, max_side: int = 400) -> bytes:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((max_side, max_side))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80)
    return out.getvalue()


class KomgaSource:
    def __init__(self, cfg):
        self.url = cfg["url"].rstrip("/")
        self.series_name = cfg["series"]
        self.s = requests.Session()
        if cfg.get("api_key"):
            self.s.headers["X-API-Key"] = cfg["api_key"]
        elif cfg.get("user"):
            self.s.auth = (cfg["user"], cfg.get("password", ""))
        self._books = None

    def _get(self, path, **params):
        r = self.s.get(f"{self.url}{path}", params=params, timeout=120)
        r.raise_for_status()
        return r

    def _load(self):
        if self._books is not None:
            return
        series = self._get("/api/v1/series", search=self.series_name, size=50).json()["content"]
        exact = [s for s in series if s["name"].lower() == self.series_name.lower()]
        if not (exact or series):
            raise SystemExit(f"Komga series '{self.series_name}' not found")
        sid = (exact or series)[0]["id"]
        books = self._get(f"/api/v1/series/{sid}/books", unpaged="true").json()["content"]
        self._books = {}
        for b in books:
            n = _prog_number(b.get("metadata", {}).get("number"), b.get("name", ""))
            if n is not None:
                self._books.setdefault(n, b)

    def prog(self, n):
        self._load()
        if n not in self._books:
            raise SystemExit(f"Prog {n} not found in Komga series '{self.series_name}'")
        return self._books[n]

    def page_count(self, book):
        return book["media"]["pagesCount"]

    def page(self, book, i):
        """Full page image (1-based). Returns (bytes, mime)."""
        r = self._get(f"/api/v1/books/{book['id']}/pages/{i}")
        return r.content, r.headers.get("Content-Type", "image/jpeg").split(";")[0]

    def thumbnail(self, book, i):
        return self._get(f"/api/v1/books/{book['id']}/pages/{i}/thumbnail").content

    def cover(self, book):
        return self._get(f"/api/v1/books/{book['id']}/thumbnail").content


class FolderSource:
    def __init__(self, cfg):
        self.root = Path(cfg["path"])
        rx = cfg.get("prog_regex")
        self.rx = re.compile(rx) if rx else None
        self._files = None

    def _load(self):
        if self._files is not None:
            return
        self._files = {}
        for f in sorted(self.root.rglob("*.cb[zZ]")):
            if self.rx:
                m = self.rx.search(f.stem)
                n = int(m.group(1)) if m else None
            else:
                n = _prog_number(None, f.stem)
            if n is not None:
                self._files.setdefault(n, f)

    def prog(self, n):
        self._load()
        if n not in self._files:
            raise SystemExit(f"Prog {n} not found under {self.root}")
        path = self._files[n]
        with zipfile.ZipFile(path) as z:
            names = sorted(
                (x for x in z.namelist() if x.lower().endswith(IMAGE_EXT) and not x.startswith("__MACOSX")),
                key=_natural,
            )
        return {"path": path, "pages": names}

    def page_count(self, book):
        return len(book["pages"])

    def page(self, book, i):
        name = book["pages"][i - 1]
        with zipfile.ZipFile(book["path"]) as z:
            data = z.read(name)
        ext = name.rsplit(".", 1)[-1].lower()
        return data, {"jpg": "image/jpeg"}.get(ext, f"image/{ext}")

    def thumbnail(self, book, i):
        return _thumb(self.page(book, i)[0])

    def cover(self, book):
        return self.thumbnail(book, 1)


def _natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _prog_number(number, name):
    """Prog number from Komga's metadata number, else from the file name.
    Skips the '2000' in '2000AD Prog 0123' and prefers a number right after 'prog'."""
    for candidate in (number, name):
        if not candidate:
            continue
        s = str(candidate)
        m = re.search(r"(?i)prog\D{0,3}(\d{1,4})", s)
        if m:
            return int(m.group(1))
        s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s)  # drop "(1985)", "[digital]" etc.
        nums = [int(x) for x in re.findall(r"\d{1,4}", s)]
        if len(nums) > 1:
            nums = [x for x in nums if x != 2000] or nums           # the "2000" of 2000 AD
        if len(nums) > 1:
            nums = [x for x in nums if not 1977 <= x <= 1999] or nums  # publication years
        if nums:
            return nums[0]
    return None


def make_source(cfg):
    kind = cfg.get("source", "komga")
    return KomgaSource(cfg["komga"]) if kind == "komga" else FolderSource(cfg["folder"])

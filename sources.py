"""Where the comic pages come from: Komga's API, or CBZ files in a folder."""
import io
import os
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
        self.series_name = cfg.get("series", "")
        self.series_id = cfg.get("series_id", "")
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
        sid = self.series_id
        if not sid:
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

    def years(self):
        """Publication year of each prog: Komga's release date, else a "(1977)" in the name."""
        self._load()
        return {n: _year(b.get("metadata", {}).get("releaseDate") or "", b.get("name", ""))
                for n, b in self._books.items()}

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


COMIC_EXT = (".cbz", ".cbr", ".cb7", ".zip", ".rar")


def comic_files(folder: Path) -> dict:
    """{issue number: file} for the comics directly inside a series folder (subfolders are other series).
    A lone file without a number (a one-off special) counts as issue 1."""
    files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in COMIC_EXT)
    out = {}
    for f in files:
        n = _prog_number(None, f.stem)
        if n is None and len(files) == 1:
            n = 1
        if n is not None:
            out.setdefault(n, f)
    return out


class Comic:
    """The page images of one comic file. CBZ is read in place; CBR/CB7 are unpacked once with bsdtar
    (libarchive, which reads RAR and 7z) into a cache folder."""

    def __init__(self, path: Path, cache_dir: Path | None = None):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            self.zip = f.read(4) == b"PK"
        if self.zip:
            with zipfile.ZipFile(self.path) as z:
                names = z.namelist()
        else:
            self.dir = _unpack(self.path, cache_dir)
            names = [x.relative_to(self.dir).as_posix() for x in self.dir.rglob("*") if x.is_file()]
        self.pages = sorted((x for x in names if x.lower().endswith(IMAGE_EXT) and not x.startswith("__MACOSX")),
                            key=_natural)

    def read(self, name: str) -> bytes:
        if self.zip:
            with zipfile.ZipFile(self.path) as z:
                return z.read(name)
        return (self.dir / name).read_bytes()


def _unpack(path: Path, cache_dir: Path | None) -> Path:
    import hashlib
    import shutil
    import subprocess
    import tempfile

    st = path.stat()
    key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:16]
    out = Path(cache_dir or Path(tempfile.gettempdir()) / "panelplay-unpacked") / key
    if not (out / ".done").exists():
        tool = shutil.which("bsdtar")
        if not tool and os.name == "nt":  # Windows' own tar is bsdtar (not Git's GNU tar, which may come first on PATH)
            tool = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "tar.exe")
        if not tool or not Path(tool).exists():
            raise SystemExit(f"Can't open {path.name}: install bsdtar (Debian: apt install libarchive-tools)")
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        subprocess.run([tool, "-xf", str(path), "-C", str(out)], check=True, capture_output=True)
        (out / ".done").touch()
    return out


class FolderSource:
    """One series folder of CBZ/CBR files, numbered from their file names."""

    def __init__(self, cfg):
        self.root = Path(cfg["path"])
        rx = cfg.get("prog_regex")
        self.rx = re.compile(rx) if rx else None
        self.cache_dir = cfg.get("unpack_dir")
        self._files = None

    def _load(self):
        if self._files is not None:
            return
        if self.rx:
            self._files = {}
            for f in sorted(self.root.iterdir()):
                m = self.rx.search(f.stem)
                if m and f.suffix.lower() in COMIC_EXT:
                    self._files.setdefault(int(m.group(1)), f)
        else:
            self._files = comic_files(self.root)

    def years(self):
        """Publication year of each issue, from a "(1977)" in its file name."""
        self._load()
        return {n: _year(f.stem) for n, f in self._files.items()}

    def prog(self, n):
        self._load()
        if n not in self._files:
            raise SystemExit(f"Issue {n} not found under {self.root}")
        return Comic(self._files[n], self.cache_dir)

    def page_count(self, book):
        return len(book.pages)

    def page(self, book, i):
        name = book.pages[i - 1]
        ext = name.rsplit(".", 1)[-1].lower()
        return book.read(name), {"jpg": "image/jpeg"}.get(ext, f"image/{ext}")

    def thumbnail(self, book, i):
        return _thumb(self.page(book, i)[0])

    def cover(self, book):
        return self.thumbnail(book, 1)


def _natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _year(*candidates):
    for c in candidates:
        m = re.search(r"(?:^|\()((?:19|20)\d\d)(?:\)|-)", c or "")
        if m:
            return int(m.group(1))
    return None


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

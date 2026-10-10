"""Prebuilt emulator images / emulator program from Artifactory (or any HTTP server / local dir).

A source is a folder URL plus a file pattern, e.g.
    url: https://artifactory.example.com/artifactory/aaos-images/tangorpro/latest/
    pattern: "RELEASE_*.zip"
The newest matching file (natural sort of the name, so RELEASE_0010 > RELEASE_0009) is
downloaded once into the cache on the runner volume and reused by later jobs; older releases are
pruned. Every use checks the cached archive against the SHA-256 the server publishes (Artifactory
sends X-Checksum-Sha256; a sibling <file>.sha256 also works) and unpacks it into a directory private
to the job: the shared cache is writable by other merge requests' build code. Without a published
checksum the release is refused (`emulator.require_checksum: false` accepts it).

Folder listing: Artifactory's storage API (`/api/storage/<repo>/<path>`) when the URL looks like
Artifactory, else the HTML index. A url naming a file (no trailing slash) is used as is.
Authentication (optional): APPCI_ARTIFACTORY_TOKEN (Bearer), or APPCI_ARTIFACTORY_USER +
APPCI_ARTIFACTORY_PASSWORD (basic); APPCI_ARTIFACTORY_HEADER overrides the header name for
tokens (e.g. X-JFrog-Art-Api). Credentials are not passed on when the server redirects to another
host or from https to http (e.g. Artifactory redirect downloads to S3).
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import shutil
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import util


def _headers() -> Dict[str, str]:
    tok = util.secret("APPCI_ARTIFACTORY_TOKEN")
    if tok:
        name = os.environ.get("APPCI_ARTIFACTORY_HEADER", "Authorization")
        return {name: f"Bearer {tok}" if name == "Authorization" else tok}
    user, pw = util.secret("APPCI_ARTIFACTORY_USER"), util.secret("APPCI_ARTIFACTORY_PASSWORD")
    if user and pw:
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
    return {}


class _AuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, re-sending credentials only to the same scheme and host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old, nxt = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
            if (old.scheme, old.netloc) == (nxt.scheme, nxt.netloc):
                for k, v in req.unredirected_hdrs.items():
                    new.add_unredirected_header(k, v)
        return new


def _open(cfg, url: str, timeout: float = 600, method: str = "GET"):
    from .tools import _ssl_context
    req = urllib.request.Request(url, method=method)
    for k, v in _headers().items():
        req.add_unredirected_header(k, v)          # never copied to a redirect by urllib itself
    ctx = _ssl_context(cfg) if cfg else None
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _AuthRedirect())
    return opener.open(req, timeout=timeout)


def _natural(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_folder(cfg, url: str) -> List[str]:
    """File names in a folder URL (Artifactory storage API, HTML index, or a local directory)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("", "file"):
        return sorted(p.name for p in Path(urllib.parse.unquote(parsed.path)).iterdir() if p.is_file())
    m = re.match(r"^(https?://[^/]+(?:/[^/]+)*?/artifactory)/(?!api/)(.+?)/?$", url)
    if m:
        try:
            with _open(cfg, f"{m.group(1)}/api/storage/{m.group(2)}", timeout=60) as r:
                data = json.loads(r.read().decode())
            return sorted(c["uri"].lstrip("/") for c in data.get("children", []) if not c.get("folder"))
        except Exception:  # not the storage API after all: fall back to the HTML index
            pass
    with _open(cfg, url, timeout=60) as r:
        page = r.read().decode(errors="replace")
    names = {urllib.parse.unquote(h.rstrip("/").split("/")[-1]) for h in re.findall(r'href="([^"?#]+)"', page)}
    return sorted(n for n in names if n and not n.endswith("/") and "." in n)


def resolve(cfg, url: str, pattern: Optional[str]) -> Tuple[str, str]:
    """(file URL, file name) of the newest file matching pattern in a folder URL."""
    if not url.endswith("/"):
        return url, urllib.parse.unquote(url.rstrip("/").split("/")[-1])
    names = [n for n in list_folder(cfg, url) if fnmatch.fnmatch(n, pattern or "*.zip")]
    if not names:
        raise RuntimeError(f"no file matching {pattern!r} in {url}")
    name = max(names, key=_natural)
    return urllib.parse.urljoin(url, urllib.parse.quote(name)), name


def published_checksum(cfg, url: str) -> Optional[str]:
    """SHA-256 the server publishes for a file: Artifactory's X-Checksum-Sha256, else <file>.sha256."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("", "file"):
        side = Path(urllib.parse.unquote(parsed.path) + ".sha256")
        return side.read_text().split()[0].lower() if side.is_file() else None
    for method in ("HEAD", "GET"):
        try:
            with _open(cfg, url, timeout=60, method=method) as r:
                value = r.headers.get("X-Checksum-Sha256")
            if value:
                return value.strip().lower()
            break
        except Exception:  # HEAD not allowed: retry with GET (only the headers are read)
            continue
    try:
        with _open(cfg, url + ".sha256", timeout=60) as r:
            return r.read().decode().split()[0].lower()
    except Exception:
        return None


def _download(cfg, url: str, dest: Path, expected: Optional[str]) -> None:
    tmp = dest.with_name(dest.name + f".part{os.getpid()}")
    h = hashlib.sha256()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("", "file"):
        shutil.copyfile(Path(urllib.parse.unquote(parsed.path)), tmp)
        digest = util.sha256_file(tmp)
    else:
        with _open(cfg, url) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
        digest = h.hexdigest()
    if expected and expected != digest:
        tmp.unlink()
        raise RuntimeError(f"checksum mismatch for {url}: got {digest}, expected {expected}")
    util.info(f"downloaded {dest.name} ({tmp.stat().st_size // (1 << 20)} MB, sha256 "
              f"{'verified' if expected else 'NOT verified (no checksum published)'})")
    tmp.replace(dest)


def fetch(cfg, kind: str, url: str, pattern: Optional[str], cache: Path, keep: int = 1,
          require_checksum: Optional[bool] = None) -> Path:
    """Newest matching release, verified and unpacked into a private directory named after it.
    The archive stays in cache/<kind>/ for later jobs, which verify it again before use."""
    from .tools import private_dir
    if require_checksum is None:
        require_checksum = bool(((cfg.get("emulator") if cfg else None) or {}).get("require_checksum", True))
    file_url, name = resolve(cfg, url, pattern)
    expected = published_checksum(cfg, file_url)
    if not expected and require_checksum:
        raise RuntimeError(f"{file_url}: the server publishes no SHA-256 (X-Checksum-Sha256 or {name}.sha256); "
                           "refusing an unverified image (emulator.require_checksum: false accepts it)")
    base = cache / kind
    base.mkdir(parents=True, exist_ok=True)
    archive = base / name
    with util.file_lock(base / ".lock", f"{kind} download"):
        if archive.exists() and expected and util.sha256_file(archive) != expected:
            util.warn(f"{kind}: cached {name} does not match the published SHA-256 (modified?); downloading again")
            archive.unlink()
        if archive.exists():
            util.info(f"{kind}: {name} (cached, sha256 {'verified' if expected else 'not published'})")
        else:
            util.info(f"{kind}: downloading {name} from {file_url.split('?')[0]}")
            _download(cfg, file_url, archive, expected)
        old = sorted((p for p in base.iterdir() if p != archive and not p.name.startswith(".")),
                     key=lambda p: p.stat().st_mtime)
        for p in old[:max(0, len(old) - keep)]:     # older releases, and unpacked dirs of earlier appci
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
    dest = private_dir(f"{kind}") / re.sub(r"\.zip$", "", name)
    dest.mkdir()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                extracted = Path(z.extract(info, dest))
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    extracted.chmod(mode)
    else:
        shutil.copy2(archive, dest / name)
    return dest


def source(cfg, kind: str) -> Tuple[Optional[str], Optional[str]]:
    """(url, pattern) of 'image' or 'program' from config `emulator.<kind>` or CI variables."""
    sec = (cfg.get("emulator") or {}).get(kind) or {}
    up = kind.upper()
    return (os.environ.get(f"APPCI_EMULATOR_{up}_URL") or sec.get("url"),
            os.environ.get(f"APPCI_EMULATOR_{up}_PATTERN") or sec.get("pattern"))


def cache_root(cfg) -> Path:
    from .tools import cache_dir
    plat = cfg.get("platform") or os.environ.get("APPCI_PLATFORM") or "default"
    return Path(os.environ.get("APPCI_EMULATOR_CACHE") or cache_dir().parent / "emulator") / plat


def image_dir(unpacked: Path) -> Path:
    """Directory holding the system images inside an unpacked release (zips often nest one level)."""
    for marker in ("system.img", "system-qemu.img"):
        hits = sorted(unpacked.rglob(marker), key=lambda p: len(p.parts))
        if hits:
            return hits[0].parent
    raise RuntimeError(f"no system.img in {unpacked}")


def emulator_binary(unpacked: Path) -> Path:
    hits = sorted((p for p in unpacked.rglob("emulator") if p.is_file() and os.access(p, os.X_OK)),
                  key=lambda p: len(p.parts))
    if not hits:
        raise RuntimeError(f"no executable 'emulator' in {unpacked}")
    return hits[0]

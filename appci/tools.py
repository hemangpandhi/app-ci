"""Third-party tools that are not in the AOSP tree, pinned by version and SHA-256.

Downloads go to $APPCI_TOOLS_CACHE (default ~/.cache/appci/tools). For air-gapped
runners pre-seed that directory, or point the URLs at an internal mirror via
appci.yaml (`tools: {detekt: {url: ...}}`).
"""
from __future__ import annotations

import atexit
import os
import shutil
import ssl
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict

from . import util
from .config import Config, deep_merge

TOOLS: Dict[str, Dict[str, str]] = {
    "detekt": {
        "version": "1.23.8",
        "url": "https://github.com/detekt/detekt/releases/download/v1.23.8/detekt-cli-1.23.8-all.jar",
        "sha256": "2ce2ff952e150baf28a29cda70a363b0340b3e81a55f43e51ec5edffc3d066c1",
        "file": "detekt-cli-all.jar",
    },
    "plantuml": {
        "version": "1.2025.4",
        "url": "https://repo1.maven.org/maven2/net/sourceforge/plantuml/plantuml-mit/1.2025.4/plantuml-mit-1.2025.4.jar",
        "sha256": "02f81239df0be554fcbf49e175f4b8b0e92334062268d4e41dba70cedc172bdd",
        "file": "plantuml.jar",
    },
    # Official HTML renderings of XML reports (applied with the JDK's XSLT, appci/java/Xslt.java).
    "checkstyle-xsl": {
        "version": "83fe7ba",
        "url": "https://raw.githubusercontent.com/checkstyle/contribution/"
               "83fe7baca8d349abc938a0ae1226dfa74f78e9f3/xsl/checkstyle-noframes-severity-sorted.xsl",
        "sha256": "b0d9b1302e2a117216f8b553e2026e107d01d0889bac011e6924f8ad4d77be9c",
        "file": "checkstyle-noframes-severity-sorted.xsl",
    },
    "ant-junit": {                     # Ant's junitreport stylesheet (junit-noframes.xsl inside)
        "version": "1.10.15",
        "url": "https://repo1.maven.org/maven2/org/apache/ant/ant-junit/1.10.15/ant-junit-1.10.15.jar",
        "sha256": "9a455eeb4bedc1d8bcd4b8099d4b1f18f1bf1bc977cd6e389a106fe0cf366711",
        "file": "ant-junit.jar",
    },
    # Dokka (Kotlin docs): the CLI plus the plugins it loads (versions as dokka-base 2.0.0's POM).
    "dokka-cli": {
        "version": "2.0.0",
        "url": "https://repo1.maven.org/maven2/org/jetbrains/dokka/dokka-cli/2.0.0/dokka-cli-2.0.0.jar",
        "sha256": "5f70f358160b48ef9763b2ced4e96037f1bfc8d0122de692550ca320ab4b9f20",
        "file": "dokka-cli.jar",
    },
    "dokka-base": {
        "version": "2.0.0",
        "url": "https://repo1.maven.org/maven2/org/jetbrains/dokka/dokka-base/2.0.0/dokka-base-2.0.0.jar",
        "sha256": "0411d1c38a1d100cbd346c6340c3c4b5f154a48a9c119ed11893d6fae94c91aa",
        "file": "dokka-base.jar",
    },
    "dokka-analysis": {
        "version": "2.0.0",
        "url": "https://repo1.maven.org/maven2/org/jetbrains/dokka/analysis-kotlin-symbols/2.0.0/analysis-kotlin-symbols-2.0.0.jar",
        "sha256": "41263fcbd154f0544fdb33079d942e76dcc298960f2c67e0dcedff18937a5d08",
        "file": "analysis-kotlin-symbols.jar",
    },
    "kotlinx-html": {
        "version": "0.9.1",
        "url": "https://repo1.maven.org/maven2/org/jetbrains/kotlinx/kotlinx-html-jvm/0.9.1/kotlinx-html-jvm-0.9.1.jar",
        "sha256": "b68f39cd9ad7b46de0a0e77e7f5908d4e7661f3d0c85d2d9171543fcd5b156fb",
        "file": "kotlinx-html-jvm.jar",
    },
    "freemarker": {
        "version": "2.3.32",
        "url": "https://repo1.maven.org/maven2/org/freemarker/freemarker/2.3.32/freemarker-2.3.32.jar",
        "sha256": "04d65ec1bde6cea20e3495d5e78ef96ab774d9936434861d3254bd88e7e94f92",
        "file": "freemarker.jar",
    },
    "sonar-scanner": {
        "version": "7.2.0.5079",
        "url": "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-7.2.0.5079-linux-x64.zip",
        "sha256": "da9f4e64a3d555f08ce38b5469ebd91fe2b311af473f7001a5ee5c1fd58b004b",
        "file": "sonar-scanner.zip",
        "unzip": "bin/sonar-scanner",
    },
    # Hook interpreter when the runner has no python3 >= 3.9 that can load compiled modules (e.g.
    # Ubuntu 20.04 images: 3.8; the tree's prebuilt py3-cmd cannot load lxml). Standalone CPython.
    "python": {
        "version": "3.12.7+20241016",
        "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/"
               "cpython-3.12.7%2B20241016-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "sha256": "43576f7db1033dd57b900307f09c2e86f371152ac8a2607133afa51cbfc36064",
        "file": "python.tar.gz",
        "unzip": "bin/python3",
    },
}


def cache_dir() -> Path:
    return Path(os.environ.get("APPCI_TOOLS_CACHE", Path.home() / ".cache" / "appci" / "tools"))


def spec(cfg: Config, name: str) -> Dict[str, str]:
    return deep_merge(TOOLS[name], (cfg.get("tools") or {}).get(name) or {})


def _ssl_context(cfg: Config):
    verify = (cfg.get("downloads") or {}).get("verify_tls", True)
    if not verify or os.environ.get("APPCI_INSECURE_DOWNLOADS") == "true":
        return ssl._create_unverified_context()  # integrity is still checked via SHA-256
    return None


def download_bytes(cfg: Config, url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300, context=_ssl_context(cfg)) as r:
        return r.read()


_READY: Dict[str, Path] = {}


def private_dir(prefix: str) -> Path:
    """A directory only this process uses (removed at exit). Runnable copies of cached tools go
    here: the shared cache on the runner volume is writable by every job, including the build and
    test code of other merge requests, so nothing is executed from it directly."""
    d = Path(tempfile.mkdtemp(prefix=f"appci-{prefix}-"))
    atexit.register(shutil.rmtree, d, True)
    return d


def ensure(cfg: Config, name: str) -> Path:
    """Path to the tool (jar or executable), downloaded if needed and verified against its pinned
    SHA-256 on every use, not only at download. Archives are unpacked into a private directory."""
    s = spec(cfg, name)
    if s.get("path"):  # explicit local override (admin-provided, outside the shared cache)
        return Path(s["path"])
    if name in _READY and _READY[name].exists():
        return _READY[name]
    if not s.get("sha256"):
        raise RuntimeError(f"{name}: no sha256 pinned; set tools.{name}.sha256 (or tools.{name}.path)")
    base = cache_dir() / name / s["version"]
    target = base / s["file"]
    if target.exists() and util.sha256_file(target) != s["sha256"]:
        util.warn(f"{name}: cached copy does not match its pinned SHA-256 (modified?); downloading again")
        target.unlink()
    if not target.exists():
        _download(cfg, name, s, target)
    if "unzip" in s:
        for old in base.iterdir():                 # unpacked copies of earlier appci versions
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        dest = private_dir(name)
        _extract_with_modes(target, dest)
        exe = next(iter(dest.glob(f"*/{s['unzip']}")), None)
        if exe is None:
            raise RuntimeError(f"{name}: {s['unzip']} not found in {target.name}")
    else:
        exe = target
    _READY[name] = exe
    return exe


def _download(cfg: Config, name: str, s: Dict[str, str], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".part{os.getpid()}")
    util.info(f"downloading {name} {s['version']} ...")
    try:
        with urllib.request.urlopen(s["url"], timeout=300, context=_ssl_context(cfg)) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except OSError as exc:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f"cannot download {name} from {s['url']}: {exc}. "
                           f"Pre-seed {target} or set tools.{name}.path in appci.yaml") from exc
    digest = util.sha256_file(tmp)
    if digest != s["sha256"]:
        tmp.unlink()
        raise RuntimeError(f"{name}: SHA-256 mismatch (got {digest}, expected {s['sha256']})")
    tmp.replace(target)


def _safe_members(t: tarfile.TarFile, dest: Path):
    """Reject absolute paths, '..' and links that point outside `dest` (tarfile has no filter on 3.8)."""
    root = os.path.realpath(dest)
    for m in t.getmembers():
        path = os.path.realpath(os.path.join(root, m.name))
        if not (path == root or path.startswith(root + os.sep)) or m.isdev():
            raise RuntimeError(f"unsafe path in archive: {m.name}")
        if m.issym() or m.islnk():
            link = m.linkname if m.islnk() else os.path.join(os.path.dirname(m.name), m.linkname)
            if os.path.isabs(m.linkname) or not os.path.realpath(os.path.join(root, link)).startswith(root + os.sep):
                raise RuntimeError(f"unsafe link in archive: {m.name} -> {m.linkname}")
        m.mode &= 0o777                       # no setuid/setgid bits
        yield m


def _extract_with_modes(archive: Path, dest: Path) -> None:
    """zipfile drops Unix permissions; restore them (bundled JREs need jspawnhelper, bin/*).
    tar archives keep them (members checked by _safe_members)."""
    if archive.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive) as t:
            t.extractall(dest, members=_safe_members(t, dest))
        return
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            path = Path(z.extract(info, dest))
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                path.chmod(mode)


def available(cfg: Config, name: str) -> bool:
    try:
        ensure(cfg, name)
        return True
    except RuntimeError:
        return False


DOKKA_PLUGINS = ("dokka-base", "dokka-analysis", "kotlinx-html", "freemarker")

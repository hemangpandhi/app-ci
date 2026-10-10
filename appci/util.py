"""Shared helpers: logging, subprocess execution with log capture, locking."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_VERBOSE = False
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None



def _git_version() -> tuple:
    try:
        out = subprocess.run(["git", "--version"], stdout=subprocess.PIPE, text=True).stdout
    except OSError:
        return ()
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else ()


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


# CI secrets are kept away from everything appci starts (Soong genrules, unit tests, pre-upload
# hooks, javadoc, git: code a merge request controls). seal_secrets() moves them out of the process
# environment at start-up; appci's own code reads them with secret(); a stage that really needs one
# passes it explicitly (sonar-scanner: SONAR_TOKEN).
SECRET_ENV_RE = re.compile(r"TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|PRIVATE_KEY|API_KEY|JWT", re.I)
SECRET_ENV_NAMES = {"CI_REPOSITORY_URL"}          # embeds the job token
_SECRETS: Dict[str, str] = {}


def is_secret_env(name: str) -> bool:
    return name in SECRET_ENV_NAMES or bool(SECRET_ENV_RE.search(name))


def seal_secrets() -> List[str]:
    """Remove secret-looking variables from os.environ (children never inherit them); keep the
    values for secret(). Returns the names (for the log; values are never printed)."""
    names = sorted(k for k in os.environ if is_secret_env(k))
    for k in names:
        _SECRETS[k] = os.environ.pop(k)
    return names


def secret(name: str, default: Optional[str] = None) -> Optional[str]:
    return _SECRETS.get(name) or os.environ.get(name) or default


def redact(text: str) -> str:
    """Hide credentials embedded in URLs (https://user:token@host -> https://***@host)."""
    return re.sub(r"(\w+://)[^/\s:@]+:[^/\s@]+@", r"\1***@", text)


def xml_root(source, text: bool = False):
    """Parse XML (a path, or a string with text=True) refusing entity declarations: expat in older
    Pythons (3.8 on Ubuntu 20.04 images) has no limit on entity expansion, so a crafted file in a
    merge request ("billion laughs") could exhaust the build host's memory. Android XML and the
    tools' reports never declare entities."""
    import xml.etree.ElementTree as ET
    data = source.encode() if text and isinstance(source, str) else (source if text else Path(source).read_bytes())
    if re.search(rb"<!ENTITY", data, re.I):
        raise ET.ParseError("XML entity declarations are not accepted")
    return ET.fromstring(data)


def setup_git_credentials() -> bool:
    """Private manifest / git servers from CI jobs: $APPCI_GIT_CREDENTIALS holds credential URLs
    (`https://user:token@host`, whitespace separated; or the path of a GitLab File variable with
    one per line). Written to git's credential store inside the throwaway container only; never
    on a host, never echoed. repo sync and every git call then authenticate."""
    value = (secret("APPCI_GIT_CREDENTIALS") or "").strip()
    if not value or not in_container():
        return False
    if os.path.isfile(value):
        value = Path(value).read_text()
    urls = [u for u in value.split() if "://" in u and "@" in u]
    if not urls:
        warn("APPCI_GIT_CREDENTIALS is set but holds no https://user:token@host entries")
        return False
    store = Path.home() / ".git-credentials"
    old = store.read_text().split() if store.is_file() else []
    store.write_text("\n".join(dict.fromkeys(old + urls)) + "\n")
    store.chmod(0o600)
    subprocess.run(["git", "config", "--global", "credential.helper", "store"])
    return True


def remove_git_credentials() -> None:
    """After `repo sync`: later steps (build, tests: merge-request code) must not find them."""
    if not in_container():
        return
    store = Path.home() / ".git-credentials"
    if store.is_file():
        store.unlink()
    subprocess.run(["git", "config", "--global", "--unset-all", "credential.helper"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def trust_git_repos() -> None:
    """CI containers often run as root on a tree owned by another user (persistent volume), and
    git then refuses every command ("dubious ownership"). Trust the repos for all git processes
    appci starts, hooks included: git >= 2.31 via command-scope config in the environment (nothing
    written); older git (e.g. 2.25 in Ubuntu 20.04 images) reads safe.directory only from global
    config, so appci adds it there. Only inside a throwaway container, never on a host."""
    if not in_container():
        return                       # hosts: the runner user owns its trees; never widen trust there
    if _git_version() >= (2, 31):
        n = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
        for i in range(n):
            if os.environ.get(f"GIT_CONFIG_KEY_{i}") == "safe.directory":
                return
        os.environ[f"GIT_CONFIG_KEY_{n}"] = "safe.directory"
        os.environ[f"GIT_CONFIG_VALUE_{n}"] = "*"
        os.environ["GIT_CONFIG_COUNT"] = str(n + 1)
    elif in_container():
        have = subprocess.run(["git", "config", "--global", "--get-all", "safe.directory"],
                              stdout=subprocess.PIPE, text=True).stdout.split()
        if "*" not in have:
            subprocess.run(["git", "config", "--global", "--add", "safe.directory", "*"])


def set_verbose(value: bool) -> None:
    global _VERBOSE
    _VERBOSE = value


def is_verbose() -> bool:
    return _VERBOSE


def _c(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _COLOR else text


def info(msg: str) -> None:
    print(f"{_c('36', '[appci]')} {redact(msg)}", flush=True)


def warn(msg: str) -> None:
    print(f"{_c('33', '[appci] WARN')} {redact(msg)}", flush=True)


def error(msg: str) -> None:
    print(f"{_c('31', '[appci] ERROR')} {redact(msg)}", file=sys.stderr, flush=True)


def ok(msg: str) -> None:
    print(f"{_c('32', '[appci]')} {msg}", flush=True)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False, default=str))
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


class CommandResult:
    def __init__(self, cmd: str, returncode: int, log: Path, duration: float):
        self.cmd = cmd
        self.returncode = returncode
        self.log = log
        self.duration = duration

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def output(self) -> str:
        try:
            return strip_ansi(self.log.read_text(errors="replace"))
        except OSError:
            return ""

    def tail(self, lines: int = 60) -> str:
        return "\n".join(self.output().splitlines()[-lines:])


PROGRESS_INTERVAL = float(os.environ.get("APPCI_PROGRESS_INTERVAL", "60"))
# Soong/ninja/siso "[ 45% 1234/5678] ..." and repo "Fetching: 37% (400/1080)" style lines
_PROGRESS_RE = re.compile(r"^(\[\s*\d+% \d+/\d+|\[\d+/\d+\]|(Fetching|Checking out|Syncing)\b.*\d+%)")


def run(
    cmd: Sequence[str] | str,
    log: Path,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    append: bool = False,
    timeout: Optional[float] = None,
    shell_bash: bool = False,
) -> CommandResult:
    """Run a command, tee-ing combined stdout/stderr to `log`.

    Output is streamed to the console only in verbose mode; otherwise the tail
    is printed by the caller on failure, and long commands print a progress line every
    PROGRESS_INTERVAL seconds (elapsed time + latest build progress / output line), so CI
    logs show that a multi-hour build is moving. `shell_bash` runs `cmd` via bash -c.
    """
    if shell_bash:
        argv: List[str] = ["bash", "-c", cmd if isinstance(cmd, str) else " ".join(cmd)]
        printable = argv[2]
    else:
        argv = [cmd] if isinstance(cmd, str) else list(cmd)
        printable = " ".join(shlex.quote(a) for a in argv)
    printable = redact(printable)
    log.parent.mkdir(parents=True, exist_ok=True)
    full_env = merge_env(env)
    start = time.time()
    with open(log, "ab" if append else "wb") as lf:
        lf.write(f"$ {printable}\n".encode())
        lf.flush()
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert proc.stdout is not None
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 15)

        watchdog = threading.Timer(timeout, _kill) if timeout else None
        if watchdog:
            watchdog.daemon = True
            watchdog.start()
        latest = {"progress": "", "line": ""}
        done = threading.Event()

        def _report() -> None:
            shown = None
            while not done.wait(PROGRESS_INTERVAL):
                what = latest["progress"] or latest["line"]
                what = what if what != shown else "(no new output)"
                shown = latest["progress"] or latest["line"]
                info(f"  ... {fmt_duration(time.time() - start)} {what[:160]}")

        reporter = None
        if not _VERBOSE and PROGRESS_INTERVAL > 0:
            reporter = threading.Thread(target=_report, daemon=True)
            reporter.start()
        try:
            for line in iter(proc.stdout.readline, b""):
                lf.write(line)
                text = line.decode(errors="replace").strip()
                if text:
                    if _PROGRESS_RE.match(text):
                        latest["progress"] = text
                    else:
                        latest["line"] = text
                if _VERBOSE:
                    sys.stdout.write(line.decode(errors="replace"))
                    sys.stdout.flush()
            proc.wait()
        except KeyboardInterrupt:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 15)
            proc.wait()
            lf.write(b"\n[appci] command interrupted\n")
            raise
        finally:
            done.set()
            proc.stdout.close()
            if watchdog:
                watchdog.cancel()
        if timed_out.is_set():
            lf.write(f"\n[appci] command timed out after {timeout}s\n".encode())
            return CommandResult(printable, 124, log, time.time() - start)
    return CommandResult(printable, proc.returncode, log, time.time() - start)


def merge_env(env: Optional[Dict[str, Optional[str]]]) -> Dict[str, str]:
    """os.environ + overrides; a None value removes the variable."""
    full = os.environ.copy()
    for k, v in (env or {}).items():
        if v is None:
            full.pop(k, None)
        else:
            full[k] = str(v)
    return full


def capture(cmd: Sequence[str], cwd: Optional[Path] = None, env=None, check=False) -> str:
    full_env = merge_env(env)
    res = subprocess.run(
        list(cmd), cwd=str(cwd) if cwd else None, env=full_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {res.stderr.strip()}")
    return res.stdout


@contextlib.contextmanager
def file_lock(path: Path, what: str):
    """Exclusive inter-process lock (one build per OUT_DIR at a time)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            info(f"waiting for lock on {what} ({path}) ...")
            fcntl.flock(f, fcntl.LOCK_EX)
        f.write(str(os.getpid()))
        f.flush()
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def rel(path: str | Path, base: str | Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(base).resolve()))
    except ValueError:
        return str(path)


def chunked(items: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"

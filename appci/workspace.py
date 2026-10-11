"""CI workspaces: one repo-managed AOSP tree per platform on a persistent volume.

Platform images carry only a `.repo` cache (e.g. /build_env/.repo). `appci workspace` turns
that into a tree the other commands can use:

* first run: seed `.repo` from the image cache (`seed: copy`, default: a one-time local copy, so
  objects come from the image instead of the network and the tree never depends on the image
  afterwards; images are replaced weekly), then `repo init` from the platform's manifest
  (URL / branch / file / groups / depth). `seed: reference` borrows objects via git alternates
  instead (no copy, but the tree breaks if a new image drops objects); `seed: none` fetches all.
  (repo's own --dissociate is not used: repo marks the borrowed object stores precious and its
  repack then fails.)
* manifest settings changed (config or CI variables): `repo init` again, then sync;
* `repo sync` per the policy (`always`, or `if-older` than max_age_hours, so only the first job
  of a pipeline pays for it); the app projects earlier jobs checked out are reset first;
* recovery: a sync that was interrupted (job cancelled / timed out) or fails leaves git lock files
  and half-made checkouts behind (HEAD already at the new revision, files missing, so a plain
  sync skips them). appci then removes stale locks, syncs with `--force-sync` (the CI tree holds
  nobody's work; this also moves object stores whose layout in the seeded `.repo` differs from
  the manifest) and restores every project's files (`git reset --hard`). `--repair` forces this;
* one process per workspace at a time (file lock; GitLab resource_group on top).

When the manifest nests the platform in a subdirectory (projects at `android/...`), `workspace.tree`
names it: the AOSP root is then `<workspace>/<tree>` and app paths stay relative to that root.

Everything comes from `platforms.<name>` in appci.yaml and can be overridden per job with
APPCI_MANIFEST_URL / _BRANCH / _FILE / _GROUPS / _DEPTH, APPCI_WORKSPACE, APPCI_REPO_REFERENCE.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import util

STATE = "appci-workspace.json"
MANIFEST_KEYS = ("url", "branch", "file", "groups", "depth")
ENV = {"url": "APPCI_MANIFEST_URL", "branch": "APPCI_MANIFEST_BRANCH", "file": "APPCI_MANIFEST_FILE",
       "groups": "APPCI_MANIFEST_GROUPS", "depth": "APPCI_MANIFEST_DEPTH"}


def platform_name(cfg) -> Optional[str]:
    return cfg.get("platform") or os.environ.get("APPCI_PLATFORM") or None


def resolve(cfg) -> Dict[str, Any]:
    """Effective workspace settings: workspace defaults < platforms.<name> < environment."""
    from .config import deep_merge
    name = platform_name(cfg)
    plat = (cfg.get("platforms") or {}).get(name or "", {}) or {}
    ws = deep_merge(cfg.get("workspace") or {}, plat.get("workspace") or {})
    manifest = dict(plat.get("manifest") or {})
    for k, var in ENV.items():
        if os.environ.get(var):
            manifest[k] = os.environ[var]
    path = os.environ.get("APPCI_WORKSPACE") or ws.get("path") or ""
    path = os.path.expandvars(path.replace("{platform}", name or "default"))
    ref = os.environ.get("APPCI_REPO_REFERENCE", ws.get("reference") or "")
    tree = str(ws.get("tree") or "").strip("/")
    return {"platform": name, "path": Path(path) if path else None, "seed": ws.get("seed", "copy"),
            "tree": tree,
            "manifest": {k: manifest.get(k) for k in MANIFEST_KEYS},
            "local_manifests": list(plat.get("local_manifests") or ws.get("local_manifests") or []),
            "reference": Path(ref) if ref else None,
            "repo": ws.get("repo") or {}, "init_args": list(ws.get("init_args") or []),
            "sync": ws.get("sync") or {}}


def _repo_cmd(ws: Dict[str, Any]) -> List[str]:
    """repo launcher: configured, else on PATH, else the workspace's own repo checkout (seeded from
    the image cache, so no network is needed for repo itself)."""
    if ws["repo"].get("command"):
        return str(ws["repo"]["command"]).split()
    found = shutil.which("repo")
    if found:
        return [found]
    own = ws["path"] / ".repo" / "repo" / "repo"
    if own.is_file():
        return [sys.executable, str(own)]
    raise RuntimeError("no `repo` launcher: put it on PATH, set workspace.repo.command, or provide "
                       f"a reference cache with .repo/repo (looked in {ws['reference']})")


def _seed_copy(ws: Dict[str, Any]) -> bool:
    """One-time copy of the image's .repo onto the volume (resumable: a partial copy is redone)."""
    src = ws["reference"] / ".repo"
    dst = ws["path"] / ".repo"
    tmp = ws["path"] / ".repo.seeding"
    if tmp.exists():
        shutil.rmtree(tmp)
    util.info(f"workspace: seeding {dst} from {src} (one-time copy) ...")
    r = util.run(["cp", "-a", "--reflink=auto", str(src), str(tmp)], log=ws["path"] / ".appci-seed.log")
    if not r.ok:
        raise RuntimeError(f"copying {src} failed:\n{r.tail(20)}")
    for f in ("TRACE_FILE", STATE, "appci-pinned-manifest.xml"):
        (tmp / f).unlink() if (tmp / f).exists() else None
    tmp.rename(dst)
    return True


def _reference_ok(ws: Dict[str, Any]) -> bool:
    ref = ws["reference"]
    return bool(ref and ((ref / ".repo" / "project-objects").is_dir() or any(ref.glob("*.git"))))


GIT_LOCKS = ("index.lock", "HEAD.lock", "config.lock", "packed-refs.lock", "shallow.lock", "FETCH_HEAD.lock")


def remove_stale_locks(root: Path) -> int:
    """Delete git lock files a killed sync left in the tree's git dirs. Only call this while
    holding the workspace lock: then no other git process can be working in the tree."""
    n = 0
    for top in (root / ".repo" / "projects", root / ".repo" / "project-objects", root / ".repo" / "manifests.git"):
        if not top.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(top):
            d = Path(dirpath)
            if d.name.endswith(".git"):
                for lock in GIT_LOCKS:
                    f = d / lock
                    if f.is_file():
                        f.unlink()
                        n += 1
                dirnames[:] = []          # objects/, refs/, ...: nothing of repo's below a git dir
    return n


def ensure(cfg, apps: List[str], force_sync: bool = False, adopt: bool = False,
           only_apps: bool = False, log_dir: Optional[Path] = None, repair: bool = False) -> Dict[str, Any]:
    ws = resolve(cfg)
    if ws["path"] is None:
        raise RuntimeError("no workspace path: set workspace.path (e.g. /appci/{platform}) or $APPCI_WORKSPACE")
    m = ws["manifest"]
    if not m.get("url"):
        raise RuntimeError(f"platform '{ws['platform']}': no manifest URL "
                           "(platforms.<name>.manifest.url or $APPCI_MANIFEST_URL)")
    root: Path = ws["path"]
    root.mkdir(parents=True, exist_ok=True)
    log_dir = log_dir or root / ".appci-logs"
    state_file = root / ".repo" / STATE
    with util.file_lock(root / ".appci-workspace.lock", f"workspace {root}"):
        state = util.read_json(state_file) if state_file.is_file() else {}
        initialized = (root / ".repo" / "manifests").is_dir()
        if not initialized and ws["seed"] == "copy" and _reference_ok(ws):
            _seed_copy(ws)
            state = {"seeded_from": str(ws["reference"]), "seeded_at": time.time()}
            util.write_json(state_file, state)
        if initialized and not state and not adopt:
            raise RuntimeError(f"{root} is a repo tree appci did not create; refusing to manage it "
                               "(it resets app projects). Use a dedicated path or pass --adopt.")
        want = {k: (str(v) if v is not None else None) for k, v in m.items()}
        # The state file on the shared volume never holds a credential from the URL: compare a hash.
        want_state = dict(want, url=util.redact(want["url"]),
                          url_sha256=hashlib.sha256(want["url"].encode()).hexdigest())
        synced = False
        if not initialized or state.get("manifest") != want_state:
            cmd = _repo_cmd(ws) + ["init", "-u", want["url"]]
            for flag, key in (("-b", "branch"), ("-m", "file"), ("-g", "groups"), ("--depth", "depth")):
                if want.get(key):
                    cmd += [flag, want[key]]
            if not (root / ".repo" / "manifests").is_dir() and ws["seed"] == "reference" and _reference_ok(ws):
                cmd += ["--reference", str(ws["reference"])]
            cmd += ws["init_args"]
            util.info(f"workspace: repo init {want['url']} ({want.get('branch') or 'default branch'}, "
                      f"{want.get('file') or 'default.xml'}) in {root}")
            r = util.run(cmd, log=log_dir / "repo-init.log", cwd=root)
            if not r.ok:
                raise RuntimeError(f"repo init failed:\n{r.tail(30)}")
            state["manifest"] = want_state
            state.pop("synced_at", None)
            util.write_json(state_file, state)
        local = root / ".repo" / "local_manifests"
        for lm in ws["local_manifests"]:
            src = cfg.path(lm) if not Path(lm).is_absolute() else Path(lm)
            local.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, local / Path(lm).name)
        scfg = ws["sync"]
        age_h = (time.time() - state.get("synced_at", 0)) / 3600
        policy = scfg.get("policy", "if-older")
        due = force_sync or "synced_at" not in state or policy == "always" or \
            (policy == "if-older" and age_h >= float(scfg.get("max_age_hours", 12)))
        interrupted = bool(state.get("sync_started")) or repair
        if interrupted:
            util.warn("workspace: repairing on request" if repair else
                      "workspace: the previous sync did not finish (job cancelled or timed out); recovering")
            n = remove_stale_locks(root)
            if n:
                util.info(f"workspace: removed {n} stale git lock file(s)")
        # App paths are relative to the AOSP root; repo's project paths to the workspace.
        apps = [f"{ws['tree']}/{a}" if ws["tree"] else a for a in apps]
        for app in apps:          # earlier jobs leave MR commits / build leftovers in app projects
            repo = root / app
            if (repo / ".git").exists():
                util.capture(["git", "reset", "--hard", "-q"], cwd=repo)
                util.capture(["git", "clean", "-fdq"], cwd=repo)
        if due or only_apps or interrupted:
            cmd = _repo_cmd(ws) + ["sync", "-j", str(scfg.get("jobs", 8))] + list(
                scfg.get("args") or ["-c", "--no-tags", "--optimized-fetch", "-d", "--retry-fetches=3"])
            if only_apps:
                cmd += apps
            util.info(f"workspace: repo sync{' ' + ' '.join(apps) if only_apps else ''} "
                      f"(last sync {'never' if 'synced_at' not in state else f'{age_h:.1f} h ago'})")
            recover = interrupted
            state["sync_started"] = time.time()
            util.write_json(state_file, state)
            r = None
            for attempt in (1, 2):
                if attempt > 1:
                    n = remove_stale_locks(root)
                    if n:
                        util.info(f"workspace: removed {n} stale git lock file(s)")
                run_cmd = cmd + (["--force-sync", "--force-remove-dirty"] if recover else [])
                r = util.run(run_cmd, log=log_dir / "repo-sync.log", cwd=root, timeout=scfg.get("timeout"),
                             append=attempt > 1)
                if r.ok or recover:
                    break
                util.warn("workspace: repo sync failed; retrying once with stale locks removed and --force-sync")
                recover = True
            if not r.ok:
                raise RuntimeError(f"repo sync failed:\n{r.tail(30)}")
            if recover and not only_apps:
                # an interrupted checkout leaves HEAD at the new revision with files missing
                util.info("workspace: restoring project files (git reset --hard in every project)")
                rr = util.run(_repo_cmd(ws) + ["forall", "-j", str(scfg.get("jobs", 8)), "-c",
                                               "git reset --hard -q"],
                              log=log_dir / "repo-sync.log", cwd=root, timeout=scfg.get("timeout"), append=True)
                if not rr.ok:
                    raise RuntimeError(f"restoring project files failed:\n{rr.tail(30)}")
            state.pop("sync_started", None)
            synced = True
            if not only_apps:
                state["synced_at"] = time.time()
            snap = util.capture(_repo_cmd(ws) + ["manifest", "-r"], cwd=root)
            if snap.strip():
                (root / ".repo" / "appci-pinned-manifest.xml").write_text(snap)
        else:
            util.info(f"workspace: up to date (synced {age_h:.1f} h ago, policy {policy})")
        state.update({"platform": ws["platform"], "path": str(root), "checked_at": time.time()})
        util.write_json(state_file, state)
        return {**state, "synced_now": synced}


def checkout(cfg: Config, app_path: str, source: str, ref: str, force: bool = False) -> str:
    """Put the commit under test into the AOSP tree's copy of the app (CI merge-request mode)."""
    repo = cfg.aosp_root / app_path
    if not (repo / ".git").exists():
        raise SystemExit(f"{repo} is not a git checkout")
    dirty = util.capture(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo).strip()
    if dirty and not force:
        raise SystemExit(f"{repo} has local modifications; refusing to check out over them (use --force)")
    log = cfg.workdir / "logs" / f"checkout-{util.slug(app_path)}.log"
    r = util.run(["git", "fetch", "--no-tags", "--quiet", source, ref], log=log, cwd=repo)
    if not r.ok:
        raise SystemExit(f"git fetch {source} {ref} failed:\n{r.tail(20)}")
    r = util.run(["git", "checkout", "--quiet", "--force", "--detach", "FETCH_HEAD"], log=log, cwd=repo, append=True)
    if not r.ok:
        raise SystemExit(f"git checkout failed:\n{r.tail(20)}")
    sha = util.capture(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    util.info(f"{app_path} checked out at {sha[:12]}")
    return sha

"""Run the project's own PREUPLOAD.cfg hooks (AOSP repohooks) on the commits under test.

repohooks needs a local branch with an upstream; CI checkouts are detached. So the hooks run in a
throwaway `git clone --shared` placed under <workdir> inside the tree (so `${REPO_ROOT}` still
resolves) with a temporary tracking branch pointing at the change's base. The real repository is
never modified. Hooks listed in `stages.preupload.skip_hooks` are disabled in the scratch copy of
PREUPLOAD.cfg (e.g. Gerrit's Change-Id rule, meaningless for GitLab merge requests).
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import List

from .. import util
from ..model import ERROR, FAILED, PASSED, SKIPPED, Finding, save_findings
from . import Stage, register

RESULT_RE = re.compile(r"^\[(FAILED|PASSED|WARNING)\]\s+(.*?)\s*$")
COMMIT_RE = re.compile(r"^\[(RUNNING|COMMIT)\b.*?([0-9a-f]{7,40})", re.I)


def _disable_hooks(cfg_path: Path, skip: List[str]) -> List[str]:
    if not skip or not cfg_path.is_file():
        return []
    cp = configparser.RawConfigParser()
    cp.optionxform = str                         # hook names are case-sensitive
    cp.read(cfg_path)
    done = []
    for name in skip:
        if cp.has_section("Hook Scripts") and cp.has_option("Hook Scripts", name):
            cp.remove_option("Hook Scripts", name)
            done.append(name)
        else:
            # Builtin hooks (also those enabled by GLOBAL-PREUPLOAD.cfg) are switched off explicitly.
            if not cp.has_section("Builtin Hooks"):
                cp.add_section("Builtin Hooks")
            cp.set("Builtin Hooks", name, "false")
            done.append(name)
    with open(cfg_path, "w") as f:
        cp.write(f)
    return done


def _hook_toolchain(ctx) -> Path:
    """A bin dir with the tree's python3 and JDK, like the environment after `lunch`.

    Hooks are scripts with `#!/usr/bin/env python3` shebangs and call `java`; host versions are
    often too old (e.g. Ubuntu 20.04: Python 3.8, Java 8). Only these tools are exposed, not the
    whole prebuilts/build-tools path (it would shadow coreutils with toybox).
    """
    from .. import tools
    d = tools.private_dir("hook-bin")          # not in the shared tree: other jobs could swap links
    links = {"python3": Path(pick_hook_python(ctx.cfg, ctx.stage_cfg("preupload")))}
    for tool in ("java", "javac", "jar"):
        links[tool] = ctx.cfg.java_home / "bin" / tool
    for name, target in links.items():
        if target.exists():
            (d / name).symlink_to(target)
    if not (d / "python3").exists():
        (d / "python3").symlink_to(shutil.which("python3") or "/usr/bin/python3")
    return d


ENV_ERROR_RE = re.compile(
    r"ModuleNotFoundError|ImportError|Please install '|UnsupportedClassVersionError|"
    r"command not found|No such file or directory: '|has no attribute 'BooleanOptionalAction'|"
    r"JNI error has occurred|Permission denied")


def _module_names(modules: List[str]) -> List[str]:
    return [m.partition("==")[0].strip() for m in modules]


def _python_ok(exe: str, modules: List[str], pythonpath: str = "") -> bool:
    code = "import sys; assert sys.version_info >= (3, 9)" + "".join(
        f"; import {m}" for m in _module_names(modules))
    env = dict(os.environ, PYTHONPATH=pythonpath) if pythonpath else None
    try:
        return subprocess.run([exe, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30, env=env).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pick_hook_python(cfg, scfg) -> str:
    """Interpreter for hook scripts: configured one, else the newest python3.x (>= 3.9) that can
    import `python_modules` (e.g. lxml for the RRO overlayable checks), else the newest >= 3.9 one
    that can load compiled modules (hook_pythonpath then supplies the missing ones), else a pinned
    standalone CPython from the tools cache, else the tree prebuilt (py3-cmd: no compiled modules)."""
    if scfg.get("python"):
        return str(cfg.path(scfg["python"]))
    modules = list(scfg.get("python_modules") or [])
    cands = [shutil.which(f"python3.{minor}") for minor in range(20, 8, -1)] + [shutil.which("python3")]
    prebuilt = cfg.path("prebuilts/build-tools/path/linux-x86/python3")
    cands = [c for c in dict.fromkeys(cands) if c]
    for mods in (modules, []):
        for c in cands:
            if _python_ok(c, mods) and (mods or not modules or _loads_wheels(c)):
                return c
    from .. import tools
    try:
        return str(tools.ensure(cfg, "python"))
    except RuntimeError as exc:
        util.warn(f"hook python: no python3 >= 3.9 on this runner and {exc}")
    return str(prebuilt) if prebuilt.exists() else (shutil.which("python3") or "/usr/bin/python3")


def _loads_wheels(exe: str) -> bool:
    """A regular CPython (manylinux wheels load), not an embedded launcher like AOSP's py3-cmd."""
    code = ("import importlib.machinery as m, sys; "
            "assert '.cpython-%d%d-x86_64-linux-gnu.so' % sys.version_info[:2] in m.EXTENSION_SUFFIXES")
    try:
        return subprocess.run([exe, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# Official SHA-256 of the binary wheels appci may fetch (from PyPI, pinned here so the shared
# cache and the download channel are both verified). More: stages.preupload.wheel_sha256.
WHEEL_SHA256 = {
    "lxml-5.3.0-cp38-cp38-manylinux_2_17_x86_64.manylinux2014_x86_64.whl":
        "146173654d79eb1fc97498b4280c1d3e1e5d58c398fa530905c9ea50ea849b22",
    "lxml-5.3.0-cp39-cp39-manylinux_2_17_x86_64.manylinux2014_x86_64.whl":
        "68b87753c784d6acb8a25b05cb526c3406913c9d988d51f80adecc2b0775d6aa",
    "lxml-5.3.0-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl":
        "41ce1f1e2c7755abfc7e759dc34d7d05fd221723ff822947132dc934d122fe22",
    "lxml-5.3.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl":
        "aa617107a410245b8660028a7483b68e7914304a6d4882b5ff3d2d3eb5948d8c",
    "lxml-5.3.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl":
        "c24037349665434f375645fa9d1f5304800cec574d0310f618490c871fd902b3",
    "lxml-5.3.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl":
        "423b121f7e6fa514ba0c7918e56955a1d4470ed35faa03e3d9f0e3baa4c7e492",
}


def _fetch_wheel(cfg, exe: str, name: str, version: str) -> Path:
    """The pinned binary wheel matching `exe`, verified on every use and unpacked privately."""
    from .. import tools
    tag = util.capture([exe, "-c", "import sys; print('cp%d%d' % sys.version_info[:2])"]).strip()
    pins = dict(WHEEL_SHA256, **(cfg.stage_cfg("preupload").get("wheel_sha256") or {}))
    prefix = f"{name}-{version}-{tag}-{tag}-manylinux"
    wanted = sorted(n for n in pins if n.startswith(prefix) and n.endswith("x86_64.whl"))
    if not wanted:
        raise RuntimeError(f"no pinned {tag} wheel for {name}=={version}; add its SHA-256 under "
                           "stages.preupload.wheel_sha256")
    fname = wanted[0]
    whl = tools.cache_dir() / "pywheels" / fname
    if whl.exists() and util.sha256_file(whl) != pins[fname]:
        util.warn(f"hook python: cached {fname} does not match its pinned SHA-256; downloading again")
        whl.unlink()
    if not whl.exists():
        mirror = ((cfg.get("downloads") or {}).get("wheel_base_url") or "").rstrip("/")
        if mirror:                               # internal mirror: a folder holding the .whl files
            url = f"{mirror}/{fname}"
        else:
            meta = json.loads(tools.download_bytes(cfg, f"https://pypi.org/pypi/{name}/{version}/json"))
            url = next((f["url"] for f in meta.get("urls", []) if f["filename"] == fname), None)
            if not url:
                raise RuntimeError(f"{fname} not found on PyPI")
        data = tools.download_bytes(cfg, url)
        if hashlib.sha256(data).hexdigest() != pins[fname]:
            raise RuntimeError(f"{fname}: SHA-256 mismatch")
        whl.parent.mkdir(parents=True, exist_ok=True)
        tmp = whl.with_name(whl.name + f".part{os.getpid()}")
        tmp.write_bytes(data)
        tmp.replace(whl)
        util.info(f"hook python: {fname} downloaded")
    dest = tools.private_dir(f"{name}-{tag}")
    with zipfile.ZipFile(whl) as z:
        z.extractall(dest)
    return dest


def hook_pythonpath(cfg, scfg, exe: str) -> str:
    """PYTHONPATH giving `exe` the pinned python_modules it cannot import itself ("" if none).

    Runner hosts often lack e.g. lxml for the interpreter new enough for repohooks, and CI must not
    pip-install into the system: the pinned wheel goes into the appci tools cache instead."""
    paths = []
    for m in scfg.get("python_modules") or []:
        if _python_ok(exe, [m]):
            continue
        name, _, version = m.partition("==")
        if not version:
            util.warn(f"hook python: {name} is missing and not pinned ('{name}==<version>'), not fetched")
            continue
        try:
            paths.append(str(_fetch_wheel(cfg, exe, name.strip(), version.strip())))
        except (RuntimeError, OSError, ValueError, KeyError) as exc:
            util.warn(f"hook python: cannot provide {m}: {exc}")
    return os.pathsep.join(paths)


def parse_repohooks_output(text: str) -> List[dict]:
    """[FAILED] sections -> [{hook, details}] (the details are the lines until the next status)."""
    failures, current = [], None
    for raw in util.strip_ansi(text).splitlines():
        m = RESULT_RE.match(raw.strip())
        if m:
            if current:
                failures.append(current)
                current = None
            if m.group(1) == "FAILED" and not m.group(2).startswith("repohooks for "):
                current = {"hook": re.sub(r" hook$", "", m.group(2)), "details": []}
            continue
        if current is not None and raw.strip() and not raw.lstrip().startswith("[RUNNING"):
            current["details"].append(raw.rstrip())
    if current:
        failures.append(current)
    return failures


@register
class PreuploadStage(Stage):
    name = "preupload"
    title = "Pre-upload hooks (PREUPLOAD.cfg / repohooks)"

    def run(self, ctx, res):
        scfg = ctx.stage_cfg("preupload")
        root = ctx.cfg.aosp_root
        app = ctx.app_abs()
        has_global = (root / ".repo" / "manifests" / "GLOBAL-PREUPLOAD.cfg").is_file() or \
            (root / "GLOBAL-PREUPLOAD.cfg").is_file()
        if not (app / "PREUPLOAD.cfg").is_file() and not has_global:
            res.status, res.summary = SKIPPED, "no PREUPLOAD.cfg / GLOBAL-PREUPLOAD.cfg"
            return
        runner = ctx.cfg.path(scfg["runner"])
        if not runner.is_file():
            res.status, res.summary = ERROR, f"repohooks not found at {runner}"
            return
        cs = ctx.changeset
        head = util.capture(["git", "rev-parse", "HEAD"], cwd=app).strip()
        if cs is not None:
            commits, base = cs.commits, cs.base
            if not commits:
                res.status = SKIPPED
                res.summary = "no commits in the change (uncommitted edits are checked by the other stages)"
                return
        else:
            # No change base: check the latest commit only.
            base = util.capture(["git", "rev-parse", "HEAD~1"], cwd=app).strip()
            commits = [head] if base else []
            if not commits:
                res.status, res.summary = SKIPPED, "single-commit history: nothing to compare against"
                return
        commits = commits[-int(scfg.get("max_commits") or 50):]

        d = ctx.stage_dir("preupload", clean=True)
        work = ctx.cfg.workdir / "preupload" / ctx.profile.slug
        if work.exists():
            shutil.rmtree(work)
        work.parent.mkdir(parents=True, exist_ok=True)
        log = d / "repohooks.log"
        res.log = ctx.rel_report(log)
        steps = [
            ["git", "clone", "--quiet", "--shared", "--no-checkout", str(app), str(work)],
            ["git", "-C", str(work), "checkout", "--quiet", "-B", "appci-preupload", head],
            ["git", "-C", str(work), "update-ref", "refs/remotes/appci/base", base],
            ["git", "-C", str(work), "config", "remote.appci.url", str(app)],
            ["git", "-C", str(work), "config", "branch.appci-preupload.remote", "appci"],
            ["git", "-C", str(work), "config", "branch.appci-preupload.merge", "refs/heads/base"],
        ]
        for step in steps:
            r = util.run(step, log=log, append=True)
            if not r.ok:
                res.status, res.summary = ERROR, f"preparing the hook workspace failed: {' '.join(step[:3])}"
                return
        disabled = _disable_hooks(work / "PREUPLOAD.cfg", list(scfg.get("skip_hooks") or []))
        project = scfg.get("project") or os.environ.get("REPO_PROJECT") or ctx.profile.path
        tool_bin = _hook_toolchain(ctx)
        env = {"PATH": f"{tool_bin}:{os.environ.get('PATH', '')}", "JAVA_HOME": str(ctx.cfg.java_home)}
        pypath = hook_pythonpath(ctx.cfg, scfg, os.path.realpath(tool_bin / "python3"))
        if pypath:
            env["PYTHONPATH"] = pypath
        r = util.run([str(tool_bin / "python3"), str(runner), "--dir", str(work), "--project", project,
                      "-j", str(scfg.get("jobs") or 4), *commits],
                     log=log, append=True, cwd=work, env=env, timeout=scfg.get("timeout"))
        failures = parse_repohooks_output(r.output())
        internal = [f for f in failures if "lookup" in f["hook"].lower() or "loading config" in f["hook"].lower()]
        # A hook that could not run (missing python module, old JDK, ...) is an environment
        # problem of the runner, not a defect of the change: report it as an error, not a gate hit.
        env_errors = [f for f in failures if f not in internal and ENV_ERROR_RE.search("\n".join(f["details"]))]
        findings = [Finding("preupload", f["hook"], "\n".join(f["details"][:30])[:3000] or f["hook"],
                            f"{ctx.profile.path}/PREUPLOAD.cfg", 1, severity="critical", category="hooks")
                    for f in failures if f not in internal and f not in env_errors]
        res.metrics["hooks_env_errors"] = [f["hook"] for f in env_errors]
        if env_errors:
            (d / "environment-errors.txt").write_text("\n\n".join(
                f"[{f['hook']}]\n" + "\n".join(f["details"][:40]) for f in env_errors))
            res.reports["hook environment errors"] = ctx.rel_report(d / "environment-errors.txt")
        save_findings(ctx.findings_path("preupload"), findings)
        res.metrics.update({"commits_checked": len(commits), "hooks_failed": len(findings),
                            "disabled_hooks": disabled, "total": len(findings),
                            "errors": len(findings), "warnings": 0})
        shutil.rmtree(work, ignore_errors=True)
        if internal:
            res.status = ERROR
            res.summary = f"repohooks could not run: {internal[0]['hook']}"
            return
        if r.returncode not in (0, 1):
            res.status, res.summary = ERROR, f"repohooks exited {r.returncode}"
            return
        failed_names = ", ".join(f.rule for f in findings[:5])
        res.summary = (f"{len(commits)} commit(s): " +
                       (f"{len(findings)} hook(s) failed: {failed_names}" if findings else "hooks passed"))
        res.status = PASSED
        self.apply_gate(ctx, res, issues_are_warning=True)
        if env_errors and res.status != FAILED:
            res.status = ERROR
        if env_errors:
            res.summary += (f"; {len(env_errors)} hook(s) could not run on this runner: "
                            f"{', '.join(f['hook'] for f in env_errors)} (see environment-errors.txt)")

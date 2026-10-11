"""Thin layer over the AOSP build: lunch environments, `m`, `atest` and Soong metadata."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import util
from .config import Config


_passed_secrets_warned: set = set()
_types_filled: set = set()               # jdeps files whose module types were completed


def _warn_passed_secret(name: str, missing: bool = False) -> None:
    """Once per process: a secret deliberately exposed to the build (or configured but absent)."""
    if name in _passed_secrets_warned:
        return
    _passed_secrets_warned.add(name)
    if missing:
        util.warn(f"build_env_secrets: ${name} is not set in this job; the build will not see it")
    else:
        util.warn(f"build_env_secrets: ${name} is passed to the build environment (readable by the code under test)")


class AospTree:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.aosp_root

    # ----------------------------------------------------------------- targets
    def out_dir(self, target: str) -> Path:
        out = self.cfg.target(target).get("out_dir") or "out"
        p = Path(out)
        return p if p.is_absolute() else self.root / p

    def lunch(self, target: str) -> str:
        return self.cfg.target(target)["lunch"]

    def product(self, target: str) -> str:
        return self.lunch(target).split("-")[0]

    def coverage_paths(self) -> List[str]:
        # Use every configured app so the env (and thus Soong analysis) is stable across apps.
        paths = sorted({a["path"].strip("/") for a in self.cfg.app_entries()})
        return paths

    def env(self, target: str, coverage_paths: Optional[Iterable[str]] = None) -> Dict[str, str]:
        t = self.cfg.target(target)
        # Keep OUT_DIR exactly as a developer's shell would (unset for `out`): any change in its
        # value makes Soong discard its analysis and re-run it.
        out = str(t.get("out_dir") or "out")
        env: Dict[str, Optional[str]] = {"OUT_DIR": None if out == "out" else out}
        # Soong writes out/soong/module_bp_java_deps.json (module types, sources, dependencies;
        # appci's discovery reads it) only with this set. Set for every appci build of the target
        # so Soong's recorded environment stays the same and analysis is not redone each time.
        env["SOONG_COLLECT_JAVA_DEPS"] = "true"
        env.update({k: str(v) for k, v in (t.get("env") or {}).items()})
        for name in self.cfg.get("build_env_secrets") or []:
            value = util.secret(name)
            if value:
                env[name] = value
                _warn_passed_secret(name)
            else:
                _warn_passed_secret(name, missing=True)
        if t.get("coverage"):
            paths = sorted(set(self.coverage_paths()) | set(coverage_paths or []))
            env.update({
                "EMMA_INSTRUMENT": "true",
                "EMMA_INSTRUMENT_FRAMEWORK": "false",
                "JAVA_COVERAGE_PATHS": " ".join(paths),
            })
        return env

    def lock(self, target: str):
        return util.file_lock(self.out_dir(target) / ".appci.lock", f"OUT_DIR {self.out_dir(target).name}")

    # ----------------------------------------------------------------- running
    def shell(self, target: str, command: str, log: Path, extra_env: Optional[Dict[str, str]] = None,
              timeout: Optional[float] = None, coverage_paths: Optional[Iterable[str]] = None,
              append: bool = False) -> util.CommandResult:
        env = self.env(target, coverage_paths)
        env.update(extra_env or {})
        script = (
            "set -o pipefail; "
            f"cd {shlex.quote(str(self.root))} && "
            "source build/envsetup.sh >/dev/null && "
            f"lunch {shlex.quote(self.lunch(target))} >/dev/null && "
            f"{command}"
        )
        return util.run(script, log=log, env=env, timeout=timeout, shell_bash=True, append=append)

    def m(self, target: str, modules: Iterable[str], log: Path, **kw) -> util.CommandResult:
        mods = " ".join(shlex.quote(m) for m in modules)
        return self.shell(target, f"m {mods}", log, **kw)

    def atest(self, target: str, args: List[str], log: Path, **kw) -> util.CommandResult:
        return self.shell(target, "atest " + " ".join(shlex.quote(a) for a in args), log, **kw)

    # ---------------------------------------------------------------- metadata
    def product_out(self, target: str) -> Path:
        out = self.out_dir(target)
        variables = out / "soong" / f"soong.{self.product(target)}.variables"
        data = util.read_json(variables, {}) or {}
        device = data.get("DeviceName")
        if device:
            return out / "target" / "product" / device
        cache = self.cfg.workdir / "cache" / f"product_out.{util.slug(self.lunch(target))}.txt"
        if cache.exists():
            return Path(cache.read_text().strip())
        envsetup = self.root / "build" / "envsetup.sh"
        if not envsetup.is_file():
            raise RuntimeError(f"Could not determine PRODUCT_OUT for {self.lunch(target)}: {envsetup} does not "
                               f"exist (the AOSP root is {self.root}; if the manifest nests the platform in a "
                               "subdirectory, set aosp_root in the platform section)")
        proc = subprocess.run(["bash", "-c",
                               f"cd {shlex.quote(str(self.root))} && source build/envsetup.sh >/dev/null && "
                               f"lunch {shlex.quote(self.lunch(target))} >/dev/null && get_build_var PRODUCT_OUT"],
                              env=util.merge_env(self.env(target)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
        value = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if not value:
            tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
            raise RuntimeError(f"Could not determine PRODUCT_OUT for {self.lunch(target)} "
                               f"(lunch in {self.root} failed):\n{tail}")
        p = Path(value) if Path(value).is_absolute() else self.root / value
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(str(p))
        return p

    def module_info_path(self, target: str) -> Path:
        return self.product_out(target) / "module-info.json"

    def jdeps_path(self, target: str) -> Path:
        return self.out_dir(target) / "soong" / "module_bp_java_deps.json"

    def ensure_metadata(self, target: str, refresh: bool = False) -> None:
        if refresh or not self.module_info_path(target).exists() or not self.jdeps_path(target).exists():
            util.info(f"generating Soong metadata for {self.lunch(target)} (m module-info) ...")
            log = self.cfg.workdir / "logs" / f"module-info-{util.slug(target)}.log"
            with self.lock(target):
                res = self.m(target, ["module-info"], log)
                if not res.ok and "unknown target 'module-info'" in res.tail(40):
                    # Some vendor trees drop the phony target; the file's own rule still exists
                    # (and in newer trees Soong writes it during analysis, so `m nothing` suffices).
                    rel = self.module_info_path(target).relative_to(self.root)
                    util.info(f"no `module-info` target in this tree; building {rel} directly")
                    res = self.m(target, [str(rel)], log, append=True)
                    if not res.ok and "unknown target" in res.tail(40):
                        res = self.m(target, ["nothing"], log, append=True)
                        if res.ok and not self.module_info_path(target).exists():
                            raise RuntimeError(f"this tree produces no {rel}; appci needs module-info.json "
                                               f"(see {log})")
            if not res.ok:
                raise RuntimeError(f"m module-info failed, see {log}\n{res.tail(30)}")
            _load_json.cache_clear()
            _types_filled.clear()

    def module_info(self, target: str) -> Dict[str, dict]:
        return _load_json(str(self.module_info_path(target)))

    def jdeps(self, target: str) -> Dict[str, dict]:
        path = str(self.jdeps_path(target))
        data = _load_json(path)
        if path not in _types_filled:
            _types_filled.add(path)
            if data and (not all(i.get("module_type") for i in data.values())
                         or not all(i.get("path") for i in data.values())):
                self._complete_jdeps(target, path, data)
        return data

    # Keys appci completes when Soong leaves them out (Android 14 and older): the module type is
    # not recorded at all; device modules carry no path and no srcs.
    _COMPLETED = ("path", "module_type", "srcs")

    def _complete_jdeps(self, target: str, path: str, data: Dict[str, dict]) -> None:
        """Fill path (from module-info.json), module_type (Android.bp, else module-info class) and
        srcs (Android.bp globs) where Soong wrote none; cached against the jdeps file's mtime."""
        from . import blueprint
        cache = self.cfg.workdir / "cache" / f"jdeps_completion.{util.slug(self.lunch(target))}.json"
        mtime = os.path.getmtime(path)
        cached = util.read_json(cache, {}) or {}
        if cached.get("jdeps_mtime") == mtime and isinstance(cached.get("modules"), dict):
            for name, extra in cached["modules"].items():
                if name in data:
                    for k, v in extra.items():
                        data[name].setdefault(k, v)
            return
        util.info("Soong wrote incomplete module data (Android 14 or older); completing it from "
                  "module-info.json and Android.bp files ...")
        minfo = self.module_info(target)
        missing = {name: [k for k in self._COMPLETED if not info.get(k)] for name, info in data.items()}
        n_paths = 0
        for name, info in data.items():
            if not info.get("path"):
                mp = (minfo.get(name) or {}).get("path") or []
                if mp:
                    info["path"] = [mp[0]]
                    n_paths += 1
        n_types = blueprint.fill_module_types(self.root, data)
        for name, info in data.items():           # last resort: the Make class
            if info.get("module_type"):
                continue
            mi = minfo.get(name) or {}
            cls = (mi.get("class") or [""])[0]
            suites = [s for s in mi.get("compatibility_suites") or [] if s != "null-suite"]
            is_test = bool(mi.get("test_config")) or bool(suites) or True in (mi.get("auto_test_config") or [])
            host = any("/host/" in p for p in mi.get("installed") or [])
            if cls == "APPS":
                info["module_type"] = "android_test" if is_test else "android_app"
            elif cls == "JAVA_LIBRARIES":
                info["module_type"] = "java_test_host" if is_test and host else "java_library"
            else:
                continue
            n_types += 1
        n_srcs = blueprint.fill_module_srcs(self.root, data)
        util.info(f"completed {len(data)} modules: path for {n_paths}, type for {n_types}, sources for {n_srcs}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        added = {name: {k: data[name][k] for k in keys if data[name].get(k)} for name, keys in missing.items()}
        util.write_json(cache, {"jdeps_mtime": mtime, "modules": {n: e for n, e in added.items() if e}})

    def intermediates(self, target: str, module_path: str, module: str) -> Path:
        return self.out_dir(target) / "soong" / ".intermediates" / module_path / module

    def host_out(self, target: str) -> Path:
        return self.out_dir(target) / "host" / "linux-x86"

    def build_var(self, target: str, name: str, max_age: float = 12 * 3600) -> str:
        """`get_build_var` for the target's lunch combo, cached in the work dir."""
        cache = self.cfg.workdir / "cache" / f"{util.slug(self.lunch(target))}.{name}.txt"
        if cache.exists() and time.time() - cache.stat().st_mtime < max_age:
            return cache.read_text()
        out = util.capture(["bash", "-c",
                            f"cd {shlex.quote(str(self.root))} && source build/envsetup.sh >/dev/null && "
                            f"lunch {shlex.quote(self.lunch(target))} >/dev/null && get_build_var {name}"],
                           env=self.env(target))
        value = out.strip().splitlines()[-1] if out.strip() else ""
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(value)
        return value

    def product_packages(self, target: str) -> set:
        """Modules the lunch target's product installs (PRODUCT_PACKAGES, inheritance resolved)."""
        return set(self.build_var(target, "PRODUCT_PACKAGES").split())


@lru_cache(maxsize=8)
def _load_json(path: str) -> Dict[str, dict]:
    with open(path) as f:
        return json.load(f)

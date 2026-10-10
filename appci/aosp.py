"""Thin layer over the AOSP build: lunch environments, `m`, `atest` and Soong metadata."""
from __future__ import annotations

import json
import shlex
import time
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import util
from .config import Config


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
        env.update({k: str(v) for k, v in (t.get("env") or {}).items()})
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
        res = util.capture(["bash", "-c",
                            f"cd {shlex.quote(str(self.root))} && source build/envsetup.sh >/dev/null && "
                            f"lunch {shlex.quote(self.lunch(target))} >/dev/null && get_build_var PRODUCT_OUT"],
                           env=self.env(target))
        value = res.strip().splitlines()[-1] if res.strip() else ""
        if not value:
            raise RuntimeError(f"Could not determine PRODUCT_OUT for {self.lunch(target)}")
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
            if not res.ok:
                raise RuntimeError(f"m module-info failed, see {log}\n{res.tail(30)}")
            _load_json.cache_clear()

    def module_info(self, target: str) -> Dict[str, dict]:
        return _load_json(str(self.module_info_path(target)))

    def jdeps(self, target: str) -> Dict[str, dict]:
        return _load_json(str(self.jdeps_path(target)))

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

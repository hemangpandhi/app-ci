"""Change impact analysis: which tests (and which other apps) a change can affect.

Rules, in order (the first that applies wins):
  1. No known change (no MR base / --changed-since)            -> full run
  2. A `impact.full_run_labels` label on the MR (e.g. full-ci)  -> full run
  3. Only ignorable files changed (docs, OWNERS, ...)           -> no tests
  4. A fail-safe file changed (Android.bp, manifest, TEST_MAPPING, flags, proguard, ...)
     or a file that no Soong module owns                       -> full run
  5. Otherwise select at module granularity:
       * production / test-support code changed -> every Robolectric+host suite of the app
       * only test code changed                 -> just the test modules that changed
       * device tests: TEST_MAPPING groups of the touched directories (incl. imports) plus
         device-test modules whose own sources changed; all of them with a device-test label
       * a library module changed               -> dependent apps (reverse Soong deps) are
         built and, with impact.dependents=test, their unit tests run too
Selection is never finer than a test module: class-level selection misses resources,
reflection, manifests and dependency injection.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from . import util
from .discovery import TestModule, discover_app, scan_apps

LIB_TYPES = {"android_library", "java_library", "java_library_static", "java_sdk_library",
             "android_library_import", "java_import"}
COMMENT_RE = re.compile(r"^\s*//.*$", re.M)


@dataclass
class Impact:
    mode: str                                   # full | selected | none
    reasons: List[str] = field(default_factory=list)
    tests: Dict[str, List[str]] = field(default_factory=dict)          # kind -> own test modules
    external_tests: List[dict] = field(default_factory=list)           # TEST_MAPPING tests elsewhere
    changed_modules: List[str] = field(default_factory=list)
    changed_libraries: List[str] = field(default_factory=list)
    dependents: List[dict] = field(default_factory=list)               # {path, slug, modules, tests}
    dependents_truncated: int = 0
    all_device_tests: bool = False

    def to_dict(self):
        return asdict(self)


def _match(path: str, rel: str, patterns: Iterable[str]) -> Optional[str]:
    name = Path(path).name
    for pat in patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
            return pat
    return None


def _owner_index(ctx) -> Dict[str, Set[str]]:
    """file -> owning modules, from Soong's srcs/resource/asset/manifest data."""
    jdeps = ctx.tree.jdeps(ctx.cfg.stage_cfg("build", ctx.entry)["target"])
    idx: Dict[str, Set[str]] = {}
    for m in ctx.profile.module_paths:
        info = jdeps.get(m) or {}
        for s in info.get("srcs") or []:
            idx.setdefault(s, set()).add(m)
        man = info.get("manifest")
        if isinstance(man, str):
            idx.setdefault(man, set()).add(m)
        for key in ("resource_dirs", "asset_dirs"):
            for d in info.get(key) or []:
                idx.setdefault(d.rstrip("/") + "/", set()).add(m)
    return idx


def _owners(path: str, idx: Dict[str, Set[str]], module_paths: Dict[str, str]) -> Set[str]:
    if path in idx:
        return idx[path]
    owners: Set[str] = set()
    for key, mods in idx.items():
        if key.endswith("/") and path.startswith(key):
            owners |= mods
    if owners:
        return owners
    # Fall back to the module(s) whose Android.bp directory is the closest ancestor.
    best = ""
    for p in module_paths.values():
        if (path.startswith(p + "/")) and len(p) > len(best):
            best = p
    return {m for m, p in module_paths.items() if p == best} if best else set()


def _load_test_mapping(path: Path) -> dict:
    try:
        return json.loads(COMMENT_RE.sub("", path.read_text(errors="replace")))
    except (OSError, ValueError):
        util.warn(f"cannot parse {path}")
        return {}


def test_mapping_tests(root: Path, changed: Iterable[str], groups: List[str]) -> Set[str]:
    """Tests of matching groups in every TEST_MAPPING from the changed files up to the tree root."""
    seen_files: Set[Path] = set()
    names: Set[str] = set()

    def visit(mapping_file: Path, depth: int = 0) -> None:
        if mapping_file in seen_files or depth > 5 or not mapping_file.is_file():
            return
        seen_files.add(mapping_file)
        data = _load_test_mapping(mapping_file)
        for group, entries in data.items():
            if group == "imports":
                for imp in entries or []:
                    if imp.get("path"):
                        visit(root / imp["path"] / "TEST_MAPPING", depth + 1)
                continue
            if any(fnmatch.fnmatch(group, g) for g in groups):
                for e in entries or []:
                    if isinstance(e, dict) and e.get("name"):
                        names.add(e["name"].split(":")[0])

    dirs: Set[Path] = set()
    for f in changed:
        d = (root / f).parent
        while True:
            if d in dirs:
                break
            dirs.add(d)
            if d == root or root not in d.parents:
                break
            d = d.parent
    for d in sorted(dirs):
        visit(d / "TEST_MAPPING")
    return names


def _kind_of(name: str, minfo: dict) -> Optional[str]:
    info = minfo.get(name)
    if not info:
        return None
    suites = set(info.get("compatibility_suites") or [])
    if "robolectric-tests" in suites:
        return "robolectric"
    if "APPS" in (info.get("class") or []) and info.get("test_config"):
        return "device"
    if info.get("test_config") or info.get("is_unit_test"):
        variants = info.get("supported_variants") or []
        return "host" if "HOST" in variants and "DEVICE" not in variants else "device"
    return None


def _reverse_deps(jdeps: dict) -> Dict[str, Set[str]]:
    rdeps: Dict[str, Set[str]] = {}
    for m, info in jdeps.items():
        for d in set((info.get("dependencies") or []) + (info.get("static_libs") or []) + (info.get("libs") or [])):
            rdeps.setdefault(d, set()).add(m)
    return rdeps


def find_dependents(ctx, libraries: List[str]) -> (List[dict], int):
    icfg = ctx.cfg.get("impact") or {}
    target = ctx.cfg.stage_cfg("build", ctx.entry)["target"]
    jdeps = ctx.tree.jdeps(target)
    rdeps = _reverse_deps(jdeps)
    dependents: Set[str] = set()
    stack = list(libraries)
    while stack:
        m = stack.pop()
        for r in rdeps.get(m, ()):
            if r not in dependents:
                dependents.add(r)
                stack.append(r)
    own = ctx.profile.path
    if icfg.get("dependent_candidates") == "scan":
        candidates = [a["path"] for a in scan_apps(ctx.tree, target=target)]
    else:
        candidates = [a["path"].strip("/") for a in ctx.cfg.app_entries()]
    hits = []
    for cand in candidates:
        if cand == own:
            continue
        mods = sorted(m for m in dependents
                      if ((jdeps.get(m) or {}).get("path") or [""])[0] == cand
                      or ((jdeps.get(m) or {}).get("path") or [""])[0].startswith(cand + "/"))
        if mods:
            hits.append((cand, mods))
    limit = int(icfg.get("max_dependent_apps") or 0)
    truncated = max(0, len(hits) - limit) if limit else 0
    out = []
    for cand, mods in hits[: limit or None]:
        prof = discover_app(ctx.tree, cand, target)
        out.append({"path": cand, "slug": prof.slug, "name": prof.name,
                    "modules": prof.prod_modules, "via": mods[:10],
                    "tests": [asdict(t) for t in prof.tests]})
    return out, truncated


def compute_impact(ctx) -> Impact:
    icfg = ctx.cfg.get("impact") or {}
    p = ctx.profile
    cs = ctx.changeset
    if not icfg.get("enabled", True):
        return Impact("full", ["impact analysis disabled (impact.enabled=false)"])
    if cs is None:
        return Impact("full", ["change unknown (no MR diff base or --changed-since): full run"])
    full_labels = {l.lower() for l in icfg.get("full_run_labels") or []}
    if ctx.labels & full_labels:
        return Impact("full", [f"label {sorted(ctx.labels & full_labels)[0]}: full run"], all_device_tests=True)

    app = p.path
    relevant = []
    for f in sorted(cs.files):
        rel = f[len(app) + 1:] if f.startswith(app + "/") else f
        if not _match(f, rel, icfg.get("ignore_patterns") or []):
            relevant.append((f, rel))
    if not relevant:
        return Impact("none", ["only documentation/metadata files changed: no tests needed"])

    reasons: List[str] = []
    for f, rel in relevant:
        pat = _match(f, rel, icfg.get("full_run_patterns") or [])
        if pat:
            reasons.append(f"fail-safe: {rel} matches {pat}")
    idx = _owner_index(ctx)
    owners: Set[str] = set()
    unmapped = []
    for f, rel in relevant:
        o = _owners(f, idx, p.module_paths)
        if o:
            owners |= o
        else:
            unmapped.append(rel)
    if unmapped:
        reasons.append(f"fail-safe: {len(unmapped)} file(s) not owned by any Soong module "
                       f"(e.g. {unmapped[0]})")

    test_names = {t.name: t for t in p.tests}
    prod = set(p.apps) | set(p.libs)
    code_changed = sorted(owners & (prod | set(p.support_modules)))
    imp = Impact("full" if reasons else "selected", reasons, changed_modules=sorted(owners))

    target = ctx.cfg.stage_cfg("build", ctx.entry)["target"]
    jdeps = ctx.tree.jdeps(target)
    imp.changed_libraries = sorted(m for m in owners & prod
                                   if (jdeps.get(m) or {}).get("module_type") in LIB_TYPES)
    if imp.changed_libraries and icfg.get("dependents", "test") != "none":
        imp.dependents, imp.dependents_truncated = find_dependents(ctx, imp.changed_libraries)
        if imp.dependents:
            imp.reasons.append(f"library change ({', '.join(imp.changed_libraries[:3])}) affects "
                               f"{len(imp.dependents)} dependent app(s)")

    device_labels = {l.lower() for l in icfg.get("device_test_labels") or []}
    imp.all_device_tests = bool(ctx.labels & device_labels)
    if imp.mode == "full":
        imp.all_device_tests = True
        return imp

    host_kinds = ("robolectric", "host")
    if code_changed:
        imp.tests["unit"] = [t.name for t in p.tests if t.kind in host_kinds]
        imp.reasons.append(f"app code changed in {', '.join(code_changed[:4])}: all unit-test suites")
    else:
        imp.tests["unit"] = sorted(n for n in owners if n in test_names and test_names[n].kind in host_kinds)
        if imp.tests["unit"]:
            imp.reasons.append("only test code changed: the changed test modules")
    device = {n for n in owners if n in test_names and test_names[n].kind == "device"}
    mapped = test_mapping_tests(ctx.cfg.aosp_root, [f for f, _ in relevant],
                                list(icfg.get("test_mapping_groups") or ["presubmit"]))
    minfo = ctx.tree.module_info(target)
    for name in sorted(mapped):
        if name in test_names:
            t = test_names[name]
            if t.kind == "device":
                device.add(name)
            elif name not in imp.tests["unit"]:
                imp.tests["unit"].append(name)
        elif icfg.get("include_external_mapped_tests", True):
            kind = _kind_of(name, minfo)
            if kind:
                imp.external_tests.append({"name": name, "kind": kind})
    if mapped:
        imp.reasons.append(f"TEST_MAPPING: {', '.join(sorted(mapped)[:5])}")
    imp.tests["device"] = sorted(device)
    return imp


def select_tests(ctx, kinds: Iterable[str]) -> (List[TestModule], str):
    """Tests for a stage given the impact; returns (tests, explanation)."""
    kinds = set(kinds)
    own = [t for t in ctx.profile.tests if t.kind in kinds]
    scfg_sel = (ctx.cfg.get("impact") or {}).get("selection", "impact")
    if scfg_sel == "all":
        return own, "impact.selection=all"
    imp = ctx.impact
    if imp.mode == "none":
        return [], "; ".join(imp.reasons)
    device = kinds == {"device"}
    if imp.mode == "full" or (device and imp.all_device_tests):
        chosen = own
    else:
        names = set(imp.tests.get("device" if device else "unit") or [])
        chosen = [t for t in own if t.name in names]
    have = {t.name for t in chosen}
    for e in imp.external_tests:
        if e["kind"] in kinds and e["name"] not in have:
            chosen.append(TestModule(e["name"], e["kind"], "", None, None))
            have.add(e["name"])
    if (ctx.cfg.get("impact") or {}).get("dependents", "test") == "test" and not device:
        for dep in imp.dependents:
            for t in dep["tests"]:
                if t["kind"] in kinds and t["name"] not in have:
                    chosen.append(TestModule(**t))
                    have.add(t["name"])
    return chosen, "; ".join(imp.reasons) or imp.mode

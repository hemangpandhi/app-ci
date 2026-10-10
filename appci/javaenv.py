"""Classpath, generated sources and compiled classes of an app, derived from Soong outputs."""
from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from .aosp import AospTree
from .discovery import AppProfile

PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
_JDK_CLASH = ("java/lang/Object.class",)


def _pick_variant(jars: List[str], root: Path) -> List[str]:
    existing = [j for j in jars if (root / j).is_file()]
    for variant in ("/android_common/", "/linux_glibc_common/"):
        picked = [j for j in existing if variant in j]
        if picked:
            return picked
    return existing[:1]


def dependency_jars(tree: AospTree, target: str, profile: AppProfile,
                    exclude_core: bool = True) -> List[Path]:
    """Header jars of the transitive dependencies of the app's production modules."""
    jdeps = tree.jdeps(target)
    own = set(profile.module_paths)
    seen: Set[str] = set()
    order: List[str] = []
    stack = list(reversed(profile.prod_modules))
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        if m not in own:
            order.append(m)
        info = jdeps.get(m) or {}
        stack.extend(reversed(info.get("dependencies") or []))
    jars: List[Path] = []
    for m in order:
        for p in module_jars(tree, target, m, jdeps.get(m) or {}):
            if exclude_core and _defines_jdk(p):
                continue
            if p not in jars:
                jars.append(p)
    return jars


def _is_class_jar(p: Path) -> bool:
    return p.suffix == ".jar" and p.name != "R.jar"


def module_jars(tree: AospTree, target: str, module: str, info: dict) -> List[Path]:
    """Best available compiled jars of a dependency, even when jdeps lists outputs never built."""
    listed = [tree.root / j for j in info.get("jars") or []]
    picked = [tree.root / j for j in _pick_variant([str(x.relative_to(tree.root)) for x in listed
                                                    if x.suffix == ".jar"], tree.root)]
    if not any(_is_class_jar(j) for j in picked):
        picked += _probe_module_jars(tree, target, module, info)
    if not any(_is_class_jar(j) for j in picked):
        other = [j for j in listed if _is_class_jar(j) and j.is_file()]
        picked += other[:1]
    if not any(_is_class_jar(j) for j in picked):
        for aar in (j for j in listed if j.suffix == ".aar" and j.is_file()):
            cj = _aar_classes(tree, aar)
            if cj:
                picked.append(cj)
                break
    return picked


def _aar_classes(tree: AospTree, aar: Path) -> Optional[Path]:
    dest = tree.cfg.workdir / "cache" / "aar" / (aar.stem + ".jar")
    if dest.is_file() and dest.stat().st_mtime >= aar.stat().st_mtime:
        return dest
    try:
        with zipfile.ZipFile(aar) as z:
            if "classes.jar" not in z.namelist():
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read("classes.jar"))
            return dest
    except (OSError, zipfile.BadZipFile):
        return None


def _probe_module_jars(tree: AospTree, target: str, module: str, info: dict) -> List[Path]:
    """Header/class jars actually present when jdeps lists outputs that were not built."""
    path = (info.get("path") or [""])[0]
    if not path:
        return []
    base = tree.intermediates(target, path, module) / "android_common"
    for sub in ("turbine-combined", "combined"):
        j = base / sub / f"{module}.jar"
        if j.is_file():
            return [j]
    found = []
    for group in (("turbine", "javac"), ("kotlin_headers", "kotlin")):
        for sub in group:
            j = base / sub / f"{module}.jar"
            if j.is_file():
                found.append(j)
                break
    return found


def _defines_jdk(jar: Path) -> bool:
    """Core-library stubs redefine java.lang; host tools (javadoc, detekt) must not see them."""
    if "core" not in jar.name and "stubs" not in jar.name and "lambda" not in jar.name:
        return False
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
        return any(n in names for n in _JDK_CLASH)
    except (OSError, zipfile.BadZipFile):
        return True


def write_argfile(path: Path, args: List[str]) -> str:
    """javac/javadoc @argfile: one quoted argument per line (titles and paths may contain spaces)."""
    def q(a: str) -> str:
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'
    path.write_text("\n".join(q(a) for a in args) + "\n")
    return f"@{path}"


def generated_sources(tree: AospTree, target: str, profile: AppProfile, dest: Path) -> Path:
    """Unpack srcjars (R, aconfig flags, kapt...) and generated .java files referenced by the app."""
    jdeps = tree.jdeps(target)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for m in profile.prod_modules:
        info = jdeps.get(m) or {}
        for sj in info.get("srcjars") or []:
            p = tree.root / sj
            if p.is_file():
                try:
                    with zipfile.ZipFile(p) as z:
                        z.extractall(dest)
                except zipfile.BadZipFile:
                    pass
        for s in info.get("srcs") or []:
            if s.startswith("out") and s.endswith(".java") and (tree.root / s).is_file():
                src = tree.root / s
                pkg = package_of(src)
                d = dest / pkg.replace(".", "/") if pkg else dest
                d.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, d / src.name)
    return dest


def package_of(path: Path) -> str:
    try:
        head = path.read_text(errors="replace")[:20000]
    except OSError:
        return ""
    m = PACKAGE_RE.search(head)
    return m.group(1) if m else ""


def source_roots(root: Path, files: Iterable[str]) -> List[str]:
    """Package roots (AOSP-relative) for the given source files, e.g. packages/apps/X/src."""
    roots: Dict[str, None] = {}
    for f in files:
        pkg = package_of(root / f)
        parent = str(Path(f).parent)
        suffix = pkg.replace(".", "/")
        if suffix and parent.endswith(suffix):
            r = parent[: -len(suffix)].rstrip("/")
        else:
            r = parent
        roots.setdefault(r or ".", None)
    # Drop roots nested inside other roots.
    out = sorted(roots)
    return [r for r in out if not any(r != o and r.startswith(o + "/") for o in out)]


def module_class_jars(tree: AospTree, target: str, profile: AppProfile, modules: Iterable[str]) -> List[Path]:
    """javac + kotlinc output jars (uninstrumented, unshrunk) of the given modules."""
    jars: List[Path] = []
    for m in modules:
        path = profile.module_paths.get(m)
        if not path:
            continue
        base = tree.intermediates(target, path, m) / "android_common"
        for sub in ("javac", "kotlin"):
            j = base / sub / f"{m}.jar"
            if j.is_file():
                jars.append(j)
    return jars


def jacoco_report_jars(tree: AospTree, target: str, profile: AppProfile, modules: Iterable[str]) -> List[Path]:
    jars: List[Path] = []
    for m in modules:
        path = profile.module_paths.get(m)
        if path:
            jars.extend(sorted((tree.intermediates(target, path, m)).glob("*/jacoco-report-classes/*.jar")))
    return jars


def merge_class_jars(jars: Iterable[Path], dest: Path, include: Optional[Iterable[str]] = None) -> int:
    """Merge jars into one, first occurrence of a class wins (JaCoCo rejects duplicates)."""
    prefixes = tuple(p.replace(".", "/") for p in (include or []))
    seen: Set[str] = set()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for j in jars:
            try:
                with zipfile.ZipFile(j) as z:
                    for n in z.namelist():
                        if not n.endswith(".class") or n in seen or n.startswith("META-INF/"):
                            continue
                        if prefixes and not n.startswith(prefixes):
                            continue
                        seen.add(n)
                        out.writestr(n, z.read(n))
            except (OSError, zipfile.BadZipFile):
                continue
    return len(seen)


def extract_classes(jars: Iterable[Path], dest: Path) -> int:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    count = 0
    seen: Set[str] = set()
    for j in jars:
        with zipfile.ZipFile(j) as z:
            for n in z.namelist():
                if n.endswith(".class") and n not in seen:
                    seen.add(n)
                    z.extract(n, dest)
                    count += 1
    return count


def packages_of_sources(root: Path, files: Iterable[str]) -> List[str]:
    return sorted({p for p in (package_of(root / f) for f in files) if p})

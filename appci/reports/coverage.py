"""Line-level coverage model.

Robolectric and on-device runs execute *different builds* of the same sources
(different R constants, EMMA instrumentation), so their JaCoCo class IDs never
match and .ec files cannot be merged at the execution-data level. Instead each
suite produces its own JaCoCo XML and the suites are merged here per source line.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .. import util

# path (AOSP-relative) -> line -> (covered, branches_total, branches_covered)
LineMap = Dict[str, Dict[int, Tuple[bool, int, int]]]


def _parse_xml(path: Path) -> ET.Element:
    return util.xml_root(path)


def build_source_index(sources: Iterable[str]) -> Dict[str, List[str]]:
    """Map 'File.java' -> candidate AOSP-relative paths."""
    idx: Dict[str, List[str]] = {}
    for s in sources:
        idx.setdefault(Path(s).name, []).append(s)
    return idx


def _resolve(pkg: str, name: str, idx: Dict[str, List[str]]) -> Optional[str]:
    cands = idx.get(name) or []
    suffix = f"{pkg}/{name}" if pkg else name
    for c in cands:
        if c.endswith("/" + suffix) or c == suffix:
            return c
    return None


def parse_jacoco(xml_path: Path, idx: Dict[str, List[str]]) -> LineMap:
    out: LineMap = {}
    root = _parse_xml(xml_path)
    for pkg in root.iter("package"):
        pname = pkg.get("name", "")
        for sf in pkg.findall("sourcefile"):
            path = _resolve(pname, sf.get("name", ""), idx)
            if not path:
                continue  # test code, generated code or other modules
            lines = out.setdefault(path, {})
            for ln in sf.findall("line"):
                nr = int(ln.get("nr"))
                ci, mb, cb = int(ln.get("ci", 0)), int(ln.get("mb", 0)), int(ln.get("cb", 0))
                prev = lines.get(nr, (False, 0, 0))
                lines[nr] = (prev[0] or ci > 0, max(prev[1], mb + cb), max(prev[2], cb))
    return out


def merge(maps: Iterable[LineMap]) -> LineMap:
    out: LineMap = {}
    for m in maps:
        for path, lines in m.items():
            dst = out.setdefault(path, {})
            for nr, (cov, bt, bc) in lines.items():
                p = dst.get(nr, (False, 0, 0))
                dst[nr] = (p[0] or cov, max(p[1], bt), max(p[2], bc))
    return out


def totals(m: LineMap, sources: Optional[Iterable[str]] = None) -> Dict[str, float]:
    lv = lc = bv = bc = 0
    for lines in m.values():
        for cov, bt, bcv in lines.values():
            lv += 1
            lc += cov
            bv += bt
            bc += bcv
    res = {
        "lines_valid": lv, "lines_covered": lc,
        "line": round(100.0 * lc / lv, 2) if lv else 0.0,
        "branches_valid": bv, "branches_covered": bc,
        "branch": round(100.0 * bc / bv, 2) if bv else 0.0,
        "files_with_data": len(m),
    }
    if sources is not None:
        res["files_without_data"] = len([s for s in sources if s not in m])
    return res


def per_file(m: LineMap) -> List[Tuple[str, int, int]]:
    rows = []
    for path, lines in m.items():
        rows.append((path, len(lines), sum(1 for c, _, _ in lines.values() if c)))
    return sorted(rows)


def write_cobertura(m: LineMap, path: Path, path_base: str) -> None:
    t = totals(m)
    root = ET.Element("coverage", {
        "line-rate": f"{t['line'] / 100:.4f}", "branch-rate": f"{t['branch'] / 100:.4f}",
        "lines-covered": str(t["lines_covered"]), "lines-valid": str(t["lines_valid"]),
        "branches-covered": str(t["branches_covered"]), "branches-valid": str(t["branches_valid"]),
        "complexity": "0", "version": "appci", "timestamp": str(int(time.time() * 1000)),
    })
    ET.SubElement(ET.SubElement(root, "sources"), "source").text = "."
    packages = ET.SubElement(root, "packages")
    by_pkg: Dict[str, List[str]] = {}
    base = path_base.rstrip("/") + "/"
    for p in sorted(m):
        by_pkg.setdefault(str(Path(p).parent), []).append(p)
    for pkg, files in by_pkg.items():
        pel = ET.SubElement(packages, "package", name=pkg.replace("/", "."), complexity="0")
        classes = ET.SubElement(pel, "classes")
        pv = pc = 0
        for f in files:
            lines = m[f]
            cov = sum(1 for c, _, _ in lines.values() if c)
            pv += len(lines)
            pc += cov
            rel = f[len(base):] if f.startswith(base) else f
            cel = ET.SubElement(classes, "class", name=Path(f).stem, filename=rel, complexity="0",
                                **{"line-rate": f"{cov / len(lines) if lines else 0:.4f}", "branch-rate": "0"})
            ET.SubElement(cel, "methods")
            lel = ET.SubElement(cel, "lines")
            for nr in sorted(lines):
                c, bt, bc = lines[nr]
                attrs = {"number": str(nr), "hits": "1" if c else "0", "branch": "true" if bt else "false"}
                if bt:
                    attrs["condition-coverage"] = f"{int(100 * bc / bt)}% ({bc}/{bt})"
                ET.SubElement(lel, "line", attrs)
        pel.set("line-rate", f"{pc / pv if pv else 0:.4f}")
        pel.set("branch-rate", "0")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def write_sonar_generic(m: LineMap, path: Path, path_base: str) -> None:
    root = ET.Element("coverage", version="1")
    base = path_base.rstrip("/") + "/"
    for f in sorted(m):
        rel = f[len(base):] if f.startswith(base) else f
        fel = ET.SubElement(root, "file", path=rel)
        for nr in sorted(m[f]):
            c, bt, bc = m[f][nr]
            attrs = {"lineNumber": str(nr), "covered": "true" if c else "false"}
            if bt:
                attrs.update({"branchesToCover": str(bt), "coveredBranches": str(bc)})
            ET.SubElement(fel, "lineToCover", attrs)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

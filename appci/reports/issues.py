"""Finding converters: GitLab Code Quality, SonarQube generic issues, SARIF/checkstyle/lint parsers."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, List

from .. import util
from ..model import Finding

# ----------------------------------------------------------------- parsers

LINT_SEVERITY = {"Fatal": "blocker", "Error": "critical", "Warning": "major", "Information": "info",
                 "Informational": "info", "Ignore": "info"}
CHECKSTYLE_SEVERITY = {"error": "critical", "warning": "major", "info": "minor", "ignore": "info"}
SARIF_SEVERITY = {"error": "critical", "warning": "major", "note": "minor", "none": "info"}


def _relpath(path: str, aosp_root: Path) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(aosp_root.resolve()))
        except ValueError:
            return str(p)
    s = str(path)
    return s[2:] if s.startswith("./") else s


def parse_android_lint(xml_path: Path, aosp_root: Path) -> List[Finding]:
    out: List[Finding] = []
    root = util.xml_root(xml_path)
    for issue in root.iter("issue"):
        loc = issue.find("location")
        out.append(Finding(
            tool="android-lint", rule=issue.get("id", "?"),
            message=issue.get("message", ""),
            path=_relpath(loc.get("file", ""), aosp_root) if loc is not None else "",
            line=int(loc.get("line", 0) or 0) if loc is not None else 0,
            column=int(loc.get("column", 0) or 0) if loc is not None else 0,
            severity=LINT_SEVERITY.get(issue.get("severity", "Warning"), "major"),
            category=issue.get("category", ""),
        ))
    return out


def parse_checkstyle(xml_path: Path, aosp_root: Path, tool: str) -> List[Finding]:
    out: List[Finding] = []
    root = util.xml_root(xml_path)
    for f in root.iter("file"):
        path = _relpath(f.get("name", ""), aosp_root)
        for e in f.iter("error"):
            src = e.get("source", "")
            out.append(Finding(
                tool=tool, rule=src.split(".")[-1] if src else "?",
                message=e.get("message", ""), path=path,
                line=int(e.get("line", 0) or 0), column=int(e.get("column", 0) or 0),
                severity=CHECKSTYLE_SEVERITY.get(e.get("severity", "warning"), "major"),
                category="style",
            ))
    return out


def parse_sarif(sarif_path: Path, aosp_root: Path, tool: str) -> List[Finding]:
    out: List[Finding] = []
    data = json.loads(sarif_path.read_text())
    for run in data.get("runs", []):
        for r in run.get("results", []):
            loc = ((r.get("locations") or [{}])[0]).get("physicalLocation", {})
            uri = loc.get("artifactLocation", {}).get("uri", "")
            if uri.startswith("file://"):
                uri = uri[len("file://"):]
            region = loc.get("region", {})
            rule = r.get("ruleId", "?")
            parts = re.split(r"[./]", rule)   # detekt: "detekt.<ruleset>.<Rule>"
            out.append(Finding(
                tool=tool, rule=parts[-1],
                message=(r.get("message") or {}).get("text", ""),
                path=_relpath(uri, aosp_root),
                line=int(region.get("startLine", 0) or 0), column=int(region.get("startColumn", 0) or 0),
                severity=SARIF_SEVERITY.get(r.get("level", "warning"), "major"),
                category=parts[-2] if len(parts) >= 2 else "",
            ))
    return out


# ---------------------------------------------------------------- writers

def _repo_path(f: Finding, path_base: str) -> str:
    if path_base and f.path.startswith(path_base.rstrip("/") + "/"):
        return f.path[len(path_base.rstrip("/")) + 1:]
    return f.path


def write_gitlab_codequality(findings: Iterable[Finding], path: Path, path_base: str = "") -> None:
    """GitLab Code Quality report; `path_base` is stripped so paths are repo-relative."""
    items = []
    for f in findings:
        items.append({
            "description": f"[{f.tool}] {f.rule}: {f.message}"[:1000],
            "check_name": f"{f.tool}:{f.rule}",
            "fingerprint": f.fingerprint(),
            "severity": f.severity,
            "location": {"path": _repo_path(f, path_base), "lines": {"begin": max(f.line, 1)}},
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=1))


SONAR_SEVERITY = {"info": "INFO", "minor": "MINOR", "major": "MAJOR", "critical": "CRITICAL", "blocker": "BLOCKER"}
SONAR_IMPACT = {"info": "INFO", "minor": "LOW", "major": "MEDIUM", "critical": "HIGH", "blocker": "BLOCKER"}


def _sonar_type(f: Finding) -> str:
    return "BUG" if f.tool == "android-lint" and f.category in ("Correctness", "Security") and f.is_error \
        else "CODE_SMELL"


def write_sonar_generic_issues(findings: Iterable[Finding], path: Path, path_base: str,
                               legacy: bool = False) -> None:
    """SonarQube external issues; paths relative to sonar.projectBaseDir.

    Default is the current format (rules + impacts, SonarQube 10.3+); `legacy` writes the
    pre-10.3 format for older servers.
    """
    issues, rules = [], {}
    for f in findings:
        if not f.path.startswith(path_base.rstrip("/") + "/"):
            continue
        loc = {"message": f.message[:4000] or f.rule, "filePath": _repo_path(f, path_base)}
        if f.line > 0:
            loc["textRange"] = {"startLine": f.line}
        if legacy:
            issues.append({"engineId": f.tool, "ruleId": f.rule, "severity": SONAR_SEVERITY.get(f.severity, "MAJOR"),
                           "type": _sonar_type(f), "primaryLocation": loc})
            continue
        rid = f"{f.tool}:{f.rule}"
        rules.setdefault(rid, {
            "id": rid, "name": f"{f.tool} {f.rule}", "engineId": f.tool,
            "cleanCodeAttribute": "LOGICAL" if _sonar_type(f) == "BUG" else "CONVENTIONAL",
            "impacts": [{"softwareQuality": "RELIABILITY" if _sonar_type(f) == "BUG" else "MAINTAINABILITY",
                         "severity": SONAR_IMPACT.get(f.severity, "MEDIUM")}],
        })
        issues.append({"ruleId": rid, "primaryLocation": loc})
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"issues": issues} if legacy else {"rules": list(rules.values()), "issues": issues}
    path.write_text(json.dumps(doc, indent=1))

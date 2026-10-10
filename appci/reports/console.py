"""Readable stage results for CI job logs: metrics, gate violations, the findings themselves and
failed tests, so a GitLab job page shows what was found without downloading artifacts."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List

from .. import util
from ..model import Finding, StageResult, is_new, load_findings

FINDING_STAGES = ("preupload", "risk", "lint", "checkstyle", "ktlint", "detekt")
SEVERITY_ORDER = {"blocker": 0, "critical": 1, "major": 2, "minor": 3, "info": 4}


def _metrics_line(r: StageResult) -> str:
    return ", ".join(f"{k}={v}" for k, v in r.metrics.items() if not isinstance(v, (dict, list)))


def _findings(ctx, stage: str, limit: int) -> List[str]:
    path = ctx.findings_path(stage)
    if not path.exists():
        return []
    findings: List[Finding] = load_findings(path)
    if not findings:
        return []
    changed = ctx.changed or {}
    new = [f for f in findings if changed and is_new(f, changed)]
    rest = [f for f in findings if f not in new]
    rest.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 5), f.path, f.line))
    out = [f"    findings: {len(findings)} total, {len(new)} on changed lines"
           + (f" (showing {min(limit, len(findings))})" if len(findings) > limit else "")]
    for f in (new + rest)[:limit]:
        tag = "NEW " if f in new else "    "
        loc = f"{f.path}:{f.line}" if f.line else f.path
        msg = " ".join(f.message.split())[:160]
        out.append(f"    {tag}{f.severity:<8} {loc}  [{f.rule}] {msg}")
    return out


def _failed_tests(ctx, stage: str, limit: int) -> List[str]:
    junit = ctx.app_dir / stage / "junit.xml"
    if not junit.exists():
        return []
    try:
        root = util.xml_root(junit)
    except ET.ParseError:
        return []
    failed = []
    for case in root.iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is not None:
            text = " ".join((bad.get("message") or bad.text or "").split())[:200]
            failed.append(f"    FAIL {case.get('classname', '')}#{case.get('name', '')}  {text}")
    if not failed:
        return []
    return [f"    failed tests: {len(failed)}"] + failed[:limit]


def print_details(ctx, results: List[StageResult], limit: int = 40) -> None:
    for r in results:
        print(f"\n== {r.stage}: {r.status.upper()} - {r.summary}")
        m = _metrics_line(r)
        if m:
            print(f"    metrics: {m}")
        for v in r.gate_violations:
            print(f"    GATE FAILED: {v}")
        lines = []
        if r.stage in FINDING_STAGES:
            lines = _findings(ctx, r.stage, limit)
        elif r.stage in ("unit_test", "device_test"):
            lines = _failed_tests(ctx, r.stage, limit)
        for line in lines:
            print(line)
        for label, path in r.reports.items():
            if "://" in path:
                print(f"    {label}: {path}")
            else:
                full = Path(ctx.app_dir, path).resolve()
                try:                    # relative to the job directory = path in the artifacts browser
                    full = full.relative_to(Path.cwd().resolve())
                except ValueError:
                    pass
                print(f"    {label}: {full}")

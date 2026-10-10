"""Result model shared by every stage, the dashboard and the CI integration."""
from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

PASSED, WARNING, FAILED, ERROR, SKIPPED = "passed", "warning", "failed", "error", "skipped"
STATUS_ORDER = {PASSED: 0, SKIPPED: 1, WARNING: 2, FAILED: 3, ERROR: 4}

# Normalised severities (GitLab Code Quality vocabulary).
SEVERITIES = ("info", "minor", "major", "critical", "blocker")


@dataclass
class Finding:
    tool: str
    rule: str
    message: str
    path: str            # relative to the AOSP root
    line: int = 0
    column: int = 0
    severity: str = "major"
    category: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity in ("critical", "blocker")

    def fingerprint(self) -> str:
        raw = f"{self.tool}|{self.rule}|{self.path}|{self.line}|{self.message}"
        return hashlib.md5(raw.encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StageResult:
    stage: str
    app: str
    status: str = PASSED
    summary: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    reports: Dict[str, str] = field(default_factory=dict)   # label -> path relative to app report dir
    gate_violations: List[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    duration: float = 0.0
    log: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StageResult":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def worst(statuses) -> str:
    statuses = list(statuses) or [SKIPPED]
    return max(statuses, key=lambda s: STATUS_ORDER.get(s, 0))


def is_new(f: Finding, changed: Dict[str, Set[int]]) -> bool:
    """A finding is 'new' when it sits on a line added/modified by the change under test."""
    lines = changed.get(f.path)
    return lines is not None and (f.line in lines or f.line <= 0)


def finding_metrics(findings: List[Finding], changed: Optional[Dict[str, Set[int]]]) -> Dict[str, int]:
    m = {
        "total": len(findings),
        "errors": sum(1 for f in findings if f.is_error),
        "warnings": sum(1 for f in findings if not f.is_error),
    }
    if changed is not None:
        new = [f for f in findings if is_new(f, changed)]
        m.update({
            "new_total": len(new),
            "new_errors": sum(1 for f in new if f.is_error),
            "new_warnings": sum(1 for f in new if not f.is_error),
        })
    return m


def evaluate_gate(gate: Dict[str, Any], metrics: Dict[str, Any]) -> List[str]:
    """`max_<metric>` / `min_<metric>` thresholds; metrics that are absent are not gated."""
    violations = []
    for key, limit in (gate or {}).items():
        if limit is None:
            continue
        if key.startswith("max_"):
            name = key[4:]
            if name in metrics and metrics[name] is not None and metrics[name] > limit:
                violations.append(f"{name}={metrics[name]} > {limit}")
        elif key.startswith("min_"):
            name = key[4:]
            if name in metrics and metrics[name] is not None and metrics[name] < limit:
                violations.append(f"{name}={metrics[name]} < {limit}")
    return violations


def save_findings(path: Path, findings: List[Finding]) -> None:
    from .util import write_json
    write_json(path, [f.to_dict() for f in findings])


def load_findings(path: Path) -> List[Finding]:
    from .util import read_json
    return [Finding(**d) for d in read_json(path, []) or []]

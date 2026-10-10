"""Per-app run context handed to every stage."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from . import util
from .aosp import AospTree
from .config import Config
from .discovery import AppProfile, discover_app
from .model import Finding, StageResult

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class RunContext:
    cfg: Config
    tree: AospTree
    profile: AppProfile
    entry: dict
    report_root: Path                      # holds one sub-directory per app
    changed: Optional[Dict[str, Set[int]]] = None
    ci: bool = False
    results: Dict[str, StageResult] = field(default_factory=dict)
    changeset: Optional["ChangeSet"] = None
    labels: Set[str] = field(default_factory=set)
    _impact: Optional[object] = None

    @property
    def impact(self):
        """Impact analysis of the change (computed once, shared by build/test stages)."""
        if self._impact is None:
            from .impact import compute_impact
            self._impact = compute_impact(self)
            util.write_json(self.app_dir / "impact.json", self._impact.to_dict())
        return self._impact

    @property
    def app_dir(self) -> Path:
        return self.report_root / self.profile.slug

    def stage_dir(self, stage: str, clean: bool = False) -> Path:
        d = self.app_dir / stage
        if clean and d.exists():
            import shutil
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def stage_cfg(self, stage: str) -> dict:
        return self.cfg.stage_cfg(stage, self.entry)

    def app_abs(self) -> Path:
        return self.cfg.aosp_root / self.profile.path

    def rel_report(self, path: Path) -> str:
        return util.rel(path, self.app_dir)

    def findings_path(self, stage: str) -> Path:
        return self.app_dir / stage / "findings.json"

    def all_findings(self) -> List[Finding]:
        from .model import load_findings
        out: List[Finding] = []
        for stage in ("lint", "checkstyle", "ktlint", "detekt", "preupload", "risk"):
            p = self.findings_path(stage)
            if p.exists():
                out.extend(load_findings(p))
        return out


@dataclass
class ChangeSet:
    """The change under test: base commit, touched files and added/modified lines."""
    base: str                                   # resolved merge base
    files: Dict[str, str]                       # AOSP-relative path -> git status (A/M/D/R/C/T)
    lines: Dict[str, Set[int]]                  # AOSP-relative path -> added/modified line numbers
    commits: List[str] = field(default_factory=list)   # base..HEAD, oldest first
    includes_worktree: bool = True              # uncommitted edits are part of the diff

    def to_dict(self) -> dict:
        return {"base": self.base, "files": self.files, "commits": self.commits,
                "lines": {k: sorted(v) for k, v in self.lines.items()}}


def _ensure_commit(repo: Path, ref: str) -> None:
    if util.capture(["git", "cat-file", "-t", ref], cwd=repo).strip() == "commit":
        return
    # Shallow trees (repo sync --depth=1): fetch just that commit from the project's remote.
    remote = (util.capture(["git", "remote"], cwd=repo).split() or ["origin"])[0]
    util.capture(["git", "fetch", "--quiet", "--depth=1", remote, ref], cwd=repo)
    if util.capture(["git", "cat-file", "-t", ref], cwd=repo).strip() != "commit":
        raise RuntimeError(f"base {ref} not available in {repo} (and could not be fetched)")


def compute_changeset(repo: Path, base: str, prefix: str) -> ChangeSet:
    """Diff the working tree of `repo` against `base` (MR diff base, upstream ref, ...)."""
    _ensure_commit(repo, base)
    # MR diff bases are already merge bases; for other refs use merge-base when history allows.
    merge_base = util.capture(["git", "merge-base", base, "HEAD"], cwd=repo).strip() or \
        util.capture(["git", "rev-parse", base], cwd=repo).strip()
    files: Dict[str, str] = {}
    for line in util.capture(["git", "diff", "--name-status", "-M", merge_base], cwd=repo).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        for p in parts[1:]:                     # renames list old and new path
            files[f"{prefix}/{p}"] = status
    for p in util.capture(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo).splitlines():
        files.setdefault(f"{prefix}/{p}", "A")
    diff = util.capture(["git", "diff", "--no-color", "-U0", "--no-renames", merge_base], cwd=repo)
    lines: Dict[str, Set[int]] = {}
    current: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            current = None if p == "/dev/null" else f"{prefix}/{p[2:] if p.startswith('b/') else p}"
            if current:
                lines.setdefault(current, set())
        elif current and line.startswith("@@"):
            m = HUNK_RE.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                lines[current].update(range(start, start + count))
    commits = util.capture(["git", "rev-list", "--reverse", "--no-merges", f"{merge_base}..HEAD"],
                           cwd=repo).split()
    return ChangeSet(merge_base, files, lines, commits)


def git_changed_lines(repo: Path, base: str, prefix: str) -> Dict[str, Set[int]]:
    """Lines added or modified between `base` and the working tree, keyed by AOSP-relative path."""
    return compute_changeset(repo, base, prefix).lines


def detect_changes(cfg: Config, app_path: str, since: Optional[str], ci: bool = False) -> Optional[ChangeSet]:
    base = since or os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
    if not base:
        return None
    repo = cfg.aosp_root / app_path
    try:
        cs = compute_changeset(repo, base, app_path)
    except Exception as exc:
        # Without the change every new_* gate is silently off, so CI must fail rather than pass.
        if ci:
            raise RuntimeError(f"cannot compute the change vs {base} in {repo}: {exc}. "
                               "Changed-line gates need it; check the checkout and git access "
                               "(container user vs tree owner: safe.directory)") from exc
        util.warn(f"cannot compute the change vs {base}: {exc}; running without change awareness")
        return None
    # Untracked files count as fully new.
    for p, st in cs.files.items():
        if st == "A" and p not in cs.lines:
            f = cfg.aosp_root / p
            if f.is_file():
                try:
                    cs.lines[p] = set(range(1, f.read_text(errors="replace").count("\n") + 2))
                except OSError:
                    pass
    return cs


def mr_labels() -> Set[str]:
    raw = os.environ.get("APPCI_LABELS") or os.environ.get("CI_MERGE_REQUEST_LABELS") or ""
    return {l.strip().lower() for l in raw.split(",") if l.strip()}


def make_context(cfg: Config, tree: AospTree, app_path: str, report_root: Path,
                 since: Optional[str] = None, ci: bool = False, slug: Optional[str] = None) -> RunContext:
    entry = dict(cfg.app_entry(app_path))
    if slug:
        entry["slug"] = slug
    app_path = entry["path"].strip("/")
    target = cfg.stage_cfg("build", entry).get("target", "device")
    profile = discover_app(tree, app_path, target, entry)
    cs = detect_changes(cfg, app_path, since, ci)
    ctx = RunContext(cfg, tree, profile, entry, report_root, cs.lines if cs else None, ci)
    ctx.changeset = cs
    ctx.labels = mr_labels()
    ctx.app_dir.mkdir(parents=True, exist_ok=True)
    util.write_json(ctx.app_dir / "profile.json", profile.to_dict())
    if cs is not None:
        util.write_json(ctx.app_dir / "changeset.json", cs.to_dict())
    # Reload results of stages that already ran (e.g. earlier CI jobs whose artifacts we received).
    for f in (ctx.app_dir / "results").glob("*.json"):
        d = util.read_json(f)
        if d:
            ctx.results[d["stage"]] = StageResult.from_dict(d)
    return ctx

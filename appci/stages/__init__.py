"""Stage registry and the pipeline runner."""
from __future__ import annotations

import time
import traceback
from typing import Dict, List, Type

from .. import util
from ..context import RunContext
from ..model import ERROR, FAILED, PASSED, SKIPPED, WARNING, StageResult, evaluate_gate


class Stage:
    name = "stage"
    title = "Stage"
    # Stages whose failure makes this stage pointless.
    requires: List[str] = []

    def run(self, ctx: RunContext, res: StageResult) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # Helpers -----------------------------------------------------------------
    @staticmethod
    def apply_gate(ctx: RunContext, res: StageResult, issues_are_warning: bool = True) -> None:
        gate = ctx.stage_cfg(res.stage).get("gate") or {}
        res.gate_violations = evaluate_gate(gate, res.metrics)
        if res.gate_violations:
            res.status = FAILED
        elif issues_are_warning and res.metrics.get("total"):
            res.status = WARNING


REGISTRY: Dict[str, Type[Stage]] = {}


def register(cls: Type[Stage]) -> Type[Stage]:
    REGISTRY[cls.name] = cls
    return cls


def _load_all() -> None:
    from . import build, lint, static, tests, coverage, docs, sonar, image, preupload, risk, deploy  # noqa: F401


def all_stage_names() -> List[str]:
    _load_all()
    return list(REGISTRY)


def run_stage(ctx: RunContext, name: str) -> StageResult:
    _load_all()
    stage = REGISTRY[name]()
    res = StageResult(stage=name, app=ctx.profile.slug)
    for dep in stage.requires:
        prev = ctx.results.get(dep)
        if prev and prev.status in (FAILED, ERROR):
            res.status = SKIPPED
            res.summary = f"skipped: required stage '{dep}' {prev.status}"
            _finish(ctx, res)
            return res
    util.info(f"[{ctx.profile.slug}] ▶ {stage.title}")
    start = time.time()
    try:
        stage.run(ctx, res)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # stage crashed: record and continue with the next stage
        res.status = ERROR
        res.summary = f"{type(exc).__name__}: {exc}"
        tb = ctx.stage_dir(name) / "appci-error.txt"
        tb.write_text(traceback.format_exc())
        res.reports.setdefault("traceback", ctx.rel_report(tb))
    res.duration = time.time() - start
    _finish(ctx, res)
    return res


def _finish(ctx: RunContext, res: StageResult) -> None:
    ctx.results[res.stage] = res
    util.write_json(ctx.app_dir / "results" / f"{res.stage}.json", res.to_dict())
    line = f"[{ctx.profile.slug}] {res.stage}: {res.status.upper()} ({util.fmt_duration(res.duration)}) {res.summary}"
    if res.gate_violations:
        line += f" | gate: {'; '.join(res.gate_violations)}"
    {PASSED: util.ok, SKIPPED: util.info, WARNING: util.warn}.get(res.status, util.error)(line)
    if res.status == ERROR and res.log and not util.is_verbose():
        try:
            tail = "\n".join(open(ctx.app_dir / res.log, errors="replace").read().splitlines()[-40:])
            print(util.strip_ansi(tail))
        except OSError:
            pass


def run_pipeline(ctx: RunContext, stages: List[str], fail_fast: bool = False) -> List[StageResult]:
    out = []
    for s in stages:
        r = run_stage(ctx, s)
        out.append(r)
        if fail_fast and r.status in (FAILED, ERROR):
            util.error(f"[{ctx.profile.slug}] stopping after '{s}' (--fail-fast)")
            break
    return out

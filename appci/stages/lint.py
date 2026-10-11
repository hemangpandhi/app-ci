"""Android Lint through Soong's per-module `<module>-lint` targets (honours lint baselines).

Soong added the `<module>-lint` phony target in January 2024, so Android 14 and older trees do
not have it; the report's own build rule exists there since Android 11, so appci then asks for
the file (out/soong/.intermediates/<path>/<module>/android_common/lint/lint-report.xml).
"""
from __future__ import annotations

import re
import shutil

from .. import util
from ..model import ERROR, FAILED, finding_metrics, save_findings
from ..reports.html import relocate_links
from ..reports.issues import parse_android_lint
from . import Stage, register
from .build import build_error_lines, diagnose

UNKNOWN_RE = re.compile(r"unknown target '([^']+)'")


def _report_xml(ctx, target: str, module: str):
    p = ctx.profile
    return ctx.tree.intermediates(target, p.module_paths.get(module, p.path), module) / "android_common" / "lint" / "lint-report.xml"


def _report_target(ctx, target: str, module: str) -> str:
    """The lint report as a ninja target (relative to the tree when the out dir is inside it)."""
    xml = _report_xml(ctx, target, module)
    try:
        return str(xml.relative_to(ctx.cfg.aosp_root))
    except ValueError:
        return str(xml)


@register
class LintStage(Stage):
    name = "lint"
    title = "Android Lint"
    requires = ["build"]       # lint needs the module's classes; skip when this run's build failed

    def run(self, ctx, res):
        scfg = ctx.stage_cfg("lint")
        target = scfg["target"]
        p = ctx.profile
        modules = list(scfg.get("modules") or p.prod_modules)
        d = ctx.stage_dir("lint", clean=True)
        log = d / "lint.log"
        res.log = ctx.rel_report(log)
        wanted = {m: f"{m}-lint" for m in modules}          # module -> ninja target
        no_rule = []
        with ctx.tree.lock(target):
            # ninja reports only the first unknown target, so this may take a few rounds: a missing
            # phony target becomes a request for the report file; a missing report rule drops the
            # module (lint disabled for it in Android.bp).
            for attempt in range(2 * len(modules) + 1):
                cmd = ctx.tree.m(target, list(wanted.values()), log, timeout=scfg.get("timeout"),
                                 append=attempt > 0)
                if cmd.ok:
                    break
                unknown = set(UNKNOWN_RE.findall(cmd.output()))
                hit = [m for m, t in wanted.items() if t in unknown]
                if not hit:
                    break
                for m in hit:
                    if wanted[m] == f"{m}-lint":
                        if not any(not t.endswith("-lint") for t in wanted.values()):
                            util.info(f"no `{m}-lint` target in this tree (Soong before 2024); "
                                      "building the lint report file directly")
                        wanted[m] = _report_target(ctx, target, m)
                    else:
                        no_rule.append(m)
                        del wanted[m]
                if not wanted:
                    break
        modules = list(wanted)
        if not modules:
            res.status = ERROR
            res.summary = f"Soong has no lint rule for {', '.join(no_rule)} (lint disabled in Android.bp?)"
            return
        if not cmd.ok:
            errs = build_error_lines(cmd.output())
            hint = diagnose(cmd.output())
            if hint:
                errs.insert(0, hint)
            res.status = FAILED
            res.summary = errs[0][:200] if errs else f"lint build failed ({cmd.returncode})"
            return
        findings, seen = [], set()
        for m in modules:
            xml = _report_xml(ctx, target, m)
            base = xml.parent
            if not xml.is_file():
                continue
            shutil.copy2(xml, d / f"{m}.xml")
            if (base / "lint-report.html").is_file():
                text = (base / "lint-report.html").read_text(errors="replace")
                (d / f"{m}.html").write_text(relocate_links(text, base, ctx.cfg.aosp_root, p.path))
                res.reports[f"{m} (html)"] = ctx.rel_report(d / f"{m}.html")
            for f in parse_android_lint(xml, ctx.cfg.aosp_root):
                fp = f.fingerprint()
                if fp not in seen:
                    seen.add(fp)
                    findings.append(f)
        if not seen and not any((d / f"{m}.xml").exists() for m in modules):
            res.status = ERROR
            res.summary = "no lint reports produced"
            return
        save_findings(ctx.findings_path("lint"), findings)
        res.metrics = finding_metrics(findings, ctx.changed)
        res.metrics["modules"] = len(modules)
        by_rule = {}
        for f in findings:
            by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        res.metrics["top_rules"] = dict(sorted(by_rule.items(), key=lambda kv: -kv[1])[:10])
        res.summary = f"{res.metrics['errors']} errors, {res.metrics['warnings']} warnings in {len(modules)} module(s)"
        self.apply_gate(ctx, res)

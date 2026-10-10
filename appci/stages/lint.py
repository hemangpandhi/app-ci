"""Android Lint through Soong's per-module `<module>-lint` targets (honours lint baselines)."""
from __future__ import annotations

import re
import shutil

from ..model import ERROR, FAILED, finding_metrics, save_findings
from ..reports.html import relocate_links
from ..reports.issues import parse_android_lint
from . import Stage, register
from .build import build_error_lines, diagnose

UNKNOWN_RE = re.compile(r"unknown target '([^']+)-lint'")


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
        with ctx.tree.lock(target):
            cmd = ctx.tree.m(target, [f"{m}-lint" for m in modules], log, timeout=scfg.get("timeout"))
            if not cmd.ok:
                unknown = set(UNKNOWN_RE.findall(cmd.output()))
                if unknown:
                    modules = [m for m in modules if m not in unknown]
                    if modules:
                        cmd = ctx.tree.m(target, [f"{m}-lint" for m in modules], log,
                                         timeout=scfg.get("timeout"), append=True)
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
            base = ctx.tree.intermediates(target, p.module_paths.get(m, p.path), m) / "android_common" / "lint"
            xml = base / "lint-report.xml"
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

"""Merge per-suite JaCoCo results into one line-level coverage view (Cobertura + Sonar + summary)."""
from __future__ import annotations

import os

from .. import util
from ..model import PASSED, SKIPPED
from ..reports import coverage as cov
from . import Stage, register

SUITES = ("unit_test", "device_test")


@register
class CoverageStage(Stage):
    name = "coverage"
    title = "Code coverage (merge)"

    def run(self, ctx, res):
        p = ctx.profile
        prod = p.java_srcs + p.kt_srcs
        idx = cov.build_source_index(prod)
        maps = {}
        for s in SUITES:
            # One jacoco.xml per suite, or one per test module (coverage/<module>/jacoco.xml).
            xmls = sorted((ctx.app_dir / s / "coverage").glob("**/jacoco.xml"))
            if xmls:
                maps[s] = cov.merge(cov.parse_jacoco(x, idx) for x in xmls)
        if not maps:
            res.status, res.summary = SKIPPED, "no coverage data (run unit_test/device_test with coverage)"
            return
        d = ctx.stage_dir("coverage", clean=True)
        merged = cov.merge(maps.values())
        total = cov.totals(merged, prod)
        res.metrics.update({"line": total["line"], "branch": total["branch"],
                            "lines_valid": total["lines_valid"], "lines_covered": total["lines_covered"],
                            "files_without_data": total.get("files_without_data", 0)})
        for s, m in maps.items():
            res.metrics[f"{s}_line"] = cov.totals(m)["line"]
        if ctx.changed is not None:
            # Coverage of the change itself: executable lines that were added or modified.
            valid = covered = 0
            uncovered = []
            for path, lines in merged.items():
                for nr in sorted(ctx.changed.get(path, ())):
                    if nr in lines:
                        valid += 1
                        if lines[nr][0]:
                            covered += 1
                        else:
                            uncovered.append(f"{path}:{nr}")
            res.metrics["new_lines_valid"] = valid
            res.metrics["new_lines_covered"] = covered
            scfg = ctx.stage_cfg("coverage")
            if valid >= int(scfg.get("min_new_lines_for_gate") or 1):
                res.metrics["new_line"] = round(100.0 * covered / valid, 2)
            if uncovered:
                (d / "uncovered-new-lines.txt").write_text("\n".join(uncovered) + "\n")
                res.reports["uncovered new lines"] = ctx.rel_report(d / "uncovered-new-lines.txt")
        cov.write_cobertura(merged, d / "cobertura.xml", p.path)
        cov.write_sonar_generic(merged, d / "sonar-coverage.xml", p.path)
        rows = cov.per_file(merged)
        util.write_json(d / "files.json", [{"path": r[0], "lines": r[1], "covered": r[2]} for r in rows])
        from ..reports.html import coverage_page
        uncov = (d / "uncovered-new-lines.txt").read_text().split() if (d / "uncovered-new-lines.txt").is_file() else []
        links = {f"{s} {h.parent.parent.name if h.parent.parent.name != 'coverage' else ''}".strip():
                 os.path.relpath(h, d) for s in SUITES for h in sorted((ctx.app_dir / s / "coverage").glob("**/html/index.html"))}
        (d / "coverage.html").write_text(coverage_page(
            f"{p.name} coverage", total, {s: cov.totals(m)["line"] for s, m in maps.items()}, rows, uncov, links))
        res.reports["coverage.html"] = ctx.rel_report(d / "coverage.html")
        res.reports["cobertura.xml"] = ctx.rel_report(d / "cobertura.xml")
        res.reports["sonar-coverage.xml"] = ctx.rel_report(d / "sonar-coverage.xml")
        res.summary = (f"line {total['line']}% ({total['lines_covered']}/{total['lines_valid']}), "
                       f"branch {total['branch']}% from {', '.join(maps)}")
        if "new_lines_valid" in res.metrics:
            res.summary += (f"; changed lines {res.metrics.get('new_line', 'n/a')}% "
                            f"({res.metrics['new_lines_covered']}/{res.metrics['new_lines_valid']})")
        # Picked up by GitLab's `coverage:` regex.
        print(f"Total line coverage: {total['line']}%")
        res.status = PASSED
        self.apply_gate(ctx, res, issues_are_warning=False)

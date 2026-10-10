"""Host-side static analysers: Checkstyle (Java), ktlint and Detekt (Kotlin)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from .. import tools, util
from ..model import ERROR, SKIPPED, finding_metrics, save_findings
from ..reports.issues import parse_checkstyle, parse_sarif
from . import Stage, register


def _files(ctx, kind: str, include_tests: bool) -> List[str]:
    p = ctx.profile
    files = list(p.java_srcs if kind == "java" else p.kt_srcs)
    if include_tests:
        files += p.test_java_srcs if kind == "java" else p.test_kt_srcs
    return files


def _argfile(path: Path, args: List[str]) -> str:
    path.write_text("\n".join(args) + "\n")
    return f"@{path}"


def _html(ctx, res, kind: str, xml: Path, out: Path) -> None:
    from ..reports.html import render_official
    err = render_official(ctx.cfg, kind, xml, out)
    if err:
        util.warn(f"[{ctx.profile.slug}] {res.stage}: {err}")
    else:
        res.reports[f"{res.stage}.html"] = ctx.rel_report(out)


def _finish(ctx, res, findings, tool_label: str):
    save_findings(ctx.findings_path(res.stage), findings)
    res.metrics.update(finding_metrics(findings, ctx.changed))
    new = f", {res.metrics['new_total']} on changed lines" if "new_total" in res.metrics else ""
    res.summary = f"{res.metrics['total']} {tool_label} issue(s) ({res.metrics['errors']} errors){new}"
    Stage.apply_gate(ctx, res)


@register
class CheckstyleStage(Stage):
    name = "checkstyle"
    title = "Checkstyle (Java)"

    def run(self, ctx, res):
        scfg = ctx.stage_cfg("checkstyle")
        files = _files(ctx, "java", scfg.get("include_tests"))
        if not files:
            res.status, res.summary = SKIPPED, "no Java sources"
            return
        d = ctx.stage_dir("checkstyle", clean=True)
        out = d / "checkstyle.xml"
        log = d / "checkstyle.log"
        res.log = ctx.rel_report(log)
        # android-style.xml pulls its checks in through external DTD entities.
        argv = [ctx.cfg.java, "-Dcheckstyle.enableExternalDtdLoad=true", "-jar", str(ctx.cfg.path(scfg["jar"])),
                "-c", str(ctx.cfg.path(scfg["config"])), "-f", "xml", "-o", str(out)]
        # Checkstyle exits with the number of errors; only a missing report means a crash.
        cmd = util.run(argv + files, log=log, cwd=ctx.cfg.aosp_root)
        if not out.is_file() or out.stat().st_size == 0:
            res.status, res.summary = ERROR, f"checkstyle crashed (exit {cmd.returncode})"
            return
        res.reports["checkstyle.xml"] = ctx.rel_report(out)
        root = str(ctx.cfg.aosp_root).rstrip("/") + "/"
        rel = d / "checkstyle-rel.xml"           # tree-relative names on the HTML page
        rel.write_text(out.read_text(errors="replace").replace(f'name="{root}', 'name="'))
        _html(ctx, res, "checkstyle", rel, d / "checkstyle.html")
        rel.unlink()
        _finish(ctx, res, parse_checkstyle(out, ctx.cfg.aosp_root, "checkstyle"), "checkstyle")
        res.metrics["files"] = len(files)


@register
class KtlintStage(Stage):
    name = "ktlint"
    title = "ktlint (Kotlin style)"

    def run(self, ctx, res):
        scfg = ctx.stage_cfg("ktlint")
        files = _files(ctx, "kt", scfg.get("include_tests"))
        if not files:
            res.status, res.summary = SKIPPED, "no Kotlin sources"
            return
        d = ctx.stage_dir("ktlint", clean=True)
        out = d / "ktlint.xml"
        log = d / "ktlint.log"
        res.log = ctx.rel_report(log)
        argv = [ctx.cfg.java, "--add-opens=java.base/java.lang=ALL-UNNAMED", "-jar",
                str(ctx.cfg.path(scfg["jar"])), "--relative",
                f"--editorconfig={ctx.cfg.path(scfg['editorconfig'])}",
                f"--reporter=checkstyle,output={out}", f"--reporter=html,output={d / 'ktlint.html'}"]
        cmd = util.run(argv + files, log=log, cwd=ctx.cfg.aosp_root)
        if not out.is_file():
            res.status, res.summary = ERROR, f"ktlint crashed (exit {cmd.returncode})"
            return
        findings = parse_checkstyle(out, ctx.cfg.aosp_root, "ktlint")
        for f in findings:  # ktlint reports everything as 'error'; it is style, not a defect
            f.severity = "minor"
        res.reports["ktlint.xml"] = ctx.rel_report(out)
        if (d / "ktlint.html").is_file():
            # ktlint's page pulls a web font from Google; offline viewers would see blank text
            page_html = (d / "ktlint.html").read_text(errors="replace")
            (d / "ktlint.html").write_text(re.sub(r'<link[^>]+fonts\.googleapis\.com[^>]*>\s*', "", page_html))
            res.reports["ktlint.html"] = ctx.rel_report(d / "ktlint.html")
        _finish(ctx, res, findings, "ktlint")
        res.metrics["files"] = len(files)


def detekt_baseline(ctx) -> Path:
    tmpl = ctx.stage_cfg("detekt")["baseline"]
    return ctx.cfg.path(tmpl.replace("{app}", ctx.profile.path).replace("{slug}", ctx.profile.slug))


@register
class DetektStage(Stage):
    name = "detekt"
    title = "Detekt (Kotlin static analysis)"

    def run(self, ctx, res, create_baseline: bool = False):
        scfg = ctx.stage_cfg("detekt")
        files = _files(ctx, "kt", scfg.get("include_tests"))
        if not files:
            res.status, res.summary = SKIPPED, "no Kotlin sources"
            return
        jar = tools.ensure(ctx.cfg, "detekt")
        d = ctx.stage_dir("detekt", clean=True)
        log = d / "detekt.log"
        res.log = ctx.rel_report(log)
        baseline = detekt_baseline(ctx)
        args = ["--input", ",".join(str(ctx.cfg.aosp_root / f) for f in files),
                "--base-path", str(ctx.cfg.aosp_root),
                "--config", str(ctx.cfg.path(scfg["config"])), "--build-upon-default-config"]
        if create_baseline:
            baseline.parent.mkdir(parents=True, exist_ok=True)
            args += ["--baseline", str(baseline), "--create-baseline"]
        else:
            if baseline.is_file():
                args += ["--baseline", str(baseline)]
                res.metrics["baseline"] = str(baseline)
            args += ["--report", f"sarif:{d / 'detekt.sarif'}", "--report", f"html:{d / 'detekt.html'}",
                     "--report", f"xml:{d / 'detekt.xml'}"]
        cmd = util.run([ctx.cfg.java, "-jar", str(jar), _argfile(d / "detekt.args", args)],
                       log=log, cwd=ctx.cfg.aosp_root)
        # 0 = clean, 2 = issues above detekt's own maxIssues; anything else is a tool failure.
        if cmd.returncode not in (0, 2):
            res.status, res.summary = ERROR, f"detekt failed (exit {cmd.returncode})"
            return
        if create_baseline:
            res.summary = f"baseline written to {baseline}"
            return
        sarif = d / "detekt.sarif"
        findings = parse_sarif(sarif, ctx.cfg.aosp_root, "detekt") if sarif.is_file() else []
        res.reports["detekt.html"] = ctx.rel_report(d / "detekt.html")
        res.reports["detekt.sarif"] = ctx.rel_report(sarif)
        _finish(ctx, res, findings, "detekt")
        res.metrics["files"] = len(files)

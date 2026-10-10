"""SonarQube analysis fed with real class files, the Soong classpath and every report produced so far."""
from __future__ import annotations

import os
import shutil

from .. import tools, util
from ..javaenv import dependency_jars, extract_classes, module_class_jars, source_roots
from ..model import FAILED, PASSED, SKIPPED
from ..reports.issues import write_sonar_generic_issues
from . import Stage, register


def _props_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", " ")


def sonar_slug(ctx) -> str:
    """Stable per-app key part. Not the report slug: CI app jobs use `--slug app` for fixed artifact
    paths, which would put every app into one Sonar project."""
    from ..discovery import _app_slug
    return ctx.cfg.app_entry(ctx.profile.path).get("slug") or _app_slug(ctx.profile.path)


@register
class SonarStage(Stage):
    name = "sonar"
    title = "SonarQube"

    def run(self, ctx, res):
        scfg = ctx.stage_cfg("sonar")
        p = ctx.profile
        root = ctx.cfg.aosp_root
        base = ctx.app_abs()
        target = ctx.cfg.stage_cfg("build", ctx.entry)["target"]
        d = ctx.stage_dir("sonar", clean=True)

        def rel_to_base(path: str) -> str:
            return util.rel(root / path, base)

        src_roots = [rel_to_base(r) for r in source_roots(root, p.java_srcs + p.kt_srcs)]
        test_roots = [rel_to_base(r) for r in source_roots(root, p.test_java_srcs + p.test_kt_srcs)]
        test_roots = [t for t in test_roots if not any(t == s or t.startswith(s + "/") for s in src_roots)]
        classes = d / "classes"
        n = extract_classes(module_class_jars(ctx.tree, target, p, p.prod_modules), classes)
        libs = dependency_jars(ctx.tree, target, p, exclude_core=False)

        props = {
            "sonar.projectKey": scfg["project_key_prefix"] + sonar_slug(ctx) + (scfg.get("project_key_suffix") or ""),
            "sonar.projectName": p.name + (f" ({ctx.cfg.get('platform')})" if ctx.cfg.get("platform") else ""),
            "sonar.projectVersion": util.capture(["git", "rev-parse", "--short", "HEAD"], cwd=base).strip() or "dev",
            "sonar.projectBaseDir": str(base),
            "sonar.working.directory": str(d / ".scannerwork"),
            "sonar.sources": ",".join(src_roots) or ".",
            "sonar.inclusions": "**/*.java,**/*.kt",
            "sonar.sourceEncoding": "UTF-8",
            "sonar.java.source": "17",
            "sonar.java.binaries": str(classes),
            "sonar.java.libraries": ",".join(map(str, libs)),
            "sonar.scm.provider": "git",
            # Analyse on the tree's JDK 21 (the CLI's embedded JRE may be older than the server needs).
            "sonar.scanner.skipJreProvisioning": "true",
            "sonar.scanner.javaExePath": ctx.cfg.java,
        }
        if test_roots:
            props["sonar.tests"] = ",".join(test_roots)
        cov = ctx.app_dir / "coverage" / "sonar-coverage.xml"
        if cov.is_file():
            props["sonar.coverageReportPaths"] = str(cov)
        findings = ctx.all_findings()
        if findings:
            issues = d / "external-issues.json"
            write_sonar_generic_issues(findings, issues, p.path, legacy=bool(scfg.get("legacy_issue_format")))
            props["sonar.externalIssuesReportPaths"] = str(issues)
        junit_dir = d / "junit"
        for s in ("unit_test", "device_test"):
            j = ctx.app_dir / s / "junit.xml"
            if j.is_file():
                junit_dir.mkdir(exist_ok=True)
                shutil.copy2(j, junit_dir / f"TEST-{s}.xml")
        if junit_dir.exists():
            props["sonar.junit.reportPaths"] = str(junit_dir)
        mr = os.environ.get("CI_MERGE_REQUEST_IID")
        if scfg.get("pull_request_analysis"):
            if mr:
                props.update({
                    "sonar.pullrequest.key": mr,
                    "sonar.pullrequest.branch": os.environ.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", ""),
                    "sonar.pullrequest.base": os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", ""),
                })
            elif os.environ.get("CI_COMMIT_BRANCH"):
                props["sonar.branch.name"] = os.environ["CI_COMMIT_BRANCH"]
        if scfg.get("wait_quality_gate"):
            props["sonar.qualitygate.wait"] = "true"
        props.update({k: str(v) for k, v in (scfg.get("extra_properties") or {}).items()})

        pf = d / "sonar-project.properties"
        pf.write_text("".join(f"{k}={_props_escape(v)}\n" for k, v in props.items()))
        res.reports["sonar-project.properties"] = ctx.rel_report(pf)
        res.metrics.update({"class_files": n, "libraries": len(libs), "external_issues": len(findings)})

        host_envs = scfg["host_url_env"] if isinstance(scfg["host_url_env"], list) else [scfg["host_url_env"]]
        host = next((os.environ[e] for e in host_envs if os.environ.get(e)), None)
        if not host:
            res.status = SKIPPED
            res.summary = f"none of ${', $'.join(host_envs)} set; properties written for a manual run"
            return
        scanner = shutil.which("sonar-scanner") or str(tools.ensure(ctx.cfg, "sonar-scanner"))
        # The scanner downloads and runs analyzer plugins from its cache: keep that cache private to
        # this job (a shared one is writable by other merge requests' build code).
        env = {"SONAR_HOST_URL": host, "SONAR_USER_HOME": str(tools.private_dir("sonar-home"))}
        token = util.secret(scfg["token_env"])
        if token:
            env["SONAR_TOKEN"] = token
        log = d / "sonar.log"
        res.log = ctx.rel_report(log)
        r = util.run([scanner, f"-Dproject.settings={pf}"], log=log, cwd=base, env=env)
        report_task = d / ".scannerwork" / "report-task.txt"
        if report_task.is_file():
            for line in report_task.read_text().splitlines():
                if line.startswith("dashboardUrl="):
                    res.reports["SonarQube dashboard"] = line.split("=", 1)[1]
        if not r.ok:
            res.status = FAILED
            res.summary = "sonar-scanner failed" + (" (quality gate)" if "QUALITY GATE STATUS: FAILED" in r.output() else "")
            return
        res.status = PASSED
        res.summary = f"analysis uploaded to {host}"

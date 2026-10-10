"""Unit (Robolectric / host) and instrumented (device / emulator) tests, run through atest + Tradefed.

Coverage: Tradefed's own JaCoCo support is used (`--coverage --auto-collect JAVA_COVERAGE`):
  * Robolectric: Tradefed attaches the jacocoagent to the test JVM (on-the-fly instrumentation).
  * Device: the APKs are built with EMMA_INSTRUMENT in the emulator OUT_DIR and Tradefed pulls the
    .ec files from the device after the run.
Each suite gets its own JaCoCo report (see reports/coverage.py for why they are merged per line).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import List

from .. import artifacts, util
from ..emulator import Emulator, adb, image_ready, prepare_device
from ..javaenv import jacoco_report_jars, merge_class_jars, module_class_jars, packages_of_sources, source_roots
from ..model import ERROR, FAILED, PASSED, SKIPPED, WARNING
from ..reports.junit import parse_atest_output, summarize, write_junit
from . import Stage, register
from .build import HOST_TOOLS


def coverage_tf_args(jacocoagent: Path) -> List[str]:
    """Tradefed's own coverage collection; used for on-device runs (offline-instrumented APKs)."""
    return ["--coverage", "--coverage-toolchain", "JACOCO", "--auto-collect", "JAVA_COVERAGE",
            "--jacocoagent-path", str(jacocoagent)]


def package_roots(packages: List[str]) -> List[str]:
    roots: List[str] = []
    for p in sorted(packages):
        if not any(p == r or p.startswith(r + ".") for r in roots):
            roots.append(p)
    return roots


def host_agent_args(jacocoagent: Path, destfile: Path, packages: List[str]) -> List[str]:
    """Attach the in-tree JaCoCo agent to Robolectric/host test JVMs ourselves.

    Tradefed's `--coverage` attaches the agent without a class filter, which instruments the whole
    android-all jar and makes the test JVM miss Tradefed's connect timeout; it also omits
    `output=file`, and AOSP's JaCoCo defaults to `output=none`, so no data would be written anyway.
    """
    includes = ":".join(f"{r}.*" for r in package_roots(packages)) or "*"
    agent = (f"-javaagent:{jacocoagent}=output=file,destfile={destfile},append=true,includes={includes},"
             "inclnolocationclasses=true,exclclassloader=jdk.internal.reflect.DelegatingClassLoader")
    return ["--test-arg", f"com.android.tradefed.testtype.IsolatedHostTest:java-flags:{agent}"]


def collect_ec(tmpdir: Path, dest: Path) -> List[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for i, ec in enumerate(sorted(tmpdir.rglob("*.ec"))):
        if ec.stat().st_size == 0:
            continue
        target = dest / f"{i:03d}-{ec.name}"
        shutil.copy2(ec, target)
        out.append(target)
    return out


def jacoco_report(ctx, target: str, ec_files: List[Path], class_jars: List[Path], out_dir: Path, title: str):
    """Run the in-tree jacoco-cli (same version as the agent / EMMA instrumentation)."""
    cli = ctx.tree.host_out(target) / "framework" / "jacoco-cli.jar"
    if not cli.is_file():
        raise RuntimeError(f"{cli} missing (build stage builds it; or `m jacoco-cli`)")
    p = ctx.profile
    prod = p.java_srcs + p.kt_srcs
    merged = out_dir / "classes.jar"
    n = merge_class_jars(class_jars, merged, include=packages_of_sources(ctx.cfg.aosp_root, prod))
    if n == 0:
        raise RuntimeError("no class files found for the coverage report")
    roots = [str(ctx.cfg.aosp_root / r) for r in source_roots(ctx.cfg.aosp_root, prod)]
    argv = [ctx.cfg.java, "-jar", str(cli), "report", *map(str, ec_files), "--classfiles", str(merged),
            "--name", title, "--xml", str(out_dir / "jacoco.xml"), "--csv", str(out_dir / "jacoco.csv"),
            "--html", str(out_dir / "html")]
    for r in roots:
        argv += ["--sourcefiles", r]
    res = util.run(argv, log=out_dir / "jacoco-report.log")
    if not res.ok:
        raise RuntimeError(f"jacoco report failed: {res.tail(10)}")


class _AtestStage(Stage):
    kinds: List[str] = []

    def tests(self, ctx):
        """Impact-selected test modules (all of them when the change is unknown or risky)."""
        from ..impact import select_tests
        if not hasattr(self, "_selected"):
            self._selected = select_tests(ctx, self.kinds)
        return self._selected[0]

    def record_selection(self, ctx, res) -> None:
        tests, why = self._selected
        total = len([t for t in ctx.profile.tests if t.kind in self.kinds])
        res.metrics["selection"] = ctx.impact.mode if ctx.changeset is not None else "full"
        res.metrics["selected_modules"] = len(tests)
        res.metrics["available_modules"] = total
        res.metrics["selection_reason"] = why

    def run_atest(self, ctx, res, target: str, extra: List[str], coverage: bool):
        scfg = ctx.stage_cfg(self.name)
        tests = self.tests(ctx)
        d = ctx.stage_dir(self.name)
        tmp = d / "tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        log = d / "atest.log"
        res.log = ctx.rel_report(log)
        args = [t.name for t in tests] + extra + list(scfg.get("atest_args") or [])
        agent = ctx.tree.host_out(target) / "framework" / "jacocoagent.jar"
        if coverage and self.name == "device_test":
            args += ["--"] + coverage_tf_args(agent)
        elif coverage:
            # The .ec lands in tmp so collect_ec() picks it up like Tradefed-collected files.
            pkgs = packages_of_sources(ctx.cfg.aosp_root, ctx.profile.java_srcs + ctx.profile.kt_srcs)
            args += ["--"] + host_agent_args(agent, tmp / "host-tests.ec", pkgs)
        env = {"TMPDIR": str(tmp)}
        if coverage and not (agent.is_file() and (agent.parent / "jacoco-cli.jar").is_file()):
            ctx.tree.m(target, HOST_TOOLS, d / "host-tools.log")
        cmd = ctx.tree.atest(target, args, log, extra_env=env, timeout=scfg.get("timeout"))
        cases = parse_atest_output(cmd.output())
        junit = d / "junit.xml"
        err = None if cases else (cmd.tail(30) or f"atest exited with {cmd.returncode}")
        write_junit(cases, junit, extra_error=err)
        res.reports["junit.xml"] = ctx.rel_report(junit)
        from ..reports.html import render_official
        herr = render_official(ctx.cfg, "junit", junit, d / "tests.html",
                               {"TITLE": f"{ctx.profile.name} {self.name}"})
        if herr:
            util.warn(f"[{ctx.profile.slug}] {self.name}: {herr}")
        else:
            res.reports["tests.html"] = ctx.rel_report(d / "tests.html")
        s = summarize(cases)
        res.metrics.update(s)
        res.metrics["exit_code"] = cmd.returncode
        res.metrics["modules"] = [t.name for t in tests]
        if not cases:
            res.status = ERROR
            res.summary = f"no test results (atest exit {cmd.returncode}); see {res.log}"
            self._keep_logs(ctx, res, d, tmp)
            return d, tmp
        runner_errors = [c for c in cases if c.status == "error"]
        res.metrics["runner_errors"] = len(runner_errors)
        if runner_errors and len(runner_errors) == len(cases):
            # Nothing executed: Tradefed / test-JVM / device problem, not a test failure.
            res.status = ERROR
            first = (runner_errors[0].details or "").splitlines()[0][:160]
            res.summary = f"no tests executed: runner error in {len(runner_errors)} module(s): {first}"
            self._keep_logs(ctx, res, d, tmp)
            return d, tmp
        res.summary = f"{s['passed']}/{s['tests']} passed, {s['failed']} failed, {s['skipped']} skipped"
        if res.metrics.get("selected_modules") is not None and \
                res.metrics["selected_modules"] != res.metrics.get("available_modules"):
            res.summary += (f" [impact: {res.metrics['selected_modules']} of "
                            f"{res.metrics['available_modules']} module(s)]")
        self.apply_gate(ctx, res, issues_are_warning=False)
        if res.status == PASSED and cmd.returncode != 0 and s["failed"] == 0:
            res.status = FAILED
            res.summary += f" (atest exit {cmd.returncode}: build or runner error)"
        return d, tmp

    @staticmethod
    def _keep_logs(ctx, res, d: Path, tmp: Path) -> None:
        """Preserve Tradefed's logs (host_log, test-JVM output) next to the report."""
        logs = d / "tradefed-logs"
        shutil.rmtree(logs, ignore_errors=True)
        for run_dir in tmp.glob("atest_result_*/*/log"):
            shutil.copytree(run_dir, logs / run_dir.parent.name, dirs_exist_ok=True)
        if logs.exists():
            res.reports["tradefed logs"] = ctx.rel_report(logs)

    def finish(self, ctx, res, d: Path, tmp: Path) -> None:
        if res.status not in (PASSED, WARNING):
            self._keep_logs(ctx, res, d, tmp)
        shutil.rmtree(tmp, ignore_errors=True)

    def host_coverage_reports(self, ctx, res, target, d: Path, tmp: Path, tests) -> None:
        """One JaCoCo report per test module, against the jar that module actually ran.

        Each Robolectric module bundles its own build of the app (its `instrumentation_for`
        app, compiled with different R constants), so class IDs differ between modules and
        from the app's javac output. All modules append to one .ec; a report built from a
        module's runtime jar picks up exactly that module's execution data.
        """
        cov = d / "coverage"
        ec = collect_ec(tmp, cov)
        res.metrics["coverage_files"] = len(ec)
        if not ec:
            util.warn(f"[{ctx.profile.slug}] {self.name}: no coverage data collected")
            return
        minfo = ctx.tree.module_info(target)
        made = 0
        for t in tests:
            jars = [ctx.cfg.aosp_root / i for i in (minfo.get(t.name) or {}).get("installed") or []
                    if i.endswith(f"/{t.name}.jar") and (ctx.cfg.aosp_root / i).is_file()]
            if not jars:
                mods = [t.instrumentation_for] if t.instrumentation_for else ctx.profile.prod_modules
                jars = module_class_jars(ctx.tree, target, ctx.profile, mods)
            try:
                jacoco_report(ctx, target, ec, jars, cov / t.name, f"{ctx.profile.name} {t.name}")
                res.reports[f"coverage {t.name} (html)"] = ctx.rel_report(cov / t.name / "html" / "index.html")
                made += 1
            except RuntimeError as exc:
                util.warn(f"[{ctx.profile.slug}] {self.name}: {t.name}: {exc}")
                res.metrics.setdefault("coverage_errors", []).append(f"{t.name}: {exc}")
        res.metrics["coverage_reports"] = made

    def coverage_report(self, ctx, res, target, d: Path, tmp: Path, class_jars: List[Path], title: str):
        cov = d / "coverage"
        ec = collect_ec(tmp, cov)
        res.metrics["coverage_files"] = len(ec)
        if not ec:
            util.warn(f"[{ctx.profile.slug}] {self.name}: no coverage data collected")
            return
        try:
            jacoco_report(ctx, target, ec, class_jars, cov, title)
            res.reports["coverage (html)"] = ctx.rel_report(cov / "html" / "index.html")
            res.reports["jacoco.xml"] = ctx.rel_report(cov / "jacoco.xml")
        except RuntimeError as exc:
            util.warn(f"[{ctx.profile.slug}] {self.name}: {exc}")
            res.metrics["coverage_error"] = str(exc)


@register
class UnitTestStage(_AtestStage):
    name = "unit_test"
    title = "Unit tests (Robolectric / host)"
    kinds = ["robolectric", "host"]
    requires = ["build"]

    def run(self, ctx, res):
        tests = self.tests(ctx)
        self.record_selection(ctx, res)
        if not tests:
            res.status = SKIPPED
            res.summary = ("no Robolectric/host test modules" if res.metrics["available_modules"] == 0
                           else f"no impacted unit tests ({res.metrics['selection_reason']})")
            return
        scfg = ctx.stage_cfg(self.name)
        target = scfg["target"]
        cov = bool(scfg.get("coverage"))
        ctx.stage_dir(self.name, clean=True)
        with ctx.tree.lock(target):
            d, tmp = self.run_atest(ctx, res, target, [], cov)
        if cov and res.status != ERROR:
            self.host_coverage_reports(ctx, res, target, d, tmp, tests)
        self.finish(ctx, res, d, tmp)


@register
class DeviceTestStage(_AtestStage):
    name = "device_test"
    title = "Instrumented tests (emulator / device)"
    kinds = ["device"]
    requires = ["build"]

    def run(self, ctx, res):
        tests = self.tests(ctx)
        self.record_selection(ctx, res)
        if not tests:
            res.status = SKIPPED
            res.summary = ("no instrumented test modules" if res.metrics["available_modules"] == 0
                           else f"no impacted device tests ({res.metrics['selection_reason']})")
            return
        scfg = ctx.stage_cfg(self.name)
        target = scfg["target"]
        cov = bool(scfg.get("coverage")) and bool(ctx.cfg.target(target).get("coverage"))
        d = ctx.stage_dir(self.name, clean=True)
        sysdir = binary = None
        img_url, img_pat = artifacts.source(ctx.cfg, "image")
        if scfg.get("mode") == "emulator" and img_url:
            try:
                cache = artifacts.cache_root(ctx.cfg)
                rel = artifacts.fetch(ctx.cfg, "image", img_url, img_pat, cache)
                sysdir = artifacts.image_dir(rel)
                res.metrics["emulator_image"] = rel.name
                prog_url, prog_pat = artifacts.source(ctx.cfg, "program")
                if prog_url:
                    binary = artifacts.emulator_binary(artifacts.fetch(ctx.cfg, "program", prog_url, prog_pat, cache))
            except Exception as exc:
                names = ", ".join(t.name for t in tests)
                res.summary = f"NOT RUN: emulator image download failed ({exc}); impacted: {names}"
                if scfg.get("missing_image") == "error":
                    res.status = ERROR
                else:
                    res.status, res.metrics["not_run"] = WARNING, len(tests)
                return
        if scfg.get("mode") == "emulator" and sysdir is None and not image_ready(ctx.tree, target):
            # Fail fast: waiting for the target lock would block on a running `appci image` build
            # (hours) while this job holds the runner's resource group.
            names = ", ".join(t.name for t in tests)
            res.summary = (f"NOT RUN: no emulator image (set emulator.image.url / APPCI_EMULATOR_IMAGE_URL, "
                           f"or run the emulator_image job); impacted: {names}")
            res.metrics["image_dir"] = str(ctx.tree.product_out(target))
            if scfg.get("missing_image") == "error":
                res.status = ERROR
            else:
                res.status, res.metrics["not_run"] = WARNING, len(tests)
            return
        emu = None
        with ctx.tree.lock(target):
            try:
                if scfg.get("mode") == "emulator":
                    emu = Emulator(ctx.tree, target, int(scfg["emulator_port"]), list(scfg["emulator_args"]),
                                   int(scfg["boot_timeout"]), d / "emulator.log", sysdir=sysdir, binary=binary)
                    serial = emu.start()
                else:
                    serial = scfg.get("serial") or os.environ.get("ANDROID_SERIAL")
                    if not serial:
                        raise RuntimeError("device_test.mode=device needs device_test.serial or ANDROID_SERIAL")
                    prepare_device(serial)
                res.metrics["serial"] = serial
                res.metrics["build_fingerprint"] = adb(serial, "shell", "getprop", "ro.build.fingerprint").stdout.strip()
                if scfg.get("install_apps"):
                    self.install_apps(ctx, target, serial, d)
                d, tmp = self.run_atest(ctx, res, target, ["-s", serial], cov)
            finally:
                if emu:
                    emu.stop()
        if cov and res.status != ERROR:
            mods = [t.name for t in tests] + ctx.profile.apps
            jars = jacoco_report_jars(ctx.tree, target, ctx.profile, mods)
            self.coverage_report(ctx, res, target, d, tmp, jars, f"{ctx.profile.name} instrumented tests")
        self.finish(ctx, res, d, tmp)

    def install_apps(self, ctx, target, serial, d):
        """Build the (coverage-instrumented) app for this target and install it as a system-app update."""
        apps = ctx.profile.apps
        if not apps:
            return
        r = ctx.tree.m(target, apps, d / "install-apps.log")
        if not r.ok:
            raise RuntimeError(f"building {', '.join(apps)} for {ctx.tree.lunch(target)} failed")
        minfo = ctx.tree.module_info(target)
        for m in apps:
            for inst in (minfo.get(m) or {}).get("installed") or []:
                apk = ctx.cfg.aosp_root / inst
                if inst.endswith(".apk") and apk.is_file():
                    out = adb(serial, "install", "-r", "-d", "-g", str(apk), timeout=300)
                    if "Success" not in out.stdout:
                        util.warn(f"install {apk.name} on {serial} failed (tests use the image version): "
                                  f"{out.stdout.strip()[-200:]}")

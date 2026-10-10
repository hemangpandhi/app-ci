"""Build the app's production modules (and optionally its tests) with Soong."""
from __future__ import annotations

import re
import shutil

from ..model import ERROR, FAILED, PASSED
from . import Stage, register

ERR_RE = re.compile(r"(^FAILED: |error: |\berror\[|^ninja: error|^error: |ERROR: )")
HOST_TOOLS = ["jacocoagent", "jacoco-cli"]


def diagnose(text: str) -> str:
    """Recognise infrastructure failures that are not the app's fault."""
    analysis = "soong bootstrap failed" in text or "FAILED: out/soong/build." in text
    if analysis and ("Killed" in text or "exit=137" in text):
        return ("Soong analysis was OOM-killed: the build host needs >= 80 GB RAM+swap for full-tree "
                "analysis (see `appci doctor`)")
    if analysis:
        return "Soong analysis (Android.bp evaluation) failed - not an app compile error"
    return ""


def build_error_lines(text: str, limit: int = 25):
    return [l for l in text.splitlines() if ERR_RE.search(l)][:limit]


@register
class BuildStage(Stage):
    name = "build"
    title = "Build (Soong)"

    def run(self, ctx, res):
        scfg = ctx.stage_cfg("build")
        target = scfg["target"]
        p = ctx.profile
        modules = list(p.prod_modules)
        if scfg.get("include_tests"):
            modules += [t.name for t in p.tests]
        modules += HOST_TOOLS + list(scfg.get("extra_modules") or [])
        # Library changes: compile every dependent app too (catches API breaks in other repos).
        imp = ctx.impact
        dep_mode = (ctx.cfg.get("impact") or {}).get("dependents", "test")
        for dep in imp.dependents:
            modules += dep["modules"]
            if dep_mode == "test":
                modules += [t["name"] for t in dep["tests"] if t["kind"] in ("robolectric", "host")]
        modules = list(dict.fromkeys(modules))
        if imp.dependents:
            res.metrics["dependent_apps"] = [d["path"] for d in imp.dependents]
        if imp.dependents_truncated:
            res.metrics["dependent_apps_not_built"] = imp.dependents_truncated
        if not p.prod_modules:
            res.status = ERROR
            res.summary = f"no production modules found under {p.path}"
            return
        d = ctx.stage_dir("build", clean=True)
        log = d / "build.log"
        res.log = ctx.rel_report(log)
        with ctx.tree.lock(target):
            cmd = ctx.tree.m(target, modules, log, timeout=scfg.get("timeout"))
        res.metrics.update({"modules": len(modules), "exit_code": cmd.returncode})
        if not cmd.ok:
            text = cmd.output()
            errors = build_error_lines(text)
            hint = diagnose(text)
            if hint:
                errors.insert(0, hint)
            (d / "errors.txt").write_text("\n".join(errors))
            res.reports["errors"] = ctx.rel_report(d / "errors.txt")
            res.status = FAILED
            res.summary = errors[0][:200] if errors else f"m exited with {cmd.returncode}"
            res.metrics["errors"] = len(errors)
            return
        # Collect the APKs that end up in the image.
        minfo = ctx.tree.module_info(target)
        apk_dir = d / "apks"
        apk_dir.mkdir(exist_ok=True)
        sizes = {}
        for m in p.apps:
            for inst in (minfo.get(m) or {}).get("installed") or []:
                src = ctx.cfg.aosp_root / inst
                if inst.endswith(".apk") and src.is_file():
                    shutil.copy2(src, apk_dir / src.name)
                    sizes[src.name] = src.stat().st_size
        for extra in scfg.get("extra_targets") or []:
            elog = d / f"build-{extra}.log"
            with ctx.tree.lock(extra):
                ecmd = ctx.tree.m(extra, p.apps, elog, timeout=scfg.get("timeout"))
            if not ecmd.ok:
                errs = build_error_lines(ecmd.output())
                res.status = FAILED
                res.summary = f"{extra} ({ctx.tree.lunch(extra)}): " + (errs[0][:180] if errs else "build failed")
                res.reports[f"build log ({extra})"] = ctx.rel_report(elog)
                return
            emi = ctx.tree.module_info(extra)
            for m in p.apps:
                for inst in (emi.get(m) or {}).get("installed") or []:
                    src = ctx.cfg.aosp_root / inst
                    if inst.endswith(".apk") and src.is_file():
                        dst = apk_dir / f"{src.stem}-{extra}.apk"
                        shutil.copy2(src, dst)
                        sizes[dst.name] = dst.stat().st_size
        res.metrics["apk_bytes"] = sizes
        if sizes:
            res.reports["apks"] = ctx.rel_report(apk_dir)
        res.status = PASSED
        res.summary = f"{len(modules)} modules built, {len(sizes)} APK(s)"

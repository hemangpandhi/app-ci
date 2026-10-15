"""Build the app's production modules (and optionally its tests) with Soong."""
from __future__ import annotations

import re
import shutil

from ..aosp import _load_json
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


def build_variants(scfg) -> dict:
    """APK suffix -> target. stages.build.variants (default debug: device, release: release), plus the
    older extra_targets list (suffix = target name)."""
    out = dict(scfg.get("variants") or {})
    for extra in scfg.get("extra_targets") or []:
        out.setdefault(extra, extra)
    return out


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
        # Collect the APKs that end up in the image, one per variant: <Module>-debug.apk from the
        # device (userdebug) target, <Module>-release.apk from the release (user) target. Variants
        # on another target than the main build get the app modules built there (own OUT_DIR).
        apk_dir = d / "apks"
        apk_dir.mkdir(exist_ok=True)
        sizes = {}
        variants = build_variants(scfg)
        res.metrics["variants"] = {suffix: ctx.tree.lunch(t) for suffix, t in variants.items()}
        for suffix, vtarget in variants.items():
            if vtarget != target:
                vlog = d / f"build-{suffix}.log"
                with ctx.tree.lock(vtarget):
                    vcmd = ctx.tree.m(vtarget, p.apps, vlog, timeout=scfg.get("timeout"))
                res.reports[f"build log ({suffix})"] = ctx.rel_report(vlog)
                if not vcmd.ok:
                    errs = build_error_lines(vcmd.output())
                    (d / f"errors-{suffix}.txt").write_text("\n".join(errs))
                    res.reports[f"errors ({suffix})"] = ctx.rel_report(d / f"errors-{suffix}.txt")
                    res.status = FAILED
                    res.summary = f"{suffix} ({ctx.tree.lunch(vtarget)}): " + (errs[0][:180] if errs else "build failed")
                    return
                _load_json.cache_clear()             # module-info.json of this OUT_DIR was just rewritten
                try:
                    ctx.tree.ensure_metadata(vtarget)   # older trees write it only with `m module-info`
                except RuntimeError as exc:
                    res.status = ERROR
                    res.summary = f"{suffix} ({ctx.tree.lunch(vtarget)}): {exc}"[:300]
                    return
            minfo = ctx.tree.module_info(vtarget)
            for m in p.apps:
                for inst in (minfo.get(m) or {}).get("installed") or []:
                    src = ctx.cfg.aosp_root / inst
                    if inst.endswith(".apk") and src.is_file():
                        dst = apk_dir / f"{src.stem}-{suffix}.apk"
                        shutil.copy2(src, dst)
                        sizes[dst.name] = dst.stat().st_size
        res.metrics["apk_bytes"] = sizes
        if sizes:
            res.reports["apks"] = ctx.rel_report(apk_dir)
        res.status = PASSED
        res.summary = f"{len(modules)} modules built, {len(sizes)} APK(s)"

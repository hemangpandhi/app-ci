"""appci command line."""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from . import tools, util
from .aosp import AospTree
from .config import TOOL_ROOT, Config, load_config
from .model import ERROR, FAILED, worst


def _csv(v: Optional[str]) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _cfg(args) -> Config:
    overrides = {}
    if getattr(args, "aosp_root", None):
        overrides["aosp_root"] = args.aosp_root
    if getattr(args, "workdir", None):
        overrides["workdir"] = args.workdir
    if getattr(args, "platform", None):
        overrides["platform"] = args.platform
    from .config import deep_merge
    for t, attr in (("device", "device_lunch"), ("emulator", "emulator_lunch")):
        if getattr(args, attr, None):
            overrides.setdefault("targets", {}).setdefault(t, {})["lunch"] = getattr(args, attr)
    for item in getattr(args, "set", None) or []:
        overrides = deep_merge(overrides, _parse_set(item))
    return load_config(getattr(args, "config", None), overrides)


def _parse_set(item: str) -> dict:
    """`stages.build.extra_targets=[release]` -> nested override dict (value parsed as YAML)."""
    key, sep, raw = item.partition("=")
    if not sep or not key:
        raise SystemExit(f"--set expects key=value, got: {item}")
    from .config import yaml_module
    value = yaml_module().safe_load(raw)
    out: dict = {}
    cur = out
    parts = key.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
    return out


def _resolve_apps(cfg: Config, tree: AospTree, args) -> List[str]:
    if getattr(args, "app", None):
        return [a.strip("/") for a in args.app]
    if getattr(args, "all", False):
        configured = [a["path"] for a in cfg.app_entries()]
        if configured:
            return configured
        from .discovery import scan_apps
        return [a["path"] for a in scan_apps(tree)]
    configured = [a["path"] for a in cfg.app_entries()]
    if len(configured) == 1:
        return configured
    raise SystemExit("choose apps with --app <path> (repeatable) or --all")


def _stages(cfg: Config, spec: Optional[str], skip: Optional[str]) -> List[str]:
    from .stages import all_stage_names
    names = _csv(spec) or list(cfg["stages"]["default"])
    valid = set(all_stage_names())
    bad = [n for n in names if n not in valid]
    if bad:
        raise SystemExit(f"unknown stage(s): {', '.join(bad)} (valid: {', '.join(sorted(valid))})")
    return [n for n in names if n not in set(_csv(skip))]


def _meminfo() -> dict:
    out = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0])  # kB
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- commands
def cmd_doctor(args) -> int:
    cfg = _cfg(args)
    tree = AospTree(cfg)
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))

    check("AOSP root", (cfg.aosp_root / "build/envsetup.sh").is_file(), str(cfg.aosp_root))
    check("config", True, str(cfg.source or "built-in defaults"))
    check("JDK", (cfg.java_home / "bin/java").is_file(), str(cfg.java_home))
    from .config import yaml_module
    check("PyYAML", True, yaml_module().__version__)
    check("git", shutil.which("git") is not None)
    check("adb", shutil.which("adb") is not None or (cfg.aosp_root / "out/host/linux-x86/bin/adb").exists())
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    check("/dev/kvm", kvm, "needed for the emulator (device_test.mode=emulator)")
    check("emulator prebuilt", (cfg.aosp_root / "prebuilts/android-emulator/linux-x86_64/emulator").exists())
    for t in cfg["targets"]:
        try:
            mi = tree.module_info_path(t)
            jd = tree.jdeps_path(t)
            check(f"target {t}", True, f"{tree.lunch(t)} OUT_DIR={tree.out_dir(t)}")
            check(f"  module-info ({t})", mi.exists(), str(mi))
            check(f"  java deps ({t})", jd.exists(), str(jd))
            if t == cfg.stage_cfg("device_test").get("target"):
                from .emulator import image_ready
                check(f"  emulator image ({t})", image_ready(tree, t), "run `appci image` to build it")
        except Exception as exc:  # noqa: BLE001
            check(f"target {t}", False, str(exc))
    for name in tools.TOOLS:
        s = tools.spec(cfg, name)
        cached = (tools.cache_dir() / name / s["version"]).exists()
        check(f"tool {name} {s['version']}", True, "cached" if cached else "downloaded on first use")
    st = os.statvfs(cfg.aosp_root)
    free_gb = st.f_bavail * st.f_frsize / 1e9
    check("free disk", free_gb > 150, f"{free_gb:.0f} GB (emulator OUT_DIR needs ~150-250 GB)")
    check("CPUs", (os.cpu_count() or 0) >= 16, str(os.cpu_count()))
    from .stages.preupload import _python_ok, hook_pythonpath, pick_hook_python
    pcfg = cfg.stage_cfg("preupload")
    hook_py = pick_hook_python(cfg, pcfg)
    mods = list(pcfg.get("python_modules") or [])
    pypath = hook_pythonpath(cfg, pcfg, hook_py)     # fetches pinned wheels into the tools cache
    check("hook python", _python_ok(hook_py, mods, pypath),
          f"{hook_py} (needs >= 3.9 with {', '.join(mods) or 'no extra modules'}"
          f"{'; from tools cache: ' + pypath if pypath else ''})")
    mem = _meminfo()
    ram, swap = mem.get("MemTotal", 0) / 1048576, mem.get("SwapTotal", 0) / 1048576
    check("RAM + swap", ram + swap >= 80,
          f"{ram:.0f} GB RAM + {swap:.0f} GB swap; full-tree Soong analysis peaked at ~60 GB RSS here, "
          "keep >= 80 GB (e.g. add a 32 GB swapfile) or analysis gets OOM-killed")
    envs = cfg["stages"]["sonar"]["host_url_env"]
    envs = envs if isinstance(envs, list) else [envs]
    have = [e for e in envs if os.environ.get(e)]
    check("sonar", bool(have), f"{have[0]} set" if have else f"none of {', '.join(envs)} set (stage will be skipped)")
    width = max(len(c[0]) for c in checks)
    bad = 0
    for name, ok, detail in checks:
        mark = "OK " if ok else "!! "
        bad += not ok
        print(f"  {mark} {name.ljust(width)}  {detail}")
    return 0 if bad == 0 else 1


def cmd_discover(args) -> int:
    cfg = _cfg(args)
    tree = AospTree(cfg)
    target = args.target
    if args.refresh:
        tree.ensure_metadata(target, refresh=True)
    if args.app:
        from .discovery import discover_app
        for a in args.app:
            p = discover_app(tree, a, target)
            if args.json:
                print(json.dumps(p.to_dict(), indent=2))
                continue
            print(f"{p.name}  [{p.path}]  slug={p.slug}")
            print(f"  apps      : {', '.join(p.apps) or '-'}")
            print(f"  libraries : {', '.join(p.libs) or '-'}")
            print(f"  support   : {', '.join(p.support_modules) or '-'}")
            for t in p.tests:
                extra = f" (instrumentation_for {t.instrumentation_for})" if t.instrumentation_for else ""
                print(f"  test      : {t.name} [{t.kind}]{extra}")
            print(f"  sources   : {len(p.java_srcs)} java, {len(p.kt_srcs)} kotlin; tests: "
                  f"{len(p.test_java_srcs)} java, {len(p.test_kt_srcs)} kotlin")
        return 0
    from .discovery import scan_apps
    apps = scan_apps(tree, args.scan or None, target, installed_only=not args.include_not_installed)
    if args.json:
        print(json.dumps(apps, indent=2))
    else:
        for a in apps:
            print(f"{a['path']:<60} {', '.join(a['in_image'] or a['modules'])}")
        print(f"{len(apps)} app(s)")
    if args.write_config:
        _append_apps(cfg, [a["path"] for a in apps])
    return 0


def _append_apps(cfg: Config, paths: List[str]) -> None:
    target = cfg.source or (TOOL_ROOT / "appci.yaml")
    existing = {a["path"].strip("/") for a in cfg.get("apps") or []}
    new = [p for p in paths if p not in existing]
    if not new:
        util.info("no new apps to add")
        return
    text = target.read_text() if target.exists() else ""
    if "\napps: []" in text:
        text = text.replace("\napps: []", "\napps:")
    elif "\napps:" not in text and not text.startswith("apps:"):
        text += "\napps:\n"
    text = text.rstrip("\n") + "\n" + "".join(f"  - path: {p}\n    enabled: false\n" for p in new)
    target.write_text(text)
    util.ok(f"added {len(new)} app(s) to {target} (enabled: false — enable the ones to process)")


def cmd_run(args) -> int:
    cfg = _cfg(args)
    util.set_verbose(args.verbose)
    tree = AospTree(cfg)
    apps = _resolve_apps(cfg, tree, args)
    stages = _stages(cfg, args.stages, args.skip)
    if args.report_dir:
        report_root = Path(args.report_dir).resolve()
    else:
        report_root = cfg.workdir / "runs" / _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    report_root.mkdir(parents=True, exist_ok=True)
    from .context import make_context
    from .stages import run_pipeline
    from .reports.dashboard import write_aggregates

    statuses = []
    not_run = []
    tree_lock = util.file_lock(cfg.aosp_root / "out" / ".appci-tree.lock", "AOSP tree") if args.checkout \
        else contextlib.nullcontext()
    with tree_lock:
        for app in apps:
            if args.checkout:
                from .workspace import checkout
                checkout(cfg, cfg.app_entry(app)["path"].strip("/"), args.checkout, args.checkout_ref, args.force)
            ctx = make_context(cfg, tree, app, report_root, since=args.changed_since, ci=args.ci, slug=args.slug)
            results = run_pipeline(ctx, stages, fail_fast=args.fail_fast)
            statuses.extend(r.status for r in results)
            if args.ci:
                from .reports.console import print_details
                print_details(ctx, results)
            not_run.extend(r for r in results if r.metrics.get("not_run"))
    summary = write_aggregates(report_root, cfg.aosp_root)
    if not args.report_dir:
        latest = cfg.workdir / "latest"
        with contextlib.suppress(FileNotFoundError):
            latest.unlink()
        os.symlink(report_root, latest)
    print()
    for slug, a in summary["apps"].items():
        print(f"  {a['name']:<32} {a['status'].upper()}")
    util.info(f"dashboard: {report_root / 'index.html'}")
    if worst(statuses) in (FAILED, ERROR):
        return 1
    # Required work skipped for lack of runner resources (e.g. no emulator image): CI shows the job
    # as a warning (allow_failure: exit_codes [3]) instead of a silent pass.
    return 3 if args.ci and not_run else 0


def cmd_impact(args) -> int:
    cfg = _cfg(args)
    tree = AospTree(cfg)
    from .context import make_context
    from .impact import select_tests
    rc = 0
    for app in _resolve_apps(cfg, tree, args):
        ctx = make_context(cfg, tree, app, cfg.workdir / "impact", since=args.changed_since)
        imp = ctx.impact
        unit, _ = select_tests(ctx, ["robolectric", "host"])
        device, _ = select_tests(ctx, ["device"])
        if args.json:
            print(json.dumps({"app": app, **imp.to_dict(),
                              "selected_unit": [t.name for t in unit],
                              "selected_device": [t.name for t in device]}, indent=2))
            continue
        cs = ctx.changeset
        print(f"{ctx.profile.name} [{app}]")
        print(f"  change      : {'unknown' if cs is None else f'{len(cs.files)} file(s) vs {cs.base[:12]}'}")
        print(f"  mode        : {imp.mode}")
        for r in imp.reasons:
            print(f"  reason      : {r}")
        print(f"  unit tests  : {', '.join(t.name for t in unit) or '-'}")
        print(f"  device tests: {', '.join(t.name for t in device) or '-'}")
        for d in imp.dependents:
            print(f"  dependent   : {d['path']} (via {', '.join(d['via'][:3])})")
        if imp.dependents_truncated:
            print(f"  dependents  : +{imp.dependents_truncated} more not processed (impact.max_dependent_apps)")
    return rc


def cmd_fetch_baseline(args) -> int:
    """Download the target branch's latest report artifacts (for the APK size comparison)."""
    import io
    import urllib.request
    import zipfile
    api = os.environ.get("CI_API_V4_URL")
    project = os.environ.get("CI_PROJECT_ID")
    ref = args.ref or os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME") or os.environ.get("CI_DEFAULT_BRANCH")
    token = util.secret("CI_JOB_TOKEN")
    if not (api and project and ref and token):
        util.warn("fetch-baseline: not in a GitLab job (CI_API_V4_URL/CI_PROJECT_ID/CI_JOB_TOKEN missing); skipped")
        return 0
    from urllib.parse import quote
    url = f"{api}/projects/{project}/jobs/artifacts/{quote(ref, safe='')}/download?job={quote(args.job)}"
    req = urllib.request.Request(url, headers={"JOB-TOKEN": token})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    except OSError as exc:
        util.warn(f"fetch-baseline: no artifacts for job '{args.job}' on {ref}: {exc}; size check will be skipped")
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = [m for m in z.namelist() if "/results/" in m or m.endswith("summary.json")]
        z.extractall(out, members)
    util.ok(f"baseline from {ref} ({len(members)} file(s)) in {out}")
    return 0


def cmd_image(args) -> int:
    cfg = _cfg(args)
    util.set_verbose(args.verbose)
    tree = AospTree(cfg)
    target = args.target or cfg["stages"]["image"]["target"]
    log = cfg.workdir / "logs" / f"image-{util.slug(target)}.log"
    util.info(f"building {tree.lunch(target)} into {tree.out_dir(target)} (log: {log}) ...")
    with tree.lock(target):
        r = tree.shell(target, "m", log, timeout=cfg["stages"]["image"].get("timeout"))
    if not r.ok:
        util.error(f"image build failed:\n{r.tail(40)}")
        return 1
    util.ok(f"image ready in {tree.product_out(target)}")
    return 0


def cmd_baseline(args) -> int:
    cfg = _cfg(args)
    tree = AospTree(cfg)
    from .context import make_context
    from .model import StageResult
    from .stages.static import DetektStage, detekt_baseline
    rc = 0
    for app in _resolve_apps(cfg, tree, args):
        ctx = make_context(cfg, tree, app, cfg.workdir / "baseline-run")
        res = StageResult(stage="detekt", app=ctx.profile.slug)
        DetektStage().run(ctx, res, create_baseline=True)
        if res.status in (FAILED, ERROR):
            util.error(f"{app}: {res.summary}")
            rc = 1
        else:
            util.ok(f"{app}: {res.summary or detekt_baseline(ctx)}")
    return rc


def cmd_report(args) -> int:
    cfg = _cfg(args)
    from .reports.dashboard import write_aggregates
    root = Path(args.report_dir).resolve() if args.report_dir else (cfg.workdir / "latest").resolve()
    summary = write_aggregates(root, cfg.aosp_root)
    util.info(f"dashboard: {root / 'index.html'}")
    if args.open:
        import webbrowser
        webbrowser.open((root / "index.html").as_uri())
    return 1 if summary["status"] in (FAILED, ERROR) and args.fail_on_status else 0


def cmd_gitlab(args) -> int:
    cfg = _cfg(args)
    from .gitlab import generate
    text = generate(cfg, _stages(cfg, args.stages, args.skip))
    if args.out:
        Path(args.out).write_text(text)
        util.ok(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_checkout(args) -> int:
    cfg = _cfg(args)
    from .workspace import checkout
    checkout(cfg, args.app[0], args.source, args.ref, args.force)
    return 0


def cmd_workspace(args) -> int:
    from . import workspace
    # The tree may not exist yet: resolve without touching aosp_root-dependent settings.
    cfg = _cfg(args)
    ws = workspace.resolve(cfg)
    if args.print:
        print(json.dumps({**ws, "path": str(ws["path"]), "reference": str(ws["reference"])}, indent=2))
        return 0
    apps = args.app or ([os.environ["APPCI_APP"]] if os.environ.get("APPCI_APP") else [])
    # Git credentials exist only while the tree is synced; later steps run merge-request code.
    if os.environ.get("CI"):
        util.setup_git_credentials()
    try:
        st = workspace.ensure(cfg, apps, force_sync=args.sync, adopt=args.adopt, only_apps=args.only_apps,
                              repair=args.repair)
    finally:
        if os.environ.get("CI"):
            util.remove_git_credentials()
    print(f"workspace {st['path']} platform={st['platform']} synced_now={st['synced_now']}")
    return 0


def cmd_tools(args) -> int:
    cfg = _cfg(args)
    rc = 0
    for name in tools.TOOLS:
        s = tools.spec(cfg, name)
        if args.fetch:
            try:
                print(f"  {name:<14} {s['version']:<12} {tools.ensure(cfg, name)}")
            except RuntimeError as exc:
                print(f"  {name:<14} {s['version']:<12} FAILED: {exc}")
                rc = 1
        else:
            print(f"  {name:<14} {s['version']:<12} {s['url']}")
    return rc


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("global options")
    g.add_argument("--config", default=argparse.SUPPRESS,
                   help="appci.yaml (default: $APPCI_CONFIG or <tool>/appci.yaml)")
    g.add_argument("--aosp-root", default=argparse.SUPPRESS, help="AOSP checkout (default: auto-detect)")
    g.add_argument("--workdir", default=argparse.SUPPRESS, help="reports/cache dir (default: <aosp>/out/appci)")
    g.add_argument("--device-lunch", default=argparse.SUPPRESS, help="override targets.device.lunch")
    g.add_argument("--emulator-lunch", default=argparse.SUPPRESS, help="override targets.emulator.lunch")
    g.add_argument("--platform", default=argparse.SUPPRESS,
                   help="platform in appci.yaml `platforms` (default: $APPCI_PLATFORM)")
    g.add_argument("--set", action="append", default=argparse.SUPPRESS, metavar="KEY=VALUE",
                   help="override any config value, e.g. stages.build.extra_targets=[release] (repeatable)")
    p = argparse.ArgumentParser(prog="appci", parents=[common],
                                description="CI/QA pipeline for AOSP / AAOS system apps")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser

    def add_parser(name, **kw):
        return _add(name, parents=[common], **kw)
    sub.add_parser = add_parser  # type: ignore[assignment]

    def apps_args(sp, many=True):
        sp.add_argument("--app", action="append", help="app path relative to the AOSP root (repeatable)")
        if many:
            sp.add_argument("--all", action="store_true", help="every enabled app in appci.yaml")

    s = sub.add_parser("doctor", help="check the machine, tree and tools")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("discover", help="show what appci finds for an app, or scan for apps")
    s.add_argument("--app", action="append")
    s.add_argument("--scan", action="append", help="root to scan (default: discovery.scan_roots)")
    s.add_argument("--include-not-installed", action="store_true", help="also apps not in the device image")
    s.add_argument("--target", default="device")
    s.add_argument("--refresh", action="store_true", help="regenerate module-info first")
    s.add_argument("--json", action="store_true")
    s.add_argument("--write-config", action="store_true", help="append found apps to appci.yaml")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("run", help="run pipeline stages for one or more apps")
    apps_args(s)
    s.add_argument("--stages", help="comma list (default: stages.default)")
    s.add_argument("--skip", help="comma list of stages to skip")
    s.add_argument("--report-dir", help="output directory (default: <workdir>/runs/<timestamp>)")
    s.add_argument("--changed-since", help="git ref; gate findings on lines changed since it")
    s.add_argument("--checkout", metavar="SOURCE", help="git URL/path to fetch the commit under test from")
    s.add_argument("--checkout-ref", default="HEAD", help="ref to fetch from --checkout (default HEAD)")
    s.add_argument("--force", action="store_true", help="allow --checkout over local modifications")
    s.add_argument("--slug", help="report sub-directory name for the app (default: derived from path)")
    s.add_argument("--fail-fast", action="store_true")
    s.add_argument("--ci", action="store_true", help="CI mode")
    s.add_argument("-v", "--verbose", action="store_true", help="stream tool output")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("impact", help="show which tests / dependent apps a change affects")
    apps_args(s)
    s.add_argument("--changed-since", help="git ref (default: $CI_MERGE_REQUEST_DIFF_BASE_SHA)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_impact)

    s = sub.add_parser("fetch-baseline", help="download target-branch reports (GitLab job token)")
    s.add_argument("--job", default="build", help="job whose artifacts hold the reports")
    s.add_argument("--ref", help="branch (default: MR target branch, else default branch)")
    s.add_argument("--out", default="baseline")
    s.set_defaults(fn=cmd_fetch_baseline)

    s = sub.add_parser("image", help="full build of the emulator image (coverage-instrumented apps)")
    s.add_argument("--target")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_image)

    s = sub.add_parser("baseline", help="(re)create the Detekt baseline for apps")
    apps_args(s)
    s.set_defaults(fn=cmd_baseline)

    s = sub.add_parser("report", help="rebuild the dashboard / aggregates for a report dir")
    s.add_argument("--report-dir")
    s.add_argument("--open", action="store_true")
    s.add_argument("--fail-on-status", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("gitlab", help="generate the GitLab pipeline template (ci/gitlab/app-pipeline.yml)")
    s.add_argument("--stages")
    s.add_argument("--skip")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_gitlab)

    s = sub.add_parser("checkout", help="fetch + detach-checkout a commit into the tree's app repo")
    s.add_argument("--app", action="append", required=True)
    s.add_argument("--source", required=True)
    s.add_argument("--ref", default="HEAD")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_checkout)

    s = sub.add_parser("workspace", help="repo init/sync the platform's CI tree (persistent volume)")
    s.add_argument("--app", action="append", default=[], help="app project to reset / sync (repeatable)")
    s.add_argument("--sync", action="store_true", help="sync now regardless of the policy")
    s.add_argument("--only-apps", action="store_true", help="sync just the --app projects")
    s.add_argument("--adopt", action="store_true", help="manage an existing repo tree appci did not create")
    s.add_argument("--repair", action="store_true",
                   help="recover a broken tree: drop stale git locks, sync with --force-sync, restore files")
    s.add_argument("--print", action="store_true", help="show the resolved settings and exit")
    s.set_defaults(fn=cmd_workspace)

    s = sub.add_parser("tools", help="list or pre-fetch third-party tools")
    s.add_argument("--fetch", action="store_true")
    s.set_defaults(fn=cmd_tools)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get("CI"):
        util.seal_secrets()          # build/test/hook processes never inherit CI secrets
        util.trust_git_repos()
    try:
        return args.fn(args)
    except RuntimeError as exc:
        util.error(str(exc))
        return 1
    except KeyboardInterrupt:
        util.error("interrupted")
        return 130

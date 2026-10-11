"""Change-risk checks: what tests and linters do not see, judged against the change's base.

Findings are either *blockers* (gated by `max_blockers`) or *review* items (shown in the MR's Code
Quality widget / dashboard; gate them with `max_review_items` if wanted):

  blocker  privileged permission requested but missing from the app's privapp allowlist
  blocker  new aconfig flag without namespace/description/bug
  blocker  high-confidence secret (private key, cloud/GitLab/GitHub/Slack token) on an added line
  blocker  Soong API check failed for an SDK library of the app
  review   new uses-permission / permission definition / exported component / sharedUserId change
  review   Android.bp: new or removed dependencies, platform_apis / privileged / platform cert turned on
  review   possible secret (generic password/token assignment) on an added line
  review   production code changed without any test change
  gate     APK size growth vs the target branch build (max_apk_growth_pct)
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .. import util
from ..blueprint import strip_comments as _strip_bp_comments, top_level_blocks as _top_level_blocks
from ..model import ERROR, PASSED, SKIPPED, Finding, save_findings
from . import Stage, register

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
COMPONENTS = ("activity", "activity-alias", "service", "receiver", "provider")
BLOCKER, REVIEW = "blocker", "critical"

SECRET_PATTERNS = [
    (BLOCKER, "private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    (BLOCKER, "aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (BLOCKER, "gitlab-token", re.compile(r"\bglpat-[0-9A-Za-z_\-]{20,}\b")),
    (BLOCKER, "github-token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b")),
    (BLOCKER, "slack-token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
    (BLOCKER, "google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    (REVIEW, "generic-secret", re.compile(
        r"""(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*["'][^"'\s]{8,}["']""")),
]
ALLOW_MARKER = "appci:allow-secret"


# ------------------------------------------------------------------------------------ helpers
def _git_show(repo: Path, rev: str, rel: str) -> Optional[str]:
    out = util.capture(["git", "show", f"{rev}:{rel}"], cwd=repo)
    return out if out else None


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


# --- manifests
def manifest_facts(text: Optional[str]) -> dict:
    facts = {"package": "", "shared_uid": None, "uses": set(), "defines": {}, "exported": set()}
    if not text:
        return facts
    if re.search(r"<!ENTITY", text, re.I):
        facts["entities"] = True               # reported as a blocker; never expanded
        return facts
    try:
        root = util.xml_root(text, text=True)
    except ET.ParseError:
        return facts
    facts["package"] = root.get("package", "")
    facts["shared_uid"] = root.get(f"{ANDROID_NS}sharedUserId")
    for el in root.iter():
        tag = el.tag
        if tag in ("uses-permission", "uses-permission-sdk-23") and el.get(f"{ANDROID_NS}name"):
            facts["uses"].add(el.get(f"{ANDROID_NS}name"))
        elif tag == "permission" and el.get(f"{ANDROID_NS}name"):
            facts["defines"][el.get(f"{ANDROID_NS}name")] = el.get(f"{ANDROID_NS}protectionLevel", "normal")
        elif tag in COMPONENTS and el.get(f"{ANDROID_NS}name"):
            exp = el.get(f"{ANDROID_NS}exported")
            if exp == "true" or (exp is None and el.find("intent-filter") is not None):
                facts["exported"].add(f"{tag}:{el.get(f'{ANDROID_NS}name')}")
    return facts


def privileged_permissions(cfg, sources: List[str]) -> Set[str]:
    cache = cfg.workdir / "cache" / "privileged-permissions.json"
    key = [str(cfg.path(s)) for s in sources]
    data = util.read_json(cache)
    if data and data.get("sources") == key:
        return set(data["permissions"])
    perms: Set[str] = set()
    for src in key:
        facts = manifest_facts(_read(Path(src)))
        perms |= {n for n, lvl in facts["defines"].items() if "privileged" in (lvl or "")}
    util.write_json(cache, {"sources": key, "permissions": sorted(perms)})
    return perms


def privapp_allowlist(ctx, target: str, package: str) -> Tuple[Optional[Set[str]], List[str]]:
    """Permissions allowed for `package` by the privapp XML its app modules install (`required:`)."""
    minfo = ctx.tree.module_info(target)
    files: List[Path] = []
    for app in ctx.profile.apps:
        for req in (minfo.get(app) or {}).get("required") or []:
            if req.startswith("allowed_privapp") or "privapp" in req:
                files += [ctx.cfg.aosp_root / i for i in (minfo.get(req) or {}).get("installed") or []]
    allowed: Set[str] = set()
    found = []
    for f in files:
        try:
            root = util.xml_root(f)
        except (OSError, ET.ParseError):
            continue
        for pp in root.iter("privapp-permissions"):
            if pp.get("package") == package:
                found.append(str(f))
                allowed |= {p.get("name") for p in pp.findall("permission") if p.get("name")}
    return (allowed if found else None), found


# --- Android.bp


def aconfig_flags(text: Optional[str]) -> Tuple[Dict[str, dict], dict]:
    flags: Dict[str, dict] = {}
    header = {}
    if not text:
        return flags, header
    t = re.sub(r"(?m)#.*$", "", text)
    for key in ("package", "container"):
        m = re.search(rf'(?m)^\s*{key}\s*:\s*"([^"]+)"', t)
        if m:
            header[key] = m.group(1)
    for m in re.finditer(r"\bflag\s*\{", t):
        depth, j = 0, m.end() - 1
        while j < len(t):
            depth += t[j] == "{"
            depth -= t[j] == "}"
            if depth == 0:
                break
            j += 1
        body = t[m.end():j]
        fields = {k: v for k, v in re.findall(r'(?m)^\s*(name|namespace|description|bug)\s*:\s*"([^"]*)"', body)}
        if fields.get("name"):
            flags[fields["name"]] = fields
    return flags, header


def bp_modules(text: Optional[str]) -> Dict[str, dict]:
    """Top-level Blueprint modules -> {type, deps, platform_apis, privileged, certificate}."""
    mods: Dict[str, dict] = {}
    if not text:
        return mods
    t = _strip_bp_comments(text)
    for mtype, body in _top_level_blocks(t):
        # Only top-level properties: drop nested { ... } blocks.
        flat = re.sub(r"\{[^{}]*\}", "{}", body)
        flat = re.sub(r"\{[^{}]*\}", "{}", flat)
        name = re.search(r'(?m)^\s*name\s*:\s*"([^"]+)"', flat)
        if not name:
            continue
        deps: Set[str] = set()
        for key in ("static_libs", "libs", "shared_libs", "uses_libs", "optional_uses_libs", "jni_libs"):
            lm = re.search(rf"(?ms)^\s*{key}\s*:\s*\[(.*?)\]", flat)
            if lm:
                deps |= {f"{key}:{d}" for d in re.findall(r'"([^"]+)"', lm.group(1))}
        def flag(key):
            fm = re.search(rf"(?m)^\s*{key}\s*:\s*(true|false)", flat)
            return fm.group(1) == "true" if fm else False
        cert = re.search(r'(?m)^\s*certificate\s*:\s*"([^"]+)"', flat)
        mods[name.group(1)] = {"type": mtype, "deps": deps, "platform_apis": flag("platform_apis"),
                               "privileged": flag("privileged"), "certificate": cert.group(1) if cert else ""}
    return mods



def added_lines(repo: Path, base: str, prefix: str) -> List[Tuple[str, int, str]]:
    """(path, line, text) of every added line in the diff of the working tree against `base`."""
    out: List[Tuple[str, int, str]] = []
    diff = util.capture(["git", "diff", "--no-color", "-U0", "--no-renames", base], cwd=repo)
    path, line_no = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            path = None if p == "/dev/null" else f"{prefix}/{p[2:] if p.startswith('b/') else p}"
        elif raw.startswith("@@"):
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
            line_no = int(m.group(1)) if m else 0
        elif raw.startswith("+") and path:
            out.append((path, line_no, raw[1:]))
            line_no += 1
    return out


# ------------------------------------------------------------------------------------ stage
@register
class RiskStage(Stage):
    name = "risk"
    title = "Change risk (manifest, permissions, deps, flags, secrets, size, API)"

    def run(self, ctx, res):
        scfg = ctx.stage_cfg("risk")
        cs = ctx.changeset
        d = ctx.stage_dir("risk", clean=True)
        findings: List[Finding] = []
        checks: Dict[str, str] = {}
        p = ctx.profile
        repo = ctx.app_abs()
        target = ctx.cfg.stage_cfg("build", ctx.entry)["target"]

        def add(sev, rule, msg, path, line=0, category="risk"):
            findings.append(Finding("risk", rule, msg, path, line, severity=sev, category=category))

        # APK size works without a change base (needs a baseline report).
        self.apk_growth(ctx, res, scfg, checks)
        if cs is None:
            checks["diff checks"] = "skipped: change unknown (no MR diff base or --changed-since)"
        else:
            rel = lambda f: f[len(p.path) + 1:]  # noqa: E731
            changed = {f: st for f, st in cs.files.items() if f.startswith(p.path + "/")}
            self.manifests(ctx, target, scfg, cs, changed, repo, rel, add, checks)
            self.blueprints(cs, changed, repo, rel, add, checks)
            self.aconfig(cs, changed, repo, rel, add, checks)
            self.secrets(ctx, cs, repo, add, checks)
            self.tests_touched(ctx, scfg, cs, changed, add, checks)
        self.api_checks(ctx, target, scfg, d, res, add, checks)

        save_findings(ctx.findings_path("risk"), findings)
        blockers = [f for f in findings if f.severity == BLOCKER]
        review = [f for f in findings if f.severity == REVIEW]
        res.metrics.update({"blockers": len(blockers), "review_items": len(review),
                            "total": len(findings), "errors": len(blockers), "warnings": len(review)})
        util.write_json(d / "checks.json", checks)
        md = self.report_md(ctx, findings, checks)
        (d / "risk-report.md").write_text(md)
        from ..reports.html import markdown_page
        (d / "risk-report.html").write_text(markdown_page(md, f"Change risk: {ctx.profile.name}"))
        res.reports["risk report"] = ctx.rel_report(d / "risk-report.html")
        res.reports["risk report (md)"] = ctx.rel_report(d / "risk-report.md")
        res.summary = (f"{len(blockers)} blocker(s), {len(review)} item(s) for review"
                       + (f"; APK +{res.metrics['apk_growth_pct']}%" if "apk_growth_pct" in res.metrics else ""))
        res.status = PASSED
        self.apply_gate(ctx, res, issues_are_warning=True)

    # -------------------------------------------------------------------------- individual checks
    def manifests(self, ctx, target, scfg, cs, changed, repo, rel, add, checks):
        mans = [f for f in changed if f.endswith("AndroidManifest.xml") and changed[f] != "D"]
        if not mans:
            checks["manifest"] = "no manifest changed"
            return
        priv = privileged_permissions(ctx.cfg, list(scfg.get("permission_sources") or []))
        is_priv_app = any("/priv-app/" in i for a in ctx.profile.apps
                          for i in (ctx.tree.module_info(target).get(a) or {}).get("installed") or [])
        for f in mans:
            before = manifest_facts(_git_show(repo, cs.base, rel(f)) if changed[f] != "A" else None)
            after = manifest_facts(_read(ctx.cfg.aosp_root / f))
            if after.get("entities"):
                add(BLOCKER, "xml-entities", "manifest declares XML entities (<!ENTITY>); not allowed", f)
                continue
            for perm in sorted(after["uses"] - before["uses"]):
                add(REVIEW, "new-permission", f"new uses-permission {perm}", f)
            for perm, lvl in sorted(after["defines"].items()):
                if perm not in before["defines"] or before["defines"][perm] != lvl:
                    add(REVIEW, "permission-definition", f"permission {perm} defined/changed (protectionLevel={lvl})", f)
            for comp in sorted(after["exported"] - before["exported"]):
                add(REVIEW, "exported-component", f"newly exported {comp} (reachable by other apps)", f)
            if after["shared_uid"] != before["shared_uid"] and (after["shared_uid"] or before["shared_uid"]):
                add(REVIEW, "shared-user-id", f"sharedUserId {before['shared_uid']} -> {after['shared_uid']}", f)
            new_priv = sorted((after["uses"] - before["uses"]) & priv)
            if new_priv and is_priv_app:
                pkg = after["package"] or manifest_facts(_read(ctx.cfg.aosp_root / f))["package"]
                allowed, files = privapp_allowlist(ctx, target, pkg) if pkg else (None, [])
                if allowed is None:
                    for perm in new_priv:
                        add(REVIEW, "privapp-allowlist-unknown",
                            f"privileged permission {perm} added; no privapp allowlist found for {pkg or 'the app'}", f)
                else:
                    for perm in new_priv:
                        if perm not in allowed:
                            add(BLOCKER, "privapp-allowlist",
                                f"privileged permission {perm} is not in the privapp allowlist ({files[0]}): "
                                "the app will not get it (or will not boot on enforcing builds)", f)
        checks["manifest"] = f"{len(mans)} manifest(s) compared"

    def blueprints(self, cs, changed, repo, rel, add, checks):
        bps = [f for f in changed if f.endswith("Android.bp")]
        if not bps:
            checks["Android.bp"] = "no Android.bp changed"
            return
        for f in bps:
            before = bp_modules(_git_show(repo, cs.base, rel(f)) if changed[f] != "A" else None)
            after = bp_modules(_read_tree_file(repo, rel(f)))
            for name, mod in sorted(after.items()):
                old = before.get(name)
                if old is None:
                    add(REVIEW, "new-module", f"new {mod['type']} module {name}", f, category="build")
                    old = {"deps": set(), "platform_apis": False, "privileged": False, "certificate": ""}
                added = sorted(mod["deps"] - old["deps"])
                removed = sorted(old["deps"] - mod["deps"])
                if added:
                    add(REVIEW, "dependency-added", f"{name}: added {', '.join(added[:10])}", f, category="build")
                if removed:
                    add("minor", "dependency-removed", f"{name}: removed {', '.join(removed[:10])}", f, category="build")
                for key, label in (("platform_apis", "platform_apis: true (hidden/system API access)"),
                                   ("privileged", "privileged: true (priv-app)")):
                    if mod[key] and not old[key]:
                        add(REVIEW, "privilege-change", f"{name}: {label} enabled", f, category="build")
                if mod["certificate"] != old["certificate"] and mod["certificate"] in ("platform", "shared", "media"):
                    add(REVIEW, "privilege-change", f"{name}: certificate -> {mod['certificate']}", f, category="build")
            for name in sorted(set(before) - set(after)):
                add("minor", "module-removed", f"module {name} removed", f, category="build")
        checks["Android.bp"] = f"{len(bps)} file(s) compared"

    def aconfig(self, cs, changed, repo, rel, add, checks):
        files = [f for f in changed if f.endswith(".aconfig") and changed[f] != "D"]
        if not files:
            checks["aconfig"] = "no flag declarations changed"
            return
        for f in files:
            before, _ = aconfig_flags(_git_show(repo, cs.base, rel(f)) if changed[f] != "A" else None)
            after, header = aconfig_flags(_read_tree_file(repo, rel(f)))
            for key in ("package", "container"):
                if key not in header:
                    add(REVIEW, "aconfig-header", f"{key}: missing in flag declaration file", f, category="flags")
            for name, fl in sorted(after.items()):
                if name in before:
                    continue
                missing = [k for k in ("namespace", "description", "bug") if not fl.get(k)]
                if missing:
                    add(BLOCKER, "aconfig-flag", f"new flag {name} lacks {', '.join(missing)}", f, category="flags")
        checks["aconfig"] = f"{len(files)} file(s) checked"

    def secrets(self, ctx, cs, repo, add, checks):
        lines = added_lines(repo, cs.base, ctx.profile.path)
        in_diff = {l[0] for l in lines}
        for p, st in cs.files.items():               # untracked files are not in `git diff`
            if st == "A" and p not in in_diff:
                text = _read(ctx.cfg.aosp_root / p) or ""
                lines += [(p, i + 1, l) for i, l in enumerate(text.splitlines())]
        hits = 0
        seen = set()
        for path, no, text in lines:
            if ALLOW_MARKER in text or (path, no) in seen:
                continue
            for sev, rule, rx in SECRET_PATTERNS:
                if rx.search(text):
                    seen.add((path, no))
                    hits += 1
                    add(sev, f"secret-{rule}", f"possible {rule.replace('-', ' ')} on an added line "
                                               f"(mark false positives with '{ALLOW_MARKER}')", path, no,
                        category="security")
                    break
        checks["secrets"] = f"{len(lines)} added line(s) scanned, {hits} hit(s)"

    def tests_touched(self, ctx, scfg, cs, changed, add, checks):
        p = ctx.profile
        prod = set(p.java_srcs + p.kt_srcs)
        test_files = set(p.test_java_srcs + p.test_kt_srcs)
        test_dirs = tuple(f"{t.path}/" for t in p.tests if t.path)
        prod_lines = sum(len(cs.lines.get(f, ())) for f in changed
                         if f in prod or (f.endswith((".java", ".kt")) and "/test" not in f and changed[f] == "A"))
        tests_changed = any(f in test_files or f.startswith(test_dirs) or "/test" in f for f in changed)
        threshold = int(scfg.get("min_code_lines_without_tests") or 0)
        if threshold and prod_lines >= threshold and not tests_changed:
            add(REVIEW, "no-test-change", f"{prod_lines} line(s) of production code changed without any "
                                          "test change; add or update tests", f"{p.path}/Android.bp",
                category="tests")
        checks["tests"] = (f"{prod_lines} production line(s) changed, "
                           + ("tests changed too" if tests_changed else "no test changes"))

    def apk_growth(self, ctx, res, scfg, checks):
        current = (ctx.results.get("build").metrics.get("apk_bytes") if ctx.results.get("build") else None) or {}
        base_dir = scfg.get("baseline_dir") or os.environ.get("APPCI_BASELINE_DIR")
        if not current:
            checks["apk size"] = "skipped: no APKs from the build stage in this report"
            return
        if not base_dir:
            checks["apk size"] = "skipped: no baseline (set APPCI_BASELINE_DIR or `appci fetch-baseline`)"
            return
        base = None
        for f in Path(base_dir).glob("*/results/build.json"):
            data = util.read_json(f) or {}
            if f.parent.parent.name == ctx.profile.slug or base is None:
                base = (data.get("metrics") or {}).get("apk_bytes")
        if not base:
            checks["apk size"] = f"skipped: no build results in baseline {base_dir}"
            return
        worst_pct, worst_bytes, rows = 0.0, 0, []
        for name, size in current.items():
            old = base.get(name)
            if not old:
                continue
            pct = round(100.0 * (size - old) / old, 2)
            rows.append(f"{name}: {old} -> {size} bytes ({pct:+}%)")
            if pct > worst_pct:
                worst_pct, worst_bytes = pct, size - old
        res.metrics["apk_growth_pct"] = worst_pct
        res.metrics["apk_growth_bytes"] = worst_bytes
        checks["apk size"] = "; ".join(rows) or "no comparable APKs"

    def api_checks(self, ctx, target, scfg, d, res, add, checks):
        jdeps = ctx.tree.jdeps(target)
        libs = [m for m in ctx.profile.libs if (jdeps.get(m) or {}).get("module_type") == "java_sdk_library"]
        if not libs or not scfg.get("api_check", True):
            checks["api"] = "no java_sdk_library in the app" if not libs else "disabled"
            return
        if ctx.changeset is not None and not ctx.impact.changed_libraries and ctx.impact.mode != "full":
            checks["api"] = "SDK libraries unchanged"
            return
        targets = [f"{l}{suffix}-check-current-api" for l in libs
                   for suffix in (".stubs.source", ".stubs.source.system", ".stubs.source.module_lib")]
        log = d / "api-check.log"
        with ctx.tree.lock(target):
            r = ctx.tree.m(target, targets, log)
            unknown = set(re.findall(r"unknown target '([^']+)'", r.output()))
            if not r.ok and unknown:
                targets = [t for t in targets if t not in unknown]
                r = ctx.tree.m(target, targets, log, append=True) if targets else r
        if not targets:
            checks["api"] = "no API check targets"
            return
        if not r.ok:
            add(BLOCKER, "api-check", "Soong API check failed (API surface changed without updating "
                                      "api/*.txt, or an incompatible change); see api-check.log",
                f"{ctx.profile.path}/Android.bp", category="api")
            res.reports["api check log"] = ctx.rel_report(log)
        checks["api"] = f"{len(targets)} check target(s): {'passed' if r.ok else 'FAILED'}"

    @staticmethod
    def report_md(ctx, findings, checks) -> str:
        lines = [f"# Change risk: {ctx.profile.name}", ""]
        cs = ctx.changeset
        if cs:
            lines.append(f"Base `{cs.base[:12]}`, {len(cs.files)} file(s), {len(cs.commits)} commit(s).\n")
        lines.append("| Check | Result |\n|---|---|")
        lines += [f"| {k} | {v} |" for k, v in checks.items()]
        for title, sev in (("Blockers", BLOCKER), ("For review", REVIEW)):
            items = [f for f in findings if f.severity == sev]
            if items:
                lines += ["", f"## {title}", ""]
                lines += [f"- `{f.rule}` {f.path}{':' + str(f.line) if f.line else ''}: {f.message}" for f in items]
        other = [f for f in findings if f.severity not in (BLOCKER, REVIEW)]
        if other:
            lines += ["", "## Notes", ""] + [f"- `{f.rule}` {f.path}: {f.message}" for f in other]
        return "\n".join(lines) + "\n"


def _read_tree_file(repo: Path, rel: str) -> Optional[str]:
    return _read(repo / rel)

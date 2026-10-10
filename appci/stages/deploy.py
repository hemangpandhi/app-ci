"""Deliver the app as prebuilts: APKs per build variant, an Android.bp with android_app_import, the
privileged-permission allowlist, version data and checksums, committed to a prebuilts repository
(merge request or direct push) with a commit message describing the source changes.

Runs on release tags (GitLab job `deploy`), or by hand: `appci run --app <path> --stages deploy`.
Layout in the prebuilts repository (deploy.path, default "{name}"):

    CarSettings/
      Android.bp                      android_app_import; user / userdebug APK chosen by `debuggable`
      user/CarSettings.apk            built with targets.release (the product's user lunch target)
      userdebug/CarSettings.apk       built with targets.device
      permissions/com.android.car.settings.xml   privapp allowlist (prebuilt_etc, required by the app)
      VERSION.json                    version, source commit, variants, permissions, checksums
      SHA256SUMS
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .. import util
from ..aosp import _load_json
from ..model import ERROR, FAILED, PASSED, SKIPPED, WARNING
from . import Stage, register
from .risk import privapp_allowlist, privileged_permissions

PERM_RE = re.compile(r"^(?:uses-permission(?:-sdk-23)?):\s*name='([^']+)'", re.M)
PKG_RE = re.compile(r"^package:\s*(\S+)", re.M)
MR_RE = re.compile(r"See merge request (\S+![0-9]+)")


def _aapt2(ctx) -> Optional[str]:
    for rel in ("prebuilts/sdk/tools/linux/bin/aapt2", "out/host/linux-x86/bin/aapt2"):
        p = ctx.cfg.aosp_root / rel
        if p.is_file():
            return str(p)
    return shutil.which("aapt2")


def apk_package_and_permissions(aapt2: str, apk: Path) -> Tuple[str, Set[str]]:
    out = subprocess.run([aapt2, "dump", "permissions", str(apk)], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, check=True).stdout
    m = PKG_RE.search(out)
    return (m.group(1) if m else ""), set(PERM_RE.findall(out))


def apk_uses_libraries(aapt2: str, apk: Path) -> Tuple[List[str], List[str]]:
    """<uses-library> tags of the APK, in manifest order: (required, optional). android_app_import must
    declare the same (Soong's class-loader-context check for dexpreopt)."""
    out = subprocess.run([aapt2, "dump", "badging", str(apk)], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True).stdout
    req = re.findall(r"^uses-library:'([^']+)'", out, re.M)
    opt = re.findall(r"^uses-library-not-required:'([^']+)'", out, re.M)
    return req, opt


def allowlist_xml(package: str, allowed: Set[str], denied: Set[str], header: str) -> str:
    lines = ['<?xml version="1.0" encoding="utf-8"?>', f"<!-- {header} -->", "<permissions>",
             f'    <privapp-permissions package="{package}">']
    lines += [f'        <permission name="{p}"/>' for p in sorted(allowed)]
    lines += [f'        <deny-permission name="{p}"/>' for p in sorted(denied)]
    lines += ["    </privapp-permissions>", "</permissions>", ""]
    return "\n".join(lines)


def _denied(files: List[str], package: str) -> Set[str]:
    out: Set[str] = set()
    for f in files:
        try:
            root = util.xml_root(f)
        except (OSError, ET.ParseError):
            continue
        for pp in root.iter("privapp-permissions"):
            if pp.get("package") == package:
                out |= {p.get("name") for p in pp.findall("deny-permission") if p.get("name")}
    return out


def _partition(installed: str) -> str:
    """Partition from an install path (out*/target/product/<device>/<partition>/...)."""
    m = re.search(r"/target/product/[^/]+/([^/]+)/", "/" + installed.lstrip("/"))
    part = m.group(1) if m else ""
    if part == "system" and "/system/system_ext/" in installed:   # system_ext inside system
        return "system_ext"
    return part if part in ("system_ext", "product", "vendor", "odm") else "system"


PARTITION_PROP = {"system_ext": "system_ext_specific", "product": "product_specific",
                  "vendor": "vendor", "odm": "device_specific"}


def _blocks(text: str) -> List[str]:
    """Top-level `type { ... }` blocks of an Android.bp (comments removed)."""
    text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = text.rfind("\n", 0, i) + 1
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                out.append(text[start:i + 1])
    return out


def source_overrides(app_dir: Path, module: str) -> List[str]:
    """`overrides` of the app module in its sources (Android.bp `overrides: [...]` or Android.mk
    LOCAL_OVERRIDES_PACKAGES), so the prebuilt replaces the same apps the source build did."""
    name_re = re.compile(r'(?:^|[{,])\s*name:\s*"' + re.escape(module) + r'"', re.M)
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if "Android.bp" in files:
            for block in _blocks((Path(root) / "Android.bp").read_text(errors="replace")):
                if name_re.search(block):
                    m = re.search(r"(?:^|[{,])\s*overrides:\s*\[([^\]]*)\]", block, re.M)
                    return re.findall(r'"([^"]+)"', m.group(1)) if m else []
        if "Android.mk" in files:
            text = (Path(root) / "Android.mk").read_text(errors="replace").replace("\\\n", " ")
            for part in text.split("include $(CLEAR_VARS)"):
                if re.search(r"^LOCAL_PACKAGE_NAME\s*:?=\s*" + re.escape(module) + r"\s*$", part, re.M):
                    m = re.search(r"^LOCAL_OVERRIDES_PACKAGES\s*[:+]?=\s*(.*)$", part, re.M)
                    return m.group(1).split() if m else []
    return []


def android_bp(apps: List[Dict], variants: Dict[str, str], scfg: Dict, header: str) -> str:
    """android_app_import per app (apk chosen by `debuggable` when both variants are delivered) and a
    prebuilt_etc for each privapp allowlist."""
    out = [f"// {header}", "// Generated by appci deploy; do not edit by hand.", ""]
    debug_dir = next((d for d in variants if "debug" in d), None)
    user_dir = next((d for d in variants if "debug" not in d), None)
    for a in apps:
        name, apk = a["module"], a["apk_name"]
        if debug_dir and user_dir:
            src = (f'select(product_variable("debuggable"), {{\n        true: "{debug_dir}/{apk}",\n'
                   f'        default: "{user_dir}/{apk}",\n    }})')
        else:
            src = f'"{next(iter(variants))}/{apk}"'
        props = [f'    name: "{name}",', f"    apk: {src},"]
        if scfg.get("presigned"):
            props.append("    presigned: true,")
        else:
            props.append(f'    certificate: "{scfg.get("certificate") or "platform"}",')
        if a["privileged"]:
            props.append("    privileged: true,")
        if a.get("overrides"):
            props.append("    overrides: [" + ", ".join(f'"{x}"' for x in a["overrides"]) + "],")
        for key in ("uses_libs", "optional_uses_libs"):
            if a.get(key):
                props.append(f"    {key}: [" + ", ".join(f'"{x}"' for x in a[key]) + "],")
        part = PARTITION_PROP.get(a["partition"])
        if part:
            props.append(f"    {part}: true,")
        if scfg.get("prefer"):
            # Only for trees that keep the source app: Soong then uses this prebuilt instead. Products
            # with auto-generated RROs for the app fail with it; remove the source project instead.
            props.append("    prefer: true,")
        props.append(f"    dex_preopt: {{ enabled: {str(bool(scfg.get('dex_preopt', True))).lower()} }},")
        if a.get("allowlist_module"):
            props.append(f'    required: ["{a["allowlist_module"]}"],')
        out += ["android_app_import {", *props, "}", ""]
        if a.get("allowlist_module"):
            # Own file name: the platform's allowed_privapp_<package> module installs <package>.xml, and
            # SystemConfig merges every privapp-permissions file of a package.
            etc = [f'    name: "{a["allowlist_module"]}",', '    sub_dir: "permissions",',
                   f'    src: "permissions/{a["package"]}.xml",', f'    filename: "{a["package"]}-prebuilt.xml",']
            if part:
                etc.append(f"    {part}: true,")
            out += ["prebuilt_etc {", *etc, "}", ""]
    return "\n".join(out)


def source_history(repo: Path, version: str) -> Tuple[str, List[Dict[str, str]]]:
    """Previous release tag and the commits since it (newest first)."""
    def git(*args) -> str:
        return util.capture(["git", *args], cwd=repo).strip()
    head = git("rev-parse", "HEAD")
    prev = git("describe", "--tags", "--abbrev=0", f"{head}^") if git("rev-list", "--count", head) != "1" else ""
    rng = f"{prev}..{head}" if prev and prev != version else head
    fmt = "%H%x1f%h%x1f%an%x1f%s%x1f%b%x1e"
    commits = []
    for rec in git("log", "--no-color", f"--format={fmt}", "-n", "200", rng).split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) >= 4 and parts[0]:
            mr = MR_RE.search(parts[4] if len(parts) > 4 else "")
            commits.append({"sha": parts[0], "short": parts[1], "author": parts[2], "title": parts[3],
                            "mr": mr.group(1) if mr else ""})
    return (prev if prev != version else ""), commits


def changed_areas(repo: Path, prev: str) -> Dict[str, int]:
    if not prev:
        return {}
    files = util.capture(["git", "diff", "--name-only", f"{prev}..HEAD"], cwd=repo).split()
    areas: Dict[str, int] = {}
    for f in files:
        key = f.split("/")[0] if "/" in f else "(top level)"
        areas[key] = areas.get(key, 0) + 1
    return dict(sorted(areas.items(), key=lambda kv: -kv[1]))


def prebuilt_gaps(minfo: Dict[str, dict], app_path: str, delivered: Set[str], product: Set[str]) -> Dict[str, List[str]]:
    """What a build from the prebuilts manifest (without the source project) would lose:
    `left_behind`: modules defined in the source project that the product installs or a delivered app
    requires, but that are not delivered; `auto_rros`: product overlays Soong generates for the source
    app (`<app>__<product>__auto_generated_rro_*`), which it does not generate for android_app_import."""
    root = app_path.strip("/") + "/"
    required = {r for m in delivered for r in (minfo.get(m) or {}).get("required") or []}
    left, rros = [], []
    for name, info in minfo.items():
        if name in delivered or not any((p.strip("/") + "/").startswith(root) for p in info.get("path") or []):
            continue
        if "__auto_generated_rro" in name:
            if name.split("__", 1)[0] in delivered and info.get("installed"):
                rros.append(name)
        elif (name in product or name in required) and info.get("installed"):
            left.append(name)
    return {"left_behind": sorted(left), "auto_rros": sorted(rros)}


def commit_message(name: str, version: str, src_url: str, src_sha: str, prev: str,
                   commits: List[Dict[str, str]], areas: Dict[str, int], perm_changes: Dict[str, Dict[str, List[str]]],
                   variants: Dict[str, str], lunches: Dict[str, str], sums: Dict[str, str],
                   gaps: Optional[Dict[str, List[str]]] = None) -> str:
    lines = [f"{name} {version}: prebuilt from {src_url.rstrip('/').split('/')[-1] or name}@{src_sha[:12]}", ""]
    lines.append(f"Source: {src_url}/-/commit/{src_sha}" if src_url else f"Source commit: {src_sha}")
    lines.append(f"Changes since {prev} ({len(commits)} commit(s)):" if prev else f"Commits ({len(commits)}):")
    for c in commits[:100]:
        mr = f" ({c['mr']})" if c["mr"] else ""
        lines.append(f"  {c['short']} {c['title']} [{c['author']}]{mr}")
    if len(commits) > 100:
        lines.append(f"  ... {len(commits) - 100} more")
    if areas:
        lines.append("Files changed: " + ", ".join(f"{k} {v}" for k, v in areas.items()))
    for pkg, ch in perm_changes.items():
        if ch["added"] or ch["removed"]:
            lines.append(f"Privileged permissions of {pkg}:")
            lines += [f"  + {p}" for p in ch["added"]] + [f"  - {p}" for p in ch["removed"]]
        if ch.get("not_in_platform_allowlist"):
            lines.append("  not in the platform's allowlist (review!): " + ", ".join(ch["not_in_platform_allowlist"]))
    gaps = gaps or {}
    if gaps.get("left_behind"):
        lines.append("Not delivered, but defined in the source project and installed by the product "
                     "(a prebuilts-manifest build must provide them): " + ", ".join(gaps["left_behind"]))
    if gaps.get("auto_rros"):
        lines.append("Product overlays applied through auto-generated RROs in the source build (Soong does not "
                     "generate them for android_app_import; check the prebuilts build): " + ", ".join(gaps["auto_rros"]))
    lines.append("Variants: " + ", ".join(f"{d} ({lunches[d]})" for d in variants))
    by_name: Dict[str, Set[str]] = {}
    for f, s in sums.items():
        if f.endswith(".apk") and "/" in f:
            by_name.setdefault(f.split("/", 1)[1], set()).add(s)
    same = [n for n, digests in by_name.items() if len(digests) == 1 and len(variants) > 1]
    if same:
        lines.append(f"Identical in all variants: {', '.join(sorted(same))}")
    lines.append("SHA-256:")
    lines += [f"  {s}  {f}" for f, s in sums.items()]
    url = os.environ.get("CI_PIPELINE_URL")
    if url:
        lines.append(f"Pipeline: {url}")
    return "\n".join(lines) + "\n"


def missing_token(scfg: Dict) -> Optional[str]:
    """Why delivery cannot authenticate, or None (checked before the builds, and again at clone)."""
    if scfg.get("mode") == "local" or not str(scfg.get("repo") or "").startswith("http"):
        return None
    var = scfg.get("token_env") or "APPCI_DEPLOY_TOKEN"
    if util.secret(var):
        return None
    why = ""
    if os.environ.get("CI_COMMIT_REF_PROTECTED") == "false":
        why = (f"; {os.environ.get('CI_COMMIT_REF_NAME')} is not a protected "
               f"{'tag' if os.environ.get('CI_COMMIT_TAG') else 'branch'}, and protected CI/CD "
               "variables only reach protected refs (Settings > Repository > Protected tags, e.g. v*)")
    return f"no deploy token: ${var} is not set for this job{why}"


class Delivery:
    """Clone the prebuilts repository, replace the app's folder, commit, push / open an MR."""

    def __init__(self, ctx, scfg: Dict, log: Path):
        self.ctx, self.scfg, self.log = ctx, scfg, log
        self.repo_url = scfg["repo"]
        self.token = util.secret(scfg.get("token_env") or "APPCI_DEPLOY_TOKEN")
        self.work = Path(tempfile.mkdtemp(prefix="appci-deploy-"))

    def _git(self, *args, cwd: Optional[Path] = None, check: bool = True) -> util.CommandResult:
        # Credentials only for this git process, via a helper reading an env var (never argv/logs).
        env = {"APPCI_DEPLOY_PASSWORD": self.token or "", "GIT_TERMINAL_PROMPT": "0"}
        helper = "!f() { echo username=oauth2; echo \"password=$APPCI_DEPLOY_PASSWORD\"; }; f"
        cmd = ["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}",
               "-c", "user.name=" + (self.scfg.get("author_name") or "appci"),
               "-c", "user.email=" + (self.scfg.get("author_email") or "appci@localhost"), *args]
        r = util.run(cmd, log=self.log, cwd=cwd or self.work / "repo", env=env, append=True)
        if check and not r.ok:
            raise RuntimeError(f"git {args[0]} failed: {r.tail(8)}")
        return r

    def checkout(self) -> Path:
        problem = missing_token(self.scfg)
        if problem:
            raise RuntimeError(problem)
        self._git("clone", "-q", "--depth", "1", "-b", self.scfg["branch"], self.repo_url, str(self.work / "repo"),
                  cwd=self.work)
        return self.work / "repo"

    def commit_and_deliver(self, folder: str, branch: str, message: str) -> Dict[str, str]:
        repo = self.work / "repo"
        self._git("add", "-A", folder)
        if self._git("diff", "--cached", "--quiet", check=False).ok:
            return {"result": "unchanged"}
        msg = self.work / "msg.txt"
        msg.write_text(message)
        self._git("commit", "-q", "-F", str(msg))
        mode = self.scfg.get("mode", "mr")
        sha = util.capture(["git", "rev-parse", "HEAD"], cwd=repo).strip()
        if mode == "local":
            return {"result": "committed locally", "commit": sha, "path": str(repo)}
        if mode == "push":
            self._git("push", "-q", "origin", f"HEAD:{self.scfg['branch']}")
            return {"result": f"pushed to {self.scfg['branch']}", "commit": sha}
        self._git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
        return self._open_mr(branch, message, sha)

    def _open_mr(self, branch: str, message: str, sha: str) -> Dict[str, str]:
        u = urllib.parse.urlparse(self.repo_url)
        project = urllib.parse.quote(re.sub(r"\.git$", "", u.path.strip("/")), safe="")
        api = f"{u.scheme}://{u.netloc}/api/v4/projects/{project}/merge_requests"
        title, _, body = message.partition("\n")
        data = urllib.parse.urlencode({"source_branch": branch, "target_branch": self.scfg["branch"],
                                       "title": title, "description": "```\n" + body.strip() + "\n```",
                                       "remove_source_branch": "true"}).encode()
        req = urllib.request.Request(api, data=data, method="POST")
        req.add_unredirected_header("PRIVATE-TOKEN", self.token or "")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                mr = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code != 409:                     # 409: an MR for this branch already exists
                raise RuntimeError(f"creating the merge request failed: HTTP {exc.code} {exc.read()[:300]!r}")
            q = urllib.request.Request(f"{api}?state=opened&source_branch={urllib.parse.quote(branch)}")
            q.add_unredirected_header("PRIVATE-TOKEN", self.token or "")
            with urllib.request.urlopen(q, timeout=60) as r:
                found = json.loads(r.read().decode())
            mr = found[0] if found else {}
        return {"result": "merge request", "commit": sha, "mr": mr.get("web_url", "")}

    def cleanup(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)


@register
class DeployStage(Stage):
    name = "deploy"
    title = "Prebuilt delivery"

    def run(self, ctx, res):
        scfg = dict(ctx.stage_cfg("deploy"))
        scfg["repo"] = os.environ.get("APPCI_DEPLOY_REPO") or scfg.get("repo")
        scfg["branch"] = os.environ.get("APPCI_DEPLOY_BRANCH") or scfg.get("branch") or "main"
        scfg["mode"] = os.environ.get("APPCI_DEPLOY_MODE") or scfg.get("mode") or "mr"
        p = ctx.profile
        if not scfg.get("repo"):
            res.status, res.summary = SKIPPED, "no deploy.repo configured"
            return
        problem = missing_token(scfg)
        if problem:                              # fail before spending an hour on two builds
            res.status, res.summary = ERROR, problem
            return
        version = scfg.get("version") or os.environ.get("CI_COMMIT_TAG") or \
            util.capture(["git", "describe", "--tags", "--always"], cwd=ctx.app_abs()).strip()
        aapt2 = _aapt2(ctx)
        if not aapt2:
            res.status, res.summary = ERROR, "aapt2 not found (prebuilts/sdk/tools/linux/bin/aapt2)"
            return
        d = ctx.stage_dir("deploy", clean=True)
        log = d / "deploy.log"
        res.log = ctx.rel_report(log)
        name = p.name
        stage_dir = d / "package"
        variants: Dict[str, str] = dict(scfg.get("variants") or {"user": "release", "userdebug": "device"})
        lunches = {folder: ctx.tree.lunch(t) for folder, t in variants.items()}

        # 1. Build every variant and collect the apps' APKs.
        apps: Dict[str, Dict] = {}
        for folder, target in variants.items():
            util.info(f"deploy: building {', '.join(p.apps)} for {folder} ({lunches[folder]})")
            with ctx.tree.lock(target):
                # module-info in the same Soong run: where each APK is installed for this variant
                r = ctx.tree.m(target, list(p.apps) + ["module-info"], log, append=True, timeout=scfg.get("timeout"))
            if not r.ok:
                res.status, res.summary = FAILED, f"{folder} build ({lunches[folder]}) failed: {r.tail(3)}"
                return
            _load_json.cache_clear()                 # module-info.json was just (re)written
            minfo = ctx.tree.module_info(target)
            for m in p.apps:
                inst = [i for i in (minfo.get(m) or {}).get("installed") or [] if i.endswith(".apk")]
                if not inst:
                    continue
                src = ctx.cfg.aosp_root / inst[0]
                (stage_dir / folder).mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, stage_dir / folder / src.name)
                a = apps.setdefault(m, {"module": m, "apk_name": src.name, "installed": inst[0],
                                        "partition": _partition(inst[0]), "privileged": "/priv-app/" in inst[0],
                                        "perms": set(), "required": (minfo.get(m) or {}).get("required") or []})
                pkg, perms = apk_package_and_permissions(aapt2, stage_dir / folder / src.name)
                a["package"], a["perms"] = pkg, a["perms"] | perms
                a["uses_libs"], a["optional_uses_libs"] = apk_uses_libraries(aapt2, stage_dir / folder / src.name)
        if not apps:
            res.status, res.summary = ERROR, "no APK installed by the app modules"
            return
        for m, a in apps.items():
            # Apps the source module replaces (e.g. CarSettings overrides Settings); config wins.
            conf = scfg.get("overrides")
            a["overrides"] = list(conf.get(m, []) if isinstance(conf, dict) else conf) if conf is not None \
                else source_overrides(ctx.app_abs(), m)
            if a["overrides"]:
                util.info(f"deploy: {m} overrides {', '.join(a['overrides'])}")
        dev_target = next((t for t in variants.values() if t == "device"), next(iter(variants.values())))
        try:
            product = ctx.tree.product_packages(dev_target)
        except Exception:                        # informational only
            product = set()
        gaps = prebuilt_gaps(ctx.tree.module_info(dev_target), p.path, set(apps), product)
        for key, what in (("left_behind", "not delivered but installed by the product"),
                          ("auto_rros", "auto-generated product RROs (not generated for prebuilts)")):
            if gaps[key]:
                util.warn(f"deploy: {what}: {', '.join(gaps[key])}")

        # 2. Privileged-permission allowlists: what the APKs request, filtered to privileged ones.
        priv = privileged_permissions(ctx.cfg, list(ctx.stage_cfg("risk").get("permission_sources") or []))
        delivery = Delivery(ctx, scfg, log)
        try:
            repo = delivery.checkout()
            folder_rel = (scfg.get("path") or "{name}").format(name=name, slug=p.slug, app=p.path)
            dest = repo / folder_rel
            perm_changes: Dict[str, Dict[str, List[str]]] = {}
            header = f"{name} {version} from {p.path}@{util.capture(['git', 'rev-parse', 'HEAD'], cwd=ctx.app_abs()).strip()[:12]}"
            for a in apps.values():
                if not a["privileged"]:
                    continue
                wanted = sorted(a["perms"] & priv)
                platform_allowed, files = privapp_allowlist(ctx, variants[next(iter(variants))], a["package"])
                old_file = dest / "permissions" / f"{a['package']}.xml"
                old: Set[str] = set()
                if old_file.is_file():
                    try:
                        old = {e.get("name") for e in util.xml_root(old_file).iter("permission") if e.get("name")}
                    except ET.ParseError:
                        pass
                perm_changes[a["package"]] = {
                    "added": sorted(set(wanted) - old), "removed": sorted(old - set(wanted)),
                    "not_in_platform_allowlist": sorted(set(wanted) - (platform_allowed or set()))
                    if platform_allowed is not None else []}
                (stage_dir / "permissions").mkdir(parents=True, exist_ok=True)
                (stage_dir / "permissions" / f"{a['package']}.xml").write_text(
                    allowlist_xml(a["package"], set(wanted), _denied(files, a["package"]), header))
                a["allowlist_module"] = f"allowed_privapp_{a['package']}_prebuilt"

            # 3. Android.bp, version data, checksums.
            (stage_dir / "Android.bp").write_text(android_bp(list(apps.values()), variants, scfg, header))
            sums = {str(f.relative_to(stage_dir)): util.sha256_file(f)
                    for f in sorted(stage_dir.rglob("*")) if f.is_file()}
            (stage_dir / "SHA256SUMS").write_text("".join(f"{s}  {f}\n" for f, s in sums.items()))
            src_repo = Path(os.environ.get("CI_PROJECT_DIR") or ctx.app_abs())
            prev, commits = source_history(src_repo, version)
            src_sha = util.capture(["git", "rev-parse", "HEAD"], cwd=src_repo).strip()
            info = {"name": name, "version": version, "source": {"path": p.path, "commit": src_sha,
                    "project": os.environ.get("CI_PROJECT_URL", ""), "previous_release": prev},
                    "variants": {f: {"lunch": lunches[f]} for f in variants},
                    "apps": {m: {"package": a.get("package"), "apk": a["apk_name"], "partition": a["partition"],
                                 "privileged": a["privileged"],
                                 "privileged_permissions": sorted(a["perms"] & priv)} for m, a in apps.items()},
                    "permission_changes": perm_changes, "prebuilt_build_checks": gaps, "sha256": sums,
                    "pipeline": os.environ.get("CI_PIPELINE_URL", ""),
                    "created": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            (stage_dir / "VERSION.json").write_text(json.dumps(info, indent=2) + "\n")
            areas = changed_areas(src_repo, prev)
            message = commit_message(name, version, os.environ.get("CI_PROJECT_URL", ""), src_sha, prev,
                                     commits, areas, perm_changes, variants, lunches, sums, gaps)
            (d / "commit-message.txt").write_text(message)
            shutil.copytree(stage_dir, d / "delivered")

            # 4. Replace the app's folder in the prebuilts repository and deliver.
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(stage_dir, dest)
            branch = f"appci/{p.slug}-{re.sub(r'[^A-Za-z0-9._-]', '_', version)}"
            out = delivery.commit_and_deliver(folder_rel, branch, message)
        except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            res.status, res.summary = ERROR, f"delivery failed: {exc}"[:300]
            return
        finally:
            delivery.cleanup()
        shutil.rmtree(stage_dir, ignore_errors=True)
        res.metrics.update({"version": version, "variants": list(variants), "apps": len(apps),
                            "commits": len(commits), **{k: v for k, v in out.items() if k != "path"}})
        res.reports["commit message"] = ctx.rel_report(d / "commit-message.txt")
        res.reports["delivered files"] = ctx.rel_report(d / "delivered")
        warn = [f"privileged permissions not in the platform allowlist: {pkg}: {', '.join(c['not_in_platform_allowlist'])}"
                for pkg, c in perm_changes.items()
                if c["not_in_platform_allowlist"]]
        if gaps["left_behind"]:
            warn.append("not delivered but installed by the product: " + ", ".join(gaps["left_behind"]))
        if gaps["auto_rros"]:
            warn.append("product overlays via auto-generated RROs (check the prebuilts build): "
                        + ", ".join(gaps["auto_rros"]))
        res.status = WARNING if warn else PASSED
        where = out.get("mr") or out.get("result")
        res.summary = (f"{name} {version} ({', '.join(variants)}): {where}"
                       + (f"; check: {'; '.join(warn)}" if warn else ""))

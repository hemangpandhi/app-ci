"""Configuration loading.

Precedence (later wins): built-in DEFAULTS -> appci.yaml -> per-app overrides
(`apps[].stages`) -> command-line flags.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

TOOL_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_STAGES = [
    "preupload", "build", "risk", "lint", "checkstyle", "ktlint", "detekt",
    "unit_test", "device_test", "coverage", "docs", "sonar",
]

DEFAULTS: Dict[str, Any] = {
    # Root of the AOSP checkout. null = auto-detect (cwd upwards, then APPCI_AOSP_ROOT); with a
    # platform selected, its workspace.
    "aosp_root": None,
    # Selected platform (or $APPCI_PLATFORM / --platform): its section under `platforms` is merged
    # over this config (targets, stages, apps, ...) and its workspace becomes the AOSP root.
    "platform": None,
    "platforms": {},
    # CI variables that look like secrets (names with TOKEN, PASSWORD, SECRET, ...) are hidden from
    # every build/test/hook process. Names listed here are passed to the build environment anyway
    # (e.g. a password a vendor build step needs to unpack resources); per platform under
    # platforms.<name>.build_env_secrets. Merge-request code can read them.
    "build_env_secrets": [],
    # repo-managed CI trees (`appci workspace`, see appci/workspace.py). Per platform under
    # platforms.<name>.workspace; manifest under platforms.<name>.manifest {url, branch, file, groups, depth}.
    # Prebuilt emulator from Artifactory (or any HTTP folder / local dir) instead of `appci image`:
    # newest file matching `pattern` in the `url` folder, cached on the runner volume. Per platform
    # under platforms.<name>.emulator; CI variables APPCI_EMULATOR_IMAGE_URL / _PATTERN and
    # APPCI_EMULATOR_PROGRAM_URL / _PATTERN override. No image url = the image built in the tree.
    "emulator": {
        "image": {"url": None, "pattern": "RELEASE_*.zip"},       # userdebug, test-keys system images
        "program": {"url": None, "pattern": "emulator*.zip"},     # none = prebuilts/android-emulator
        # Refuse releases whose SHA-256 the server does not publish (cached copies are re-checked
        # against it on every use; the runner volume is writable by other jobs).
        "require_checksum": True,
    },
    "workspace": {
        "path": "/appci/{platform}",          # on the runner's persistent volume
        "tree": "",                           # subdirectory holding the AOSP tree when the manifest nests it (e.g. android)
        "reference": "/build_env",            # .repo cache inside the platform image
        "seed": "copy",                       # copy | reference (git alternates) | none
        "adopt": False,                       # manage a tree appci did not create (tree baked into the image)
        "repo": {"command": None},            # default: repo on PATH, else seeded from the cache
        "init_args": [],
        "local_manifests": [],
        "sync": {"policy": "if-older",        # if-older | always | never (tree baked into the image)
                 "max_age_hours": 12, "jobs": 8, "timeout": 4 * 3600,
                 "args": None},               # None = -c --no-tags --optimized-fetch -d --retry-fetches=3
    },
    # Where reports/logs go. null = <aosp_root>/out/appci (pruned by Soong, wiped by `m clean`).
    "workdir": None,
    # JDK used for host-side analysis tools (relative to aosp_root or absolute).
    "java_home": "prebuilts/jdk/jdk21/linux-x86",
    # Lunch targets. Each target gets its own OUT_DIR so switching never forces re-analysis.
    "targets": {
        "device": {
            "lunch": "aosp_tangorpro_car-trunk_staging-userdebug",
            "out_dir": "out",
            "env": {},
        },
        "emulator": {
            "lunch": "sdk_car_x86_64-trunk_staging-userdebug",
            "out_dir": "out_appci_emu",
            # Builds in this OUT_DIR get EMMA_INSTRUMENT + JAVA_COVERAGE_PATHS=<app paths>.
            "coverage": True,
            "env": {},
        },
    },
    "stages": {
        "default": DEFAULT_STAGES,
        # extra_targets: also build the app for these targets (e.g. "release" = the user variant),
        # each in its own OUT_DIR; APKs are collected as <name>-<target>.apk.
        "build": {"target": "device", "include_tests": True, "extra_modules": [], "extra_targets": [],
                  "timeout": 14400},
        # Gates on `new_*` metrics only apply when the change under test is known
        # (MR pipelines / --changed-since); full scans then just report.
        "preupload": {
            "runner": "tools/repohooks/pre-upload.py",
            # Gerrit-only rules make no sense for GitLab merge requests.
            "skip_hooks": ["commit_msg_changeid_field"],
            "project": None,                      # repo project name; default: the app path
            "python": None,                       # hook interpreter; default: auto (see below)
            # Modules hooks need (RRO overlayable checks use lxml). Pinned ones the chosen interpreter
            # lacks are fetched as wheels into the tools cache (PyPI SHA-256 checked), never pip-installed.
            "python_modules": ["lxml==5.3.0"],
            "jobs": 4, "max_commits": 50, "timeout": 1800,
            "gate": {"max_hooks_failed": 0},
        },
        "risk": {
            # Manifests defining permissions; their protectionLevel tells which are privileged.
            "permission_sources": ["frameworks/base/core/res/AndroidManifest.xml",
                                   "packages/services/Car/service/AndroidManifest.xml"],
            "min_code_lines_without_tests": 20,   # review item when exceeded with no test change
            "baseline_dir": None,                 # report dir of the target branch (APK size)
            "api_check": True,
            "gate": {"max_blockers": 0, "max_apk_growth_pct": 10, "max_review_items": None},
        },
        "lint": {"target": "device", "timeout": 7200, "modules": None, "gate": {"max_new_errors": 0}},
        "checkstyle": {
            "config": "prebuilts/checkstyle/android-style.xml",
            "jar": "prebuilts/checkstyle/checkstyle.jar",
            "include_tests": False,
            "gate": {"max_new_errors": 0},
        },
        "ktlint": {
            "jar": "prebuilts/ktlint/ktlint-android-all.jar",
            "editorconfig": "prebuilts/ktlint/.editorconfig",
            "include_tests": True,
            "gate": {"max_new_errors": 0},
        },
        "detekt": {
            "config": str(TOOL_ROOT / "config" / "detekt.yml"),
            # Per-app baseline; {tool} = aosp-app-ci root, {app} = app path, {slug} = app slug.
            "baseline": "{tool}/baselines/{slug}/detekt-baseline.xml",
            "include_tests": True,
            "gate": {"max_new_errors": 0},
        },
        "unit_test": {
            "target": "device", "coverage": True, "atest_args": [], "timeout": 10800,
            "gate": {"max_failed": 0},
        },
        "device_test": {
            "target": "emulator",
            # emulator: boot the image built for the emulator target; device: use ANDROID_SERIAL.
            "mode": "emulator",
            "serial": None,
            "emulator_port": 5580,
            "emulator_args": [
                "-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot",
                "-wipe-data", "-gpu", "swiftshader_indirect", "-memory", "6144", "-cores", "4",
            ],
            "boot_timeout": 900,
            # No emulator image on the runner: "warn" reports the impacted tests as not run (CI job
            # shows a warning, coverage continues with unit tests); "error" fails the job.
            "missing_image": "warn",
            # adb-install the coverage build of the app(s) under test before running tests,
            # so tests that instrument the system app run against the current code.
            "install_apps": True,
            "coverage": True, "atest_args": [], "timeout": 10800,
            "gate": {"max_failed": 0},
        },
        # min_new_line: coverage of added/modified executable lines (only when at least
        # min_new_lines_for_gate such lines exist, so 3-line fixes are not blocked).
        "coverage": {"min_new_lines_for_gate": 10, "gate": {"min_line": None, "min_new_line": 50}},
        "docs": {"javadoc": True, "uml": True, "kotlin": True, "visibility": "protected", "timeout": 3600},
        "sonar": {
            "host_url_env": ["SONAR_HOST_URL", "SONARQUBE_HOST_URL"],
            "token_env": "SONAR_TOKEN",
            "project_key_prefix": "aosp-",
            "project_key_suffix": "",        # e.g. -a14 when one app is analysed for several platforms
            # Branch/MR analysis needs SonarQube Developer Edition (or the community branch plugin).
            "pull_request_analysis": False,
            "wait_quality_gate": False,
            # true for SonarQube < 10.3 (external issues without rules/impacts).
            "legacy_issue_format": False,
            "extra_properties": {},
        },
        "image": {"target": "emulator", "timeout": 6 * 3600},
        # Prebuilt delivery (release tags): see appci/stages/deploy.py. variants: folder -> target
        # ("release" = the device lunch target as -user, OUT_DIR out_user, unless targets.release is set).
        "deploy": {"repo": None, "branch": "main", "path": "{name}",
                   "variants": {"user": "release", "userdebug": "device"},
                   "mode": "mr",                         # mr | push | local (commit only, for testing)
                   "certificate": "platform", "presigned": False, "prefer": False, "dex_preopt": True,
                   "overrides": None,                    # None = from the source Android.bp/.mk; list or {module: list}
                   "token_env": "APPCI_DEPLOY_TOKEN", "author_name": "appci", "author_email": "appci@localhost",
                   "timeout": 4 * 3600, "gate": {}},
    },
    "discovery": {
        "scan_roots": ["packages/apps"],
        # Only apps in the device target's PRODUCT_PACKAGES.
        "installed_only": True,
        # Module names matching this are treated as test/support modules, not production.
        "test_module_pattern": r"(Test|Tests|Testing|ForTesting|TestApp|TestHelper|Robo|Mock|Fake|Sample|Demo)([A-Z_-]|$)|[-_.](test|tests|testing|testutils)([-_.]|$)",
        "exclude_paths": [],
        # Vendor-defined module types (soong_config_module_type) to treat like android_app /
        # android_library, e.g. ["xxx_car_app"]; appci reports unknown types it skipped.
        "extra_prod_types": [],
    },
    # Change impact analysis (see appci/impact.py). Only active when the change is known
    # (merge requests via CI_MERGE_REQUEST_DIFF_BASE_SHA, or --changed-since locally).
    "impact": {
        "enabled": True,
        "selection": "impact",                # impact | all
        "full_run_labels": ["full-ci"],
        "device_test_labels": ["device-test", "full-ci"],
        # Any of these changing means module-level mapping is not trustworthy -> full run.
        "full_run_patterns": ["Android.bp", "Android.mk", "*.mk", "AndroidManifest.xml",
                              "AndroidTest.xml", "*-AndroidTest.xml", "TEST_MAPPING", "PREUPLOAD.cfg",
                              "*.aconfig", "proguard*", "*.flags", "lint-baseline.xml", "*.gradle",
                              "*.gradle.kts", "*.properties"],
        "ignore_patterns": ["*.md", "OWNERS", "*.txt", "LICENSE*", "NOTICE*", "METADATA",
                            "MODULE_LICENSE_*", ".gitignore", "docs/*", "*.png.md"],
        "test_mapping_groups": ["presubmit", "presubmit-large", "*-presubmit"],
        "include_external_mapped_tests": True,
        # Library changes: none | build (compile dependent apps) | test (build + their unit tests)
        "dependents": "test",
        "dependent_candidates": "configured",  # configured (appci.yaml apps) | scan (whole image)
        "max_dependent_apps": 15,
    },
    # Generated GitLab pipeline (`appci gitlab`): jobs run in the platform image on Docker runners
    # tagged per platform; the tree is the platform workspace on the runner's volume.
    "gitlab": {
        "build_tags": ["appci-${APPCI_PLATFORM}"],
        "emulator_tags": ["appci-${APPCI_PLATFORM}-kvm"],
        "appci_project": "tools/aosp-app-ci",     # GitLab path of this repository (job-token clone)
        "appci_version": "main",                # branch or tag the jobs run; pin a release tag
        "volume": "/appci",                     # runner volume: workspaces + tools cache
        "report_dir": "reports",
        "artifact_expire": "2 weeks",
        "allow_failure": {"checkstyle": False, "ktlint": False, "detekt": False, "sonar": True, "docs": True},
        # Job timeouts (GitLab's project default is 1 h). The first job on a new machine seeds,
        # syncs and builds the tree; runners' maximum_timeout must allow these.
        "timeouts": {"default": "3h", "build": "8h", "unit_test": "5h", "device_test": "5h", "deploy": "8h",
                     "emulator_image": "10h"},
    },
    # TLS verification for third-party tool downloads. Integrity is still enforced by the pinned
    # SHA-256, so disabling it is acceptable behind TLS-intercepting proxies. Proxies come from
    # the standard https_proxy/HTTPS_PROXY environment variables.
    # wheel_base_url: internal folder with the pinned hook wheels (default: pypi.org).
    "downloads": {"verify_tls": True, "wheel_base_url": None},
    # Apps to process. Each entry: {path: ..., enabled: true, modules: [...], stages: {<stage>: {...}}}
    "apps": [],
}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def yaml_module():
    try:
        import yaml  # type: ignore
    except ImportError:
        raise SystemExit("appci needs PyYAML: apt install python3-yaml (or pip install pyyaml)")
    return yaml


def _load_file(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    return yaml_module().safe_load(text) or {}


def find_aosp_root(start: Optional[Path] = None) -> Optional[Path]:
    env = os.environ.get("APPCI_AOSP_ROOT") or os.environ.get("ANDROID_BUILD_TOP")
    candidates: List[Path] = []
    cur = (start or Path.cwd()).resolve()
    candidates.extend([cur, *cur.parents])
    if env:
        candidates.insert(0, Path(env))
    for c in candidates:
        if (c / "build" / "envsetup.sh").is_file() and (c / "build" / "soong").is_dir():
            return c
    # The tool usually lives inside the tree (vendor/<x>/aosp-app-ci).
    for c in TOOL_ROOT.parents:
        if (c / "build" / "envsetup.sh").is_file():
            return c
    return None


class Config:
    def __init__(self, data: Dict[str, Any], source: Optional[Path]):
        self.data = data
        self.source = source
        root = data.get("aosp_root") or find_aosp_root()
        # Resolved lazily: commands like `appci gitlab` must work without a tree (e.g. CI self-test).
        self._aosp_root = Path(os.path.expandvars(str(root))).resolve() if root else None
        self._workdir: Optional[Path] = None
        self._jdk_warned = False

    @property
    def aosp_root(self) -> Path:
        if self._aosp_root is None:
            raise SystemExit("Cannot find the AOSP root; set aosp_root in appci.yaml or APPCI_AOSP_ROOT")
        return self._aosp_root

    @property
    def workdir(self) -> Path:
        if self._workdir is None:
            wd = self.data.get("workdir") or os.environ.get("APPCI_WORKDIR") or str(self.aosp_root / "out" / "appci")
            self._workdir = Path(os.path.expandvars(wd)).resolve()
        return self._workdir

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def path(self, value: str) -> Path:
        """Resolve a config path: absolute, {tool}-relative or AOSP-relative."""
        value = value.replace("{tool}", str(TOOL_ROOT))
        p = Path(os.path.expandvars(value))
        return p if p.is_absolute() else self.aosp_root / p

    # JDKs AOSP trees ship, newest first; the host tools need 17+ (Android 14 ships jdk17).
    TREE_JDKS = ("prebuilts/jdk/jdk21/linux-x86", "prebuilts/jdk/jdk17/linux-x86")

    @property
    def java_home(self) -> Path:
        """The configured JDK; when that is not in the tree, the newest JDK the tree ships."""
        configured = self.path(self.data["java_home"])
        if (configured / "bin" / "java").is_file():
            return configured
        for rel in self.TREE_JDKS:
            cand = self.aosp_root / rel
            if (cand / "bin" / "java").is_file():
                if not self._jdk_warned:
                    self._jdk_warned = True
                    from . import util
                    util.info(f"java_home {self.data['java_home']} is not in this tree; using {rel}")
                return cand
        return configured

    @property
    def java(self) -> str:
        j = self.java_home / "bin" / "java"
        if j.is_file():
            return str(j)
        found = shutil.which("java")
        if found:
            return found
        raise RuntimeError(f"no JDK: {j} does not exist and no `java` is on PATH; set java_home to the "
                           f"tree's JDK (e.g. {self.TREE_JDKS[-1]}) or install one in the image")

    def target(self, name: str) -> Dict[str, Any]:
        t = self.data["targets"].get(name)
        if t is None and name == "release" and "device" in self.data["targets"]:
            # The product's user build: the device lunch target with -user, in its own OUT_DIR.
            dev = self.data["targets"]["device"]
            t = {"lunch": re.sub(r"-userdebug$|-eng$", "-user", dev["lunch"]), "out_dir": "out_user", "env": {}}
            self.data["targets"]["release"] = t
        if t is None:
            raise SystemExit(f"Unknown target '{name}' (defined: {', '.join(self.data['targets'])})")
        return t

    def app_entries(self) -> List[Dict[str, Any]]:
        return [a for a in self.data.get("apps") or [] if a.get("enabled", True)]

    def app_entry(self, app_path: str) -> Dict[str, Any]:
        norm = app_path.strip("/")
        for a in self.data.get("apps") or []:
            if a["path"].strip("/") == norm or a.get("name") == app_path:
                return a
        return {"path": norm}

    def stage_cfg(self, stage: str, app_entry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        base = self.data["stages"].get(stage, {})
        if app_entry and stage in (app_entry.get("stages") or {}):
            return deep_merge(base, app_entry["stages"][stage])
        return copy.deepcopy(base)


def load_config(path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Config:
    candidates = [path, os.environ.get("APPCI_CONFIG"), str(TOOL_ROOT / "appci.yaml")]
    source = None
    data = copy.deepcopy(DEFAULTS)
    for c in candidates:
        if c and Path(c).is_file():
            source = Path(c).resolve()
            data = deep_merge(data, _load_file(source))
            break
        if c and c == path:
            raise SystemExit(f"Config file not found: {c}")
    name = (overrides or {}).get("platform") or os.environ.get("APPCI_PLATFORM") or data.get("platform")
    if name:
        plat = (data.get("platforms") or {}).get(name) or {}
        own = {k: v for k, v in plat.items() if k not in ("manifest", "workspace", "local_manifests")}
        data = deep_merge(data, own)
        data["platform"] = name
    if overrides:
        data = deep_merge(data, overrides)
    if name and not (overrides or {}).get("aosp_root"):
        from .workspace import resolve
        ws = resolve(data)
        ws_path = ws["path"]
        # CI: the platform's workspace is the tree (or its workspace.tree subdirectory when the
        # manifest nests the platform, e.g. android/). On a developer machine (no such workspace)
        # the platform section still applies (lunch targets, JDK, ...) to the tree found as usual.
        if ws_path and (os.environ.get("CI") or Path(ws_path).exists()):
            data["aosp_root"] = str(ws_path / ws["tree"] if ws["tree"] else ws_path)
    return Config(data, source)

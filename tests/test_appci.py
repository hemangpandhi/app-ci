"""Unit tests for the tree-independent parts of appci: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from appci.context import git_changed_lines  # noqa: E402
from appci.model import Finding, evaluate_gate, finding_metrics  # noqa: E402
from appci.reports import coverage as cov  # noqa: E402
from appci.reports.issues import parse_android_lint, parse_checkstyle, parse_sarif, write_gitlab_codequality  # noqa: E402
from appci.reports.junit import parse_atest_output, summarize, write_junit  # noqa: E402

ATEST_OUT = """\
\x1b[1;34mCarSettingsMultivalentRoboTests\x1b[0m (3 Tests)
[1/3] com.android.car.settings.FooTest#testA: \x1b[1;32mPASSED\x1b[0m (12ms)
[2/3] com.android.car.settings.FooTest#testB: FAILED (1.5s)

STACKTRACE:
java.lang.AssertionError: expected:<1> but was:<2>
\tat com.android.car.settings.FooTest.testB(FooTest.java:42)

[3/3] com.android.car.settings.BarTest#testC: IGNORED
x86_64 OtherModule (1 Test)
RUNNER ERROR: Instrumentation run failed due to 'Process crashed.'

Summary
-------
"""


class AtestParsing(unittest.TestCase):
    def test_parse(self):
        cases = parse_atest_output(ATEST_OUT)
        s = summarize(cases)
        self.assertEqual(s, {"tests": 4, "passed": 1, "failed": 2, "skipped": 1})
        failed = [c for c in cases if c.status == "failed"][0]
        self.assertEqual((failed.classname, failed.name), ("com.android.car.settings.FooTest", "testB"))
        self.assertIn("FooTest.java:42", failed.details)
        self.assertNotIn("BarTest", failed.details)
        self.assertAlmostEqual(failed.time, 1.5)
        self.assertEqual(cases[-1].status, "error")
        self.assertEqual(cases[-1].suite, "OtherModule")          # ABI-prefixed header (newer atest)
        self.assertEqual(cases[0].suite, "CarSettingsMultivalentRoboTests")

    def test_junit_written(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "j.xml"
            write_junit(parse_atest_output(ATEST_OUT), p)
            text = p.read_text()
            self.assertIn('<testsuite name="com.android.car.settings.FooTest" package="com.android.car.settings"',
                          text)                                  # one suite per class, Surefire/Ant layout
            self.assertIn('name="module" value="CarSettingsMultivalentRoboTests"', text)
            self.assertIn("<failure", text)
            write_junit([], p, extra_error="build failed")
            self.assertIn("build failed", p.read_text())


class Progress(unittest.TestCase):
    def test_long_command_reports_progress(self):
        """CI logs must show a multi-hour build moving: periodic lines with the latest build progress."""
        import contextlib
        import io
        from unittest import mock
        from appci import util
        script = ("echo starting; sleep 0.5; echo '[ 28% 6261/22255] AIDL Java x.aidl'; echo 'stderr:'; "
                  "sleep 0.5; echo 'WARNING: noise'; sleep 0.5")
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(util, "PROGRESS_INTERVAL", 0.4), \
                contextlib.redirect_stdout(out):
            r = util.run(script, log=Path(d) / "x.log", shell_bash=True)
        self.assertTrue(r.ok)
        lines = [l for l in out.getvalue().splitlines() if "..." in l]
        self.assertGreaterEqual(len(lines), 2)
        self.assertIn("[ 28% 6261/22255]", out.getvalue())        # build progress preferred over noise
        self.assertNotIn("WARNING: noise", out.getvalue())


class HookPython(unittest.TestCase):
    def test_embedded_python_not_used_for_hooks(self):
        """AOSP's py3-cmd cannot load manylinux wheels (lxml); a regular CPython can."""
        import sys
        from appci.stages.preupload import _loads_wheels
        self.assertTrue(_loads_wheels(sys.executable))
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "py3-cmd"                     # stand-in: an interpreter that fails the check
            fake.write_text("#!/bin/sh\nexit 1\n")
            fake.chmod(0o755)
            self.assertFalse(_loads_wheels(str(fake)))


class ReportLinks(unittest.TestCase):
    def test_lint_links_relocated(self):
        """Lint links sources relative to out/...; copied into reports/ they must point somewhere real."""
        import os
        from unittest import mock
        from appci.reports.html import relocate_links
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d)
            src = tree / "packages/apps/X/src/A.java"
            src.parent.mkdir(parents=True)
            src.write_text("class A {}")
            origin = tree / "out/soong/.intermediates/packages/apps/X/X/android_common/lint"
            page = ('<a href="../../packages/apps/X/src/A.java">A</a>'
                    '<a href="soong/.intermediates/m/AndroidManifest.xml">m</a><a href="#top">t</a>'
                    '<a href="https://developer.android.com/x">d</a>')
            env = {"CI_PROJECT_URL": "https://gl/p", "CI_COMMIT_SHA": "abc"}
            with mock.patch.dict(os.environ, env):
                out = relocate_links(page, origin, tree, "packages/apps/X")
            self.assertIn('href="https://gl/p/-/blob/abc/src/A.java"', out)       # app source -> MR file view
            self.assertIn('data-unavailable="1">m<', out)                         # build output: no dead link
            self.assertIn('href="#top"', out)
            self.assertIn('href="https://developer.android.com/x"', out)
            with mock.patch.dict(os.environ, {}, clear=True):
                out = relocate_links(page, origin, tree, "packages/apps/X")
            self.assertIn(f'href="{src.as_uri()}"', out)                          # local run: file in the tree


class DashboardHardening(unittest.TestCase):
    def test_forged_results_cannot_inject(self):
        """results/*.json are artifacts of earlier jobs (which run MR code): no script links or markup."""
        from appci.model import StageResult
        from appci.reports.dashboard import _clean, _href
        r = StageResult(stage="unit_test", app="X", status="passed")
        r.metrics = {"tests": "<script>x</script>", "passed": "12", "line": "45.5", "selection": "a<b"}
        r.reports = {"ok": "unit_test/tests.html", "js": "javascript://%0aalert(1)", "up": "../../etc/x.html",
                     "web": "https://sonar.example/x"}
        r.log = "javascript:alert(1)"
        _clean(r)
        self.assertNotIn("tests", r.metrics)                        # not a number: dropped
        self.assertEqual((r.metrics["passed"], r.metrics["line"]), (12, 45.5))
        self.assertEqual(r.metrics["selection"], "a<b")             # text metrics stay text (escaped on output)
        self.assertEqual(sorted(r.reports), ["ok", "web"])
        self.assertEqual(r.log, "")
        self.assertEqual(_href("app", "javascript://x"), "#")


class ToolCache(unittest.TestCase):
    def test_tampered_tool_redownloaded_and_archives_private(self):
        """The shared tools cache is writable by other jobs: verify on every use, run private copies."""
        import hashlib
        import io
        import os
        import tarfile
        from unittest import mock
        from appci import tools
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "tool.jar"
            src.write_bytes(b"good jar")
            tar = Path(d) / "py.tar.gz"
            with tarfile.open(tar, "w:gz") as t:
                info = tarfile.TarInfo("python/bin/python3")
                data = b"#!/bin/sh\n"
                info.size, info.mode = len(data), 0o755
                t.addfile(info, io.BytesIO(data))
            fake = {"j": {"version": "1", "url": src.as_uri(), "file": "j.jar",
                          "sha256": hashlib.sha256(b"good jar").hexdigest()},
                    "p": {"version": "1", "url": tar.as_uri(), "file": "p.tar.gz", "unzip": "bin/python3",
                          "sha256": hashlib.sha256(tar.read_bytes()).hexdigest()}}
            cache = Path(d) / "cache"
            with mock.patch.dict(tools.TOOLS, fake), mock.patch.dict(tools._READY, {}, clear=True), \
                    mock.patch.dict(os.environ, {"APPCI_TOOLS_CACHE": str(cache)}):
                jar = tools.ensure({}, "j")
                jar.write_bytes(b"evil jar")                  # another job rewrites the cached tool
                tools._READY.clear()
                self.assertEqual(tools.ensure({}, "j").read_bytes(), b"good jar")
                exe = tools.ensure({}, "p")
                self.assertNotIn(str(cache), str(exe))         # unpacked privately, not in the cache
                self.assertTrue(os.access(exe, os.X_OK))

    def test_unsafe_tar_refused(self):
        import io
        import tarfile
        from appci import tools
        with tempfile.TemporaryDirectory() as d:
            for name, kind in (("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
                tar = Path(d) / "bad.tar.gz"
                with tarfile.open(tar, "w:gz") as t:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
                    t.addfile(info, io.BytesIO(b""))
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    tools._extract_with_modes(tar, Path(d) / "out")


class Deploy(unittest.TestCase):
    def _git(self, repo, *args):
        import subprocess
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_source_overrides(self):
        from appci.stages import deploy
        with tempfile.TemporaryDirectory() as d:
            app = Path(d)
            (app / "Android.bp").write_text(textwrap.dedent("""
                // overrides: ["Wrong"]
                android_library { name: "XLib", overrides: ["NotMe"] }
                android_app {
                    name: "xxxCarSettings",
                    defaults: ["x_defaults"],
                    overrides: [
                        "CarSettings", // the AOSP app
                        "Settings",
                    ],
                    optimize: { enabled: true },
                }
                """))
            (app / "sub").mkdir()
            (app / "sub" / "Android.mk").write_text(
                "include $(CLEAR_VARS)\nLOCAL_PACKAGE_NAME := OldApp\nLOCAL_OVERRIDES_PACKAGES := A \\\n    B\n")
            self.assertEqual(deploy.source_overrides(app, "xxxCarSettings"), ["CarSettings", "Settings"])
            self.assertEqual(deploy.source_overrides(app, "XLib"), ["NotMe"])
            self.assertEqual(deploy.source_overrides(app, "OldApp"), ["A", "B"])
            self.assertEqual(deploy.source_overrides(app, "Missing"), [])
            bp = deploy.android_bp([{"module": "xxxCarSettings", "apk_name": "x.apk", "privileged": True,
                                     "partition": "system", "overrides": ["CarSettings"]}],
                                   {"user": "release"}, {}, "h")
            self.assertIn('overrides: ["CarSettings"],', bp)

    def test_prebuilt_gaps(self):
        from appci.stages import deploy
        minfo = {
            "XApp": {"path": ["vendor/x/XApp"], "installed": ["out/.../XApp.apk"], "required": ["XPerms", "fwk_lib"]},
            "XPerms": {"path": ["vendor/x/XApp/perms"], "installed": ["out/.../x.xml"]},
            "XOverlay": {"path": ["vendor/x/XApp/overlay"], "installed": ["out/.../XOverlay.apk"]},
            "XAppTests": {"path": ["vendor/x/XApp/tests"], "installed": ["out/.../XAppTests.apk"]},
            "XApp__prod__auto_generated_rro_vendor": {"path": ["vendor/x/XApp"], "installed": ["out/.../rro.apk"]},
            "XAppOther": {"path": ["vendor/x/XAppOther"], "installed": ["out/.../o.apk"]},
            "fwk_lib": {"path": ["frameworks/x"], "installed": ["out/.../f.jar"]},
        }
        gaps = deploy.prebuilt_gaps(minfo, "vendor/x/XApp", {"XApp"}, {"XApp", "XOverlay", "XAppOther"})
        self.assertEqual(gaps, {"left_behind": ["XOverlay", "XPerms"],
                                "auto_rros": ["XApp__prod__auto_generated_rro_vendor"]})
        msg = deploy.commit_message("XApp", "v1", "", "abc", "", [], {}, {}, {"user": "release"},
                                    {"user": "x-user"}, {}, gaps)
        self.assertIn("installed by the product", msg)
        self.assertIn("auto-generated RROs", msg)

    def test_missing_token_detected_before_build(self):
        from appci.stages import deploy
        cfg = {"repo": "https://gitlab.example.com/x/prebuilts.git", "mode": "mr", "token_env": "APPCI_T_TEST"}
        env = {"CI_COMMIT_REF_PROTECTED": "false", "CI_COMMIT_REF_NAME": "v9", "CI_COMMIT_TAG": "v9"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("APPCI_T_TEST", None)
            msg = deploy.missing_token(cfg)
            self.assertIn("v9 is not a protected tag", msg)
            self.assertIsNone(deploy.missing_token(dict(cfg, mode="local")))
            os.environ["APPCI_T_TEST"] = "x"
            self.assertIsNone(deploy.missing_token(cfg))
            os.environ.pop("APPCI_T_TEST")

    def test_history_message_and_local_delivery(self):
        from appci.stages import deploy
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src"
            src.mkdir()
            self._git(src, "init", "-q")
            for i, title in enumerate(["first", "second", "Fix crash\n\nSee merge request grp/settings!7"]):
                (src / "res").mkdir(exist_ok=True)
                (src / "res" / f"f{i}.xml").write_text(str(i))
                self._git(src, "add", "-A")
                self._git(src, "commit", "-qm", title.replace("\\n", "\n"))
                if i == 0:
                    self._git(src, "tag", "v1.0.0")
            self._git(src, "tag", "v1.1.0")
            prev, commits = deploy.source_history(src, "v1.1.0")
            self.assertEqual(prev, "v1.0.0")
            self.assertEqual([c["title"] for c in commits], ["Fix crash", "second"])
            self.assertEqual(commits[0]["mr"], "grp/settings!7")
            msg = deploy.commit_message("CarSettings", "v1.1.0", "https://gl/grp/settings", "a" * 40, prev, commits,
                                        deploy.changed_areas(src, prev),
                                        {"com.x": {"added": ["android.permission.A"], "removed": [],
                                                   "not_in_platform_allowlist": []}},
                                        {"user": "release"}, {"user": "p_car-user"}, {"user/X.apk": "b" * 64})
            self.assertTrue(msg.startswith("CarSettings v1.1.0: prebuilt from settings@aaaaaaaaaaaa"))
            for part in ("Changes since v1.0.0 (2 commit(s))", "Fix crash [t] (grp/settings!7)", "res 2",
                         "+ android.permission.A", "user (p_car-user)", "b" * 64):
                self.assertIn(part, msg)

            target = Path(d) / "prebuilts.git"
            seed = Path(d) / "seed"
            seed.mkdir()
            self._git(seed, "init", "-q")
            self._git(seed, "checkout", "-q", "-b", "main")
            (seed / "README.md").write_text("x")
            self._git(seed, "add", "-A")
            self._git(seed, "commit", "-qm", "init")
            self._git(d, "clone", "-q", "--bare", str(seed), str(target))
            log = Path(d) / "deploy.log"
            dl = deploy.Delivery(None, {"repo": str(target), "branch": "main", "mode": "local"}, log)
            repo = dl.checkout()
            (repo / "CarSettings").mkdir()
            (repo / "CarSettings" / "Android.bp").write_text("// x")
            out = dl.commit_and_deliver("CarSettings", "appci/car-settings-v1.1.0", msg)
            self.assertEqual(out["result"], "committed locally")
            import subprocess
            body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo, stdout=subprocess.PIPE,
                                  text=True).stdout
            self.assertIn("Fix crash", body)
            self.assertEqual(dl.commit_and_deliver("CarSettings", "b", msg)["result"], "unchanged")
            dl.cleanup()
        self.assertEqual(deploy._partition("out_user/target/product/tangorpro/system/priv-app/X/X.apk"), "system")
        self.assertEqual(deploy._partition("out/target/product/tangorpro/product/priv-app/X/X.apk"), "product")
        self.assertEqual(deploy._partition("out/target/product/p/system_ext/priv-app/X/X.apk"), "system_ext")


class Gates(unittest.TestCase):
    def test_new_findings_on_changed_lines_only(self):
        f = [Finding("checkstyle", "R", "m", "a/B.java", 10, severity="critical"),
             Finding("checkstyle", "R", "m", "a/B.java", 99, severity="critical"),
             Finding("ktlint", "R", "m", "a/C.kt", 5, severity="minor")]
        m = finding_metrics(f, {"a/B.java": {10, 11}})
        self.assertEqual((m["total"], m["errors"], m["new_total"], m["new_errors"]), (3, 2, 1, 1))
        self.assertEqual(evaluate_gate({"max_new_errors": 0}, m), ["new_errors=1 > 0"])
        self.assertEqual(evaluate_gate({"max_new_errors": 0}, finding_metrics(f, None)), [])
        self.assertEqual(evaluate_gate({"min_line": 50, "max_failed": None}, {"line": 40.0}), ["line=40.0 < 50"])

    def test_default_gates_block_new_errors_not_warnings(self):
        from appci.config import DEFAULTS
        changed = {"a/B.java": {10}}
        for stage in ("lint", "checkstyle", "ktlint", "detekt"):
            gate = DEFAULTS["stages"][stage]["gate"]
            for sev in ("info", "minor", "major"):          # warnings on changed lines: reported only
                m = finding_metrics([Finding(stage, "R", "m", "a/B.java", 10, severity=sev)], changed)
                self.assertEqual(evaluate_gate(gate, m), [], f"{stage} blocked a {sev} warning")
            m = finding_metrics([Finding(stage, "R", "m", "a/B.java", 10, severity="critical")], changed)
            self.assertTrue(evaluate_gate(gate, m), f"{stage} let a new error through")


class Parsers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lint(self):
        (self.d / "l.xml").write_text(textwrap.dedent("""\
            <issues format="6"><issue id="NewApi" severity="Error" message="Call requires API 36" category="Correctness">
            <location file="packages/apps/X/src/A.java" line="7" column="3"/></issue>
            <issue id="Typos" severity="Warning" message="typo" category="Typography">
            <location file="/root/packages/apps/X/res/values/strings.xml" line="2"/></issue></issues>"""))
        f = parse_android_lint(self.d / "l.xml", Path("/root"))
        self.assertEqual([(x.rule, x.severity, x.path, x.line) for x in f], [
            ("NewApi", "critical", "packages/apps/X/src/A.java", 7),
            ("Typos", "major", "packages/apps/X/res/values/strings.xml", 2)])

    def test_checkstyle_and_codequality(self):
        (self.d / "c.xml").write_text(
            '<checkstyle><file name="/root/packages/apps/X/src/A.java">'
            '<error line="3" column="1" severity="error" message="Line too long" '
            'source="com.puppycrawl.tools.checkstyle.checks.sizes.LineLengthCheck"/></file></checkstyle>')
        f = parse_checkstyle(self.d / "c.xml", Path("/root"), "checkstyle")
        self.assertEqual((f[0].rule, f[0].path, f[0].severity), ("LineLengthCheck", "packages/apps/X/src/A.java", "critical"))
        write_gitlab_codequality(f, self.d / "cq.json", "packages/apps/X")
        cq = json.loads((self.d / "cq.json").read_text())
        self.assertEqual(cq[0]["location"], {"path": "src/A.java", "lines": {"begin": 3}})
        self.assertEqual(len(cq[0]["fingerprint"]), 32)

    def test_sarif(self):
        (self.d / "s.sarif").write_text(json.dumps({"runs": [{"results": [{
            "ruleId": "detekt.style.MagicNumber", "level": "warning", "message": {"text": "magic"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": "file:///root/p/A.kt"},
                                                "region": {"startLine": 4}}}]}]}]}))
        f = parse_sarif(self.d / "s.sarif", Path("/root"), "detekt")
        self.assertEqual((f[0].rule, f[0].path, f[0].line), ("MagicNumber", "p/A.kt", 4))


JACOCO_A = """<?xml version="1.0"?><report name="a"><package name="com/x">
<sourcefile name="A.java"><line nr="1" mi="0" ci="2" mb="1" cb="1"/><line nr="2" mi="3" ci="0" mb="0" cb="0"/></sourcefile>
<sourcefile name="ATest.java"><line nr="1" mi="0" ci="1" mb="0" cb="0"/></sourcefile></package></report>"""
JACOCO_B = """<?xml version="1.0"?><report name="b"><package name="com/x">
<sourcefile name="A.java"><line nr="1" mi="2" ci="0" mb="2" cb="0"/><line nr="2" mi="0" ci="3" mb="0" cb="0"/></sourcefile>
</package></report>"""


class Coverage(unittest.TestCase):
    def test_line_merge_and_writers(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.xml").write_text(JACOCO_A)
            (d / "b.xml").write_text(JACOCO_B)
            idx = cov.build_source_index(["packages/apps/X/src/com/x/A.java"])
            a, b = cov.parse_jacoco(d / "a.xml", idx), cov.parse_jacoco(d / "b.xml", idx)
            self.assertEqual(cov.totals(a)["line"], 50.0)          # test sources are ignored
            m = cov.merge([a, b])
            t = cov.totals(m, ["packages/apps/X/src/com/x/A.java", "packages/apps/X/src/com/x/B.java"])
            self.assertEqual((t["line"], t["branches_valid"], t["branches_covered"], t["files_without_data"]),
                             (100.0, 2, 1, 1))
            cov.write_cobertura(m, d / "c.xml", "packages/apps/X")
            self.assertIn('filename="src/com/x/A.java"', (d / "c.xml").read_text())
            cov.write_sonar_generic(m, d / "s.xml", "packages/apps/X")
            self.assertIn('branchesToCover="2" coveredBranches="1"', (d / "s.xml").read_text())


class ChangedLines(unittest.TestCase):
    def test_git_changed_lines(self):
        with tempfile.TemporaryDirectory() as d:
            run = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)  # noqa: E731
            run("init", "-q")
            run("config", "user.email", "t@t")
            run("config", "user.name", "t")
            Path(d, "A.java").write_text("a\nb\nc\n")
            run("add", ".")
            run("commit", "-qm", "base")
            Path(d, "A.java").write_text("a\nB\nc\nd\n")
            Path(d, "N.kt").write_text("x\ny\n")
            run("add", ".")
            run("commit", "-qm", "change")
            ch = git_changed_lines(Path(d), "HEAD~1", "pkg/app")
            self.assertEqual(ch, {"pkg/app/A.java": {2, 4}, "pkg/app/N.kt": {1, 2}})


class ContainerSafety(unittest.TestCase):
    def test_trust_git_repos_once(self):
        import os
        from unittest import mock
        from appci.util import trust_git_repos
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.x",
                                          "GIT_CONFIG_VALUE_0": "y"}), \
                mock.patch("appci.util._git_version", return_value=(2, 43)), \
                mock.patch("appci.util.in_container", return_value=True):
            trust_git_repos()
            trust_git_repos()
            self.assertEqual(os.environ["GIT_CONFIG_COUNT"], "2")      # existing entry kept, added once
            self.assertEqual((os.environ["GIT_CONFIG_KEY_1"], os.environ["GIT_CONFIG_VALUE_1"]),
                             ("safe.directory", "*"))
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "0"}), \
                mock.patch("appci.util._git_version", return_value=(2, 43)), \
                mock.patch("appci.util.in_container", return_value=False):
            trust_git_repos()
            self.assertEqual(os.environ["GIT_CONFIG_COUNT"], "0")      # host: trust never widened

    def test_secrets_sealed_from_children(self):
        """MR build/test code (children of appci) must not see CI secrets; appci itself can."""
        import os
        import subprocess
        from unittest import mock
        from appci import util
        env = {"APPCI_GIT_CREDENTIALS": "https://u:t@h", "SONAR_TOKEN": "s3cr3t",
               "CI_REPOSITORY_URL": "https://gitlab-ci-token:jt@h/p.git", "APPCI_PLATFORM": "p1"}
        with mock.patch.dict(os.environ, env), mock.patch.dict(util._SECRETS, {}, clear=True):
            names = util.seal_secrets()
            self.assertIn("SONAR_TOKEN", names)
            self.assertNotIn("APPCI_PLATFORM", names)
            self.assertEqual(util.secret("SONAR_TOKEN"), "s3cr3t")
            child = subprocess.run(["env"], stdout=subprocess.PIPE, text=True,
                                   env=util.merge_env(None)).stdout
            self.assertNotIn("s3cr3t", child)
            self.assertNotIn("u:t@h", child)
            self.assertNotIn("jt@", child)
            self.assertIn("APPCI_PLATFORM=p1", child)
        self.assertEqual(util.redact("$ repo init -u https://bot:tok123@git.example.com/m.git"),
                         "$ repo init -u https://***@git.example.com/m.git")

    def test_old_git_writes_global_config_only_in_containers(self):
        from unittest import mock
        from appci import util
        with mock.patch("appci.util._git_version", return_value=(2, 25)), \
                mock.patch("appci.util.in_container", return_value=False), \
                mock.patch("appci.util.subprocess.run") as run:
            util.trust_git_repos()
            run.assert_not_called()                                      # host: never touch config
        with mock.patch("appci.util._git_version", return_value=(2, 25)), \
                mock.patch("appci.util.in_container", return_value=True), \
                mock.patch("appci.util.subprocess.run") as run:
            run.return_value.stdout = ""
            util.trust_git_repos()
            self.assertEqual(run.call_args[0][0], ["git", "config", "--global", "--add", "safe.directory", "*"])

    def test_ci_fails_when_change_is_unknown(self):
        from types import SimpleNamespace
        from appci.context import detect_changes
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", str(Path(d, "app"))], check=True)
            cfg = SimpleNamespace(aosp_root=Path(d))
            bad = "0" * 40
            self.assertIsNone(detect_changes(cfg, "app", bad))           # local: warn, run ungated
            with self.assertRaises(RuntimeError):                        # CI: never silently ungated
                detect_changes(cfg, "app", bad, ci=True)


class Workspace(unittest.TestCase):
    PLAT = {"platforms": {"p1": {"manifest": {"url": "https://h/m.git", "branch": "b1", "file": "f.xml"},
                                 "targets": {"device": {"lunch": "p1_car-userdebug"}},
                                 "workspace": {"sync": {"max_age_hours": 2}}}}}

    def cfg(self, env, **ov):
        import os
        from unittest import mock
        from appci.config import load_config
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(self.PLAT, f)
        with mock.patch.dict(os.environ, env, clear=False):
            for k in ("APPCI_PLATFORM", "APPCI_MANIFEST_URL", "APPCI_MANIFEST_BRANCH", "APPCI_WORKSPACE"):
                if k not in env:
                    os.environ.pop(k, None)
            from appci.workspace import resolve
            c = load_config(f.name, ov)
            return c, resolve(c)

    def test_platform_section_env_and_cli_precedence(self):
        c, ws = self.cfg({"APPCI_PLATFORM": "p1", "APPCI_WORKSPACE": "/vol/{platform}", "CI": "true"})
        self.assertEqual(c["targets"]["device"]["lunch"], "p1_car-userdebug")    # platform section
        self.assertEqual(str(c.aosp_root), "/vol/p1")                              # CI: tree = workspace
        c, ws = self.cfg({"APPCI_PLATFORM": "p1", "APPCI_WORKSPACE": "/vol/{platform}", "CI": "true"},
                         workspace={"tree": "android/"})
        self.assertEqual((str(c.aosp_root), ws["tree"]), ("/vol/p1/android", "android"))  # nested tree
        import os
        with tempfile.TemporaryDirectory() as own, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CI", None)
            (Path(own) / "build" / "soong").mkdir(parents=True)
            (Path(own) / "build" / "envsetup.sh").write_text("")
            c, _ = self.cfg({"APPCI_PLATFORM": "p1", "APPCI_WORKSPACE": "/vol/{platform}", "APPCI_AOSP_ROOT": own})
            self.assertEqual(str(c.aosp_root), str(Path(own).resolve()))          # local: own tree
            self.assertEqual(c["targets"]["device"]["lunch"], "p1_car-userdebug")  # section still applies
        self.assertEqual(ws["manifest"]["branch"], "b1")
        self.assertEqual(ws["sync"]["max_age_hours"], 2)
        self.assertEqual(ws["sync"]["jobs"], 8)                                     # default kept
        c, ws = self.cfg({"APPCI_PLATFORM": "p1", "APPCI_MANIFEST_BRANCH": "b2", "APPCI_WORKSPACE": "/vol/x"},
                         aosp_root="/explicit")
        self.assertEqual(ws["manifest"]["branch"], "b2")                           # CI variable wins
        self.assertEqual(str(c.aosp_root), "/explicit")                            # CLI wins
        c, ws = self.cfg({"APPCI_PLATFORM": "new", "APPCI_MANIFEST_URL": "https://h/n.git",
                          "APPCI_WORKSPACE": "/vol/{platform}"})
        self.assertEqual((ws["manifest"]["url"], str(ws["path"])), ("https://h/n.git", "/vol/new"))  # env only

    def test_refuses_foreign_tree(self):
        from appci import workspace
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".repo" / "manifests").mkdir(parents=True)
            import os
            from unittest import mock
            env = {"APPCI_PLATFORM": "p1", "APPCI_WORKSPACE": d}
            c, _ = self.cfg(env)
            with mock.patch.dict(os.environ, env), self.assertRaisesRegex(RuntimeError, "did not create"):
                workspace.ensure(c, ["packages/apps/X"])

    def test_adopts_tree_baked_into_the_image(self):
        """workspace.adopt + sync.policy never: a tree the image carries is used as it is. No repo
        init, no sync; app projects are still reset. --sync still forces one."""
        import os
        from unittest import mock
        from appci import util, workspace
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "build_env"
            fake = Path(d) / "repo"
            calls = Path(d) / "calls"
            fake.write_text(f"#!/bin/sh\necho \"$*\" >> {calls}\n[ \"$1\" = manifest ] && echo '<manifest/>'\nexit 0\n")
            fake.chmod(0o755)
            (root / ".repo" / "manifests").mkdir(parents=True)          # synced by the image pipeline
            app = root / "android" / "vendor" / "x" / "App"
            app.mkdir(parents=True)
            util.capture(["git", "init", "-q"], cwd=app)
            (app / "leftover.txt").write_text("from an earlier job")
            env = {"APPCI_PLATFORM": "p1", "APPCI_WORKSPACE": str(root)}
            c, _ = self.cfg(env, workspace={"adopt": True, "tree": "android", "sync": {"policy": "never"},
                                            "repo": {"command": str(fake)}})
            with mock.patch.dict(os.environ, env):
                st = workspace.ensure(c, ["vendor/x/App"])
                self.assertFalse(st["synced_now"])
                self.assertFalse(calls.exists())                          # neither init nor sync
                self.assertFalse((app / "leftover.txt").exists())          # app project reset
                self.assertIn("adopted_at", util.read_json(root / ".repo" / workspace.STATE))
                st = workspace.ensure(c, [], force_sync=True)             # appci workspace --sync
                self.assertTrue(st["synced_now"])
                self.assertTrue(calls.read_text().startswith("sync "))
            # without adopt the same tree is still refused
            c, _ = self.cfg(env, workspace={"sync": {"policy": "never"}, "repo": {"command": str(fake)}})
            (root / ".repo" / workspace.STATE).unlink()
            with mock.patch.dict(os.environ, env), self.assertRaisesRegex(RuntimeError, "did not create"):
                workspace.ensure(c, [])

    def test_interrupted_sync_recovers(self):
        """A cancelled job leaves git lock files; plain `repo sync` then fails. appci removes stale
        locks and retries with --force-sync, and an interrupted sync is redone even when not due."""
        import os
        import time
        from unittest import mock
        from appci import util, workspace
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            fake = Path(d) / "repo"
            calls = Path(d) / "calls"
            # fake repo: init makes .repo/manifests; sync fails while an index.lock exists and
            # --force-sync is not given (like repo's "cannot overwrite a local work tree")
            fake.write_text(f"""#!/bin/sh
echo "$*" >> {calls}
case "$1" in
  init) mkdir -p .repo/manifests .repo/projects/a.git ;;
  sync) if [ -e .repo/projects/a.git/index.lock ]; then exit 1; fi
        case "$*" in *--force-sync*) ;; *) [ -e .repo/projects/a.git/moved ] || exit 1 ;; esac ;;
  manifest) echo '<manifest/>' ;;
esac
""")
            fake.chmod(0o755)
            env = {"APPCI_PLATFORM": "p1", "APPCI_WORKSPACE": str(root)}
            c, _ = self.cfg(env, workspace={"seed": "none", "repo": {"command": str(fake)}})
            with mock.patch.dict(os.environ, env):
                lock = root / ".repo" / "projects" / "a.git" / "index.lock"
                lock.parent.mkdir(parents=True)
                lock.write_text("")                                   # killed checkout
                st = workspace.ensure(c, [])                           # fresh tree: init, sync fails, recovers
                self.assertTrue(st["synced_now"])
                self.assertFalse(lock.exists())
                log = calls.read_text().splitlines()
                self.assertIn("--force-sync", log[2])                  # init, sync, sync --force-sync
                self.assertTrue(log[3].startswith("forall"))           # files restored in every project
                self.assertNotIn("sync_started", st)
                # recent good sync, then a sync that was interrupted: redone although not due
                state_file = root / ".repo" / workspace.STATE
                s = util.read_json(state_file)
                s.update({"synced_at": time.time(), "sync_started": time.time()})
                util.write_json(state_file, s)
                lock.write_text("")
                calls.write_text("")
                st = workspace.ensure(c, [])
                self.assertTrue(st["synced_now"])
                self.assertIn("--force-sync", calls.read_text().splitlines()[0])
                calls.write_text("")
                st = workspace.ensure(c, [])                           # healthy now: not due, no sync
                self.assertFalse(st["synced_now"])
                self.assertEqual(calls.read_text(), "")
                st = workspace.ensure(c, [], repair=True)              # admin: appci workspace --repair
                self.assertIn("--force-sync", calls.read_text().splitlines()[0])
                self.assertIn("forall", calls.read_text())


class GitlabGeneration(unittest.TestCase):
    def test_template(self):
        import yaml
        from appci.config import load_config
        from appci.gitlab import generate
        cfg = load_config()
        text = generate(cfg, list(cfg["stages"]["default"]))
        doc = yaml.safe_load(text)
        self.assertEqual(doc["unit_test"]["needs"], [{"job": "build", "artifacts": True}])
        self.assertEqual(doc["lint"]["needs"], [{"job": "build", "artifacts": True}])   # lint needs classes
        for job in ("preupload", "build", "risk", "lint", "checkstyle", "ktlint", "detekt", "unit_test", "coverage"):
            self.assertNotIn("allow_failure", doc[job], f"{job} must block the MR when its gate fails")
        self.assertEqual(doc["pages"]["needs"], [{"job": "report", "artifacts": True}])
        self.assertEqual(doc["pages"]["rules"], [{"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"}])
        self.assertEqual(text.count("\nworkflow:"), 1)
        self.assertNotIn("needs", doc["report"])          # waits for all earlier stages
        self.assertEqual(doc["device_test"]["allow_failure"], {"exit_codes": [3]})
        self.assertIn("codequality", doc["lint"]["artifacts"]["reports"])
        self.assertIn("--checkout", doc["lint"]["script"][0])
        self.assertIn("coverage_report", doc["coverage"]["artifacts"]["reports"])
        self.assertEqual(doc["default"]["image"], "$APPCI_IMAGE")
        self.assertEqual(doc["build"]["tags"], ["appci-${APPCI_PLATFORM}"])
        self.assertEqual(doc["device_test"]["tags"], ["appci-${APPCI_PLATFORM}-kvm"])
        self.assertEqual(doc["build"]["resource_group"], "appci-tree-${APPCI_PLATFORM}")
        bs = doc["build"]["before_script"]
        self.assertIn("git clone", bs[1])
        self.assertTrue(bs[-1].endswith('workspace --app "$APPCI_APP"'))   # tree ready before the stage
        self.assertNotIn("workspace", " ".join(doc["report"]["before_script"]))
        self.assertNotIn("before_script", doc["pages"])
        self.assertEqual(doc["variables"]["APPCI_WORKSPACE"], "/appci/{platform}")
        self.assertEqual((doc["build"]["timeout"], doc["checkstyle"]["timeout"]), ("8h", "3h"))
        self.assertEqual(doc["deploy"]["rules"], [{"if": "$CI_COMMIT_TAG"}])
        img = doc["emulator_image"]
        self.assertEqual(img["script"], ["$APPCI_HOME/bin/appci image"])
        self.assertEqual(img["resource_group"], "appci-tree-${APPCI_PLATFORM}")
        self.assertIn("APPCI_EMULATOR_IMAGE", img["rules"][0]["if"])
        self.assertEqual(img["rules"][1]["when"], "manual")

    def test_set_option(self):
        from appci.cli import _parse_set
        self.assertEqual(_parse_set("stages.build.extra_targets=[release]"),
                         {"stages": {"build": {"extra_targets": ["release"]}}})


class HtmlReports(unittest.TestCase):
    def test_markdown_page(self):
        from appci.reports.html import markdown_page
        out = markdown_page("# Risk\n\nBase `abc`, **2** files.\n\n| Check | Result |\n|---|---|\n"
                            "| secrets | 0 <hits> |\n\n- one\n- two\n", "t")
        self.assertIn("<h1>Risk</h1>", out)
        self.assertIn("<code>abc</code>", out)
        self.assertIn("<th>Check</th>", out)
        self.assertIn("<td>0 &lt;hits&gt;</td>", out)                  # escaped
        self.assertIn("<ul><li>one</li><li>two</li></ul>", out)
        self.assertNotIn("---", out)

    def test_coverage_page_least_covered_first(self):
        from appci.reports.html import coverage_page
        out = coverage_page("c", {"line": 50.0, "branch": 40.0, "lines_covered": 2, "lines_valid": 4},
                            {"unit_test": 50.0}, [("a/Full.java", 2, 2), ("a/None.java", 2, 0)],
                            ["a/None.java:3"], {"unit_test X": "../unit_test/x/html/index.html"})
        self.assertLess(out.index(">None.java</span>"), out.index("Full.java"))
        self.assertIn("Changed lines not covered <span class=\"badge failed\">1</span>", out)
        self.assertIn('href="../unit_test/x/html/index.html"', out)


class Tools(unittest.TestCase):
    def test_every_download_is_pinned(self):
        from appci.tools import DOKKA_PLUGINS, TOOLS
        for name in ("dokka-cli",) + DOKKA_PLUGINS:
            self.assertIn(name, TOOLS)
        for name, spec in TOOLS.items():
            self.assertRegex(spec.get("sha256", ""), r"^[0-9a-f]{64}$", f"{name} is not SHA-256 pinned")
            self.assertTrue(spec["url"].startswith("https://"), name)


class SonarKey(unittest.TestCase):
    def test_key_ignores_report_slug(self):
        from types import SimpleNamespace
        from appci.config import load_config
        from appci.stages.sonar import sonar_slug
        ctx = SimpleNamespace(cfg=load_config(), profile=SimpleNamespace(path="packages/apps/Car/Settings", slug="app"))
        self.assertEqual(sonar_slug(ctx), "car-settings")


if __name__ == "__main__":
    unittest.main()


class BuildEnvSecrets(unittest.TestCase):
    def test_listed_secret_reaches_build_env_only_when_configured(self):
        import os
        from unittest import mock
        from appci import util
        from appci.aosp import AospTree
        from appci.config import Config
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {"SECRET_PASS": "zip-pw", "OTHER_TOKEN": "t"}, clear=False):
            util._SECRETS.clear()
            util.seal_secrets()
            self.assertNotIn("SECRET_PASS", os.environ)                       # hidden from children
            base = {"aosp_root": root, "targets": {"device": {"lunch": "x-userdebug", "out_dir": "out", "env": {}}}}
            env = AospTree(Config(dict(base), None)).env("device")
            self.assertNotIn("SECRET_PASS", env)                              # default: not passed
            self.assertEqual(env.get("SOONG_COLLECT_JAVA_DEPS"), "true")      # jdeps json always on
            env = AospTree(Config(dict(base, build_env_secrets=["SECRET_PASS"]), None)).env("device")
            self.assertEqual(env.get("SECRET_PASS"), "zip-pw")                # listed: passed
            self.assertNotIn("OTHER_TOKEN", env)                              # others stay hidden
            util._SECRETS.clear()


class ModuleInfoFallback(unittest.TestCase):
    def test_file_target_when_phony_missing(self):
        from unittest import mock
        from appci import util
        from appci.aosp import AospTree
        from appci.config import Config
        with tempfile.TemporaryDirectory() as root:
            cfg = Config({"aosp_root": root, "workdir": root + "/wd",
                          "targets": {"device": {"lunch": "x-userdebug", "out_dir": "out", "env": {}}}}, None)
            tree = AospTree(cfg)
            pout = Path(root) / "out" / "target" / "product" / "dev"
            calls = []

            def fake_m(target, modules, log, **kw):
                calls.append(list(modules))
                log.parent.mkdir(parents=True, exist_ok=True)
                if modules == ["module-info"]:
                    log.write_text("...\nFAILED: ninja: unknown target 'module-info'\n")
                    return util.CommandResult("m module-info", 1, log, 0.0)
                pout.mkdir(parents=True, exist_ok=True)
                (pout / "module-info.json").write_text("{}")
                (Path(root) / "out" / "soong").mkdir(parents=True, exist_ok=True)
                (Path(root) / "out" / "soong" / "module_bp_java_deps.json").write_text("{}")
                log.write_text("ok\n")
                return util.CommandResult("m", 0, log, 0.0)
            with mock.patch.object(tree, "m", side_effect=fake_m), \
                    mock.patch.object(tree, "product_out", return_value=pout), \
                    mock.patch.object(tree, "lock", return_value=mock.MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: False)):
                tree.ensure_metadata("device")
            self.assertEqual(calls, [["module-info"], ["out/target/product/dev/module-info.json"]])


class LintTargetFallback(unittest.TestCase):
    """Android 14 and older: Soong has no `<module>-lint` phony target. The lint stage must ask
    for the report file instead, and read it from the same place afterwards."""

    def test_report_file_when_phony_missing(self):
        from appci import util
        from appci.aosp import AospTree
        from appci.config import Config, DEFAULTS, deep_merge
        from appci.context import RunContext
        from appci.discovery import AppProfile
        from appci.model import StageResult
        from appci.stages.lint import LintStage
        with tempfile.TemporaryDirectory() as root:
            data = deep_merge(DEFAULTS, {"aosp_root": root, "workdir": root + "/wd", "apps": []})
            cfg = Config(data, None)
            tree = AospTree(cfg)
            profile = AppProfile(path="vendor/x/apps/Media", name="Media", slug="app",
                                 apps=["XCarMediaApp"], module_paths={"XCarMediaApp": "vendor/x/apps/Media/Media"})
            ctx = RunContext(cfg=cfg, tree=tree, profile=profile, entry={}, report_root=Path(root) / "reports")
            xml_rel = "out/soong/.intermediates/vendor/x/apps/Media/Media/XCarMediaApp/android_common/lint/lint-report.xml"
            calls = []

            def fake_m(target, modules, log, **kw):
                calls.append(list(modules))
                log.parent.mkdir(parents=True, exist_ok=True)
                if modules == ["XCarMediaApp-lint"]:
                    log.write_text("FAILED: ninja: unknown target 'XCarMediaApp-lint'\n")
                    return util.CommandResult("m", 1, log, 0.0)
                xml = Path(root) / xml_rel
                xml.parent.mkdir(parents=True, exist_ok=True)
                xml.write_text('<issues format="6"><issue id="Typos" severity="Warning" message="t" '
                               'category="Typography"><location file="vendor/x/apps/Media/Media/A.java" '
                               'line="2"/></issue></issues>')
                log.write_text("ok\n")
                return util.CommandResult("m", 0, log, 0.0)
            res = StageResult("lint", "app")
            with mock.patch.object(tree, "m", side_effect=fake_m), \
                    mock.patch.object(tree, "lock", return_value=mock.MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: False)):
                LintStage().run(ctx, res)
            self.assertEqual(calls, [["XCarMediaApp-lint"], [xml_rel]])
            self.assertEqual(res.metrics["warnings"], 1)
            self.assertIn("1 warnings in 1 module(s)", res.summary)

    def test_module_without_lint_rule_is_reported(self):
        from appci import util
        from appci.aosp import AospTree
        from appci.config import Config, DEFAULTS, deep_merge
        from appci.context import RunContext
        from appci.discovery import AppProfile
        from appci.model import ERROR, StageResult
        from appci.stages.lint import LintStage
        with tempfile.TemporaryDirectory() as root:
            cfg = Config(deep_merge(DEFAULTS, {"aosp_root": root, "workdir": root + "/wd", "apps": []}), None)
            tree = AospTree(cfg)
            profile = AppProfile(path="p/X", name="X", slug="app", apps=["X"], module_paths={"X": "p/X"})
            ctx = RunContext(cfg=cfg, tree=tree, profile=profile, entry={}, report_root=Path(root) / "reports")

            def fake_m(target, modules, log, **kw):
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(f"FAILED: ninja: unknown target '{modules[0]}'\n")
                return util.CommandResult("m", 1, log, 0.0)
            res = StageResult("lint", "app")
            with mock.patch.object(tree, "m", side_effect=fake_m), \
                    mock.patch.object(tree, "lock", return_value=mock.MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: False)):
                LintStage().run(ctx, res)
            self.assertEqual(res.status, ERROR)
            self.assertIn("no lint rule for X", res.summary)


class DiscoveryVendorTypes(unittest.TestCase):
    """A vendor tree defines its apps with soong_config_module_type names appci does not know:
    discovery must say what it skipped (the job log is all the admin sees), and
    discovery.extra_prod_types makes such a type count as an app."""

    def run_discovery(self, extra, warnings):
        from unittest import mock
        from appci import discovery, util
        from appci.aosp import AospTree
        from appci.config import Config, DEFAULTS, deep_merge
        with tempfile.TemporaryDirectory() as root:
            data = deep_merge(DEFAULTS, {"aosp_root": root, "workdir": root + "/wd", "apps": [],
                                         "discovery": {"extra_prod_types": extra}})
            cfg = Config(data, None)
            tree = AospTree(cfg)
            (Path(root) / "vendor" / "x" / "apps" / "Media" / "Media").mkdir(parents=True)
            (Path(root) / "vendor" / "x" / "apps" / "Media" / "Media" / "Android.bp").write_text("")
            jdeps = {"XCarMediaApp": {"module_type": "x_car_app", "path": ["vendor/x/apps/Media/Media"],
                                      "srcs": [], "dependencies": []},
                     "Other": {"module_type": "android_app", "path": ["packages/apps/Other"]}}
            with mock.patch.object(tree, "ensure_metadata"), mock.patch.object(tree, "jdeps", return_value=jdeps), \
                    mock.patch.object(tree, "module_info", return_value={}), \
                    mock.patch.object(util, "warn", side_effect=lambda m: warnings.append(m)):
                return discovery.discover_app(tree, "vendor/x/apps/Media", "device", {})

    def test_unknown_type_is_reported_then_accepted(self):
        warnings = []
        p = self.run_discovery([], warnings)
        self.assertEqual(p.prod_modules, [])
        self.assertTrue(any("XCarMediaApp (x_car_app, vendor/x/apps/Media/Media)" in w for w in warnings), warnings)
        self.assertTrue(any("extra_prod_types" in w for w in warnings))
        warnings.clear()
        p = self.run_discovery(["x_car_app"], warnings)
        self.assertEqual(p.apps, ["XCarMediaApp"])
        self.assertEqual(warnings, [])

    def test_nothing_listed_names_build_files_and_near_misses(self):
        from unittest import mock
        from appci import discovery, util
        from appci.aosp import AospTree
        from appci.config import Config, DEFAULTS, deep_merge
        warnings = []
        with tempfile.TemporaryDirectory() as root:
            cfg = Config(deep_merge(DEFAULTS, {"aosp_root": root, "workdir": root + "/wd", "apps": []}), None)
            tree = AospTree(cfg)
            (Path(root) / "vendor" / "x" / "apps" / "Media" / "Media").mkdir(parents=True)
            (Path(root) / "vendor" / "x" / "apps" / "Media" / "Media" / "Android.bp").write_text("")
            jdeps = {"MediaPrebuilt": {"module_type": "android_app_import", "path": ["vendor/x/prebuilts/Media"]}}
            with mock.patch.object(tree, "ensure_metadata"), mock.patch.object(tree, "jdeps", return_value=jdeps), \
                    mock.patch.object(tree, "module_info", return_value={}), \
                    mock.patch.object(util, "warn", side_effect=lambda m: warnings.append(m)):
                p = discovery.discover_app(tree, "vendor/x/apps/Media", "device", {})
        self.assertEqual(p.prod_modules, [])
        text = "\n".join(warnings)
        self.assertIn("Soong lists no module under vendor/x/apps/Media", text)
        self.assertIn("vendor/x/apps/Media/Media/Android.bp", text)
        self.assertIn("MediaPrebuilt (android_app_import, vendor/x/prebuilts/Media)", text)


class BlueprintModuleTypes(unittest.TestCase):
    """Android 14 Soong writes no module_type into module_bp_java_deps.json: appci reads the type
    from Android.bp, resolving soong_config_module_type wrappers to the Soong type they wrap."""

    def test_types_from_android_bp_with_custom_types(self):
        from appci import blueprint
        with tempfile.TemporaryDirectory() as root:
            r = Path(root)
            (r / "vendor" / "x" / "soong").mkdir(parents=True)
            (r / "vendor" / "x" / "soong" / "Android.bp").write_text(
                'soong_config_module_type {\n    name: "x_car_app",\n    module_type: "android_app",\n'
                '    config_namespace: "x",\n    variables: ["flavor"],\n    properties: ["srcs"],\n}\n')
            d = r / "vendor" / "x" / "apps" / "Media" / "Media"
            d.mkdir(parents=True)
            (d / "Android.bp").write_text(
                '// the app\nsoong_config_module_type_import {\n    from: "vendor/x/soong/Android.bp",\n'
                '    module_types: ["x_car_app"],\n}\n\n'
                'x_car_app {\n    name: "XCarMediaApp",\n    srcs: ["src/**/*.java"],\n'
                '    optimize: { enabled: false },\n}\n\n'
                'android_library {\n    name: "XCarMediaLib",\n    srcs: ["lib/**/*.kt"],\n}\n\n'
                'android_test {\n    name: "XCarMediaTests",\n}\n')
            types = blueprint.module_types(root, str(d / "Android.bp"))
            self.assertEqual(types, {"XCarMediaApp": "android_app", "XCarMediaLib": "android_library",
                                     "XCarMediaTests": "android_test"})
            jdeps = {"XCarMediaApp": {"path": ["vendor/x/apps/Media/Media"], "srcs": []},
                     "XCarMediaLib": {"path": ["vendor/x/apps/Media/Media"]},
                     "Elsewhere": {"path": ["packages/apps/None"]}}
            self.assertEqual(blueprint.fill_module_types(r, jdeps), 2)
            self.assertEqual(jdeps["XCarMediaApp"]["module_type"], "android_app")
            self.assertNotIn("module_type", jdeps["Elsewhere"])

    def test_tree_completes_jdeps_once_and_caches(self):
        """Android 14 jdeps: no module_type anywhere, device modules without path or srcs. appci
        fills them from module-info.json and Android.bp, and the next process reads the cache."""
        from unittest import mock
        from appci import aosp
        from appci.aosp import AospTree
        from appci.config import Config
        with tempfile.TemporaryDirectory() as root:
            r = Path(root)
            cfg = Config({"aosp_root": root, "workdir": root + "/wd",
                          "targets": {"device": {"lunch": "x-userdebug", "out_dir": "out", "env": {}}}}, None)
            tree = AospTree(cfg)
            (r / "out" / "soong").mkdir(parents=True)
            app = r / "vendor" / "x" / "apps" / "Media" / "MediaApp"
            (app / "src" / "com" / "x").mkdir(parents=True)
            (app / "src" / "com" / "x" / "Main.java").write_text("")
            (app / "src" / "com" / "x" / "Util.kt").write_text("")
            (app / "src" / "com" / "x" / "Gen.aidl").write_text("")
            (app / "Android.bp").write_text('android_app {\n    name: "XCarMediaApp",\n'
                                            '    srcs: ["src/**/*.java", "src/**/*.kt", ":gen"],\n}\n')
            (r / "cts" / "host").mkdir(parents=True)
            jd = r / "out" / "soong" / "module_bp_java_deps.json"
            jd.write_text(json.dumps({
                "XCarMediaApp": {"dependencies": ["androidx.core_core"]},          # device: no path, no srcs
                "HostTest": {"path": ["cts/host"], "srcs": ["cts/host/T.java"]},  # host: path, no type
                "Mk": {},                                                           # unknown to Android.bp
            }))
            minfo = {"XCarMediaApp": {"class": ["APPS"], "path": ["vendor/x/apps/Media/MediaApp"]},
                     "HostTest": {"class": ["JAVA_LIBRARIES"], "path": ["cts/host"], "test_config": ["c.xml"],
                                  "installed": ["out/host/linux-x86/framework/HostTest.jar"]},
                     "Mk": {"class": ["APPS"], "path": ["vendor/x/mk"]}}
            aosp._load_json.cache_clear(); aosp._types_filled.clear()
            with mock.patch.object(tree, "module_info", return_value=minfo):
                d = tree.jdeps("device")
            self.assertEqual(d["XCarMediaApp"]["path"], ["vendor/x/apps/Media/MediaApp"])
            self.assertEqual(d["XCarMediaApp"]["module_type"], "android_app")
            self.assertEqual(d["XCarMediaApp"]["srcs"], ["vendor/x/apps/Media/MediaApp/src/com/x/Main.java",
                                                         "vendor/x/apps/Media/MediaApp/src/com/x/Util.kt"])
            self.assertEqual(d["HostTest"]["module_type"], "java_test_host")       # from the Make class
            self.assertEqual(d["Mk"]["module_type"], "android_app")
            cache = json.loads((r / "wd" / "cache" / "jdeps_completion.x-userdebug.json").read_text())
            self.assertEqual(set(cache["modules"]), {"XCarMediaApp", "HostTest", "Mk"})
            self.assertNotIn("srcs", cache["modules"]["HostTest"])                   # Soong's own data not copied
            # next process: the cache is used, Android.bp and module-info not needed any more
            (app / "Android.bp").unlink()
            aosp._load_json.cache_clear(); aosp._types_filled.clear()
            with mock.patch.object(tree, "module_info", side_effect=AssertionError("must not be read")):
                d = tree.jdeps("device")
            self.assertEqual(d["XCarMediaApp"]["module_type"], "android_app")
            self.assertEqual(len(d["XCarMediaApp"]["srcs"]), 2)


class JdkResolution(unittest.TestCase):
    """Android 14 trees ship prebuilts/jdk/jdk17, not jdk21: the JDK is found in the tree, and
    without any JDK the stages fail with a message instead of a traceback from subprocess."""

    def test_tree_jdk_fallback_and_clear_error(self):
        import os
        from unittest import mock
        from appci.config import Config
        with tempfile.TemporaryDirectory() as root:
            cfg = Config({"aosp_root": root, "java_home": "prebuilts/jdk/jdk21/linux-x86", "targets": {}}, None)
            j17 = Path(root) / "prebuilts" / "jdk" / "jdk17" / "linux-x86" / "bin"
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
                with self.assertRaisesRegex(RuntimeError, "no JDK"):
                    cfg.java
                j17.mkdir(parents=True)
                (j17 / "java").write_text("")
                self.assertEqual(cfg.java, str(j17 / "java"))
                self.assertEqual(cfg.java_home, j17.parent)
            cfg = Config({"aosp_root": root, "java_home": "prebuilts/jdk/jdk17/linux-x86", "targets": {}}, None)
            self.assertEqual(cfg.java_home, j17.parent)                 # configured one wins when present

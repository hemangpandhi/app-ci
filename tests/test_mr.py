"""Tests for merge-request features: impact selection, pre-upload parsing, change-risk parsers."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from appci.config import DEFAULTS, deep_merge  # noqa: E402
from appci.context import ChangeSet  # noqa: E402
from appci.discovery import AppProfile, TestModule  # noqa: E402
from appci.impact import compute_impact, select_tests, test_mapping_tests  # noqa: E402
from appci.stages.preupload import parse_repohooks_output  # noqa: E402
from appci.stages.risk import SECRET_PATTERNS, aconfig_flags, bp_modules, manifest_facts  # noqa: E402

APP = "packages/apps/X"
JDEPS = {
    "XApp": {"module_type": "android_app", "path": [APP], "srcs": [f"{APP}/src/a/Main.java"],
             "dependencies": ["XLib"], "manifest": f"{APP}/AndroidManifest.xml"},
    "XLib": {"module_type": "android_library", "path": [f"{APP}/lib"], "srcs": [f"{APP}/lib/src/a/L.java"],
             "resource_dirs": [f"{APP}/lib/res"], "dependencies": []},
    "XRoboTests": {"module_type": "android_robolectric_test", "path": [f"{APP}/tests/robo"],
                   "srcs": [f"{APP}/tests/robo/src/a/MainTest.java"], "dependencies": ["XApp"]},
    "XDeviceTests": {"module_type": "android_test", "path": [f"{APP}/tests/device"],
                     "srcs": [f"{APP}/tests/device/src/a/DevTest.java"], "dependencies": []},
    "YApp": {"module_type": "android_app", "path": ["packages/apps/Y"], "srcs": [], "dependencies": ["XLib"]},
    "YRoboTests": {"module_type": "android_robolectric_test", "path": ["packages/apps/Y/tests"],
                   "srcs": [], "dependencies": ["YApp"]},
}
MINFO = {"XRoboTests": {"compatibility_suites": ["robolectric-tests"]},
         "YRoboTests": {"compatibility_suites": ["robolectric-tests"]},
         "XDeviceTests": {"class": ["APPS"], "test_config": ["x.xml"]}}


def profile(path=APP):
    if path == APP:
        return AppProfile(
            path=APP, name="XApp", slug="x", apps=["XApp"], libs=["XLib"],
            tests=[TestModule("XRoboTests", "robolectric", f"{APP}/tests/robo", "XApp"),
                   TestModule("XDeviceTests", "device", f"{APP}/tests/device")],
            module_paths={m: v["path"][0] for m, v in JDEPS.items() if v["path"][0].startswith(APP)})
    return AppProfile(path=path, name="YApp", slug="y", apps=["YApp"],
                      tests=[TestModule("YRoboTests", "robolectric", "packages/apps/Y/tests", "YApp")],
                      module_paths={"YApp": path, "YRoboTests": "packages/apps/Y/tests"})


class FakeCfg(dict):
    def __init__(self, root, apps):
        super().__init__(deep_merge(DEFAULTS, {"apps": apps}))
        self.aosp_root = root

    def stage_cfg(self, stage, entry=None):
        return self["stages"].get(stage, {})

    def app_entries(self):
        return self["apps"]


def make_ctx(root, files, labels=()):
    import appci.impact as impact_mod
    impact_mod.discover_app = lambda tree, p, target=None, entry=None: profile(p)  # noqa: E731
    tree = SimpleNamespace(jdeps=lambda t: JDEPS, module_info=lambda t: MINFO)
    cfg = FakeCfg(root, [{"path": APP}, {"path": "packages/apps/Y"}])
    cs = None if files is None else ChangeSet("base", {f: "M" for f in files}, {f: {1} for f in files})
    ctx = SimpleNamespace(cfg=cfg, tree=tree, profile=profile(), entry={"path": APP}, changeset=cs,
                          labels=set(labels), changed=None)
    ctx.impact = compute_impact(ctx)
    return ctx


class Impact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, ctx, kinds):
        return [t.name for t in select_tests(ctx, kinds)[0]]

    def test_unknown_change_runs_everything(self):
        ctx = make_ctx(self.root, None)
        self.assertEqual(ctx.impact.mode, "full")
        self.assertEqual(self.names(ctx, ["robolectric"]), ["XRoboTests"])
        self.assertEqual(self.names(ctx, ["device"]), ["XDeviceTests"])

    def test_docs_only(self):
        ctx = make_ctx(self.root, [f"{APP}/README.md", f"{APP}/OWNERS"])
        self.assertEqual(ctx.impact.mode, "none")
        self.assertEqual(self.names(ctx, ["robolectric"]), [])

    def test_failsafe_file(self):
        ctx = make_ctx(self.root, [f"{APP}/Android.bp"])
        self.assertEqual(ctx.impact.mode, "full")
        self.assertTrue(any("Android.bp" in r for r in ctx.impact.reasons))

    def test_unmapped_file_is_failsafe(self):
        ctx = make_ctx(self.root, ["vendor/other/run.sh"])
        self.assertEqual(ctx.impact.mode, "full")

    def test_test_only_change(self):
        ctx = make_ctx(self.root, [f"{APP}/tests/device/src/a/DevTest.java"])
        self.assertEqual(ctx.impact.mode, "selected")
        self.assertEqual(self.names(ctx, ["robolectric"]), [])
        self.assertEqual(self.names(ctx, ["device"]), ["XDeviceTests"])

    def test_app_code_change_and_test_mapping(self):
        (self.root / APP).mkdir(parents=True)
        (self.root / APP / "TEST_MAPPING").write_text(
            '// comment\n{"x-presubmit": [{"name": "XDeviceTests"}], "postsubmit": [{"name": "Other"}]}')
        ctx = make_ctx(self.root, [f"{APP}/src/a/Main.java"])
        self.assertEqual(ctx.impact.mode, "selected")
        self.assertEqual(self.names(ctx, ["robolectric"]), ["XRoboTests"])
        self.assertEqual(self.names(ctx, ["device"]), ["XDeviceTests"])
        self.assertEqual(ctx.impact.dependents, [])

    def test_library_change_pulls_in_dependents(self):
        ctx = make_ctx(self.root, [f"{APP}/lib/res/values/strings.xml"])
        self.assertEqual(ctx.impact.changed_libraries, ["XLib"])
        self.assertEqual([d["path"] for d in ctx.impact.dependents], ["packages/apps/Y"])
        self.assertEqual(self.names(ctx, ["robolectric"]), ["XRoboTests", "YRoboTests"])

    def test_labels(self):
        ctx = make_ctx(self.root, [f"{APP}/tests/robo/src/a/MainTest.java"], labels=["device-test"])
        self.assertEqual(self.names(ctx, ["robolectric"]), ["XRoboTests"])
        self.assertEqual(self.names(ctx, ["device"]), ["XDeviceTests"])
        ctx = make_ctx(self.root, [f"{APP}/README.md"], labels=["full-ci"])
        self.assertEqual(ctx.impact.mode, "full")

    def test_test_mapping_imports(self):
        (self.root / "lib").mkdir()
        (self.root / "lib" / "TEST_MAPPING").write_text('{"presubmit": [{"name": "LibTests"}]}')
        (self.root / "app").mkdir()
        (self.root / "app" / "TEST_MAPPING").write_text('{"imports": [{"path": "lib"}]}')
        self.assertEqual(test_mapping_tests(self.root, ["app/x.java"], ["presubmit"]), {"LibTests"})


class Preupload(unittest.TestCase):
    def test_parse(self):
        out = ("[RUNNING 1/3] checkstyle_hook\n[FAILED] checkstyle_hook hook\n    Line too long\n"
               "[RUNNING 2/3] x\n[PASSED] overlayable hook\n[FAILED] commit_msg_test_field hook\n"
               "    missing Test:\n[FAILED] repohooks for platform/x failed in 1s\n")
        f = parse_repohooks_output(out)
        self.assertEqual([x["hook"] for x in f], ["checkstyle_hook", "commit_msg_test_field"])
        self.assertEqual(f[0]["details"], ["    Line too long"])

    def test_pinned_modules(self):
        import sys
        from appci.stages.preupload import _module_names, _python_ok, hook_pythonpath
        self.assertEqual(_module_names(["lxml==5.3.0", "yaml"]), ["lxml", "yaml"])
        if sys.version_info >= (3, 9):   # importable module: nothing to fetch, pin ignored
            self.assertTrue(_python_ok(sys.executable, ["json==1.0"]))
            self.assertEqual(hook_pythonpath(None, {"python_modules": ["json==1.0"]}, sys.executable), "")


class RiskParsers(unittest.TestCase):
    def test_manifest(self):
        m = manifest_facts('''<manifest xmlns:android="http://schemas.android.com/apk/res/android"
            package="a.b" android:sharedUserId="android.uid.system">
          <uses-permission android:name="android.permission.MANAGE_USERS"/>
          <permission android:name="a.b.P" android:protectionLevel="signature"/>
          <application>
            <activity android:name=".A"><intent-filter><action android:name="x"/></intent-filter></activity>
            <activity android:name=".B" android:exported="false"><intent-filter/></activity>
            <service android:name=".S" android:exported="true"/>
          </application></manifest>''')
        self.assertEqual(m["package"], "a.b")
        self.assertEqual(m["uses"], {"android.permission.MANAGE_USERS"})
        self.assertEqual(m["exported"], {"activity:.A", "service:.S"})
        self.assertEqual(m["defines"], {"a.b.P": "signature"})

    def test_blueprint(self):
        mods = bp_modules('''
        // comment { not a module
        android_app {
            name: "X",
            static_libs: ["a", "b"], // trailing
            libs: ["c"],
            platform_apis: true,
            optimize: { enabled: false, },
            certificate: "platform",
        }
        filegroup { name: "fg", srcs: ["x/**/*.java"] }''')
        self.assertEqual(mods["X"]["deps"], {"static_libs:a", "static_libs:b", "libs:c"})
        self.assertTrue(mods["X"]["platform_apis"])
        self.assertFalse(mods["X"]["privileged"])
        self.assertEqual(mods["X"]["certificate"], "platform")
        self.assertIn("fg", mods)

    def test_aconfig(self):
        flags, header = aconfig_flags('''package: "a.b"
        container: "system"
        flag {
            name: "good"
            namespace: "car"
            description: "d"
            bug: "123"
            metadata { purpose: PURPOSE_BUGFIX }
        }
        flag { name: "bad" namespace: "car" }''')
        self.assertEqual(header, {"package": "a.b", "container": "system"})
        self.assertEqual(flags["good"]["bug"], "123")
        self.assertNotIn("description", flags["bad"])

    def test_secrets(self):
        def hit(text):
            return next((rule for sev, rule, rx in SECRET_PATTERNS if rx.search(text)), None)
        self.assertEqual(hit('token = "glpat-' + "a" * 20 + '"'), "gitlab-token")
        self.assertEqual(hit("-----BEGIN RSA PRIVATE KEY-----"), "private-key")
        self.assertEqual(hit('password = "hunter2hunter2"'), "generic-secret")
        self.assertIsNone(hit('String password = getPassword();'))


if __name__ == "__main__":
    unittest.main()

"""Prebuilt emulator downloads: folder listing (Artifactory storage API / HTML index / local dir),
newest-release pick, checksum check, cache reuse and pruning, auth headers."""
import hashlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from appci import artifacts  # noqa: E402


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class FakeArtifactory(http.server.BaseHTTPRequestHandler):
    """/artifactory/api/storage/<repo>/<path> (JSON) and /artifactory/<repo>/<path>/<file>."""
    files = {}
    seen_auth = []
    bad_checksum = False
    no_checksum = False

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(body=False)

    def do_GET(self, body=True):
        FakeArtifactory.seen_auth.append(self.headers.get("Authorization"))
        if self.path.startswith("/redirect/"):          # e.g. Artifactory redirect download to S3
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/artifactory/imgs/"
                             "tangorpro/latest/" + self.path.rsplit("/", 1)[-1])
            self.end_headers()
            return
        p = self.path
        if p.startswith("/artifactory/api/storage/imgs/tangorpro/latest"):
            body = json.dumps({"children": [{"uri": "/" + n, "folder": False} for n in self.files]
                               + [{"uri": "/old", "folder": True}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if p.endswith(".sha256"):
            self.send_response(404)
            self.end_headers()
            return
        name = p.rsplit("/", 1)[-1]
        if p.startswith("/artifactory/imgs/tangorpro/latest/") and name in self.files:
            data = self.files[name]
            digest = hashlib.sha256(data).hexdigest()
            self.send_response(200)
            if not self.no_checksum:
                self.send_header("X-Checksum-Sha256", "0" * 64 if self.bad_checksum else digest)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()


class Downloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeArtifactory)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/artifactory/imgs/tangorpro/latest/"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        FakeArtifactory.bad_checksum = False
        FakeArtifactory.no_checksum = False
        FakeArtifactory.files = {
            "RELEASE_0009.zip": _zip({"x86_64/system.img": b"old"}),
            "RELEASE_0010.zip": _zip({"x86_64/system.img": b"new", "x86_64/vendor.img": b"v"}),
            "notes.txt": b"n",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_newest_release_downloaded_verified_cached(self):
        rel = artifacts.fetch(None, "image", self.base, "RELEASE_*.zip", self.cache)
        self.assertEqual(rel.name, "RELEASE_0010")                        # natural sort, not 0009
        self.assertEqual((artifacts.image_dir(rel) / "system.img").read_bytes(), b"new")
        self.assertNotIn(str(self.cache), str(rel))                       # unpacked privately, not in the cache
        archive = self.cache / "image" / "RELEASE_0010.zip"
        self.assertTrue(archive.exists())                                  # kept for later jobs
        rel2 = artifacts.fetch(None, "image", self.base + "RELEASE_0010.zip", None, self.cache)
        self.assertEqual((artifacts.image_dir(rel2) / "system.img").read_bytes(), b"new")

    def test_tampered_cache_replaced(self):
        """Another job (MR build code) rewrites the cached release: detected and downloaded again."""
        artifacts.fetch(None, "image", self.base, "RELEASE_*.zip", self.cache)
        archive = self.cache / "image" / "RELEASE_0010.zip"
        archive.write_bytes(_zip({"x86_64/system.img": b"evil"}))
        rel = artifacts.fetch(None, "image", self.base, "RELEASE_*.zip", self.cache)
        self.assertEqual((artifacts.image_dir(rel) / "system.img").read_bytes(), b"new")

    def test_unverifiable_release_refused(self):
        FakeArtifactory.no_checksum = True
        with self.assertRaisesRegex(RuntimeError, "publishes no SHA-256"):
            artifacts.fetch(None, "image", self.base, "RELEASE_*.zip", self.cache)
        rel = artifacts.fetch(None, "image", self.base, "RELEASE_*.zip", self.cache, require_checksum=False)
        self.assertEqual(rel.name, "RELEASE_0010")

    def test_credentials_not_forwarded_to_other_host(self):
        """A redirect to another host (127.0.0.1 -> localhost) must not receive the token."""
        FakeArtifactory.seen_auth.clear()
        host = self.base.split("/artifactory/")[0]
        with mock.patch.dict(os.environ, {"APPCI_ARTIFACTORY_TOKEN": "t0k"}):
            with artifacts._open(None, host + "/redirect/RELEASE_0010.zip") as r:
                self.assertEqual(r.read(), FakeArtifactory.files["RELEASE_0010.zip"])
        self.assertEqual(FakeArtifactory.seen_auth, ["Bearer t0k", None])

    def test_new_release_prunes_old(self):
        artifacts.fetch(None, "image", self.base, "RELEASE_*.zip", self.cache)
        FakeArtifactory.files["RELEASE_0011.zip"] = _zip({"system.img": b"newer"})
        artifacts.fetch(None, "image", self.base, "RELEASE_*.zip", self.cache, keep=0)
        self.assertEqual(sorted(p.name for p in (self.cache / "image").iterdir() if not p.name.startswith(".")),
                         ["RELEASE_0011.zip"])

    def test_checksum_mismatch_rejected(self):
        FakeArtifactory.bad_checksum = True
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            artifacts.fetch(None, "image", self.base, "RELEASE_*.zip", self.cache)
        self.assertFalse((self.cache / "image" / "RELEASE_0010.zip").exists())

    def test_auth_headers(self):
        FakeArtifactory.seen_auth.clear()
        with mock.patch.dict(os.environ, {"APPCI_ARTIFACTORY_TOKEN": "t0k"}):
            artifacts.resolve(None, self.base, "RELEASE_*.zip")
        self.assertIn("Bearer t0k", FakeArtifactory.seen_auth)
        with mock.patch.dict(os.environ, {"APPCI_ARTIFACTORY_USER": "u", "APPCI_ARTIFACTORY_PASSWORD": "p"}):
            self.assertEqual(artifacts._headers(), {"Authorization": "Basic dTpw"})

    def test_local_dir_and_html_index(self):
        d = self.cache / "share"
        d.mkdir()
        for n, data in FakeArtifactory.files.items():
            (d / n).write_bytes(data)
        self.assertEqual(artifacts.resolve(None, d.as_uri() + "/", "RELEASE_*.zip")[1], "RELEASE_0010.zip")
        page = '<a href="RELEASE_0002.zip">x</a><a href="RELEASE_0010.zip">y</a><a href="../">up</a>'
        with mock.patch.object(artifacts, "_open") as op:
            op.return_value.__enter__.return_value.read.return_value = page.encode()
            self.assertEqual(artifacts.resolve(None, "http://h/share/", "RELEASE_*.zip")[1], "RELEASE_0010.zip")

    def test_emulator_program(self):
        d = self.cache / "emu" / "emulator"
        d.mkdir(parents=True)
        (d / "emulator").write_text("#!/bin/sh\n")
        (d / "emulator").chmod(0o755)
        self.assertEqual(artifacts.emulator_binary(self.cache / "emu"), d / "emulator")


if __name__ == "__main__":
    unittest.main()

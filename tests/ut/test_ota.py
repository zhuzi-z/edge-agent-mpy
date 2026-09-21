"""OTA: bundle install, the update states, and the boot-time recovery layer."""

import compat  # noqa: F401

import json
import os
import hashlib
import unittest

import machine
import app.config as config
import app.ota as ota
import main as recovery
from app.api.server import HTTPServer
from app.controllers import WiFiManager
from app.util import ensure_dir, ensure_parent_dir
from helpers import ServerHarness, mkdtemp, path_join, raw_request, rmtree, tar_entry

NEW_APP = {"app/main.py": b"new = True\n", "app/web/index.html": b"<html>new</html>"}
OLD_APP = {
    "main.py": b"old = True\n",
    "web/index.html": b"<html>old</html>",
    "gone.py": b"x = 1\n",
}


def build_bundle(files, version="v2", manifest=None):
    """A whole bundle as bytes: manifest.json first, then every file."""
    if manifest is None:
        manifest = {
            "version": version,
            "files": {
                name: {"size": len(data), "sha256": hashlib.sha256(data).digest().hex()}
                for name, data in files.items()
            },
        }
    bundle = tar_entry("manifest.json", json.dumps(manifest).encode())
    for name in sorted(files):
        bundle += tar_entry(name, files[name])
    return bundle + b"\0" * 1024  # the two zero blocks that end a tar


class OtaCase(unittest.TestCase):
    """Points the OTA paths at a throwaway directory that stands in for flash."""

    def setUp(self):
        self.root = mkdtemp("ota_")
        self.app = path_join(self.root, "app")
        self.pre = path_join(self.root, "pre")
        self.skills = path_join(self.root, "skills")
        self.bundle = path_join(self.root, "ota.tar")
        self._saved = (
            config.OTA_APP_DIR,
            config.OTA_PRE_DIR,
            config.OTA_STATE_PATH,
            config.OTA_BUNDLE_PATH,
            config.UPLOADED_SKILLS_PATH,
        )
        config.OTA_APP_DIR = self.app
        config.OTA_PRE_DIR = self.pre
        config.OTA_STATE_PATH = path_join(self.root, "ota.json")
        config.OTA_BUNDLE_PATH = self.bundle
        config.UPLOADED_SKILLS_PATH = self.skills
        self._write(OLD_APP, self.app)
        super().setUp()  # ServerHarness, for the tests that serve over a socket

    def tearDown(self):
        super().tearDown()
        (
            config.OTA_APP_DIR,
            config.OTA_PRE_DIR,
            config.OTA_STATE_PATH,
            config.OTA_BUNDLE_PATH,
            config.UPLOADED_SKILLS_PATH,
        ) = self._saved
        rmtree(self.root)

    @staticmethod
    def _write(files, root):
        for name, data in files.items():
            path = root + "/" + name
            ensure_parent_dir(path)
            with open(path, "wb") as handle:
                handle.write(data)

    def _put_bundle(self, files=NEW_APP, version="v2", manifest=None, raw=None):
        data = raw if raw is not None else build_bundle(files, version, manifest)
        with open(self.bundle, "wb") as handle:
            handle.write(data)
        return data

    def _read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def _app_files(self, root=None, prefix=""):
        """Every file under the installed app (no os.walk on MicroPython)."""
        root = root or self.app
        found = []
        for name in os.listdir(root):
            path = root + "/" + name
            try:
                found += self._app_files(path, prefix + name + "/")
            except OSError:
                found.append(prefix + name)
        return sorted(found)


class TestInstall(OtaCase):
    def test_install_keeps_the_previous_app(self):
        """A good bundle replaces /app, keeps what it replaced, and arms a trial."""
        self._put_bundle()
        result = ota.install(self.bundle)
        # The new tree is exactly the bundle: gone.py went with the old app.
        self.assertEqual(self._app_files(), ["main.py", "web/index.html"])
        self.assertEqual(self._read(self.app + "/main.py"), b"new = True\n")
        # ...and the old one is under /pre/app, where `import app` finds it.
        self.assertEqual(self._read(self.pre + "/app/main.py"), b"old = True\n")
        self.assertEqual(self._read(self.pre + "/app/gone.py"), b"x = 1\n")
        state = ota.read_state()
        self.assertEqual(
            (state["status"], state["current"], state["previous"]), ("trial", "v2", "")
        )
        self.assertEqual(
            (result["status"], result["version"], result["running"], result["has_previous"]),
            ("trial", "v2", self.app, True),
        )

    def test_install_a_second_time_keeps_the_running_one(self):
        """The fallback is always the version that was running, not an older one."""
        self._put_bundle()
        ota.install(self.bundle)
        ota.confirm()
        self._put_bundle({"app/main.py": b"third = True\n"}, version="v3")
        ota.install(self.bundle)
        self.assertEqual(self._read(self.app + "/main.py"), b"third = True\n")
        self.assertEqual(self._read(self.pre + "/app/main.py"), b"new = True\n")
        state = ota.read_state()
        self.assertEqual(
            (state["status"], state["current"], state["previous"]), ("trial", "v3", "v2")
        )

    def test_install_over_a_rollback_keeps_the_good_fallback(self):
        """Running from /pre, an update replaces the failed /app and keeps /pre."""
        self._put_bundle()
        ota.install(self.bundle)
        ota.rollback()  # the new app never came up: /pre/app is the good one
        self._put_bundle({"app/main.py": b"fixed = True\n"}, version="v3")
        ota.install(self.bundle)
        self.assertEqual(self._read(self.app + "/main.py"), b"fixed = True\n")
        self.assertEqual(self._read(self.pre + "/app/main.py"), b"old = True\n")
        state = ota.read_state()
        self.assertEqual(
            (state["status"], state["current"], state["previous"]), ("trial", "v3", "")
        )

    def test_skills_land_beside_the_app(self):
        """A bundle may carry skills; they go to the uploaded-skills root."""
        self._put_bundle(
            {"app/main.py": b"new = True\n", "skills/mijia/code.py": b"def run(a): pass\n"}
        )
        ota.install(self.bundle)
        self.assertEqual(self._read(self.skills + "/mijia/code.py"), b"def run(a): pass\n")

    def test_refused_bundles_leave_the_device_alone(self):
        """Everything wrong with a bundle is caught before /app is touched."""
        refusals = [
            (build_bundle(NEW_APP)[512 * 2 :], "no manifest"),  # header of the first file only
            (build_bundle(NEW_APP, manifest={"version": "v2", "files": {}}), "empty manifest"),
            (build_bundle({"main.py": b"x"}), "outside app/ and skills/"),
            (build_bundle({"app/../config/wifi.json": b"x"}), "a path that climbs out"),
            (
                build_bundle(
                    NEW_APP,
                    manifest={
                        "version": "v2",
                        "files": {"app/other.py": {"size": 1, "sha256": "0"}},
                    },
                ),
                "manifest lists a file the bundle lacks",
            ),
        ]
        for raw, why in refusals:
            self._put_bundle(raw=raw)
            with self.assertRaises(ota.OtaError):
                ota.install(self.bundle)
            self.assertEqual(self._read(self.app + "/main.py"), b"old = True\n", why)
            self.assertEqual(ota.read_state(), {}, why)
            self.assertFalse(os.listdir(self.root).count("pre"), why)

    def test_a_corrupt_file_is_put_back(self):
        """A digest that does not match is caught mid-install, and /app returns."""
        files = dict(NEW_APP)
        manifest = {
            "version": "v2",
            "files": {
                name: {"size": len(data), "sha256": hashlib.sha256(b"other").digest().hex()}
                for name, data in files.items()
            },
        }
        self._put_bundle(files, manifest=manifest)
        with self.assertRaises(ota.OtaError):
            ota.install(self.bundle)
        # The app that was running is back where it was, and nothing was armed.
        self.assertEqual(self._read(self.app + "/main.py"), b"old = True\n")
        self.assertEqual(self._read(self.app + "/gone.py"), b"x = 1\n")
        self.assertEqual(ota.read_state(), {})
        self.assertFalse(ota.has_pre())


class TestState(OtaCase):
    def test_confirm_status_and_rollback(self):
        """The states an update walks through, as src/main.py reads them."""
        # No update ever: nothing to confirm, and no state file is invented.
        ota.confirm()
        self.assertEqual(ota.read_state(), {})
        self.assertEqual(ota.status()["status"], "none")
        self.assertFalse(ota.status()["has_previous"])
        # A trial becomes "ok" once the app says it came up.
        self._put_bundle()
        ota.install(self.bundle)
        ota.confirm()
        self.assertEqual(ota.read_state()["status"], "ok")
        self.assertEqual((ota.status()["version"], ota.status()["fallback"]), ("v2", ""))
        # Rolling back marks the update bad, which is what makes /pre run, and
        # the reported version becomes the one that is actually answering.
        result = ota.rollback()
        self.assertEqual((result["status"], result["running"]), ("bad", self.pre))
        self.assertEqual(result["version"], "")  # nothing was confirmed before v2
        ota.confirm()  # a fallback boot is not an update: the state stays bad
        self.assertEqual(ota.read_state()["status"], "bad")

    def test_rollback_without_a_fallback(self):
        with self.assertRaises(ota.OtaError):
            ota.rollback()


class TestRecovery(OtaCase):
    """src/main.py: which app runs, decided before any of it is imported."""

    def setUp(self):
        super().setUp()
        self._saved_recovery = (recovery.STATE_PATH, recovery.ROOT_PRE, recovery.run)
        recovery.STATE_PATH = config.OTA_STATE_PATH
        recovery.ROOT_PRE = self.pre
        self.roots = []
        self.raised = None

        def fake_run(root):
            self.roots.append(root)
            if self.raised:
                raise self.raised

        recovery.run = fake_run
        self.resets = machine._RESETS

    def tearDown(self):
        (recovery.STATE_PATH, recovery.ROOT_PRE, recovery.run) = self._saved_recovery
        super().tearDown()

    def _set_state(self, **state):
        with open(config.OTA_STATE_PATH, "w") as handle:
            json.dump(state, handle)

    def _keep_pre(self):
        ensure_dir(self.pre + "/app")
        self._write(OLD_APP, self.pre + "/app")

    def test_boot_picks_the_app(self):
        # A device that never updated: /app runs, and no state is written.
        recovery.boot()
        self.assertEqual(self.roots, [recovery.ROOT_NEW])
        self.assertEqual(recovery.read_state(), {})
        # A trial boot tries the update, and says so before it does: that is
        # what makes a crash Python cannot catch visible on the next boot.
        self._set_state(status="trial", current="v2", previous="v1")
        self._keep_pre()
        recovery.boot()
        self.assertEqual(self.roots[-1], recovery.ROOT_NEW)
        self.assertEqual(recovery.read_state()["status"], "booting")
        # Still "booting" on the next boot means the update never came up.
        recovery.boot()
        self.assertEqual(self.roots[-1], recovery.ROOT_PRE)
        self.assertEqual(recovery.read_state()["status"], "bad")
        # "bad" is sticky: the kept app runs until an update replaces it.
        recovery.boot()
        self.assertEqual(self.roots[-1], recovery.ROOT_PRE)
        self.assertEqual(recovery.read_state()["status"], "bad")

    def test_boot_without_a_fallback(self):
        """Nothing kept, nothing to fall back to: /app runs rather than looping."""
        self._set_state(status="bad", current="v2", previous="v1")
        recovery.boot()
        self.assertEqual(self.roots, [recovery.ROOT_NEW])
        self.assertEqual(recovery.read_state()["status"], "bad")

    def test_a_failing_update_reboots_into_the_fallback(self):
        """An update that raises is marked bad and answered with a reboot."""
        self._set_state(status="trial", current="v2", previous="v1")
        self._keep_pre()
        self.raised = ImportError("no module named app.nosuch")
        recovery.boot()
        self.assertEqual(self.roots, [recovery.ROOT_NEW])
        self.assertEqual(recovery.read_state()["status"], "bad")
        self.assertEqual(machine._RESETS, self.resets + 1)


class TestUpload(ServerHarness, OtaCase):
    """POST /ota over a real socket: the bundle is the raw body."""

    # Neither base calls the other, so both are named here: the throwaway flash
    # has to exist before the server is built, and has to outlive it.
    def setUp(self):
        OtaCase.setUp(self)
        ServerHarness.setUp(self)

    def tearDown(self):
        ServerHarness.tearDown(self)
        OtaCase.tearDown(self)

    def _build_server(self):
        config.OTA_REBOOT_DELAY_SEC = 3600  # the tests end long before a reboot
        wifi = WiFiManager(ssid="test", password="test123")
        server = HTTPServer(wifi, port=self.port)
        server.register_route("GET", "/ota", ota.route_status)
        server.register_route("POST", "/ota/rollback", ota.route_rollback)
        server.register_stream_route("POST", "/ota", ota.route_upload)
        return server

    def test_upload_install_and_refusals(self):
        self._start()
        bundle = build_bundle(NEW_APP)
        # A bundle the size limit does not cover is refused before anything is
        # read, so the app on the device is untouched.
        limit = config.OTA_MAX_BUNDLE_BYTES
        config.OTA_MAX_BUNDLE_BYTES = len(bundle) - 1
        try:
            status, body = self._post_bundle(bundle)
            self.assertEqual(status, 413)
            self.assertIn("at most", json.loads(body)["error"])
            # No Content-Length at all: the device cannot know where it ends.
            status, body = self._request("POST", "/ota")
            self.assertEqual(status, 411)
            self.assertIn("error", json.loads(body))
        finally:
            config.OTA_MAX_BUNDLE_BYTES = limit
        self.assertEqual(self._read(self.app + "/main.py"), b"old = True\n")
        # The same bundle inside the limit installs and answers with its state.
        status, body = self._post_bundle(bundle)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["status"], data["version"]), ("trial", "v2"))
        self.assertEqual(self._read(self.app + "/main.py"), b"new = True\n")
        self.assertEqual(self._read(self.pre + "/app/main.py"), b"old = True\n")
        # GET /ota reports it, and a rollback needs a reboot it does not take.
        status, body = self._get("/ota")
        self.assertEqual(json.loads(body)["running"], self.app)
        status, data = self._post_json("/ota/rollback", {})
        self.assertEqual((status, data["running"], data["status"]), (200, self.pre, "bad"))
        # A bundle that is not a bundle is refused with the reason in it.
        status, body = self._post_bundle(b"not a tar at all" * 40)
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def _post_bundle(self, bundle):
        status, _head, body = raw_request(self.port, "POST", "/ota", body=bundle)
        return status, body.decode()


if __name__ == "__main__":
    unittest.main(globals())

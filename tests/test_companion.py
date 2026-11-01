import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import AppError, Companion, StashError, WebServer, clean_folders, folder_fingerprint


class FolderTests(unittest.TestCase):
    def test_path_boundaries_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            home.mkdir()
            allowed = home / "movies"
            allowed.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (home / "escape").symlink_to(outside, target_is_directory=True)
            app = Companion(home=home)
            self.assertEqual(str(allowed), app._valid_new_path(str(allowed)))
            with self.assertRaises(AppError):
                app._valid_new_path(str(home / "escape"))
            with self.assertRaises(AppError):
                app.browse(str(outside))
            names = [entry["name"] for entry in app.browse(str(home))["directories"]]
            self.assertIn("movies", names)
            self.assertNotIn("escape", names)

    def test_clean_input_and_fingerprint(self):
        row = {"path": "/media/a", "excludeVideo": False, "excludeImage": True}
        self.assertEqual(clean_folders([row]), [row])
        self.assertNotEqual(folder_fingerprint([row]), folder_fingerprint([]))
        with self.assertRaises(AppError):
            clean_folders([row, row])
        with self.assertRaises(AppError):
            clean_folders([{"path": "relative"}])
        with self.assertRaises(AppError):
            clean_folders([{"path": "/media/a", "excludeVideo": "false"}])
        # Preserve existing Stash configurations even if both types are excluded.
        excluded = {"path": "/media/old", "excludeVideo": True, "excludeImage": True}
        self.assertEqual(clean_folders([excluded]), [excluded])

    def test_preserve_temporarily_unavailable_existing_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Companion(home=tmp, allow_non_termux=True)
            existing = [{"path": "/unmounted/drive", "excludeVideo": False, "excludeImage": True}]
            state = {"state": "running", "system": "OK"}
            with mock.patch.object(app, "server_state", return_value=state), \
                 mock.patch.object(app, "configured_folders", return_value=existing), \
                 mock.patch.object(app, "gql", return_value={"configureGeneral": {}}) as gql:
                app.save_folders(existing, folder_fingerprint(existing))
                self.assertEqual(gql.call_args.args[1]["input"]["stashes"], existing)

    def test_partial_success_is_reported_if_scan_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home / "media"
            folder.mkdir()
            app = Companion(home=home, allow_non_termux=True)
            row = {"path": str(folder), "excludeVideo": False, "excludeImage": False}
            state = {"state": "running", "system": "SETUP"}
            with mock.patch.object(app, "server_state", return_value=state), \
                 mock.patch.object(app, "gql", return_value={"setup": True}), \
                 mock.patch.object(app, "_queue_scan", side_effect=StashError("scan failed", 502)):
                result = app.save_folders([row], folder_fingerprint([]), scan=True)
                self.assertTrue(result["saved"])
                self.assertEqual(result["scan_error"], "scan failed")
                self.assertTrue((folder / "Unsorted").is_dir())
                self.assertTrue((folder / "Sorted").is_dir())
                self.assertTrue(result["workflow_pairs"][0]["ready"])

    def test_new_library_gets_pair_without_changing_old_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old = root / 'old'; new = root / 'new'; image = root / 'images'
            old.mkdir(); new.mkdir(); image.mkdir()
            (old / 'existing.mp4').write_bytes(b'old content')
            app = Companion(home=root, allow_non_termux=True)
            existing = [{'path': str(old), 'excludeVideo': False, 'excludeImage': False}]
            wanted = existing + [{'path': str(new), 'excludeVideo': False, 'excludeImage': False},
                                 {'path': str(image), 'excludeVideo': True, 'excludeImage': False}]
            with mock.patch.object(app, 'server_state', return_value={'state': 'running', 'system': 'OK'}), \
                 mock.patch.object(app, 'configured_folders', return_value=existing), \
                 mock.patch.object(app, 'gql', return_value={'configureGeneral': {}}):
                result = app.save_folders(wanted, folder_fingerprint(existing))
            self.assertTrue(all(p['ready'] for p in result['workflow_pairs']))
            for folder in (old, new):
                self.assertTrue((folder / 'Unsorted').is_dir())
                self.assertTrue((folder / 'Sorted').is_dir())
            self.assertFalse((image / 'Unsorted').exists())
            self.assertEqual((old / 'existing.mp4').read_bytes(), b'old content')


class ScanAndHashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Companion(home=self.temp.name, allow_non_termux=True)
        self.state = {"state": "running", "system": "OK"}
        self.video = {"path": "/videos", "excludeVideo": False, "excludeImage": True}
        self.image = {"path": "/photos", "excludeVideo": True, "excludeImage": False}

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_explicitly_enables_video_phashes_by_default_and_can_disable(self):
        with mock.patch.object(self.app, "server_state", return_value=self.state), \
             mock.patch.object(self.app, "configured_folders", return_value=[self.video]), \
             mock.patch.object(self.app, "gql", return_value={"metadataScan": "42"}) as gql:
            self.assertEqual(self.app.scan(), {"job_id": "42"})
            self.assertIn("scanGeneratePhashes: $phashes", gql.call_args.args[0])
            self.assertEqual(gql.call_args.args[1], {"phashes": True})
            self.app.scan(False)
            self.assertEqual(gql.call_args.args[1], {"phashes": False})
            with self.assertRaises(AppError):
                self.app.scan("false")

    def test_existing_hashes_are_generated_only_for_video_libraries_without_overwrite(self):
        with mock.patch.object(self.app, "server_state", return_value=self.state), \
             mock.patch.object(self.app, "configured_folders", return_value=[self.video, self.image]), \
             mock.patch.object(self.app, "gql", return_value={"metadataGenerate": "17"}) as gql:
            self.assertEqual(self.app.generate_missing_phashes(), {"job_id": "17"})
            self.assertIn("phashes: true, overwrite: false", gql.call_args.args[0])
            self.assertEqual(gql.call_args.args[1], {"paths": ["/videos"]})

    def test_hash_actions_refuse_invalid_or_busy_state(self):
        with self.assertRaises(AppError):
            self.app._queue_scan(None)
        with mock.patch.object(self.app.workflow, "is_running", return_value=True):
            with self.assertRaises(AppError):
                self.app.scan()
            with self.assertRaises(AppError):
                self.app.generate_missing_phashes()
        with mock.patch.object(self.app, "server_state", return_value=self.state), \
             mock.patch.object(self.app, "configured_folders", return_value=[self.image]):
            with self.assertRaises(AppError):
                self.app.generate_missing_phashes()

    def test_save_and_scan_forwards_the_users_hash_option(self):
        with mock.patch.object(self.app, "server_state", return_value=self.state), \
             mock.patch.object(self.app, "configured_folders", return_value=[self.video]), \
             mock.patch.object(self.app, "gql", return_value={"configureGeneral": {}}), \
             mock.patch.object(self.app, "_queue_scan", return_value="8") as scan:
            result = self.app.save_folders([self.video], folder_fingerprint([self.video]), True, False)
            self.assertEqual(result["job_id"], "8")
            scan.assert_called_once_with(False)


class DemoTests(unittest.TestCase):
    def test_demo_workflow_without_real_processes(self):
        app = Companion(demo=True)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("demo must not run a process")):
            app.install_packages()
            deadline = time.monotonic() + 8
            while app.install["state"] == "running" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(app.install["state"], "complete")
            self.assertEqual(app.server_state()["system"], "SETUP")
            view = app.browse("/storage/emulated/0/Movies")
            self.assertIn("Clips", [x["name"] for x in view["directories"]])
            folder = {"path": view["current"], "excludeVideo": False, "excludeImage": False}
            result = app.save_folders([folder], folder_fingerprint([]), scan=True)
            self.assertEqual(result["job_id"], "demo")
            self.assertEqual(app.status()["folders"], [folder])
            with self.assertRaises(AppError) as err:
                app.save_folders([folder], folder_fingerprint([]))
            self.assertEqual(err.exception.status, 409)
            app.stop_server()
            self.assertEqual(app.server_state()["state"], "stopped")

    def test_invalid_demo_folder_rejected(self):
        app = Companion(demo=True)
        with self.assertRaises(AppError):
            app.browse("/etc")


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Companion(home=self.temp.name, stash_port=19989)
        self.server = WebServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body or {}).encode() if method == "POST" else None
        all_headers = {"Host": "127.0.0.1:%s" % self.port}
        all_headers.update(headers or {})
        connection.request(method, path, body=data, headers=all_headers)
        response = connection.getresponse()
        result = (response.status, json.loads(response.read()))
        connection.close()
        return result

    def test_host_rebinding_and_csrf_checks(self):
        result, _ = self.request("GET", "/api/status", headers={"Host": "evil.example:%s" % self.port})
        self.assertEqual(result, 403)
        result, _ = self.request("POST", "/api/key", {"key": "abc"}, {"Content-Type": "application/json"})
        self.assertEqual(result, 403)
        headers = {"Content-Type": "application/json", "X-Pocket-Token": self.app.csrf,
                   "Origin": "https://evil.example"}
        result, _ = self.request("POST", "/api/key", {"key": "abc"}, headers)
        self.assertEqual(result, 403)
        headers["Origin"] = "http://127.0.0.1:%s" % self.port
        result, _ = self.request("POST", "/api/key", {"key": "abc"}, headers)
        self.assertEqual(result, 200)
        settings = Path(self.temp.name) / ".pocket-stash" / "settings.json"
        self.assertEqual(os.stat(settings).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(settings.read_text())["api_key"], "abc")
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertNotIn("abc", json.dumps(payload))

    def test_health_is_fast_local_and_contains_no_private_data(self):
        with mock.patch.object(self.app, "status", side_effect=AssertionError("health must not contact Stash")):
            code, health = self.request("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(health["name"], "Pocket Stash")
        self.assertFalse(health["demo"])
        self.assertEqual(set(health), {"name", "version", "demo"})
        code, _ = self.request("GET", "/api/health", headers={"Host": "evil.example:%s" % self.port})
        self.assertEqual(code, 403)

    def test_stop_requires_token_and_does_not_interrupt_install(self):
        headers = {"Content-Type": "application/json", "X-Pocket-Token": self.app.csrf}
        code, _ = self.request("POST", "/api/companion/stop", {}, {"Content-Type": "application/json"})
        self.assertEqual(code, 403)
        self.app.install["state"] = "running"
        code, _ = self.request("POST", "/api/companion/stop", {}, headers)
        self.assertEqual(code, 409)
        self.assertFalse(self.app.shutting_down)
        self.app.install["state"] = "idle"
        stopped = threading.Event()
        with mock.patch.object(self.server, "shutdown", side_effect=stopped.set):
            code, result = self.request("POST", "/api/companion/stop", {}, headers)
            self.assertEqual(code, 200)
            self.assertTrue(result["stopping"])
            self.assertTrue(stopped.wait(2))
        self.assertTrue(self.app.shutting_down)
        with mock.patch.object(self.app, "termux", True), mock.patch("shutil.which", return_value="/bin/apt"):
            with self.assertRaises(AppError):
                self.app.install_packages()

    def test_header_control_characters_are_rejected(self):
        with self.assertRaises(AppError):
            self.app.set_key("bad\nheader")
        with self.assertRaises(AppError):
            self.app.set_key("non-ascii-☃")

    def test_non_termux_install_is_refused(self):
        if not self.app.termux:
            with self.assertRaises(AppError):
                self.app.install_packages()


if __name__ == "__main__":
    unittest.main()

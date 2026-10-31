"""Exercise the APK's Termux bootstrap with fake Termux tools, not an Android device."""
import base64
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT / "android/app/src/main/assets/bootstrap.sh"
ALLOWED = {
    "server.py": b"# fake server\n",
    "performer_workflow.py": b"# fake workflow\n",
    "web/index.html": b"<title>test</title>\n",
    "web/style.css": b"body {}\n",
    "web/app.js": b"'use strict';\n",
    "web/favicon.svg": b"<svg/>\n",
}


def make_archive(contents):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    return output.getvalue()


class AndroidBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.prefix = self.root / "fake-termux"
        binary = self.prefix / "bin"
        binary.mkdir(parents=True)
        # These stubs allow the shell bootstrap and embedded Python to run, but
        # intercept the final exec instead of starting a real server.
        for name in ("pkg", "apt"):
            item = binary / name
            item.write_text("#!/bin/sh\necho 'unexpected package command' >&2\nexit 99\n")
            item.chmod(0o755)
        python = binary / "python3"
        python.write_text("""#!/bin/sh
case "$1" in
  */server.py) printf '%s' "$1" > "$TEST_SERVER_PATH"; exit 0 ;;
  *) exec "$TEST_SYSTEM_PYTHON" "$@" ;;
esac
""")
        python.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(HOME=str(self.home), PREFIX=str(self.prefix), TEST_PREFIX=str(self.prefix),
                        TEST_SYSTEM_PYTHON=sys.executable, TEST_SERVER_PATH=str(self.root / "server-path"),
                        TERMUX_APP_PACKAGE_MANAGER="apt", PATH=str(binary) + os.pathsep + os.environ["PATH"])

    def tearDown(self):
        self.tmp.cleanup()

    def run_bootstrap(self, payload, digest=None, **env):
        script = TEMPLATE.read_text()
        # Production accepts only Termux's /data/data prefix. Substitute a test
        # directory *in the test copy*, never in the shipped template.
        guard = '[[ "${PREFIX:-}" != /data/data/com.termux/* ]]'
        self.assertEqual(script.count(guard), 1)
        script = script.replace(guard, '[[ "${PREFIX:-}" != "$TEST_PREFIX" ]]')
        digest = digest or hashlib.sha256(payload).hexdigest()
        script = script.replace("__POCKET_SHA256__", digest)
        script = script.replace("__POCKET_B64__", base64.b64encode(payload).decode("ascii"))
        return subprocess.run(["/bin/bash", "-s"], input=script, text=True, capture_output=True,
                              env={**self.env, **env}, timeout=20), digest

    def test_installs_exact_files_without_touching_manual_install_and_is_repeatable(self):
        manual = self.home / "pocket-stash"
        manual.mkdir()
        (manual / "server.py").write_text("my existing install")
        payload = make_archive(ALLOWED)
        result, digest = self.run_bootstrap(payload)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target = self.home / ".pocket-stash-android" / "versions" / digest
        self.assertEqual({p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}, set(ALLOWED))
        for name, data in ALLOWED.items():
            self.assertEqual((target / name).read_bytes(), data)
        self.assertEqual((manual / "server.py").read_text(), "my existing install")
        self.assertEqual((self.root / "server-path").read_text(), str(target / "server.py"))
        self.assertFalse((self.home / ".pocket-stash-android/start.lock").exists())
        second, _ = self.run_bootstrap(payload)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_rejects_bad_digest_and_unexpected_archive_paths(self):
        result, _ = self.run_bootstrap(make_archive(ALLOWED), digest="0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stderr.lower())
        attack = dict(ALLOWED)
        attack["../escape"] = b"no"
        result, _ = self.run_bootstrap(make_archive(attack))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected file list", result.stderr.lower())
        self.assertFalse((self.home / "escape").exists())

    def test_pacman_is_rejected_before_startup(self):
        result, _ = self.run_bootstrap(make_archive(ALLOWED), TERMUX_APP_PACKAGE_MANAGER="pacman")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("APT-based", result.stderr)
        self.assertFalse((self.root / "server-path").exists())

    def test_missing_python_is_installed_without_consuming_the_payload_stdin(self):
        binary = self.prefix / "bin"
        saved_wrapper = self.root / "saved-python-wrapper"
        saved_wrapper.write_bytes((binary / "python3").read_bytes())
        (binary / "python3").unlink()
        for name in ("mkdir", "rm", "cat", "sleep"):
            (binary / name).symlink_to("/usr/bin/" + name)
        (binary / "pkg").write_text('''#!/bin/sh
if IFS= read -r _line; then echo 'pkg must not read the rest of the script' >&2; exit 98; fi
printf '%s' "$*" > "$TEST_PKG_LOG"
/bin/cp "$TEST_PY_WRAPPER" "$PREFIX/bin/python3"
/bin/chmod +x "$PREFIX/bin/python3"
''')
        # A large, incompressible payload also exercises the stdin pipe boundary.
        contents = dict(ALLOWED)
        contents["web/style.css"] = os.urandom(80_000)
        log = self.root / "pkg-log"
        result, _ = self.run_bootstrap(make_archive(contents), PATH=str(binary),
                                       TEST_PY_WRAPPER=str(saved_wrapper), TEST_PKG_LOG=str(log))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(log.read_text(), "install -y --no-install-recommends python")
        self.assertTrue((self.root / "server-path").exists())

    def test_stale_startup_lock_is_recovered(self):
        lock = self.home / ".pocket-stash-android/start.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text("99999999")
        result, _ = self.run_bootstrap(make_archive(ALLOWED))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(lock.exists())

    def test_active_startup_lock_is_not_overwritten(self):
        lock = self.home / ".pocket-stash-android/start.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(str(os.getpid()))
        result, _ = self.run_bootstrap(make_archive(ALLOWED))
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertTrue(lock.exists())
        self.assertFalse((self.root / "server-path").exists())


if __name__ == "__main__":
    unittest.main()

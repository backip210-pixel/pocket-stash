#!/usr/bin/env python3
"""Pocket Stash: a small, local-only Termux companion for Stash.

Uses only the Python standard library. Stash itself is installed separately from
Termux User Repository (TUR). The --demo mode is a sandboxed UI demonstration:
it never reads local files, runs a command, or connects to a real Stash server.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from performer_workflow import PerformerWorkflow, WorkflowError

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
APP_VERSION = "0.3.3"
STASH_PORT = 9999
MAX_BODY = 65536


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class StashError(AppError):
    pass


def folder_fingerprint(folders):
    canonical = json.dumps(folders, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except (ValueError, OSError):
        return False


def can_list(path: str) -> bool:
    try:
        with os.scandir(path):
            return True
    except (PermissionError, OSError):
        return False


def clean_folders(folders):
    """Normalize the fields we intentionally support; reject malformed input."""
    if not isinstance(folders, list) or len(folders) > 100:
        raise AppError("Select up to 100 folders.")
    result = []
    seen = set()
    for entry in folders:
        if not isinstance(entry, dict):
            raise AppError("Invalid folder selection.")
        path = entry.get("path")
        ev = entry.get("excludeVideo", False)
        ei = entry.get("excludeImage", False)
        if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path or not os.path.isabs(path):
            raise AppError("Each folder must have an absolute, valid path.")
        if not isinstance(ev, bool) or not isinstance(ei, bool):
            raise AppError("Invalid media type selection.")
        # Existing Stash installations can contain an excluded-both entry;
        # preserve it instead of refusing to read the user's configuration.
        if path in seen:
            raise AppError("The same folder was selected more than once.")
        seen.add(path)
        result.append({"path": path, "excludeVideo": ev, "excludeImage": ei})
    return result


class Companion:
    def __init__(self, *, demo=False, home=None, stash_port=STASH_PORT, allow_non_termux=False):
        self.demo = demo
        self.home = Path(home or os.environ.get("HOME", str(Path.home()))).resolve()
        self.private = self.home / ".pocket-stash"
        self.stash_port = stash_port
        self.allow_non_termux = allow_non_termux
        self.csrf = secrets.token_urlsafe(32)
        prefix = os.environ.get("PREFIX", "")
        self.termux = prefix.startswith("/data/data/com.termux/") and bool(shutil.which("pkg"))
        self.lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.process = None
        self.process_log = None
        self.start_time = 0.0
        self.last_start_error = ""
        self.last_log_trim = 0.0
        self.install = {"state": "idle", "step": "", "log": []}
        self.shutting_down = False
        self.key = ""
        self.demo_tools = False
        self.demo_running = False
        self.demo_configured = False
        self.demo_folders = []
        self.demo_extra_dirs = set()
        self.demo_job_end = 0.0
        self.demo_job_description = "Scanning sample library"
        if not demo:
            self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.private, 0o700)
            except OSError:
                pass
            self._load_settings()
        self.workflow = PerformerWorkflow(self)

    def _load_settings(self):
        try:
            with (self.private / "settings.json").open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            key = data.get("api_key", "")
            if isinstance(key, str) and len(key) <= 1024 and key.isascii() and not any(ord(c) < 32 or ord(c) == 127 for c in key):
                self.key = key
        except (OSError, ValueError, TypeError):
            pass

    def set_key(self, key):
        if not isinstance(key, str) or len(key) > 1024 or not key.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise AppError("Enter a valid ASCII API key, without control characters.")
        if self.demo:
            self.key = key.strip()
            return
        key = key.strip()
        tmp = self.private / "settings.json.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"api_key": key}, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.private / "settings.json")
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.key = key

    def tools(self):
        if self.demo:
            return {"stash": self.demo_tools, "ffmpeg": self.demo_tools, "ffprobe": self.demo_tools}
        return {name: bool(shutil.which(name)) for name in ("stash", "ffmpeg", "ffprobe")}

    def storage(self):
        if self.demo:
            return {"available": True, "path": "/storage/emulated/0", "message": "Sample phone storage"}
        shared = self.home / "storage" / "shared"
        available = shared.is_dir() and can_list(str(shared))
        return {
            "available": available,
            "path": str(shared),
            "message": "Shared storage is readable" if available else "Shared storage is not accessible yet",
        }

    def roots(self):
        if self.demo:
            return [
                {"label": "Phone storage", "path": "/storage/emulated/0", "available": True},
                {"label": "Termux home", "path": "/data/data/com.termux/files/home", "available": True},
            ]
        result = []
        shared = self.home / "storage" / "shared"
        if shared.is_dir() and can_list(str(shared)):
            result.append({"label": "Phone storage", "path": os.path.realpath(shared), "available": True})
        # Android may expose a removable storage root here, subject to its own permissions.
        # Never assume that a Storage Access Framework grant maps to a Unix path.
        if self.termux:
            try:
                for item in sorted(os.listdir("/storage")):
                    if item in ("emulated", "self") or item.startswith("."):
                        continue
                    candidate = os.path.realpath(os.path.join("/storage", item))
                    if os.path.isdir(candidate) and can_list(candidate) and not any(r["path"] == candidate for r in result):
                        result.append({"label": "Removable storage · " + item, "path": candidate, "available": True})
            except OSError:
                pass
        if can_list(str(self.home)):
            result.append({"label": "Termux home", "path": str(self.home), "available": True})
        return result

    def browse(self, path=None):
        roots = self.roots()
        if not roots:
            raise AppError("No readable folders are available. Check Termux storage permissions.")
        if self.demo:
            tree = {
                "/storage/emulated/0": ["DCIM", "Download", "Movies", "Music", "Pictures"],
                "/storage/emulated/0/DCIM": ["Camera", "Screenshots"],
                "/storage/emulated/0/DCIM/Camera": [],
                "/storage/emulated/0/DCIM/Screenshots": [],
                "/storage/emulated/0/Download": [],
                "/storage/emulated/0/Movies": ["Clips", "Favorites"],
                "/storage/emulated/0/Movies/Clips": [],
                "/storage/emulated/0/Movies/Favorites": [],
                "/storage/emulated/0/Music": [],
                "/storage/emulated/0/Pictures": ["Collections", "Screenshots"],
                "/storage/emulated/0/Pictures/Collections": [],
                "/storage/emulated/0/Pictures/Screenshots": [],
                "/data/data/com.termux/files/home": ["media"],
                "/data/data/com.termux/files/home/media": [],
            }
            for extra in sorted(self.demo_extra_dirs):
                tree.setdefault(extra, [])
                parent = os.path.dirname(extra)
                if parent in tree and os.path.basename(extra) not in tree[parent]:
                    tree[parent].append(os.path.basename(extra))
            current = path or roots[0]["path"]
            if current not in tree:
                raise AppError("That demo folder doesn't exist.")
            root = next((r["path"] for r in roots if within(current, r["path"])), None)
            parent = None if current == root else os.path.dirname(current)
            return {"roots": roots, "current": current, "parent": parent,
                    "directories": [{"name": x, "path": current.rstrip("/") + "/" + x} for x in tree[current]]}
        current = os.path.realpath(path or roots[0]["path"])
        if not os.path.isabs(current) or not any(within(current, r["path"]) for r in roots):
            raise AppError("Choose a folder inside an accessible storage location.", 403)
        # The most specific root gives the most natural back-button behaviour.
        matching = [r["path"] for r in roots if within(current, r["path"])]
        root = max(matching, key=len)
        if not os.path.isdir(current):
            raise AppError("That folder no longer exists.", 404)
        entries = []
        try:
            with os.scandir(current) as scan:
                for entry in scan:
                    if entry.name.startswith("."):
                        continue
                    target = os.path.realpath(entry.path)
                    try:
                        if entry.is_dir(follow_symlinks=True) and any(within(target, r["path"]) for r in roots):
                            entries.append({"name": entry.name, "path": target})
                    except OSError:
                        continue
                    if len(entries) >= 2000:
                        break
        except PermissionError:
            raise AppError("Termux can't read this folder. Check Android storage permissions.", 403)
        except OSError as exc:
            raise AppError("Couldn't read that folder: " + str(exc))
        entries.sort(key=lambda x: x["name"].casefold())
        parent = None if current == root else os.path.dirname(current)
        return {"roots": roots, "current": current, "parent": parent, "directories": entries}

    def _valid_new_path(self, path):
        if self.demo:
            self.browse(path)
            return path
        resolved = os.path.realpath(path)
        if not any(within(resolved, root["path"]) for root in self.roots()):
            raise AppError("A selected folder is outside the accessible storage locations.", 403)
        if not os.path.isdir(resolved) or not can_list(resolved):
            raise AppError("Termux cannot read the selected folder: " + path)
        return resolved

    def gql(self, query, variables=None, timeout=2.0):
        body = json.dumps({"query": query, "variables": variables or {}}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.key:
            headers["ApiKey"] = self.key
        conn = http.client.HTTPConnection("127.0.0.1", self.stash_port, timeout=timeout)
        try:
            conn.request("POST", "/graphql", body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read(2_000_000)
        except (OSError, http.client.HTTPException) as exc:
            raise StashError("Can't reach Stash on this device: " + str(exc), 503)
        finally:
            conn.close()
        if response.status == 403 and any(term in payload.lower() for term in (b"tripwire", b"stash is exposed", b"over the internet")):
            raise StashError("Stash's public-access security tripwire is active. Check Stash's logs and documentation.", 403)
        if response.status in (401, 403):
            raise StashError("Stash requires an API key. Add it under Connection settings.", 401)
        if response.status != 200:
            raise StashError("Stash returned HTTP " + str(response.status) + ". Check its logs or connection settings.", 502)
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise StashError("The service on Stash's port did not return a valid Stash response.", 502)
        if not isinstance(result, dict):
            raise StashError("The service on Stash's port did not return a valid Stash response.", 502)
        if result.get("errors"):
            messages = [str(x.get("message", "GraphQL error")) for x in result["errors"] if isinstance(x, dict)]
            message = "; ".join(messages)[:800] or "Stash returned an error."
            if any(word in message.lower() for word in ("unauthorized", "unauthorised", "not authenticated", "access denied")):
                raise StashError("Stash requires an API key. Add it under Connection settings.", 401)
            raise StashError(message, 502)
        data = result.get("data")
        if not isinstance(data, dict):
            raise StashError("Stash returned no data.", 502)
        return data

    def system_status(self):
        data = self.gql("query PocketStatus { systemStatus { status } }")
        status = data.get("systemStatus", {}).get("status")
        if status not in ("SETUP", "OK", "NEEDS_MIGRATION"):
            raise StashError("The service on this port isn't a recognized Stash instance.", 502)
        return status

    def configured_folders(self):
        data = self.gql("query PocketFolders { configuration { general { stashes { path excludeVideo excludeImage } } } }")
        try:
            return clean_folders(data["configuration"]["general"]["stashes"])
        except (KeyError, TypeError, AppError):
            raise StashError("Couldn't read Stash's folder configuration.", 502)

    def jobs(self):
        try:
            data = self.gql("query PocketJobs { jobQueue { id status description progress error } }", timeout=1.0)
            return data.get("jobQueue") or []
        except StashError:
            return []

    def _port_open(self):
        try:
            with socket.create_connection(("127.0.0.1", self.stash_port), timeout=0.2):
                return True
        except OSError:
            return False

    def _managed_alive(self):
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def _log_tail(self):
        try:
            with (self.private / "stash.log").open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 12000), os.SEEK_SET)
                text = fh.read().decode("utf-8", "replace")
                return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).splitlines()[-60:]
        except OSError:
            return []

    def _trim_log(self):
        # Stash may log each status poll. Keep recent output without allowing
        # a long-running phone to fill its private storage with access logs.
        with self.lock:
            if time.monotonic() - self.last_log_trim < 30:
                return
            self.last_log_trim = time.monotonic()
            try:
                log = self.private / "stash.log"
                if log.stat().st_size > 2_000_000:
                    with log.open("r+b") as fh:
                        fh.seek(-500_000, os.SEEK_END)
                        tail = fh.read()
                        fh.seek(0)
                        fh.write(b"[Earlier output trimmed by Pocket Stash.]\n" + tail)
                        fh.truncate()
            except OSError:
                pass

    def server_state(self):
        if self.demo:
            return {"state": "running" if self.demo_running else "stopped", "managed": self.demo_running,
                    "system": ("OK" if self.demo_configured else "SETUP") if self.demo_running else None,
                    "message": "Demo Stash is running" if self.demo_running else "Demo Stash is stopped",
                    "port": self.stash_port, "key_configured": bool(self.key)}
        managed = self._managed_alive()
        with self.lock:
            proc = self.process
            exited = proc is not None and proc.poll() is not None
            start_time = self.start_time
        if not self._port_open():
            if managed and time.monotonic() - start_time < 30:
                state, message = "starting", "Starting Stash…"
            elif managed:
                state, message = "starting", "Stash is taking a while to start. Check its log if needed."
            elif exited:
                state, message = "error", "Stash stopped unexpectedly. Open the log for details."
            else:
                state, message = "stopped", self.last_start_error or "Stash is not running."
            return {"state": state, "managed": managed, "system": None, "message": message,
                    "port": self.stash_port, "key_configured": bool(self.key)}
        try:
            status = self.system_status()
            return {"state": "running", "managed": managed, "system": status,
                    "message": "Running on this device" if managed else "An existing Stash instance is using this port",
                    "port": self.stash_port, "key_configured": bool(self.key)}
        except StashError as exc:
            if exc.status in (401, 403):
                return {"state": "running", "managed": managed,
                        "system": "AUTH_REQUIRED" if exc.status == 401 else "SECURITY_BLOCKED", "message": str(exc),
                        "port": self.stash_port, "key_configured": bool(self.key)}
            if managed:
                message = "Starting Stash…" if time.monotonic() - start_time < 30 else "Still starting. Check the log if needed."
                return {"state": "starting", "managed": True, "system": None, "message": message,
                        "port": self.stash_port, "key_configured": bool(self.key)}
            return {"state": "conflict", "managed": managed, "system": None,
                    "message": "Port %s is in use, but it isn't responding as Stash." % self.stash_port,
                    "port": self.stash_port, "key_configured": bool(self.key)}

    def status(self):
        if not self.demo:
            self._trim_log()
        tools = self.tools()
        server = self.server_state()
        folders = None
        jobs = []
        folder_error = ""
        if server["state"] == "running":
            if server["system"] == "SETUP":
                folders = []
            elif server["system"] == "OK":
                if self.demo:
                    folders = list(self.demo_folders)
                    if self.demo_job_end > time.monotonic():
                        jobs = [{"id": "demo", "status": "RUNNING", "description": self.demo_job_description, "progress": None, "error": None}]
                else:
                    try:
                        folders = self.configured_folders()
                        jobs = self.jobs()
                    except StashError as exc:
                        folder_error = str(exc)
                        if exc.status in (401, 403):
                            server["system"] = "AUTH_REQUIRED" if exc.status == 401 else "SECURITY_BLOCKED"
                            server["message"] = str(exc)
        with self.lock:
            installation = {"state": self.install["state"], "step": self.install["step"], "log": list(self.install["log"])[-60:]}
        return {"version": APP_VERSION, "demo": self.demo, "termux": self.termux,
                "tools": tools, "storage": self.storage(), "server": server, "folders": folders,
                "folder_error": folder_error, "fingerprint": folder_fingerprint(folders) if folders is not None else None,
                "jobs": jobs, "install": installation}

    def install_packages(self):
        if not self.demo and not self.termux:
            raise AppError("Package installation is only available inside Termux.", 400)
        if not self.demo and (os.environ.get("TERMUX_APP_PACKAGE_MANAGER", "apt") == "pacman" or not shutil.which("apt")):
            raise AppError("Use an APT-based Termux build (F-Droid or the GitHub APT APK). Pacman builds are not supported by this installer.")
        with self.lock:
            if self.shutting_down:
                raise AppError("The companion is shutting down.", 409)
            if self.install["state"] == "running":
                raise AppError("Installation is already running.", 409)
            self.install = {"state": "running", "step": "Preparing", "log": []}
        threading.Thread(target=self._install_worker, daemon=True).start()

    def _install_log(self, text):
        with self.lock:
            self.install["log"].append(text.rstrip()[:2000])
            self.install["log"] = self.install["log"][-120:]

    def _install_worker(self):
        try:
            if self.demo:
                for step, line in (("Enabling repository", "Demo: enabling the Termux User Repository…"),
                                   ("Installing packages", "Demo: installing Stash and FFmpeg…"),
                                   ("Finishing", "Demo: installation complete. Starting Stash…")):
                    with self.lock:
                        self.install["step"] = step
                    self._install_log(line)
                    time.sleep(0.65)
                self.demo_tools = True
                self.demo_running = True
            else:
                commands = [
                    ("Refreshing package lists", ["pkg", "update", "-y"]),
                    ("Enabling the community repository", ["pkg", "install", "-y", "tur-repo"]),
                    ("Installing Stash and FFmpeg", ["pkg", "install", "-y", "stash", "ffmpeg"]),
                ]
                env = os.environ.copy()
                env["DEBIAN_FRONTEND"] = "noninteractive"
                for label, args in commands:
                    with self.lock:
                        self.install["step"] = label
                    self._install_log("$ " + " ".join(args))
                    child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, errors="replace", env=env, bufsize=1)
                    assert child.stdout is not None
                    for line in child.stdout:
                        self._install_log(line)
                    if child.wait() != 0:
                        raise RuntimeError(label + " failed. Check the output above, then retry.")
                if not all(self.tools().values()):
                    raise RuntimeError("The packages finished installing, but stash, ffmpeg or ffprobe was not found on PATH.")
                self._install_log("Installation complete. Starting Stash locally…")
                try:
                    self.start_server()
                except AppError as exc:
                    self._install_log("Installed, but Stash could not start: " + str(exc))
            with self.lock:
                self.install["state"] = "complete"
                self.install["step"] = "Installation complete"
        except (OSError, RuntimeError) as exc:
            self._install_log("ERROR: " + str(exc))
            with self.lock:
                self.install["state"] = "failed"
                self.install["step"] = "Installation failed"

    def start_server(self):
        if self.demo:
            if not self.demo_tools:
                raise AppError("Install the demo packages first.")
            self.demo_running = True
            return
        if not (self.termux or self.allow_non_termux):
            raise AppError("Run this companion inside Termux to start Stash.")
        with self.lock:
            if self._managed_alive():
                return
            if self._port_open():
                raise AppError("Port %s is already in use. If it's an existing Stash, open it instead." % self.stash_port, 409)
            binary = shutil.which("stash")
            if not binary:
                raise AppError("Stash isn't installed yet. Install it first.")
            config_dir = self.home / ".stash"
            config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Stash is deliberately kept on loopback, even if the user's config says otherwise.
            # Avoid inherited STASH_* environment overrides changing paths or exposure.
            env = {k: v for k, v in os.environ.items() if not k.startswith("STASH_")}
            env["HOME"] = str(self.home)
            logfile = self.private / "stash.log"
            fd = os.open(str(logfile), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            output = os.fdopen(fd, "ab", buffering=0)
            try:
                proc = subprocess.Popen([binary, "--host", "127.0.0.1", "--port", str(self.stash_port),
                                         "--config", str(config_dir / "config.yml"), "--nobrowser"],
                                        cwd=str(self.home), env=env, stdin=subprocess.DEVNULL,
                                        stdout=output, stderr=subprocess.STDOUT)
            except OSError as exc:
                output.close()
                self.last_start_error = str(exc)
                raise AppError("Couldn't start Stash: " + str(exc), 500)
            if self.process_log:
                self.process_log.close()
            self.process = proc
            self.process_log = output
            self.start_time = time.monotonic()
            self.last_start_error = ""

    def stop_server(self):
        # Coordinate the active-move check with starting a new workflow batch.
        with self.workflow.lock:
            if self.workflow.active:
                raise AppError("Wait for the performer move to finish before stopping Stash.", 409)
            if self.demo:
                self.demo_running = False
                return
            with self.lock:
                proc = self.process
                if proc is None or proc.poll() is not None:
                    raise AppError("This companion didn't start the running Stash process, so it won't stop it.", 409)
                proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            with self.lock:
                self.process = None
                if self.process_log:
                    self.process_log.close()
                    self.process_log = None

    def shutdown(self):
        if self.workflow.is_running():
            # Finish the current request if possible; never kill Stash mid-move.
            # On interruption the persisted 'moving' entry is marked uncertain.
            self.workflow.request_stop()
        if not self.demo and self._managed_alive() and not self.workflow.is_running():
            try:
                self.stop_server()
            except (AppError, OSError, subprocess.SubprocessError):
                pass

    def save_folders(self, requested, fingerprint, scan=False, generate_phashes=True):
        if not isinstance(scan, bool) or not isinstance(generate_phashes, bool):
            raise AppError("Invalid scan option.")
        requested = clean_folders(requested)
        with self.action_lock:
            if self.workflow.is_running():
                raise AppError("Wait for the current performer move to finish.", 409)
            state = self.server_state()
            if state["state"] != "running" or state["system"] not in ("SETUP", "OK"):
                raise AppError("Start Stash and resolve any connection or migration issue before saving folders.", 409)
            current = [] if state["system"] == "SETUP" else (list(self.demo_folders) if self.demo else self.configured_folders())
            if not isinstance(fingerprint, str) or not hmac.compare_digest(fingerprint, folder_fingerprint(current)):
                raise AppError("Stash's folders changed since this page loaded. Reload the folders before saving.", 409)
            existing = {x["path"] for x in current}
            for folder in requested:
                # Preserve previously configured, temporarily unavailable locations.
                if folder["path"] not in existing:
                    if folder["excludeVideo"] and folder["excludeImage"]:
                        raise AppError("A new folder must include videos, images, or both.")
                    folder["path"] = self._valid_new_path(folder["path"])
            if len({x["path"] for x in requested}) != len(requested):
                raise AppError("Two selections point to the same folder.")
            if state["system"] == "SETUP" and not requested:
                raise AppError("Choose at least one folder to complete setup.")
            if self.demo:
                self.demo_folders = requested
                self.demo_configured = True
            elif state["system"] == "SETUP":
                setup_input = {
                    "configLocation": str(self.home / ".stash" / "config.yml"),
                    "stashes": requested, "databaseFile": "", "generatedLocation": "", "cacheLocation": "",
                    "storeBlobsInDatabase": False, "blobsLocation": "",
                }
                result = self.gql("mutation PocketSetup($input: SetupInput!) { setup(input: $input) }", {"input": setup_input}, timeout=45)
                if result.get("setup") is not True:
                    raise StashError("Stash did not finish its setup.", 502)
            else:
                self.gql("mutation PocketConfigure($input: ConfigGeneralInput!) { configureGeneral(input: $input) { stashes { path excludeVideo excludeImage } } }",
                         {"input": {"stashes": requested}}, timeout=15)
            # Prepare the two workflow folders without replacing existing files.
            # A permission failure is reported per library; it must not roll
            # back the already-saved Stash configuration.
            workflow_pairs = self.workflow._prepare_pairs_locked(requested)
            # A successful config update is not rolled back if the separate scan request fails.
            job_id, scan_error = None, None
            if scan and requested:
                try:
                    job_id = self._queue_scan(generate_phashes)
                except StashError as exc:
                    scan_error = str(exc)
            return {"saved": True, "job_id": job_id, "scan_error": scan_error, "workflow_pairs": workflow_pairs["pairs"]}

    def _queue_scan(self, generate_phashes=True):
        if not isinstance(generate_phashes, bool):
            raise AppError("Invalid perceptual-hash option.")
        if self.demo:
            self.demo_job_description = "Scanning sample library"
            self.demo_job_end = time.monotonic() + 4
            return "demo"
        # Stash's scan flag affects new/changed videos. Other generation
        # settings are left untouched; the separate generate action below
        # fills in hashes for videos that were already scanned.
        result = self.gql(
            "mutation PocketScan($phashes: Boolean!) { metadataScan(input: {scanGeneratePhashes: $phashes}) }",
            {"phashes": generate_phashes}, timeout=15)
        job = result.get("metadataScan")
        if job is None:
            raise StashError("Stash didn't return a scan job ID.", 502)
        return str(job)

    def scan(self, generate_phashes=True):
        if not isinstance(generate_phashes, bool):
            raise AppError("Invalid perceptual-hash option.")
        with self.action_lock:
            if self.workflow.is_running():
                raise AppError("Wait for the current performer move to finish.", 409)
            state = self.server_state()
            if state["state"] != "running" or state["system"] != "OK":
                raise AppError("Finish Stash setup before starting a scan.")
            folders = self.demo_folders if self.demo else self.configured_folders()
            if not folders:
                raise AppError("Add a library folder before scanning.")
            return {"job_id": self._queue_scan(generate_phashes)}

    def generate_missing_phashes(self):
        # Explicit, non-overwriting job for existing indexed videos, restricted
        # to configured video libraries. Do not silently run it after a scan.
        with self.action_lock:
            if self.workflow.is_running():
                raise AppError("Wait for the current performer move to finish.", 409)
            state = self.server_state()
            if state["state"] != "running" or state["system"] != "OK":
                raise AppError("Finish Stash setup before generating perceptual hashes.")
            folders = self.demo_folders if self.demo else self.configured_folders()
            paths = [f["path"] for f in folders if not f["excludeVideo"]]
            if not paths:
                raise AppError("Add a video library folder before generating perceptual hashes.")
            if self.demo:
                self.demo_job_description = "Generating sample perceptual hashes"
                self.demo_job_end = time.monotonic() + 4
                return {"job_id": "demo"}
            result = self.gql(
                "mutation PocketGeneratePhashes($paths: [String!]) { "
                "metadataGenerate(input: {paths: $paths, phashes: true, overwrite: false}) }",
                {"paths": paths}, timeout=15)
            job = result.get("metadataGenerate")
            if job is None:
                raise StashError("Stash didn't return a perceptual-hash job ID.", 502)
            return {"job_id": str(job)}


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        super().__init__(address, Handler)
        self.app = app


class Handler(BaseHTTPRequestHandler):
    server_version = "PocketStash"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Avoid writing folder names or API request URLs to Termux's terminal.
        return

    def _headers(self, content_type, size, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._headers("application/json; charset=utf-8", len(data), status)
        self.wfile.write(data)

    def _check_host(self):
        if self.server.app.demo:
            return
        host = self.headers.get("Host", "").lower()
        port = self.server.server_address[1]
        if host not in {"127.0.0.1:%d" % port, "localhost:%d" % port}:
            raise AppError("Use the local address shown in Termux.", 403)

    def _check_post(self):
        self._check_host()
        token = self.headers.get("X-Pocket-Token", "")
        if not hmac.compare_digest(token, self.server.app.csrf):
            raise AppError("This page has expired. Reload it and try again.", 403)
        origin = self.headers.get("Origin")
        if origin and not self.server.app.demo:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != self.headers.get("Host", "").lower():
                raise AppError("Cross-origin requests are not allowed.", 403)
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise AppError("Expected a JSON request.", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise AppError("Invalid request length.")
        if length < 0 or length > MAX_BODY:
            raise AppError("Request is too large.", 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise AppError("Invalid JSON request.")
        if not isinstance(body, dict):
            raise AppError("Expected a JSON object.")
        return body

    def do_GET(self):
        try:
            self._check_host()
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path
            if route == "/api/health":
                # Fast, non-sensitive identity check for the Android launcher.
                # This deliberately does not contact Stash or trigger package work.
                return self._json({"name": "Pocket Stash", "version": APP_VERSION,
                                   "demo": self.server.app.demo})
            if route == "/api/status":
                return self._json(self.server.app.status())
            if route == "/api/workflow":
                return self._json(self.server.app.workflow.status())
            if route == "/api/workflow/item":
                token = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                return self._json(self.server.app.workflow.resolve_item(token))
            if route == "/api/workflow/scan-list":
                return self._json(self.server.app.workflow.scan_paths())
            if route == "/api/browse":
                query = urllib.parse.parse_qs(parsed.query)
                path = query.get("path", [None])[0]
                return self._json(self.server.app.browse(path))
            if route == "/api/logs":
                app = self.server.app
                logs = ["Demo mode: no real Stash process has been started."] if app.demo else app._log_tail()
                return self._json({"stash": logs, "installation": app.status()["install"]["log"]})
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if route not in static:
                raise AppError("Not found.", 404)
            filename, mime = static[route]
            data = (WEB_DIR / filename).read_bytes()
            if route == "/":
                data = data.replace(b"__POCKET_TOKEN__", self.server.app.csrf.encode("ascii"))
            self._headers(mime, len(data))
            self.wfile.write(data)
        except (AppError, WorkflowError) as exc:
            self._json({"error": str(exc)}, exc.status)
        except (OSError, ValueError) as exc:
            self._json({"error": "Couldn't read the requested resource."}, 500)

    def do_POST(self):
        try:
            body = self._check_post()
            path = urllib.parse.urlsplit(self.path).path
            app = self.server.app
            stop_companion = False
            if path == "/api/install":
                app.install_packages()
                result = {"started": True}
            elif path == "/api/server/start":
                app.start_server()
                result = {"started": True}
            elif path == "/api/server/stop":
                app.stop_server()
                result = {"stopped": True}
            elif path == "/api/folders":
                result = app.save_folders(body.get("folders"), body.get("fingerprint"),
                                          body.get("scan", False), body.get("generate_phashes", True))
            elif path == "/api/scan":
                result = app.scan(body.get("generate_phashes", True))
            elif path == "/api/phashes/generate":
                result = app.generate_missing_phashes()
            elif path == "/api/workflow/prepare-folders":
                result = app.workflow.prepare_pairs()
            elif path == "/api/workflow/create-folder":
                result = app.workflow.create_folder(body.get("parent"), body.get("name"))
            elif path == "/api/workflow/preview":
                result = app.workflow.preview(body.get("source"), body.get("destination"), body.get("choices", {}), body.get("excluded", []))
            elif path == "/api/workflow/apply":
                result = app.workflow.start(body.get("plan_id"), body.get("enable_rename"), body.get("acknowledged"))
            elif path == "/api/workflow/review":
                result = app.workflow.review(body.get("item_id"), body.get("backup"), body.get("album"))
            elif path == "/api/workflow/resolve":
                result = app.workflow.resolve_uncertain(body.get("item_id"), body.get("resolution"), body.get("inspected"))
            elif path == "/api/key":
                app.set_key(body.get("key", ""))
                result = {"saved": True}
            elif path == "/api/companion/stop" and not app.demo:
                # A package manager must not be interrupted mid-installation.
                with app.workflow.lock:
                    with app.lock:
                        if app.install["state"] == "running":
                            raise AppError("Wait until installation finishes before stopping the companion.", 409)
                        if app.workflow.active:
                            raise AppError("Wait for the performer move to finish before stopping the companion.", 409)
                        app.shutting_down = True
                result = {"stopping": True}
                stop_companion = True
            elif path == "/api/demo/reset" and app.demo:
                if app.workflow.is_running():
                    raise AppError("Wait for the demo move to finish before resetting.", 409)
                with app.lock:
                    if app.install["state"] == "running":
                        raise AppError("Wait for the demo installation to finish before resetting.", 409)
                    app.demo_tools = False
                    app.demo_running = False
                    app.demo_configured = False
                    app.demo_folders = []
                    app.demo_extra_dirs = set()
                    app.demo_job_end = 0.0
                    app.demo_job_description = "Scanning sample library"
                    app.key = ""
                    app.install = {"state": "idle", "step": "", "log": []}
                app.workflow = PerformerWorkflow(app)
                result = {"reset": True}
            else:
                raise AppError("Not found.", 404)
            if stop_companion:
                try:
                    self._json(result)
                finally:
                    # shutdown() must run outside serve_forever's thread. main()
                    # then stops only the Stash process this companion started.
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(result)
        except (AppError, WorkflowError) as exc:
            self._json({"error": str(exc)}, exc.status)
        except (OSError, ValueError) as exc:
            self._json({"error": "Operation failed: " + str(exc)[:400]}, 500)


def main():
    parser = argparse.ArgumentParser(description="Local setup companion for Stash on Termux")
    parser.add_argument("--demo", action="store_true", help="Interactive sample data only; no real system changes")
    parser.add_argument("--host", default="127.0.0.1", help="Listen address (non-loopback allowed only in demo mode)")
    parser.add_argument("--port", type=int, default=8765, help="Companion port (default: 8765)")
    parser.add_argument("--stash-port", type=int, default=STASH_PORT, help="Stash port (default: 9999)")
    parser.add_argument("--no-autostart", action="store_true", help="Do not automatically start an installed Stash")
    parser.add_argument("--allow-non-termux", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.host != "127.0.0.1" and not args.demo:
        parser.error("For safety, non-demo mode must listen on 127.0.0.1.")
    if not (1 <= args.port <= 65535 and 1 <= args.stash_port <= 65535):
        parser.error("Ports must be between 1 and 65535.")
    app = Companion(demo=args.demo, stash_port=args.stash_port, allow_non_termux=args.allow_non_termux)
    if not args.demo and not (app.termux or args.allow_non_termux):
        parser.error("Run this inside Termux, or use --demo for a safe preview.")
    try:
        server = WebServer((args.host, args.port), app)
    except OSError as exc:
        print("Could not start the companion: %s" % exc, file=sys.stderr)
        print("If it is already running, open http://127.0.0.1:%s/" % args.port, file=sys.stderr)
        return 1
    if not args.demo and not args.no_autostart and app.tools()["stash"] and not app._port_open():
        try:
            app.start_server()
        except AppError as exc:
            app.last_start_error = str(exc)
    mode = "Demo (no real changes)" if args.demo else "Local setup companion"
    print("\nPocket Stash — " + mode)
    print("Open http://127.0.0.1:%s/ on this device." % args.port)
    print("Keep Termux running. Press Ctrl+C to stop the companion and its managed Stash.\n", flush=True)

    def on_terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_terminate)
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

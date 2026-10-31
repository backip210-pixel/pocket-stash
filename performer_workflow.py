"""Review-first Stash -> Android Google Photos preparation.

Stash owns every move and its database updates.  This module never deletes files,
uses root, calls Google APIs, or claims to know Google Photos' backup state.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import unicodedata


class WorkflowError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# Google Photos' Android backup documentation lists a 10 GB video limit. Being
# conservative with decimal GB avoids staging files at an ambiguous boundary.
MAX_VIDEO_BYTES = 10_000_000_000
MAX_SCENES = 3000
MAX_PLAN_FILES = 500
MAX_HISTORY_FILES = 3000
MAX_HISTORY_BYTES = 8_000_000
SUPPORTED_EXTENSIONS = frozenset({
    ".3gp", ".3g2", ".asf", ".avi", ".divx", ".m2t", ".m2ts", ".m4v", ".mkv",
    ".mmv", ".mod", ".mov", ".mp4", ".mpg", ".mts", ".tod", ".wmv",
})
PLAN_LIFETIME = 30 * 60
ID_RE = re.compile(r"^[0-9]{1,18}$")
TOKEN_RE = re.compile(r"^[a-f0-9]{24}$")


def inside(path, root):
    try:
        resolved = os.path.realpath(path)
        base = os.path.realpath(root)
        return os.path.commonpath((resolved, base)) == base
    except (OSError, ValueError, TypeError):
        return False


def has_nomedia(path, root):
    """Whether this directory or an ancestor inside shared storage hides media."""
    current = os.path.realpath(path)
    root = os.path.realpath(root)
    while inside(current, root):
        if os.path.lexists(os.path.join(current, ".nomedia")):
            return True
        if current == root:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return False


def component(value, max_bytes=150):
    """Make one filename component, never a path; preserve Unicode safely."""
    value = unicodedata.normalize("NFC", str(value or ""))
    value = "".join(" " if c in '/\\:*?\"<>|' or unicodedata.category(c).startswith("C") else c
                    for c in value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    while value and len(value.encode("utf-8")) > max_bytes:
        value = value[:-1]
    return value.rstrip(" .")


def file_snapshot(path):
    # Reject symlink files even when their resolved target lies within the root.
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise WorkflowError("The source is not a regular file.")
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "device": info.st_dev, "inode": info.st_ino}


class PerformerWorkflow:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.active = False
        self.stop_requested = False
        self.worker = None
        self.plan = None
        self.history = {"version": 1, "runs": []}
        self.history_error = ""
        self.path = app.private / "performer-workflow.json"
        if not app.demo:
            self._load()

    def _load(self):
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > MAX_HISTORY_BYTES:
                raise ValueError("history is too large")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("runs"), list):
                raise ValueError("unknown history format")
            self.history = data
            changed = False
            for run in self.history["runs"]:
                if run.get("state") == "running":
                    run["state"] = "interrupted"
                    run["message"] = "Interrupted. Inspect source, destination and Stash before retrying."
                    for item in run.get("items", []):
                        if item.get("state") == "moving":
                            item["state"] = "uncertain"
                        elif item.get("state") == "queued":
                            item["state"] = "not_started"
                    changed = True
            if changed:
                self._save_locked()
        except (OSError, ValueError, TypeError, AttributeError):
            # Never silently overwrite an unreadable run journal.
            self.history_error = "The workflow journal could not be read. Back it up and resolve it before making changes."

    def _save_locked(self):
        if self.app.demo:
            return
        if self.history_error:
            raise WorkflowError(self.history_error, 409)
        payload = json.dumps(self.history, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > MAX_HISTORY_BYTES:
            raise WorkflowError("The workflow journal is too large; back it up before clearing history.", 409)
        temp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass  # Some filesystems do not permit syncing directories.
        except Exception:
            try:
                temp.unlink()
            except OSError:
                pass
            raise

    def is_running(self):
        with self.lock:
            return self.active

    def request_stop(self):
        with self.lock:
            self.stop_requested = True
            thread = self.worker
        if thread:
            thread.join(timeout=65)

    def _require_ready(self):
        state = self.app.server_state()
        if state.get("state") != "running" or state.get("system") != "OK":
            raise WorkflowError("Start Stash and finish its initial setup first.", 409)
        if self.history_error:
            raise WorkflowError(self.history_error, 409)

    def _folders(self):
        return list(self.app.demo_folders) if self.app.demo else self.app.configured_folders()

    def _roots(self, source, destination):
        if not isinstance(source, str) or not isinstance(destination, str):
            raise WorkflowError("Select a source and a Photos destination folder.")
        source = self.app._valid_new_path(source)
        destination = self.app._valid_new_path(destination)
        if inside(source, destination):
            raise WorkflowError("Choose a destination outside the source folder, or a separate folder within the source.")
        if not self.app.demo:
            shared = self.app.home / "storage" / "shared"
            if not shared.is_dir() or not inside(destination, str(shared)):
                raise WorkflowError("Choose a destination in Termux's shared phone storage, so Google Photos can discover it.")
            if not os.access(destination, os.W_OK):
                raise WorkflowError("Termux cannot write to the chosen destination folder.")
            if has_nomedia(destination, str(shared)):
                raise WorkflowError("A .nomedia file hides the chosen destination from Android's media apps. Choose a visible folder.")
        folders = [f["path"] for f in self._folders() if not f.get("excludeVideo")]
        if not any(inside(source, folder) for folder in folders):
            raise WorkflowError("Add the source folder (or its parent) as a video library in Pocket Stash first.")
        if not any(inside(destination, folder) for folder in folders):
            raise WorkflowError("Add the destination folder (or its parent) as a video library in Pocket Stash first. Stash only moves files inside its libraries.")
        return source, destination

    def create_folder(self, parent, name):
        self._require_ready()
        if not isinstance(parent, str) or not isinstance(name, str):
            raise WorkflowError("Choose a parent and enter a folder name.")
        clean = component(name, max_bytes=120)
        if not clean or clean != name or name.startswith("."):
            raise WorkflowError("Use a simple folder name without slashes, control characters or leading/trailing spaces or dots.")
        with self.app.action_lock:
            with self.lock:
                if self.active:
                    raise WorkflowError("Wait for the current move to finish.", 409)
            parent = self.app._valid_new_path(parent)
            shared = "/storage/emulated/0" if self.app.demo else str(self.app.home / "storage" / "shared")
            if not inside(parent, shared):
                raise WorkflowError("Create the staging folder in shared phone storage.")
            if not any(inside(parent, f["path"]) for f in self._folders() if not f.get("excludeVideo")):
                raise WorkflowError("Add this parent folder (or a parent above it) as a Stash video library first.")
            target = os.path.join(parent, clean)
            if self.app.demo:
                try:
                    self.app.browse(target)
                except Exception:
                    self.app.demo_extra_dirs.add(target)
                else:
                    raise WorkflowError("A folder with that name already exists.", 409)
            else:
                if has_nomedia(parent, shared):
                    raise WorkflowError("A .nomedia file hides this parent from Android media apps.")
                try:
                    os.mkdir(target, 0o755)
                except FileExistsError:
                    raise WorkflowError("A file or folder with that name already exists.", 409)
                except OSError as exc:
                    raise WorkflowError("Couldn't create the folder: " + str(exc)[:200])
            return {"path": target, "demo": self.app.demo}

    def _scenes(self, source):
        if self.app.demo:
            return [
                {"id": "1", "title": "Summer morning", "performers": [{"id": "10", "name": "Alex Morgan", "disambiguation": None}],
                 "files": [{"id": "101", "path": "/storage/emulated/0/Movies/Summer.mp4", "size": 31500000}]},
                {"id": "2", "title": "Weekend together", "performers": [{"id": "10", "name": "Alex Morgan", "disambiguation": None},
                 {"id": "11", "name": "Jamie Lee", "disambiguation": None}],
                 "files": [{"id": "102", "path": "/storage/emulated/0/Movies/Weekend.mp4", "size": 26000000}]},
                {"id": "3", "title": "Not identified yet", "performers": [],
                 "files": [{"id": "103", "path": "/storage/emulated/0/Movies/Unknown.mp4", "size": 9000000}]},
            ]
        query = """query PocketWorkflowScenes($page: Int!, $path: String!) {
          findScenes(scene_filter: {path: {value: $path, modifier: INCLUDES}},
                     filter: {page: $page, per_page: 100, sort: "id", direction: ASC}) {
            count scenes { id title performers { id name disambiguation } files { id path size } }
          }
        }"""
        result = []
        for page in range(1, MAX_SCENES // 100 + 2):
            data = self.app.gql(query, {"page": page, "path": source}, timeout=15)
            found = data.get("findScenes") or {}
            count = found.get("count")
            rows = found.get("scenes")
            if not isinstance(count, int) or not isinstance(rows, list):
                raise WorkflowError("Couldn't read scenes from Stash.", 502)
            if count > MAX_SCENES:
                raise WorkflowError("This preview supports up to %d matching scenes; choose a smaller source folder." % MAX_SCENES)
            result.extend(rows)
            if len(result) >= count or not rows:
                if len(result) != count:
                    raise WorkflowError("Stash's scene list changed while loading. Try the preview again.", 409)
                return result
        raise WorkflowError("Stash's scene list changed while loading. Try again.", 409)

    def preview(self, source, destination, choices, excluded=None):
        self._require_ready()
        with self.lock:
            if self.active:
                raise WorkflowError("Wait for the current move to finish.", 409)
        source, destination = self._roots(source, destination)
        if not isinstance(choices, dict) or len(choices) > MAX_PLAN_FILES or any(
                not isinstance(k, str) or not ID_RE.fullmatch(k) or not isinstance(v, str) or not ID_RE.fullmatch(v)
                for k, v in choices.items()):
            raise WorkflowError("Invalid performer selections.")
        if excluded is None:
            excluded = []
        if not isinstance(excluded, list) or len(excluded) > MAX_PLAN_FILES or any(
                not isinstance(x, str) or not ID_RE.fullmatch(x) for x in excluded):
            raise WorkflowError("Invalid excluded files.")
        excluded = set(excluded)
        scenes = self._scenes(source)
        candidate_files = []
        for scene in scenes:
            if not isinstance(scene, dict) or not ID_RE.fullmatch(str(scene.get("id", ""))):
                raise WorkflowError("Stash returned an invalid scene.", 502)
            for file in scene.get("files") or []:
                path = file.get("path") if isinstance(file, dict) else None
                if isinstance(path, str) and inside(path, source) and not inside(path, destination):
                    candidate_files.append((scene, file))
        if len(candidate_files) > MAX_PLAN_FILES:
            raise WorkflowError("This batch has more than %d files. Select a smaller source folder." % MAX_PLAN_FILES)
        counts = {}
        for _, file in candidate_files:
            fid = str(file.get("id", ""))
            counts[fid] = counts.get(fid, 0) + 1
        rows = []
        owner_by_folder = {}
        # Preserve the performer/folder identity across batches as well as within
        # the current preview; otherwise identical names could merge albums later.
        with self.lock:
            for run in self.history.get("runs", []):
                if os.path.realpath(run.get("destination", "")) != os.path.realpath(destination):
                    continue
                for previous in run.get("items", []):
                    if previous.get("state") not in ("moved", "moving", "uncertain", "queued"):
                        continue
                    old_name, old_id = previous.get("album"), previous.get("selected_performer")
                    if not isinstance(old_name, str) or not isinstance(old_id, str) or not old_name or not old_id:
                        continue
                    key = old_name.casefold()
                    if key in owner_by_folder and owner_by_folder[key] != old_id:
                        owner_by_folder[key] = ""  # Existing journal already has an ambiguous folder.
                    else:
                        owner_by_folder[key] = old_id
        known_history = dict(owner_by_folder)
        dest_device = None if self.app.demo else os.stat(destination).st_dev
        for scene, file in candidate_files:
            sid, fid, original = str(scene.get("id")), str(file.get("id", "")), file["path"]
            title = str(scene.get("title") or "").strip()
            performers = []
            for p in scene.get("performers") or []:
                pid = str(p.get("id", "")) if isinstance(p, dict) else ""
                name = str(p.get("name") or "").strip() if isinstance(p, dict) else ""
                if ID_RE.fullmatch(pid) and name:
                    performers.append({"id": pid, "name": name, "disambiguation": str(p.get("disambiguation") or "")})
            # Treat a bad or duplicated file ID as ambiguous rather than guessing.
            row = {"scene_id": sid, "file_id": fid, "source": original, "title": title,
                   "performers": performers, "selected_performer": "", "target": "", "album": "",
                   "state": "ready", "reason": "", "size": file.get("size", 0)}
            rows.append(row)
            def skip(reason, state="skipped"):
                row["state"], row["reason"] = state, reason
            if not ID_RE.fullmatch(fid) or counts.get(fid, 0) != 1:
                skip("This file belongs to multiple scenes or has an invalid ID. Review it in Stash.")
                continue
            if fid in excluded:
                skip("You excluded this file from this batch.", "excluded")
                continue
            if not performers:
                skip("No performer in Stash metadata. Identify or edit this scene first.")
                continue
            choice = choices.get(sid)
            if len(performers) > 1 and not choice:
                skip("Choose one performer for this file. A file is not duplicated across folders.", "needs_choice")
                continue
            chosen = next((p for p in performers if p["id"] == choice), None) if choice else performers[0]
            if chosen is None:
                skip("The chosen performer is no longer on this scene. Refresh the preview.", "needs_choice")
                continue
            folder = component(chosen["name"])
            if not folder:
                skip("The performer name cannot be used as a folder name.")
                continue
            # Distinct performers whose sanitized names collide must not be merged.
            key = folder.casefold()
            other_id = owner_by_folder.setdefault(key, chosen["id"])
            if other_id != chosen["id"]:
                skip("Two different performers map to this folder name. Rename/disambiguate them in Stash.")
                continue
            ext = os.path.splitext(original)[1]
            if ext.lower() not in SUPPORTED_EXTENSIONS:
                skip("This file type is not on the supported Google Photos video list.")
                continue
            stem = component(title or os.path.splitext(os.path.basename(original))[0])
            if not stem:
                skip("The title and original filename cannot produce a safe filename.")
                continue
            target = os.path.join(destination, folder, stem + " [" + fid + "]" + ext)
            if not inside(target, destination):
                skip("Destination is outside the selected folder.")
                continue
            row.update(selected_performer=chosen["id"], album=folder, target=target)
            if self.app.demo:
                row["snapshot"] = {"size": int(file.get("size", 0)), "mtime_ns": 1, "device": 1, "inode": int(fid)}
            else:
                try:
                    snap = file_snapshot(original)
                    row["snapshot"] = snap
                    row["size"] = snap["size"]
                    if snap["size"] <= 0:
                        skip("Empty videos are not staged.")
                    elif snap["size"] > MAX_VIDEO_BYTES:
                        skip("Over the 10 GB Android Google Photos backup limit.")
                    elif snap["device"] != dest_device:
                        skip("Source and destination are on different volumes. Use a same-volume staging folder.")
                    elif os.path.islink(os.path.dirname(target)):
                        skip("The performer destination folder is a symlink. Choose a different staging folder.")
                    elif os.path.lexists(os.path.dirname(target)) and not os.path.isdir(os.path.dirname(target)):
                        skip("The performer destination already exists but is not a folder.")
                    elif os.path.isdir(os.path.dirname(target)) and known_history.get(key) != chosen["id"]:
                        # Do not quietly mix a new performer's videos with an
                        # unrelated pre-existing device folder.
                        with os.scandir(os.path.dirname(target)) as entries:
                            if next(entries, None) is not None:
                                skip("This existing performer folder contains untracked files. Choose an empty staging folder.")
                    if row["state"] == "ready" and has_nomedia(os.path.dirname(target), str(self.app.home / "storage" / "shared")):
                        skip("A .nomedia file hides this performer folder from Google Photos.")
                    if row["state"] == "ready" and os.path.lexists(target):
                        skip("Destination already exists. Nothing will be overwritten.")
                except (OSError, WorkflowError):
                    skip("The source file is missing, inaccessible or not a regular file.")
        plan = {"id": secrets.token_hex(12), "created": time.time(), "source": source,
                "destination": destination, "items": rows}
        with self.lock:
            if self.active:
                raise WorkflowError("A move started while the preview was loading. Try again.", 409)
            self.plan = plan
        return copy.deepcopy(plan)

    def _jobs_idle(self):
        if self.app.demo:
            return
        data = self.app.gql("query PocketWorkflowJobs { jobQueue { status } }", timeout=5)
        if "jobQueue" not in data:
            raise WorkflowError("Couldn't check Stash jobs. Try again.", 409)
        # Stash v0.31 returns null (not []) when its queue is empty.
        queue = data["jobQueue"]
        if queue is None:
            queue = []
        if not isinstance(queue, list) or any(not isinstance(j, dict) or not isinstance(j.get("status"), str) for j in queue):
            raise WorkflowError("Couldn't check Stash jobs. Try again.", 409)
        if any(j.get("status") not in ("FINISHED", "CANCELLED", "FAILED") for j in queue):
            raise WorkflowError("Wait for Stash's running or queued tasks to finish before moving files.", 409)

    def start(self, plan_id, enabled, acknowledged):
        if enabled is not True or acknowledged is not True:
            raise WorkflowError("Enable renaming and acknowledge the move before proceeding.")
        self._require_ready()
        with self.lock:
            if self.app.shutting_down:
                raise WorkflowError("The companion is shutting down.", 409)
            if self.active:
                raise WorkflowError("A move is already running.", 409)
            plan = self.plan
            if not plan or not isinstance(plan_id, str) or plan_id != plan["id"]:
                raise WorkflowError("Preview the current batch again before moving it.", 409)
            if time.time() - plan["created"] > PLAN_LIFETIME:
                raise WorkflowError("That preview expired. Generate a fresh preview.", 409)
            ready = [copy.deepcopy(x) for x in plan["items"] if x["state"] == "ready"]
            if not ready:
                raise WorkflowError("There are no ready files to move.")
            prior = sum(len(r.get("items", [])) for r in self.history["runs"])
            if prior + len(ready) > MAX_HISTORY_FILES:
                raise WorkflowError("The workflow journal is full. Back it up before clearing any history.", 409)
            if any(i.get("state") == "uncertain" for r in self.history["runs"] for i in r.get("items", [])):
                raise WorkflowError("A previous move is uncertain. Inspect the journal and resolve it before starting another batch.", 409)
            self._roots(plan["source"], plan["destination"])
            self._jobs_idle()
            run = {"id": secrets.token_hex(12), "created": time.time(), "state": "running",
                   "message": "Preparing a Stash database backup…", "source": plan["source"],
                   "destination": plan["destination"], "items": ready}
            for item in ready:
                item["item_id"] = secrets.token_hex(12)
                item["state"] = "queued"
                item["user_confirmed_backup"] = False
                item["user_confirmed_album"] = False
            self.history["runs"].append(run)
            try:
                # Leave room for later status and diagnostic text in this run.
                if len(json.dumps(self.history, ensure_ascii=False).encode("utf-8")) > MAX_HISTORY_BYTES - 500_000:
                    raise WorkflowError("The workflow journal is nearly full. Back it up before clearing history.", 409)
                self._save_locked()
            except Exception:
                self.history["runs"].pop()
                raise
            self.active = True
            self.stop_requested = False
            self.plan = None
            self.worker = threading.Thread(target=self._run, args=(run,), daemon=True)
            self.worker.start()
            return {"started": True, "run_id": run["id"], "count": len(ready)}

    def _validate_item(self, item, source, destination):
        if not inside(item["source"], source) or inside(item["source"], destination):
            raise WorkflowError("The source is no longer inside the selected source folder.")
        parent = os.path.dirname(item["target"])
        if (not inside(item["target"], destination) or os.path.lexists(item["target"]) or os.path.islink(parent) or
                (os.path.lexists(parent) and not os.path.isdir(parent))):
            raise WorkflowError("Destination changed or already exists. Nothing was overwritten.")
        if has_nomedia(parent, str(self.app.home / "storage" / "shared")):
            raise WorkflowError("A .nomedia file now hides the destination from Android media apps.")
        if file_snapshot(item["source"]) != item["snapshot"]:
            raise WorkflowError("The source changed since the preview.")
        if os.stat(destination).st_dev != item["snapshot"]["device"]:
            raise WorkflowError("Source and destination are no longer on the same volume.")
        query = """query PocketWorkflowScene($id: ID!) {
          findScene(id: $id) { id title performers { id name disambiguation } files { id path size } }
        }"""
        scene = self.app.gql(query, {"id": item["scene_id"]}, timeout=10).get("findScene")
        if not isinstance(scene, dict) or str(scene.get("title") or "").strip() != item["title"]:
            raise WorkflowError("Scene metadata changed since the preview.")
        actual_performers = {(str(p.get("id", "")), str(p.get("name") or "").strip(), str(p.get("disambiguation") or ""))
                             for p in scene.get("performers", []) if isinstance(p, dict)}
        expected_performers = {(p["id"], p["name"], p["disambiguation"]) for p in item["performers"]}
        match = next((p for p in scene.get("performers", []) if str(p.get("id")) == item["selected_performer"]), None)
        file = next((f for f in scene.get("files", []) if str(f.get("id")) == item["file_id"]), None)
        if (actual_performers != expected_performers or not match or component(match.get("name")) != item["album"]
                or not file or file.get("path") != item["source"]):
            raise WorkflowError("Stash's file path or performer metadata changed since the preview.")

    def _run(self, run):
        # Coordinate with this companion's scan/library configuration operations.
        # Other Stash clients are outside this lock; the user must keep them idle.
        with self.app.action_lock:
            self._run_locked(run)

    def _run_locked(self, run):
        current = None
        request_sent = False
        try:
            if not self.app.demo:
                self._roots(run["source"], run["destination"])
                self._jobs_idle()
                # A database backup is not a backup of videos or all generated assets.
                data = self.app.gql("mutation PocketWorkflowBackup { backupDatabase(input: {download: false, includeBlobs: false}) }", timeout=90)
                if "backupDatabase" not in data:
                    raise WorkflowError("Stash did not confirm its database backup.")
            for item in run["items"]:
                with self.lock:
                    if self.stop_requested:
                        raise WorkflowError("Stopped before the next file. Inspect the journal before continuing.")
                    run["message"] = "Moving: " + (item["title"] or os.path.basename(item["source"]))
                    current = item
                if not self.app.demo:
                    self._roots(run["source"], run["destination"])
                    self._jobs_idle()
                    self._validate_item(item, run["source"], run["destination"])
                with self.lock:
                    item["state"] = "moving"
                    self._save_locked()
                if self.app.demo:
                    time.sleep(0.1)
                else:
                    request_sent = True
                    result = self.app.gql("mutation PocketMove($input: MoveFilesInput!) { moveFiles(input: $input) }",
                                          {"input": {"ids": [item["file_id"]],
                                                     "destination_folder": os.path.dirname(item["target"]),
                                                     "destination_basename": os.path.basename(item["target"])}}, timeout=120)
                    if result.get("moveFiles") is not True:
                        raise WorkflowError("Stash did not confirm the move.")
                    # Do not call an uncertain result a success.
                    info = self.app.gql("query PocketWorkflowFile($id: ID!) { findFile(id: $id) { id path size } }",
                                        {"id": item["file_id"]}, timeout=10).get("findFile")
                    if (not isinstance(info, dict) or info.get("path") != item["target"] or
                            not os.path.isfile(item["target"]) or os.path.lexists(item["source"]) or
                            os.stat(item["target"]).st_size != item["snapshot"]["size"]):
                        raise WorkflowError("The move could not be verified. Inspect both locations and Stash.")
                with self.lock:
                    item["state"] = "moved"
                    self._save_locked()
                current = None
                request_sent = False
            with self.lock:
                run["state"] = "complete"
                run["message"] = "Files moved. Google Photos backup and album membership have NOT been verified."
                self._save_locked()
        except Exception as exc:
            with self.lock:
                if current is not None:
                    # Once a request was sent, even a failed post-move journal
                    # write is ambiguous and must block the next batch.
                    current["state"] = "uncertain" if request_sent or current.get("state") == "moving" else "failed"
                    current["reason"] = str(exc)[:400]
                for item in run["items"]:
                    if item.get("state") == "queued":
                        item["state"] = "not_started"
                run["state"] = "attention"
                run["message"] = "Stopped: " + str(exc)[:400]
                try:
                    self._save_locked()
                except (OSError, WorkflowError):
                    self.history_error = "Could not write the workflow journal. Inspect source, destination and Stash before continuing."
        finally:
            with self.lock:
                self.active = False

    def status(self):
        with self.lock:
            result = copy.deepcopy(self.history)
            result["running"] = self.active
            result["history_error"] = self.history_error
        for run in result.get("runs", []):
            for item in run.get("items", []):
                if item.get("state") == "moved":
                    item["local_present"] = True if self.app.demo else os.path.isfile(item.get("target", ""))
                item.pop("snapshot", None)
        return result

    def _item(self, token):
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise WorkflowError("Invalid item.")
        for run in self.history.get("runs", []):
            for item in run.get("items", []):
                if item.get("item_id") == token:
                    return run, item
        raise WorkflowError("This item is not in the workflow journal.", 404)

    def review(self, token, backup, album):
        if not isinstance(backup, bool) or not isinstance(album, bool):
            raise WorkflowError("Choose valid review states.")
        with self.lock:
            run, item = self._item(token)
            if item.get("state") != "moved":
                raise WorkflowError("Only successfully moved videos can be reviewed.")
            if not self.app.demo and not os.path.isfile(item.get("target", "")) and (
                    (backup and not item.get("user_confirmed_backup")) or (album and not item.get("user_confirmed_album"))):
                raise WorkflowError("The local file is missing. Inspect Google Photos manually; do not assume it was backed up.", 409)
            item["user_confirmed_backup"] = backup
            item["user_confirmed_album"] = album
            item["reviewed_at"] = time.time() if backup and album else None
            self._save_locked()
        return {"saved": True, "note": "These are your confirmations, not automatic Google Photos verification."}

    def resolve_uncertain(self, token, resolution, inspected):
        if inspected is not True or resolution not in ("moved", "not_moved"):
            raise WorkflowError("Inspect both locations and Stash, then choose the matching resolution.")
        with self.lock:
            if self.active:
                raise WorkflowError("Wait for the current move to finish.", 409)
            run, item = self._item(token)
            if item.get("state") != "uncertain":
                raise WorkflowError("This item does not have an uncertain move.", 409)
            info = self.app.gql("query PocketResolveFile($id: ID!) { findFile(id: $id) { id path size } }",
                                {"id": item["file_id"]}, timeout=10).get("findFile")
            if not isinstance(info, dict):
                raise WorkflowError("Stash could not find this file. Resolve it manually first.", 409)
            if resolution == "moved":
                valid = (info.get("path") == item["target"] and not os.path.lexists(item["source"]) and
                         os.path.isfile(item["target"]) and inside(item["target"], run["destination"]) and
                         os.stat(item["target"]).st_size == item["snapshot"]["size"])
            else:
                try:
                    valid = (info.get("path") == item["source"] and not os.path.lexists(item["target"]) and
                             inside(item["source"], run["source"]) and file_snapshot(item["source"]) == item["snapshot"])
                except (OSError, WorkflowError):
                    valid = False
            if not valid:
                raise WorkflowError("The filesystem and Stash do not match that choice. Inspect and resolve the partial move manually; no files were changed.", 409)
            item["state"] = "moved" if resolution == "moved" else "not_moved"
            item["reason"] = "Resolved after user inspection; no files changed by this action."
            item["resolved_at"] = time.time()
            self._save_locked()
            return {"resolved": True}

    def resolve_item(self, token):
        with self.lock:
            run, item = self._item(token)
            target = item.get("target", "")
            if item.get("state") != "moved" or not inside(target, run.get("destination", "")):
                raise WorkflowError("That video is not ready to open.", 409)
            if not self.app.demo and not os.path.isfile(target):
                raise WorkflowError("The local video is no longer present.", 404)
            return {"path": target, "album": item.get("album", ""), "demo": self.app.demo}

    def scan_paths(self):
        with self.lock:
            paths = []
            for run in self.history.get("runs", []):
                for item in run.get("items", []):
                    path = item.get("target", "")
                    if item.get("state") == "moved" and inside(path, run.get("destination", "")) and (self.app.demo or os.path.isfile(path)):
                        paths.append(path)
            return {"paths": paths[-MAX_PLAN_FILES:], "demo": self.app.demo}

"""Review-first Stash -> Android Google Photos preparation.

Stash owns moves and their database updates. Additional performer copies are
published only after verification. This module never deletes source videos, uses
root, calls Google APIs, or claims to know Google Photos' backup state.
"""
from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import json
import os
import shutil
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
MAX_PERFORMERS_PER_FILE = 40
MIN_FREE_BYTES = 16 * 1024 * 1024
COPY_CHUNK = 1024 * 1024
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


def snapshot_stat(info):
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "device": info.st_dev, "inode": info.st_ino}


def file_snapshot(path):
    # Reject symlink files even when their resolved target lies within the root.
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise WorkflowError("The source is not a regular file.")
    return snapshot_stat(info)


def checked_sha256(path, expected, progress=None):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or snapshot_stat(before) != expected:
            raise WorkflowError("A video changed before its bytes could be verified.")
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(fd, COPY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
            if progress:
                progress(count, expected["size"])
        if (count != expected["size"] or snapshot_stat(os.fstat(fd)) != expected
                or file_snapshot(path) != expected):
            raise WorkflowError("A video changed while its bytes were being verified.")
        return digest.hexdigest()
    finally:
        os.close(fd)


class AtomicPublishUnavailable(WorkflowError):
    pass


def publish_without_replacing(directory_fd, temporary, final):
    """Publish a complete file atomically, refusing to replace an existing one.

    Android 11+ exposes renameat2 in bionic. On other systems a hard link plus
    unlink is an equally non-overwriting fallback. Fail closed if neither is
    supported; ordinary rename would overwrite a concurrently created file.
    """
    unsupported = {errno.ENOSYS, errno.EINVAL, errno.EPERM, errno.EACCES,
                   getattr(errno, "EOPNOTSUPP", errno.ENOSYS), getattr(errno, "ENOTSUP", errno.ENOSYS)}
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is not None:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(directory_fd, os.fsencode(temporary), directory_fd, os.fsencode(final), 1) == 0:
            return
        error = ctypes.get_errno()
        if error not in unsupported:
            raise OSError(error, os.strerror(error), final)
    try:
        os.link(temporary, final, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise
        raise AtomicPublishUnavailable("Atomic no-replace publishing is unavailable on this storage.") from exc
    # The final link already references complete data. A leftover hidden temp is
    # safer than treating a successful publish as a failure or deleting final.
    try:
        os.unlink(temporary, dir_fd=directory_fd)
    except OSError:
        pass


def can_publish_atomically(directory_fd):
    """Probe with two tiny private names; never use a real destination.

    Android 15's MediaProvider FUSE rejects renameat2 flags and does not
    implement hard links. In that case we use exclusive creation of the final
    filename instead of a racy, potentially overwriting plain rename.
    """
    a = ".pocket-probe-" + secrets.token_hex(12)
    b = ".pocket-probe-" + secrets.token_hex(12)
    owned = set()
    try:
        for name in (a, b):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600, dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                owned.add((info.st_dev, info.st_ino))
            finally:
                os.close(fd)
        try:
            publish_without_replacing(directory_fd, a, b)
        except FileExistsError:
            pass  # A no-replace operation must reject an existing destination.
        except AtomicPublishUnavailable:
            return False
        else:
            return False  # This filesystem did not honor no-replace semantics.
        # The first test proves nonreplacement; the second proves support.
        os.unlink(b, dir_fd=directory_fd)
        try:
            publish_without_replacing(directory_fd, a, b)
        except AtomicPublishUnavailable:
            return False
        return True
    finally:
        for name in (a, b):
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) in owned:
                    os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def copy_verified(source, destination, album, filename, temporary, snapshot, progress=None):
    """Copy one video, verify bytes and never replace an existing target.

    Prefer a hidden temp followed by atomic publication. Android shared FUSE
    often lacks that primitive, so there use O_EXCL on the final name and
    journal an interruption as uncertain. In either case the original remains
    untouched until the copies have all been verified.
    """
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(destination, directory_flags)
    parent_fd = source_fd = temp_fd = None
    created = published = False
    temp_info = None
    working_name = temporary
    atomic = False
    try:
        try:
            os.mkdir(album, 0o755, dir_fd=root_fd)
        except FileExistsError:
            pass
        parent_fd = os.open(album, directory_flags, dir_fd=root_fd)
        # MediaProvider on Android 15 rejects flagged rename and hard links.
        # Avoid a probe (and its scratch files) on Termux shared storage.
        is_termux = os.environ.get("PREFIX", "").startswith("/data/data/com.termux/")
        atomic = False if is_termux else can_publish_atomically(parent_fd)
        # O_EXCL is safe on Android's MediaProvider even though its FUSE
        # implementation rejects renameat2 and hard links. A crash can leave
        # an incomplete final file, so it is always journaled and rechecked.
        working_name = temporary if atomic else filename
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or snapshot_stat(before) != snapshot:
            raise WorkflowError("The source changed since the preview.")
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        temp_fd = os.open(working_name, flags, 0o644, dir_fd=parent_fd)
        temp_info = os.fstat(temp_fd)
        created = True
        source_hash = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, COPY_CHUNK)
            if not chunk:
                break
            source_hash.update(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(temp_fd, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, "Could not write the performer copy")
                remaining = remaining[written:]
            copied += len(chunk)
            if progress:
                progress(copied, snapshot["size"])
        if (copied != snapshot["size"] or snapshot_stat(os.fstat(source_fd)) != snapshot
                or file_snapshot(source) != snapshot):
            raise WorkflowError("The source changed during copying; the original was not moved.")
        os.fsync(temp_fd)
        os.lseek(temp_fd, 0, os.SEEK_SET)
        destination_hash = hashlib.sha256()
        verified = 0
        while True:
            chunk = os.read(temp_fd, COPY_CHUNK)
            if not chunk:
                break
            destination_hash.update(chunk)
            verified += len(chunk)
            if progress:
                progress(verified, snapshot["size"])
        if destination_hash.digest() != source_hash.digest():
            raise WorkflowError("The copied bytes did not match the source; the original was not moved.")
        if file_snapshot(source) != snapshot:
            raise WorkflowError("The source changed while the copy was checked.")
        # Preserve the plugin's filename and modification time, not new metadata.
        try:
            os.utime(working_name, ns=(before.st_atime_ns, before.st_mtime_ns),
                     dir_fd=parent_fd, follow_symlinks=False)
        except (NotImplementedError, OSError):
            pass  # Some shared-storage providers do not permit setting times.
        if atomic:
            publish_without_replacing(parent_fd, temporary, filename)
        published = True
        try:
            os.fsync(parent_fd)
        except OSError:
            pass  # Android shared storage may not permit syncing directories.
        return source_hash.hexdigest()
    finally:
        # Clean up only our own incomplete file (which on Android may have the
        # final name), never an existing target or a file someone replaced.
        if created and not published and parent_fd is not None and temp_info is not None:
            try:
                existing = os.stat(working_name, dir_fd=parent_fd, follow_symlinks=False)
                if existing.st_dev == temp_info.st_dev and existing.st_ino == temp_info.st_ino:
                    os.unlink(working_name, dir_fd=parent_fd)
            except OSError:
                pass
        for fd in (temp_fd, source_fd, parent_fd, root_fd):
            if fd is not None:
                os.close(fd)


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
                        if item.get("state") in ("moving", "copying"):
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

    def _prepare_pairs_locked(self, folders):
        """Idempotently prepare each chosen video library; never replace files.

        The caller holds app.action_lock. Failures are returned per library so
        an unavailable or read-only library does not invalidate a saved config.
        """
        pairs = []
        for folder in folders:
            if folder.get("excludeVideo"):
                continue
            library = folder.get("path", "")
            pair = {"library": library, "unsorted": os.path.join(library, "Unsorted"),
                    "sorted": os.path.join(library, "Sorted"), "ready": False,
                    "photos_supported": False, "created": [], "error": ""}
            pairs.append(pair)
            try:
                library = self.app._valid_new_path(library)
                pair.update(library=library, unsorted=os.path.join(library, "Unsorted"),
                            sorted=os.path.join(library, "Sorted"))
                shared = "/storage/emulated/0" if self.app.demo else str(self.app.home / "storage" / "shared")
                pair["photos_supported"] = inside(library, shared)
                for name in ("Unsorted", "Sorted"):
                    path = os.path.join(library, name)
                    if self.app.demo:
                        if path not in self.app.demo_extra_dirs:
                            # A demo path may already be in its fixed sample tree.
                            try:
                                self.app.browse(path)
                            except Exception:
                                self.app.demo_extra_dirs.add(path)
                                pair["created"].append(name)
                    else:
                        try:
                            os.mkdir(path, 0o755)
                            pair["created"].append(name)
                        except FileExistsError:
                            pass
                        info = os.lstat(path)
                        if not stat.S_ISDIR(info.st_mode) or os.path.islink(path):
                            raise WorkflowError(name + " exists but is not a normal directory; nothing was replaced.")
                        if not inside(path, library):
                            raise WorkflowError(name + " points outside the chosen library.")
                if not self.app.demo and not os.access(pair["sorted"], os.W_OK):
                    raise WorkflowError("Sorted is not writable by Termux.")
                if not self.app.demo and has_nomedia(pair["sorted"], self._visibility_root(pair["sorted"])):
                    raise WorkflowError("A .nomedia file hides Sorted from Android media apps.")
                pair["ready"] = True
            except Exception as exc:
                # The library configuration is kept; expose the precise issue.
                pair["error"] = str(exc)[:300] or "Could not prepare folders."
        return {"pairs": pairs}

    def prepare_pairs(self):
        state = self.app.server_state()
        if state.get("state") != "running" or state.get("system") != "OK":
            raise WorkflowError("Start Stash and finish its initial setup first.", 409)
        if self.is_running():
            raise WorkflowError("Wait for the current sorting batch to finish.", 409)
        with self.app.action_lock:
            if self.is_running():
                raise WorkflowError("Wait for the current sorting batch to finish.", 409)
            return self._prepare_pairs_locked(self._folders())

    def _visibility_root(self, path):
        matching = [r["path"] for r in self.app.roots() if inside(path, r["path"])]
        return max(matching, key=len) if matching else path

    def _roots(self, source, destination):
        if not isinstance(source, str) or not isinstance(destination, str):
            raise WorkflowError("Select a source and a Photos destination folder.")
        if not self.app.demo and (os.path.islink(source) or os.path.islink(destination)):
            raise WorkflowError("Unsorted and Sorted must be normal folders, not symlinks.")
        source = self.app._valid_new_path(source)
        destination = self.app._valid_new_path(destination)
        parent = os.path.dirname(source)
        if (os.path.basename(source) != "Unsorted" or os.path.basename(destination) != "Sorted"
                or os.path.dirname(destination) != parent):
            raise WorkflowError("Choose the paired Unsorted and Sorted folders for one library.")
        libraries = [os.path.realpath(f["path"]) for f in self._folders() if not f.get("excludeVideo")]
        if os.path.realpath(parent) not in libraries:
            raise WorkflowError("Choose an Unsorted/Sorted pair inside a configured video library.")
        if not self.app.demo:
            if not os.access(destination, os.W_OK):
                raise WorkflowError("Termux cannot write to the chosen destination folder.")
            if has_nomedia(destination, self._visibility_root(destination)):
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
                 "files": [{"id": "101", "path": os.path.join(source, "Summer.mp4"), "size": 31500000}]},
                {"id": "2", "title": "Weekend together", "performers": [{"id": "10", "name": "Alex Morgan", "disambiguation": None},
                 {"id": "11", "name": "Jamie Lee", "disambiguation": None}],
                 "files": [{"id": "102", "path": os.path.join(source, "Weekend.mp4"), "size": 26000000}]},
                {"id": "3", "title": "Not identified yet", "performers": [],
                 "files": [{"id": "103", "path": os.path.join(source, "Unknown.mp4"), "size": 9000000}]},
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

    def preview(self, source, destination, choices=None, excluded=None):
        self._require_ready()
        with self.lock:
            if self.active:
                raise WorkflowError("Wait for the current sorting batch to finish.", 409)
        source, destination = self._roots(source, destination)
        # Older clients may send this field. Sorting always includes every
        # performer; a single-performer choice must never silently drop others.
        if choices is not None and not isinstance(choices, dict):
            raise WorkflowError("Invalid performer selections.")
        if excluded is None:
            excluded = []
        if not isinstance(excluded, list) or len(excluded) > MAX_PLAN_FILES or any(
                not isinstance(x, str) or not ID_RE.fullmatch(x) for x in excluded):
            raise WorkflowError("Invalid excluded files.")
        excluded = set(excluded)
        scenes = self._scenes(source)
        # Overlapping libraries have independent pairs. Never pull files from a
        # nested selected library (or any library's Sorted tree) into this one.
        this_library = os.path.realpath(os.path.dirname(source))
        libraries = [os.path.realpath(f["path"]) for f in self._folders() if not f.get("excludeVideo")]
        nested_libraries = [root for root in libraries if root != this_library and inside(root, source)]
        sorted_roots = [os.path.join(root, "Sorted") for root in libraries
                        if inside(os.path.join(root, "Sorted"), source)]
        candidate_files = []
        for scene in scenes:
            if not isinstance(scene, dict) or not ID_RE.fullmatch(str(scene.get("id", ""))):
                raise WorkflowError("Stash returned an invalid scene.", 502)
            for file in scene.get("files") or []:
                path = file.get("path") if isinstance(file, dict) else None
                if (isinstance(path, str) and inside(path, source)
                        and not any(inside(path, root) for root in nested_libraries)
                        and not any(inside(path, root) for root in sorted_roots)):
                    candidate_files.append((scene, file))
        if len(candidate_files) > MAX_PLAN_FILES:
            raise WorkflowError("This batch has more than %d files. Select fewer videos." % MAX_PLAN_FILES)
        counts = {}
        for _, file in candidate_files:
            fid = str(file.get("id", ""))
            counts[fid] = counts.get(fid, 0) + 1
        rows, owner_by_folder, planned_targets = [], {}, {}
        with self.lock:
            for run in self.history.get("runs", []):
                if os.path.realpath(run.get("destination", "")) != os.path.realpath(destination):
                    continue
                for previous in run.get("items", []):
                    if previous.get("state") not in ("moved", "moving", "copied", "copying", "uncertain", "queued"):
                        continue
                    old_name, old_id = previous.get("album"), previous.get("selected_performer")
                    if not isinstance(old_name, str) or not isinstance(old_id, str) or not old_name or not old_id:
                        continue
                    key = old_name.casefold()
                    if key in owner_by_folder and owner_by_folder[key] != old_id:
                        owner_by_folder[key] = ""
                    else:
                        owner_by_folder[key] = old_id
        dest_device = None if self.app.demo else os.stat(destination).st_dev
        for scene, file in candidate_files:
            sid, fid, original = str(scene.get("id")), str(file.get("id", "")), file["path"]
            title = str(scene.get("title") or "").strip()
            raw_performers = scene.get("performers") or []
            performers = []
            malformed = not isinstance(raw_performers, list)
            if isinstance(raw_performers, list):
                for person in raw_performers:
                    pid = str(person.get("id", "")) if isinstance(person, dict) else ""
                    name = str(person.get("name") or "").strip() if isinstance(person, dict) else ""
                    if not ID_RE.fullmatch(pid) or not name:
                        malformed = True
                    else:
                        performers.append({"id": pid, "name": name,
                                           "disambiguation": str(person.get("disambiguation") or "")})
            row = {"scene_id": sid, "file_id": fid, "source": original, "title": title,
                   "performers": performers, "selected_performer": "", "target": "", "album": "",
                   "targets": [], "state": "ready", "reason": "", "size": file.get("size", 0)}
            rows.append(row)

            def skip(reason, state="skipped"):
                row["state"], row["reason"] = state, reason

            if not ID_RE.fullmatch(fid) or counts.get(fid, 0) != 1:
                skip("This file belongs to multiple scenes or has an invalid ID. Review it in Stash.")
                continue
            if fid in excluded:
                skip("You excluded this file from this batch.", "excluded")
                continue
            if not title:
                skip("No title in Stash metadata. Identify or edit this scene first.")
                continue
            if not performers or malformed:
                skip("A named performer is missing from Stash metadata. Identify or edit this scene first.")
                continue
            if len(performers) > MAX_PERFORMERS_PER_FILE or len({p['id'] for p in performers}) != len(performers):
                skip("This scene has too many or duplicate performer entries; review it in Stash.")
                continue
            filename = os.path.basename(original)
            ext = os.path.splitext(filename)[1]
            if ext.lower() not in SUPPORTED_EXTENSIONS:
                skip("This file type is not on the supported Google Photos video list.")
                continue
            if filename in ("", ".", "..") or len(os.fsencode(filename)) > 250:
                skip("The existing filename is not safe for the destination filesystem.")
                continue
            targets = []
            for index, person in enumerate(performers):
                folder = component(person["name"])
                if not folder:
                    skip("A performer name cannot be used as a folder name.")
                    break
                key = folder.casefold()
                other_id = owner_by_folder.setdefault(key, person["id"])
                if other_id != person["id"]:
                    skip("Two different performers map to the same folder name. Rename or disambiguate them in Stash.")
                    break
                target = os.path.join(destination, folder, filename)
                if not inside(target, destination):
                    skip("A destination is outside the Sorted folder.")
                    break
                targets.append({"performer_id": person["id"], "album": folder, "target": target,
                                "operation": "move" if index == 0 else "copy"})
            if row["state"] != "ready":
                continue
            row.update(selected_performer=targets[0]["performer_id"], album=targets[0]["album"],
                       target=targets[0]["target"], targets=targets)
            if self.app.demo:
                row["snapshot"] = {"size": int(file.get("size", 0)), "mtime_ns": 1,
                                   "ctime_ns": 1, "device": 1, "inode": int(fid)}
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
                        skip("Unsorted and Sorted are on different volumes. Use a same-volume library.")
                    for target in targets:
                        if row["state"] != "ready":
                            break
                        directory = os.path.dirname(target["target"])
                        if os.path.islink(directory):
                            skip("A performer destination is a symlink. Nothing was changed.")
                        elif os.path.lexists(directory) and not os.path.isdir(directory):
                            skip("A performer destination exists but is not a folder.")
                        elif has_nomedia(directory, self._visibility_root(destination)):
                            skip("A .nomedia file hides a performer folder from Google Photos.")
                        elif os.path.lexists(target["target"]):
                            skip("A destination file already exists. Nothing will be overwritten.")
                except (OSError, WorkflowError):
                    skip("The source file is missing, inaccessible or not a regular file.")
            if row["state"] != "ready":
                continue
            for target in targets:
                key = os.path.normcase(target["target"]).casefold()
                if key in planned_targets:
                    previous = planned_targets[key]
                    previous["state"] = "skipped"
                    previous["reason"] = "Two source videos would use the same destination filename. Rename one in Stash."
                    skip(previous["reason"])
                else:
                    planned_targets[key] = row
        copy_bytes = sum(int(row["size"]) * (len(row["targets"]) - 1)
                         for row in rows if row["state"] == "ready")
        output_files = sum(len(row["targets"]) for row in rows if row["state"] == "ready")
        if output_files > MAX_HISTORY_FILES:
            raise WorkflowError("This batch would create too many performer files. Exclude some videos.")
        plan = {"id": secrets.token_hex(12), "created": time.time(), "source": source,
                "destination": destination, "items": rows, "copy_bytes": copy_bytes,
                "output_files": output_files}
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
            raise WorkflowError("Enable reviewed sorting and acknowledge the operation before proceeding.")
        self._require_ready()
        with self.lock:
            if self.app.shutting_down:
                raise WorkflowError("The companion is shutting down.", 409)
            if self.active:
                raise WorkflowError("A sorting batch is already running.", 409)
            plan = self.plan
            if not plan or not isinstance(plan_id, str) or plan_id != plan["id"]:
                raise WorkflowError("Preview the current batch again before sorting it.", 409)
            if time.time() - plan["created"] > PLAN_LIFETIME:
                raise WorkflowError("That preview expired. Generate a fresh preview.", 409)
            ready = [copy.deepcopy(x) for x in plan["items"] if x["state"] == "ready"]
            if not ready:
                raise WorkflowError("There are no ready files to sort.")
            items = []
            for source_item in ready:
                group_id = secrets.token_hex(12)
                # Make and verify N-1 copies FIRST. Stash moves the original
                # only after every additional performer has a complete copy.
                targets = source_item["targets"]
                for target in targets[1:] + targets[:1]:
                    item = copy.deepcopy(source_item)
                    item.update(group_id=group_id, selected_performer=target["performer_id"],
                                album=target["album"], target=target["target"],
                                operation=target["operation"], item_id=secrets.token_hex(12),
                                state="queued", user_confirmed_backup=False, user_confirmed_album=False)
                    if item["operation"] == "copy":
                        item["temporary"] = os.path.join(os.path.dirname(item["target"]),
                                                          ".pocket-copy-" + secrets.token_hex(12) + ".partial")
                    items.append(item)
            prior = sum(len(r.get("items", [])) for r in self.history["runs"])
            if prior + len(items) > MAX_HISTORY_FILES:
                raise WorkflowError("The workflow journal is full. Back it up before clearing any history.", 409)
            if any(i.get("state") == "uncertain" for r in self.history["runs"] for i in r.get("items", [])):
                raise WorkflowError("A previous operation is uncertain. Inspect the journal and resolve it before starting another batch.", 409)
            self._roots(plan["source"], plan["destination"])
            self._jobs_idle()
            if not self.app.demo and plan["copy_bytes"]:
                try:
                    free = shutil.disk_usage(plan["destination"]).free
                except OSError as exc:
                    raise WorkflowError("Could not check free space for performer copies.") from exc
                if free < plan["copy_bytes"] + MIN_FREE_BYTES:
                    raise WorkflowError("Not enough free space for all additional performer copies plus a safety reserve.", 409)
            run = {"id": secrets.token_hex(12), "created": time.time(), "state": "running",
                   "message": "Preparing a Stash database backup…", "source": plan["source"],
                   "destination": plan["destination"], "copy_bytes": plan["copy_bytes"], "items": items}
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
            return {"started": True, "run_id": run["id"], "count": len(ready), "output_files": len(items)}

    def _validate_item(self, item, run):
        source, destination = run["source"], run["destination"]
        if not inside(item["source"], source) or inside(item["source"], destination):
            raise WorkflowError("The source is no longer inside the selected Unsorted folder.")
        if file_snapshot(item["source"]) != item["snapshot"]:
            raise WorkflowError("The source changed since the preview.")
        if os.stat(destination).st_dev != item["snapshot"]["device"]:
            raise WorkflowError("Source and destination are no longer on the same volume.")
        completed = {x["target"]: x for x in run["items"]
                     if x.get("group_id") == item["group_id"] and x.get("state") == "copied"}
        filename = os.path.basename(item["source"])
        for target in item["targets"]:
            path = target["target"]
            parent = os.path.dirname(path)
            if (path != os.path.join(destination, target["album"], filename) or not inside(path, destination)
                    or os.path.islink(parent) or (os.path.lexists(parent) and not os.path.isdir(parent))):
                raise WorkflowError("A performer destination changed. Nothing was overwritten.")
            if has_nomedia(parent, self._visibility_root(destination)):
                raise WorkflowError("A .nomedia file now hides a performer folder from Android media apps.")
            if path in completed:
                if file_snapshot(path) != completed[path].get("target_snapshot"):
                    raise WorkflowError("An already verified performer copy changed; the original was not moved.")
            elif os.path.lexists(path):
                raise WorkflowError("A destination file appeared after preview. Nothing was overwritten.")
        query = """query PocketWorkflowScene($id: ID!) {
          findScene(id: $id) { id title performers { id name disambiguation } files { id path size } }
        }"""
        scene = self.app.gql(query, {"id": item["scene_id"]}, timeout=10).get("findScene")
        if not isinstance(scene, dict) or str(scene.get("title") or "").strip() != item["title"]:
            raise WorkflowError("Scene metadata changed since the preview.")
        actual = {(str(p.get("id", "")), str(p.get("name") or "").strip(), str(p.get("disambiguation") or ""))
                  for p in scene.get("performers", []) if isinstance(p, dict)}
        expected = {(p["id"], p["name"], p["disambiguation"]) for p in item["performers"]}
        file = next((f for f in scene.get("files", []) if str(f.get("id")) == item["file_id"]), None)
        if actual != expected or not file or file.get("path") != item["source"]:
            raise WorkflowError("Stash's file path or performer metadata changed since the preview.")

    def _run(self, run):
        # Coordinate with this companion's scan/library configuration operations.
        # Other Stash clients are outside this lock; the user must keep them idle.
        with self.app.action_lock:
            self._run_locked(run)

    def _run_locked(self, run):
        current = None
        operation_started = False
        published = False
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
                        raise WorkflowError("Stopped before the next operation. Inspect the journal before continuing.")
                    current = item
                    verb = "Copying" if item["operation"] == "copy" else "Moving original"
                    run["message"] = verb + ": " + item["title"] + " → " + item["album"]
                    run.pop("progress", None)
                if not self.app.demo:
                    self._roots(run["source"], run["destination"])
                    self._jobs_idle()
                    self._validate_item(item, run)

                def progress(count, total):
                    with self.lock:
                        if self.stop_requested:
                            raise WorkflowError("Stopped during verification; inspect the journal before continuing.")
                        run["progress"] = {"done": count, "total": total}

                previous_copies = [x for x in run["items"] if x.get("group_id") == item["group_id"] and x.get("state") == "copied"]
                if not self.app.demo and item["operation"] == "move" and previous_copies:
                    with self.lock:
                        run["message"] = "Verifying original before moving: " + item["title"]
                    digest = checked_sha256(item["source"], item["snapshot"], progress)
                    if any(x.get("sha256") != digest for x in previous_copies):
                        raise WorkflowError("The source bytes no longer match all performer copies; the original was not moved.")
                    self._jobs_idle()
                    self._validate_item(item, run)
                with self.lock:
                    item["state"] = "copying" if item["operation"] == "copy" else "moving"
                    self._save_locked()
                if self.app.demo:
                    time.sleep(0.1)
                elif item["operation"] == "copy":
                    operation_started = True
                    digest = copy_verified(item["source"], run["destination"], item["album"],
                                           os.path.basename(item["target"]), os.path.basename(item["temporary"]),
                                           item["snapshot"], progress)
                    published = True
                    item["target_snapshot"] = file_snapshot(item["target"])
                    item["sha256"] = digest
                    if any(x.get("sha256") != digest for x in previous_copies):
                        raise WorkflowError("The source bytes changed between copies; the original was not moved.")
                else:
                    # Stash updates its DB path. Its move implementation is not
                    # atomic no-overwrite under an unrelated concurrent writer.
                    operation_started = True
                    result = self.app.gql("mutation PocketMove($input: MoveFilesInput!) { moveFiles(input: $input) }",
                                          {"input": {"ids": [item["file_id"]],
                                                     "destination_folder": os.path.dirname(item["target"]),
                                                     "destination_basename": os.path.basename(item["target"])}}, timeout=120)
                    if result.get("moveFiles") is not True:
                        raise WorkflowError("Stash did not confirm the move.")
                    info = self.app.gql("query PocketWorkflowFile($id: ID!) { findFile(id: $id) { id path size } }",
                                        {"id": item["file_id"]}, timeout=10).get("findFile")
                    if (not isinstance(info, dict) or info.get("path") != item["target"] or
                            not os.path.isfile(item["target"]) or os.path.lexists(item["source"]) or
                            os.stat(item["target"]).st_size != item["snapshot"]["size"]):
                        raise WorkflowError("The move could not be verified. Inspect both locations and Stash.")
                with self.lock:
                    item["state"] = "copied" if item["operation"] == "copy" else "moved"
                    self._save_locked()
                current = None
                operation_started = False
                published = False
            with self.lock:
                run["state"] = "complete"
                run.pop("progress", None)
                run["message"] = "Sorting finished. Google Photos backup and album membership have NOT been verified."
                self._save_locked()
        except Exception as exc:
            with self.lock:
                if current is not None:
                    # Once Stash has received a move, or a copy may have been
                    # published, never guess or silently retry the operation.
                    uncertain = operation_started and (current.get("operation") == "move" or published or
                                                       os.path.lexists(current.get("target", "")) or
                                                       os.path.lexists(current.get("temporary", "")))
                    current["state"] = "uncertain" if uncertain else "failed"
                    current["reason"] = str(exc)[:400]
                for item in run["items"]:
                    if item.get("state") == "queued":
                        item["state"] = "not_started"
                run["state"] = "attention"
                run.pop("progress", None)
                run["message"] = "Stopped: " + str(exc)[:400] + " Inspect any completed copies and the original before retrying."
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
                if item.get("state") in ("moved", "copied"):
                    item["local_present"] = True if self.app.demo else os.path.isfile(item.get("target", ""))
                item.pop("snapshot", None)
                item.pop("target_snapshot", None)
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
            if item.get("state") not in ("moved", "copied"):
                raise WorkflowError("Only successfully moved or copied videos can be reviewed.")
            if not self.app.demo and not os.path.isfile(item.get("target", "")) and (
                    (backup and not item.get("user_confirmed_backup")) or (album and not item.get("user_confirmed_album"))):
                raise WorkflowError("The local file is missing. Inspect Google Photos manually; do not assume it was backed up.", 409)
            item["user_confirmed_backup"] = backup
            item["user_confirmed_album"] = album
            item["reviewed_at"] = time.time() if backup and album else None
            self._save_locked()
        return {"saved": True, "note": "These are your confirmations, not automatic Google Photos verification."}

    def resolve_uncertain(self, token, resolution, inspected):
        if inspected is not True or resolution not in ("moved", "not_moved", "copied", "not_copied"):
            raise WorkflowError("Inspect the listed paths and Stash, then choose the matching resolution.")
        with self.lock:
            if self.active:
                raise WorkflowError("Wait for the current sorting batch to finish.", 409)
            run, item = self._item(token)
            if item.get("state") != "uncertain":
                raise WorkflowError("This item does not have an uncertain operation.", 409)
            is_copy = item.get("operation") == "copy"
            if is_copy != (resolution in ("copied", "not_copied")):
                raise WorkflowError("Choose a resolution for this operation type.", 409)
            info = self.app.gql("query PocketResolveFile($id: ID!) { findFile(id: $id) { id path size } }",
                                {"id": item["file_id"]}, timeout=10).get("findFile")
            if not isinstance(info, dict):
                raise WorkflowError("Stash could not find this file. Resolve it manually first.", 409)
            if is_copy:
                temporary = item.get("temporary", "")
                if temporary and os.path.lexists(temporary):
                    raise WorkflowError("The listed temporary file still exists. Inspect it manually after stopping the companion; nothing was deleted.", 409)
                try:
                    valid_source = (info.get("path") == item["source"] and inside(item["source"], run["source"])
                                    and file_snapshot(item["source"]) == item["snapshot"])
                    if resolution == "copied":
                        valid = valid_source and inside(item["target"], run["destination"])
                        target_snapshot = file_snapshot(item["target"]) if valid else None
                        if valid:
                            src_hash = checked_sha256(item["source"], item["snapshot"])
                            valid = (target_snapshot["size"] == item["snapshot"]["size"] and
                                     checked_sha256(item["target"], target_snapshot) == src_hash)
                    else:
                        valid = valid_source and not os.path.lexists(item["target"])
                except (OSError, WorkflowError):
                    valid = False
                if valid and resolution == "copied":
                    item["target_snapshot"] = target_snapshot
                    item["sha256"] = src_hash
            elif resolution == "moved":
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
                raise WorkflowError("The filesystem and Stash do not match that choice. Inspect the files manually; nothing was changed.", 409)
            item["state"] = resolution
            item["reason"] = "Resolved after user inspection; no files changed by this action."
            item["resolved_at"] = time.time()
            self._save_locked()
            return {"resolved": True}

    def resolve_item(self, token):
        with self.lock:
            run, item = self._item(token)
            target = item.get("target", "")
            if item.get("state") not in ("moved", "copied") or not inside(target, run.get("destination", "")):
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
                    if item.get("state") in ("moved", "copied") and inside(path, run.get("destination", "")) and (self.app.demo or os.path.isfile(path)):
                        paths.append(path)
            return {"paths": paths[-MAX_PLAN_FILES:], "demo": self.app.demo}

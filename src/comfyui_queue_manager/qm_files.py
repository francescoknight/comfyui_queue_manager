"""
Output file actions for ComfyUI Queue Manager.

  * open a completed job's file with the system's default application,
  * delete a job for good: its output files, its database row and its entry in
    ComfyUI's history.

Every path acted upon must be listed in that job's own `outputs` metadata AND resolve
inside ComfyUI's output directory. Anything else is refused: these endpoints delete
files, so the job's own metadata is the only accepted source of truth, never a path
coming straight from the client.

Deletion goes to the Recycle Bin unless the caller explicitly asks for a permanent
delete, and a failed Recycle Bin operation never falls back to deleting for good.
"""

import ctypes
import json
import os
import subprocess
import sys

import folder_paths
from server import PromptServer

from .qm_db import get_conn, read_single, write_query
from .qm_log import qm_log

# SHFileOperationW, used to move files to the Recycle Bin without a third party package.
_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400


def _shfileopstruct():
    """The Win32 struct, built lazily so this module still imports on Linux / macOS."""
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    return SHFILEOPSTRUCTW


def _recycle(paths):
    """Move files to the Recycle Bin. Returns (recycled, failed)."""
    if not paths:
        return [], []

    if sys.platform != "win32":
        return [], list(paths)

    # pFrom is a double null terminated list of null terminated paths
    buffer = "\0".join(paths) + "\0\0"

    operation = _shfileopstruct()(
        hwnd=None,
        wFunc=_FO_DELETE,
        pFrom=buffer,
        pTo=None,
        fFlags=_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_NOERRORUI | _FOF_SILENT,
        fAnyOperationsAborted=False,
        hNameMappings=None,
        lpszProgressTitle=None,
    )

    try:
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except Exception as error:  # noqa: BLE001
        qm_log.error("Recycle Bin operation failed: %s", error)
        return [], list(paths)

    if result != 0 or operation.fAnyOperationsAborted:
        qm_log.error("Recycle Bin operation returned %s", result)
        return [], list(paths)

    # SHFileOperationW is all or nothing for this flag set
    return list(paths), []


class QM_Files:
    def __init__(self, queue):
        self.queue = queue  # QM_Queue

    # =====================================================================
    # Helpers
    # =====================================================================

    def output_root(self):
        return os.path.realpath(folder_paths.get_output_directory())

    def job_files(self, db_id):
        """Absolute paths of the output files recorded for a job.

        Only files that are both listed in the job's `outputs` metadata and inside the
        output directory are returned.
        """
        row = read_single(
            """
            SELECT value
            FROM meta
            WHERE item_id = ? AND key = 'outputs'
        """,
            (db_id,),
        )

        if row is None:
            return []

        try:
            outputs = json.loads(row[0])
        except (TypeError, ValueError):
            return []

        root = self.output_root()
        files = []

        for output in outputs.values():
            if not isinstance(output, dict):
                continue

            entries = output.get("images") or output.get("gifs") or []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != "output":
                    continue  # temp / input files are not ours to delete

                filename = str(entry.get("filename") or "")
                subfolder = str(entry.get("subfolder") or "")
                if not filename:
                    continue

                path = os.path.realpath(os.path.join(root, subfolder, filename))

                try:
                    inside = os.path.commonpath([root, path]) == root
                except ValueError:  # different drive
                    inside = False

                if not inside:
                    qm_log.error("Refusing to touch a file outside the output folder: %s", path)
                    continue

                if path not in files:
                    files.append(path)

        return files

    # =====================================================================
    # Open
    # =====================================================================

    def open_file(self, db_id, filename="", subfolder=""):
        """Open one of a job's output files with the system's default application."""
        if not filename:
            return {"opened": False, "error": "No file given"}

        wanted = os.path.realpath(os.path.join(self.output_root(), subfolder, filename))

        if wanted not in self.job_files(db_id):
            return {"opened": False, "error": "File is not one of this job's outputs"}

        if not os.path.isfile(wanted):
            return {"opened": False, "error": "File no longer exists on disk"}

        try:
            if sys.platform == "win32":
                os.startfile(wanted)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", wanted])  # noqa: S603, S607
            else:
                subprocess.Popen(["xdg-open", wanted])  # noqa: S603, S607
        except Exception as error:  # noqa: BLE001
            qm_log.error("Could not open %s: %s", wanted, error)
            return {"opened": False, "error": str(error)}

        qm_log.info("Opened %s", wanted)
        return {"opened": True, "path": wanted}

    # =====================================================================
    # Delete for good
    # =====================================================================

    def preview_purge(self, db_ids):
        """What a purge would remove. Used by the confirmation dialog."""
        jobs = []

        for db_id in db_ids:
            row = read_single("SELECT id, name, status FROM queue WHERE id = ?", (int(db_id),))
            if row is None:
                continue

            files = self.job_files(row["id"])
            jobs.append(
                {
                    "db_id": row["id"],
                    "name": row["name"],
                    "running": row["status"] == 1,
                    "files": [os.path.basename(path) for path in files],
                    "total_files": len(files),
                }
            )

        return {
            "jobs": jobs,
            "total_jobs": len(jobs),
            "total_files": sum(job["total_files"] for job in jobs),
            "running_jobs": sum(1 for job in jobs if job["running"]),
        }

    def purge_items(self, db_ids, permanent=False):
        """Delete jobs and their output files.

        Files go to the Recycle Bin unless `permanent` is set. A job that is currently
        running is skipped: it has to be cancelled first.
        """
        with self.queue.native_queue.mutex:
            deleted_jobs = 0
            skipped_running = 0
            to_delete = []
            prompt_ids = []
            missing = 0

            for db_id in db_ids:
                try:
                    db_id = int(db_id)
                except (TypeError, ValueError):
                    continue

                row = read_single("SELECT id, prompt_id, status FROM queue WHERE id = ?", (db_id,))
                if row is None:
                    continue

                if row["status"] == 1:
                    skipped_running += 1
                    continue

                for path in self.job_files(db_id):
                    if os.path.isfile(path):
                        to_delete.append(path)
                    else:
                        missing += 1

                prompt_ids.append((db_id, row["prompt_id"]))

            if permanent:
                removed, failed = [], []
                for path in to_delete:
                    try:
                        os.remove(path)
                        removed.append(path)
                    except OSError as error:
                        qm_log.error("Could not delete %s: %s", path, error)
                        failed.append(path)
            else:
                removed, failed = _recycle(to_delete)

            if failed and not removed:
                return {
                    "deleted_jobs": 0,
                    "deleted_files": 0,
                    "failed_files": len(failed),
                    "skipped_running": skipped_running,
                    "error": "No file could be deleted, the jobs were kept",
                }

            history = getattr(self.queue.native_queue, "history", None)

            for db_id, prompt_id in prompt_ids:
                deleted_jobs += write_query("DELETE FROM queue WHERE id = ?", (db_id,), False)
                if isinstance(history, dict):
                    history.pop(prompt_id, None)

            get_conn().commit()

            if deleted_jobs:
                qm_log.info(
                    "Purged %d job(s), %s %d file(s)",
                    deleted_jobs,
                    "deleted" if permanent else "recycled",
                    len(removed),
                )
                PromptServer.instance.queue_updated()
                PromptServer.instance.send_sync("queue-manager-queue-updated", {"deleted": deleted_jobs})

            return {
                "deleted_jobs": deleted_jobs,
                "deleted_files": len(removed),
                "failed_files": len(failed),
                "missing_files": missing,
                "skipped_running": skipped_running,
                "permanent": bool(permanent),
            }

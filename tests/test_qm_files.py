"""Tests for the output file actions (qm_files): path containment, open, purge.

These exercise code that deletes files, so the containment rules are the important
part: only files listed in a job's own `outputs` metadata and resolving inside the
output directory may ever be touched. ComfyUI's modules are stubbed and everything
happens in a temporary directory.

Runs under pytest, or directly:  python tests/test_qm_files.py
"""

import json
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeNativeQueue:
    def __init__(self):
        self.mutex = threading.RLock()
        self.not_empty = threading.Condition(self.mutex)
        self.queue = []
        self.currently_running = {}
        self.history = {}
        self.task_counter = 0


class _FakeServer:
    instance = None

    def __init__(self):
        self.number = 0
        self.prompt_queue = _FakeNativeQueue()
        self.events = []

    def queue_updated(self):
        self.events.append(("queue_updated", None))

    def send_sync(self, name, data=None):
        self.events.append((name, data))


if "server" not in sys.modules:
    _server_stub = types.ModuleType("server")
    _server_stub.PromptServer = _FakeServer
    _FakeServer.instance = _FakeServer()
    sys.modules["server"] = _server_stub

if "folder_paths" not in sys.modules:
    _folder_stub = types.ModuleType("folder_paths")
    _out = tempfile.mkdtemp(prefix="qm_files_test_out_")
    _folder_stub.get_output_directory = lambda: _out
    sys.modules["folder_paths"] = _folder_stub

import folder_paths  # noqa: E402
from server import PromptServer  # noqa: E402
from src.comfyui_queue_manager import qm_db  # noqa: E402

if not hasattr(qm_db._local, "conn"):
    qm_db.DB_PATH = Path(tempfile.mkdtemp(prefix="qm_files_test_")) / "test-queue.db"
qm_db.init_schema()

from src.comfyui_queue_manager.qm_files import QM_Files  # noqa: E402

OUT = Path(folder_paths.get_output_directory())


# ---------------------------------------------------------------- helpers


class _FakeQMQueue:
    def __init__(self):
        self.native_queue = _FakeNativeQueue()


def _files():
    return QM_Files(_FakeQMQueue())


def _make_job(entries, status=2, name="job"):
    """Insert a completed job whose outputs metadata lists `entries`."""
    qm_db.write_query("DELETE FROM queue")

    qm_db.write_query(
        "INSERT INTO queue (prompt_id, number, name, workflow_id, prompt, status) VALUES (?, 1, ?, 'wf', '[]', ?)",
        (f"pid-{name}", name, status),
    )
    db_id = qm_db.read_single("SELECT id FROM queue WHERE prompt_id = ?", (f"pid-{name}",))[0]

    qm_db.write_query(
        "INSERT INTO meta (item_id, key, value) VALUES (?, 'outputs', ?)",
        (db_id, json.dumps({"9": {"images": entries}})),
    )
    return db_id


def _write(subfolder, filename, content=b"x"):
    folder = OUT / subfolder if subfolder else OUT
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------- containment


def test_job_files_returns_recorded_outputs():
    path = _write("", "clip.mp4")
    db_id = _make_job([{"filename": "clip.mp4", "subfolder": "", "type": "output"}])

    assert _files().job_files(db_id) == [os.path.realpath(str(path))]


def test_job_files_skips_temp_and_input_files():
    _write("", "preview.png")
    db_id = _make_job([{"filename": "preview.png", "subfolder": "", "type": "temp"}])

    assert _files().job_files(db_id) == []


def test_job_files_refuses_to_escape_the_output_folder():
    db_id = _make_job([{"filename": "secret.txt", "subfolder": "../../..", "type": "output"}])

    assert _files().job_files(db_id) == []


def test_job_files_refuses_an_absolute_path_in_the_filename():
    db_id = _make_job([{"filename": "C:\\Windows\\system.ini", "subfolder": "", "type": "output"}])

    assert _files().job_files(db_id) == []


def test_job_files_without_metadata_is_empty():
    qm_db.write_query("DELETE FROM queue")
    qm_db.write_query(
        "INSERT INTO queue (prompt_id, number, name, workflow_id, prompt, status) VALUES ('pid-bare', 1, 'n', 'wf', '[]', 2)"
    )
    db_id = qm_db.read_single("SELECT id FROM queue WHERE prompt_id = 'pid-bare'")[0]

    assert _files().job_files(db_id) == []


# ---------------------------------------------------------------- open


def test_open_refuses_a_file_the_job_does_not_own():
    _write("", "mine.mp4")
    _write("", "someone_else.mp4")
    db_id = _make_job([{"filename": "mine.mp4", "subfolder": "", "type": "output"}])

    result = _files().open_file(db_id, "someone_else.mp4", "")

    assert result["opened"] is False
    assert "outputs" in result["error"]


def test_open_reports_a_missing_file():
    db_id = _make_job([{"filename": "gone.mp4", "subfolder": "", "type": "output"}])

    result = _files().open_file(db_id, "gone.mp4", "")

    assert result["opened"] is False
    assert "no longer exists" in result["error"].lower()


def test_open_without_a_filename_is_refused():
    db_id = _make_job([{"filename": "a.mp4", "subfolder": "", "type": "output"}])

    assert _files().open_file(db_id, "", "")["opened"] is False


# ---------------------------------------------------------------- purge


def test_preview_lists_files_and_flags_running_jobs():
    _write("", "a.mp4")
    db_id = _make_job([{"filename": "a.mp4", "subfolder": "", "type": "output"}], status=1)

    preview = _files().preview_purge([db_id])

    assert preview["total_jobs"] == 1
    assert preview["total_files"] == 1
    assert preview["running_jobs"] == 1
    assert preview["jobs"][0]["files"] == ["a.mp4"]


def test_purge_deletes_the_files_and_the_row():
    path = _write("sub", "clip.mp4")
    db_id = _make_job([{"filename": "clip.mp4", "subfolder": "sub", "type": "output"}])

    result = _files().purge_items([db_id], permanent=True)

    assert result["deleted_jobs"] == 1
    assert result["deleted_files"] == 1
    assert not path.exists()
    assert qm_db.read_single("SELECT id FROM queue WHERE id = ?", (db_id,)) is None


def test_purge_skips_a_running_job_and_keeps_its_file():
    path = _write("", "running.mp4")
    db_id = _make_job([{"filename": "running.mp4", "subfolder": "", "type": "output"}], status=1)

    result = _files().purge_items([db_id], permanent=True)

    assert result["skipped_running"] == 1
    assert result["deleted_jobs"] == 0
    assert path.exists()
    assert qm_db.read_single("SELECT id FROM queue WHERE id = ?", (db_id,)) is not None


def test_purge_of_a_job_whose_files_are_already_gone_still_removes_the_row():
    db_id = _make_job([{"filename": "vanished.mp4", "subfolder": "", "type": "output"}])

    result = _files().purge_items([db_id], permanent=True)

    assert result["deleted_jobs"] == 1
    assert result["missing_files"] == 1
    assert qm_db.read_single("SELECT id FROM queue WHERE id = ?", (db_id,)) is None


def test_purge_drops_the_native_history_entry():
    _write("", "h.mp4")
    db_id = _make_job([{"filename": "h.mp4", "subfolder": "", "type": "output"}])

    files = _files()
    files.queue.native_queue.history["pid-job"] = {"outputs": {}}

    files.purge_items([db_id], permanent=True)

    assert "pid-job" not in files.queue.native_queue.history


def test_purge_ignores_unknown_ids():
    assert _files().purge_items([999999], permanent=True)["deleted_jobs"] == 0


def test_purge_cannot_delete_a_file_outside_the_output_folder():
    outside = Path(tempfile.mkdtemp(prefix="qm_files_outside_")) / "keepme.txt"
    outside.write_bytes(b"important")

    db_id = _make_job([{"filename": "keepme.txt", "subfolder": "../../..", "type": "output"}])
    result = _files().purge_items([db_id], permanent=True)

    assert outside.exists()
    assert result["deleted_files"] == 0


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc or 'assertion failed'}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILURES: %d' % failures if failures else 'all tests passed'}")
    sys.exit(1 if failures else 0)

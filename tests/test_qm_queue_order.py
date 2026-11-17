"""Tests for the ordering hooks inside QM_Queue: LIFO submit mode and prefetch refresh.

These exercise the paths that only run with a live native queue, with ComfyUI's
`server`, `execution` and `folder_paths` modules stubbed. The database is a throwaway
sqlite file, so the real queue is never touched.

Runs under pytest, or directly:  python tests/test_qm_queue_order.py
"""

import heapq
import json
import sys
import tempfile
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------- ComfyUI stubs


class _FakeNativeQueue:
    """Enough of execution.PromptQueue for QM_Queue to hijack it."""

    def __init__(self):
        self.mutex = threading.RLock()
        self.not_empty = threading.Condition(self.mutex)
        self.queue = []
        self.currently_running = {}
        self.history = {}
        self.task_counter = 0
        self.puts = []

    def put(self, item):
        self.puts.append(item)
        heapq.heappush(self.queue, item)

    def get(self, timeout=None):
        with self.not_empty:
            if not self.queue:
                return None
            item = heapq.heappop(self.queue)
            i = self.task_counter
            self.currently_running[i] = item
            self.task_counter += 1
            return (item, i)

    def get_current_queue(self, *args, **kwargs):
        return [], list(self.queue)

    def get_tasks_remaining(self):
        return len(self.queue)

    def task_done(self, *args, **kwargs):
        return None


class _FakeSettings:
    def get_settings(self, request=None):
        return {}


class _FakeServer:
    instance = None

    def __init__(self):
        self.number = 0
        self.prompt_queue = _FakeNativeQueue()
        self.user_manager = types.SimpleNamespace(settings=_FakeSettings())
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

if "execution" not in sys.modules:
    _execution_stub = types.ModuleType("execution")

    class _PromptQueue:
        class ExecutionStatus(tuple):
            pass

    _execution_stub.PromptQueue = _PromptQueue
    sys.modules["execution"] = _execution_stub

if "folder_paths" not in sys.modules:
    _folder_stub = types.ModuleType("folder_paths")
    _output_dir = tempfile.mkdtemp(prefix="qm_queue_test_out_")
    _folder_stub.get_output_directory = lambda: _output_dir
    sys.modules["folder_paths"] = _folder_stub

from server import PromptServer  # noqa: E402
from src.comfyui_queue_manager import qm_db  # noqa: E402

# Another test module may already hold the connection; the schema is the same either way.
if not hasattr(qm_db._local, "conn"):
    qm_db.DB_PATH = Path(tempfile.mkdtemp(prefix="qm_queue_test_")) / "test-queue.db"
qm_db.init_schema()

from src.comfyui_queue_manager.qm_queue import QM_Queue  # noqa: E402


# ---------------------------------------------------------------- test doubles


class _Options:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None, with_timestamp=False):
        if with_timestamp:
            return self.values.get(key, default), None
        return self.values.get(key, default)

    def set(self, key, value=None):
        self.values[key] = value


def _build(submit_order="FIFO"):
    """A fresh QM_Queue over a fresh fake native queue and an empty pending queue."""
    qm_db.write_query("DELETE FROM queue")

    PromptServer.instance.number = 0
    PromptServer.instance.events = []
    PromptServer.instance.prompt_queue = _FakeNativeQueue()
    PromptServer.instance.user_manager = types.SimpleNamespace(settings=_FakeSettings())

    manager = types.SimpleNamespace(options=_Options({"submit_order": submit_order}))
    return QM_Queue(manager)


def _item(number, name="job"):
    """A job in ComfyUI's native format, with the workflow metadata QM requires."""
    return [
        number,
        f"pid-{name}-{number}",
        {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a.ckpt"}}},
        {"extra_pnginfo": {"workflow": {"id": "wf-1", "workflow_name": name}}},
        [],
    ]


def _rows():
    return qm_db.read_query("SELECT prompt_id, number FROM queue WHERE status = 0 ORDER BY number")


# ---------------------------------------------------------------- FIFO / LIFO


def test_fifo_keeps_the_submitted_order():
    queue = _build("FIFO")

    queue.queue_put(_item(1, "first"))
    queue.queue_put(_item(2, "second"))

    assert [row[0] for row in _rows()] == ["pid-first-1", "pid-second-2"]
    assert all(row[1] > 0 for row in _rows())


def test_lifo_puts_the_newest_job_first():
    queue = _build("LIFO")

    queue.queue_put(_item(1, "first"))
    queue.queue_put(_item(2, "second"))
    queue.queue_put(_item(3, "third"))

    assert [row[0] for row in _rows()] == ["pid-third-3", "pid-second-2", "pid-first-1"]
    assert all(row[1] < 0 for row in _rows())


def test_lifo_job_sorts_before_jobs_queued_in_fifo_mode():
    queue = _build("FIFO")
    queue.queue_put(_item(1, "old"))

    queue.queue_manager.options.set("submit_order", "LIFO")
    queue.queue_put(_item(2, "new"))

    assert [row[0] for row in _rows()] == ["pid-new-2", "pid-old-1"]


def test_lifo_prefetches_the_new_job():
    queue = _build("LIFO")
    queue.queue_put(_item(1, "first"))
    queue.queue_put(_item(2, "second"))

    # the native heap holds exactly the job that must run next
    assert len(queue.native_queue.queue) == 1
    assert queue.native_queue.queue[0][1] == "pid-second-2"


def test_lifo_stored_prompt_carries_the_negative_number():
    queue = _build("LIFO")
    queue.queue_put(_item(5, "only"))

    stored = json.loads(qm_db.read_single("SELECT prompt FROM queue WHERE prompt_id = 'pid-only-5'")[0])
    number = qm_db.read_single("SELECT number FROM queue WHERE prompt_id = 'pid-only-5'")[0]

    assert stored[0] == number < 0


def test_api_jobs_are_managed_too():
    # Since 0.2.0 upstream stores API prompts (no extra_pnginfo) in the database as well,
    # so every ordering feature applies to jobs submitted by scripts.
    queue = _build("LIFO")
    queue.queue_put([1, "pid-api", {"1": {"class_type": "KSampler", "inputs": {}}}, {}, []])

    assert [row[0] for row in _rows()] == ["pid-api"]
    assert _rows()[0][1] < 0  # and LIFO applies to them too


# ---------------------------------------------------------------- prefetch


def test_refresh_prefetch_loads_the_best_pending_job():
    queue = _build("FIFO")
    queue.queue_put(_item(1, "first"))
    queue.queue_put(_item(2, "second"))

    # make the second job the best one, the way a reorder would
    qm_db.write_query("UPDATE queue SET number = -10 WHERE prompt_id = 'pid-second-2'")
    queue.refresh_prefetch()

    assert len(queue.native_queue.queue) == 1
    assert queue.native_queue.queue[0][1] == "pid-second-2"
    assert queue.native_queue.queue[0][0] == -10  # heap priority matches the database


def test_refresh_prefetch_keeps_the_heap_empty_while_paused():
    queue = _build("FIFO")
    queue.queue_put(_item(1, "first"))
    queue.paused = True

    queue.refresh_prefetch()

    assert queue.native_queue.queue == []


def test_refresh_prefetch_does_not_lose_the_dropped_job():
    queue = _build("FIFO")
    queue.queue_put(_item(1, "first"))
    queue.queue_put(_item(2, "second"))

    before = {row[0] for row in _rows()}
    queue.refresh_prefetch()

    assert {row[0] for row in _rows()} == before  # still pending in the database


# ---------------------------------------------------------------- reorder integration


def test_reordering_then_running_executes_the_new_head():
    queue = _build("FIFO")
    queue.queue_put(_item(1, "first"))
    queue.queue_put(_item(2, "second"))

    queue.order.invert_order()

    executed = queue.queue_get(timeout=1)

    assert executed is not None
    assert executed[0][1] == "pid-second-2"
    assert qm_db.read_single("SELECT status FROM queue WHERE prompt_id = 'pid-second-2'")[0] == 1
    assert queue.started_at.get("pid-second-2") is not None


def test_invert_reverses_the_pending_queue():
    queue = _build("FIFO")
    for i in range(1, 4):
        queue.queue_put(_item(i, f"job{i}"))

    queue.order.invert_order()

    assert [row[0] for row in _rows()] == ["pid-job3-3", "pid-job2-2", "pid-job1-1"]
    assert queue.native_queue.queue[0][1] == "pid-job3-3"


def test_optimize_is_available_from_the_queue():
    queue = _build("FIFO")
    queue.queue_put(_item(1, "a"))
    queue.queue_put(_item(2, "b"))

    result = queue.order.optimize_order()

    assert result["total"] == 2
    assert result["groups"] == 1  # same checkpoint


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

"""Tests for the queue ordering engine (qm_order): FIFO/LIFO, manual moves, model grouping.

qm_order imports ComfyUI's `server` module, which is not available outside a running
ComfyUI, so a minimal stub is installed before the import. The database is a throwaway
sqlite file, so nothing here touches the real queue.

Runs under pytest, or directly:  python tests/test_qm_order.py
"""

import json
import sys
import tempfile
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------- ComfyUI stub


class _FakePromptQueue:
    def __init__(self):
        self.mutex = threading.RLock()
        self.not_empty = threading.Condition(self.mutex)
        self.queue = []
        self.task_counter = 0
        self.currently_running = {}


class _FakeServer:
    instance = None

    def __init__(self):
        self.number = 0
        self.prompt_queue = _FakePromptQueue()
        self.events = []

    def queue_updated(self):
        self.events.append(("queue_updated", None))

    def send_sync(self, name, data=None):
        self.events.append((name, data))


if "server" not in sys.modules:
    _stub = types.ModuleType("server")
    _stub.PromptServer = _FakeServer
    _FakeServer.instance = _FakeServer()
    sys.modules["server"] = _stub

from src.comfyui_queue_manager import qm_db  # noqa: E402
from server import PromptServer  # noqa: E402

qm_db.DB_PATH = Path(tempfile.mkdtemp(prefix="qm_order_test_")) / "test-queue.db"
qm_db.init_schema()

from src.comfyui_queue_manager.qm_order import QM_Order, _short  # noqa: E402


# ---------------------------------------------------------------- test doubles


class _Options:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None, with_timestamp=False):
        return self.values.get(key, default)

    def set(self, key, value=None):
        self.values[key] = value


class _FakeQMQueue:
    """Stands in for QM_Queue: only what QM_Order touches."""

    def __init__(self, options=None):
        self.native_queue = PromptServer.instance.prompt_queue
        self.queue_manager = types.SimpleNamespace(options=options or _Options())
        self.paused = False
        self.prefetch_calls = 0

    def refresh_prefetch(self):
        self.prefetch_calls += 1


def _prompt(ckpt=None, loras=(), vae=None, nested_loras=()):
    """Build a prompt in ComfyUI's stored format: [number, prompt_id, graph, extra, outputs]."""
    graph = {}
    node = 1

    if ckpt:
        graph[str(node)] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}}
        node += 1

    for lora in loras:
        graph[str(node)] = {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": lora,
                "strength_model": 0.8,
                "strength_clip": 1.0,
                "model": ["1", 0],  # node link: must be ignored
            },
        }
        node += 1

    if nested_loras:
        graph[str(node)] = {
            "class_type": "Power Lora Loader (rgthree)",
            "inputs": {
                "lora_1": {"lora": nested_loras[0], "on": True, "strength": 1.0},
            },
        }
        node += 1

    if vae:
        graph[str(node)] = {"class_type": "VAELoader", "inputs": {"vae_name": vae}}
        node += 1

    graph[str(node)] = {
        "class_type": "KSampler",
        "inputs": {"seed": 42, "model": ["1", 0], "positive": ["2", 0], "text": "a prompt, not a model"},
    }

    return [0, "pid", graph, {"extra_pnginfo": {"workflow": {"id": "wf", "workflow_name": "wf"}}}, []]


def _seed(prompts):
    """Replace the pending queue with the given prompts, in order. Returns their db ids."""
    qm_db.write_query("DELETE FROM queue")
    ids = []
    for i, prompt in enumerate(prompts):
        prompt[1] = f"pid-{i}"
        qm_db.write_query(
            "INSERT INTO queue (prompt_id, number, name, workflow_id, prompt, status) VALUES (?, ?, ?, ?, ?, 0)",
            (prompt[1], i + 1, "wf", "wf", json.dumps(prompt)),
        )
        ids.append(qm_db.read_single("SELECT id FROM queue WHERE prompt_id = ?", (prompt[1],))[0])
    return ids


def _order():
    return [row[0] for row in qm_db.read_query("SELECT id FROM queue WHERE status = 0 ORDER BY number, id")]


def _fresh(options=None):
    PromptServer.instance.number = 0
    PromptServer.instance.events = []
    return QM_Order(_FakeQMQueue(options))


# ---------------------------------------------------------------- signature


def test_signature_splits_heavy_support_and_loras():
    order = _fresh()
    heavy, support, loras = order.signature(json.dumps(_prompt(ckpt="sdxl.safetensors", loras=["a.safetensors"], vae="ae.sft")))

    assert heavy == ("sdxl.safetensors",)
    assert support == ("ae.sft",)
    assert loras == ("a.safetensors",)


def test_signature_ignores_links_and_prompt_text():
    order = _fresh()
    heavy, support, loras = order.signature(json.dumps(_prompt(ckpt="m.safetensors")))

    assert heavy == ("m.safetensors",)
    assert support == ()
    assert loras == ()


def test_signature_reads_nested_lora_widgets():
    order = _fresh()
    _, _, loras = order.signature(json.dumps(_prompt(ckpt="m.ckpt", nested_loras=["nested.safetensors"])))

    assert loras == ("nested.safetensors",)


def test_signature_is_order_independent():
    order = _fresh()
    one = order.signature(json.dumps(_prompt(ckpt="m.ckpt", loras=["a.st", "b.st"])))
    two = order.signature(json.dumps(_prompt(ckpt="m.ckpt", loras=["b.st", "a.st"])))

    assert one == two


def test_signature_survives_garbage():
    order = _fresh()
    assert order.signature("not json") == ((), (), ())
    assert order.signature(json.dumps([0, "pid"])) == ((), (), ())


def test_short_label():
    assert _short("subdir\\my_model.safetensors") == "my_model"


# ---------------------------------------------------------------- invert


def test_invert_reverses_everything():
    order = _fresh()
    ids = _seed([_prompt(ckpt="a.ckpt") for _ in range(4)])

    result = order.invert_order()

    assert _order() == list(reversed(ids))
    assert result["moved"] == 4
    assert order.queue.prefetch_calls == 1


# ---------------------------------------------------------------- grouping


def test_optimize_clusters_same_models_keeping_first_appearance():
    order = _fresh()
    ids = _seed(
        [
            _prompt(ckpt="a.ckpt"),
            _prompt(ckpt="b.ckpt"),
            _prompt(ckpt="a.ckpt"),
            _prompt(ckpt="c.ckpt"),
            _prompt(ckpt="b.ckpt"),
        ]
    )

    result = order.optimize_order()

    assert _order() == [ids[0], ids[2], ids[1], ids[4], ids[3]]
    assert result["groups"] == 3
    assert result["model_loads_before"] == 5
    assert result["model_loads_after"] == 3
    assert result["reordered"] > 0


def test_optimize_never_moves_the_head():
    order = _fresh()
    ids = _seed([_prompt(ckpt="a.ckpt"), _prompt(ckpt="b.ckpt"), _prompt(ckpt="b.ckpt")])

    order.optimize_order()

    assert _order()[0] == ids[0]
    assert order.queue.prefetch_calls == 0


def test_optimize_groups_by_lora_within_the_same_model():
    order = _fresh()
    ids = _seed(
        [
            _prompt(ckpt="a.ckpt", loras=["x.st"]),
            _prompt(ckpt="a.ckpt", loras=["y.st"]),
            _prompt(ckpt="a.ckpt", loras=["x.st"]),
        ]
    )

    result = order.optimize_order()

    assert _order() == [ids[0], ids[2], ids[1]]
    assert result["model_loads_before"] == 1  # same checkpoint throughout
    assert result["switches_before"] == 2
    assert result["switches_after"] == 1


def test_optimize_is_idempotent():
    order = _fresh()
    _seed([_prompt(ckpt="a.ckpt"), _prompt(ckpt="b.ckpt"), _prompt(ckpt="a.ckpt")])

    order.optimize_order()
    first = _order()
    second_result = order.optimize_order()

    assert _order() == first
    assert second_result["reordered"] == 0


def test_optimize_with_one_job_does_nothing():
    order = _fresh()
    _seed([_prompt(ckpt="a.ckpt")])

    assert order.optimize_order()["reordered"] == 0


# ---------------------------------------------------------------- submit order


def test_submit_order_defaults_to_fifo():
    assert _fresh().submit_order() == "FIFO"


def test_submit_order_reads_the_option():
    assert _fresh(_Options({"submit_order": "LIFO"})).submit_order() == "LIFO"
    assert _fresh(_Options({"submit_order": "lifo"})).submit_order() == "LIFO"
    assert _fresh(_Options({"submit_order": "nonsense"})).submit_order() == "FIFO"


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

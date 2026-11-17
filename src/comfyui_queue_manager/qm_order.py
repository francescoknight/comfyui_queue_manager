"""
Queue ordering features for ComfyUI Queue Manager.

On top of upstream Queue Manager this adds:
  * FIFO / LIFO submit mode (a newly queued job goes to the back or to the front),
  * manual reordering of pending jobs (up / down / top / bottom),
  * automatic grouping of pending jobs by the models / LoRAs they load, so that
    consecutive jobs reuse what is already in VRAM.

Ordering is expressed purely through the `queue.number` column of the existing
schema (lower number = executed earlier), so no migration is needed.
"""

import json
import os

from server import PromptServer

from .qm_db import get_conn, read_query, write_many
from .qm_log import qm_log

# Input names that carry the name of a heavyweight model file (several GB; the
# loads that are worth optimising for).
HEAVY_KEYS = {
    "ckpt_name",
    "checkpoint",
    "checkpoint_name",
    "unet_name",
    "model_name",
    "model_path",
    "diffusion_model",
    "diffusion_model_name",
    "gguf_name",
}

# Input names of lighter companion models. Still worth keeping together.
SUPPORT_KEYS = {
    "vae_name",
    "clip_name",
    "clip_name1",
    "clip_name2",
    "clip_name3",
    "clip_vision",
    "clip_vision_name",
    "text_encoder",
    "text_encoder_name",
    "control_net_name",
    "controlnet_name",
    "style_model_name",
    "upscale_model_name",
    "ipadapter_file",
}

# Values that mean "nothing selected".
EMPTY_VALUES = {"", "none", "null", "undefined", "no lora", "no_lora"}

MAX_WALK_DEPTH = 4


def _walk(key, value, out, depth=0):
    """Collect (input name, string value) pairs out of a node's inputs.

    Node links are encoded as ["<node_id>", <output_index>] and carry no file
    name, so they are skipped. Some custom nodes (power LoRA loaders and the
    like) nest their selections in dicts or lists, hence the recursion.
    """
    if isinstance(value, str):
        out.append((key, value))
        return

    if depth >= MAX_WALK_DEPTH:
        return

    if isinstance(value, dict):
        for k, v in value.items():
            _walk(k if isinstance(k, str) else key, v, out, depth + 1)
        return

    if isinstance(value, list):
        # ComfyUI node link, not a value
        if (
            len(value) == 2
            and isinstance(value[0], str)
            and isinstance(value[1], (int, float))
            and not isinstance(value[1], bool)
        ):
            return
        for v in value:
            _walk(key, v, out, depth + 1)


def _short(name):
    """Readable label for a model file name."""
    return os.path.splitext(os.path.basename(str(name).replace("\\", "/")))[0]


class QM_Order:
    def __init__(self, queue):
        self.queue = queue  # QM_Queue
        self.options = queue.queue_manager.options

    # =====================================================================
    # Submit order (FIFO / LIFO)
    # =====================================================================

    def submit_order(self):
        """Current submit mode: FIFO (default) or LIFO."""
        value = self.options.get("submit_order", "FIFO")
        return "LIFO" if str(value).upper() == "LIFO" else "FIFO"

    # =====================================================================
    # Model signature
    # =====================================================================

    def signature(self, prompt_json):
        """Return (heavy models, support models, loras) used by a stored prompt."""
        heavy, support, loras = set(), set(), set()

        try:
            item = json.loads(prompt_json)
            graph = item[2] if len(item) > 2 and isinstance(item[2], dict) else {}
        except Exception:
            return (), (), ()

        for node in graph.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue

            pairs = []
            for key, value in inputs.items():
                if isinstance(key, str):
                    _walk(key, value, pairs)

            for key, value in pairs:
                name = value.strip()
                if not name or name.lower() in EMPTY_VALUES or len(name) > 260:
                    continue

                k = key.lower()
                if "lora" in k:
                    loras.add(name)
                elif k in HEAVY_KEYS:
                    heavy.add(name)
                elif k in SUPPORT_KEYS:
                    support.add(name)

        return tuple(sorted(heavy)), tuple(sorted(support)), tuple(sorted(loras))

    def group_label(self, heavy, support, loras):
        parts = [_short(h) for h in heavy] or ["(no model)"]
        label = " + ".join(parts)
        if loras:
            label += " | %d LoRA: %s" % (len(loras), ", ".join(_short(x) for x in loras))
        if support:
            label += " | +%d" % len(support)
        return label

    # =====================================================================
    # Low level ordering helpers (always called with the native queue mutex held)
    # =====================================================================

    def _pending_ids(self):
        return [row[0] for row in read_query("SELECT id FROM queue WHERE status = 0 ORDER BY number, id")]

    def _renumber(self, ordered_ids):
        """Give the pending rows sequential numbers in the order provided.

        The block is allocated above the server's current job counter, so jobs
        queued afterwards (in FIFO mode) still land behind them, while LIFO jobs
        (negative numbers) still land in front of them.
        """
        if not ordered_ids:
            return 0

        server = PromptServer.instance
        base = max(int(server.number or 0), 0) + 1

        total = write_many(
            "UPDATE queue SET number = ? WHERE id = ? AND status = 0",
            [(base + i, db_id) for i, db_id in enumerate(ordered_ids)],
        )

        server.number = base + len(ordered_ids) - 1
        return total

    def _notify(self, payload=None):
        PromptServer.instance.queue_updated()
        PromptServer.instance.send_sync("queue-manager-queue-updated", payload or {"reordered": True})

    def invert_order(self):
        """Reverse the pending queue: the last job becomes the first one."""
        with self.queue.native_queue.mutex:
            order = self._pending_ids()
            if len(order) < 2:
                return {"moved": 0, "total": len(order), "reason": "not enough pending jobs"}

            new_order = list(reversed(order))

            self._renumber(new_order)
            get_conn().commit()
            self.queue.refresh_prefetch()

            qm_log.info("Queue inverted: %d job(s)", len(new_order))
            self._notify({"reordered": len(new_order), "inverted": True})

            return {"moved": len(new_order), "total": len(new_order)}

    def optimize_order(self):
        """Cluster pending jobs that load the same models / LoRAs.

        Groups are kept in order of first appearance (and so is the order of
        jobs inside a group), which keeps the queue close to the submit order
        and leaves the job that is about to run exactly where it is.
        """
        with self.queue.native_queue.mutex:
            items = []
            cursor = get_conn().execute("SELECT id, prompt FROM queue WHERE status = 0 ORDER BY number, id")
            for index, row in enumerate(cursor):
                heavy, support, loras = self.signature(row[1])
                items.append(
                    {
                        "id": row[0],
                        "index": index,
                        "heavy": heavy,
                        "sub": (support, loras),
                        "full": (heavy, support, loras),
                    }
                )

            if len(items) < 2:
                return {"reordered": 0, "total": len(items), "reason": "not enough pending jobs"}

            first_heavy, first_sub = {}, {}
            for it in items:
                first_heavy.setdefault(it["heavy"], it["index"])
                first_sub.setdefault((it["heavy"], it["sub"]), it["index"])

            ordered = sorted(
                items,
                key=lambda it: (first_heavy[it["heavy"]], first_sub[(it["heavy"], it["sub"])], it["index"]),
            )

            def switches(sequence):
                return sum(1 for a, b in zip(sequence, sequence[1:]) if a != b)

            before_full = switches([it["full"] for it in items])
            after_full = switches([it["full"] for it in ordered])
            before_heavy = switches([it["heavy"] for it in items])
            after_heavy = switches([it["heavy"] for it in ordered])

            old_order = [it["id"] for it in items]
            new_order = [it["id"] for it in ordered]

            groups = []
            for it in ordered:
                label = self.group_label(it["heavy"], *it["sub"])
                if groups and groups[-1]["label"] == label:
                    groups[-1]["jobs"] += 1
                else:
                    groups.append({"label": label, "jobs": 1})

            result = {
                "total": len(items),
                "groups": len(groups),
                "group_list": groups[:40],
                "switches_before": before_full,
                "switches_after": after_full,
                "model_loads_before": before_heavy + 1,
                "model_loads_after": after_heavy + 1,
                "reordered": 0,
            }

            if new_order == old_order:
                result["reason"] = "queue is already grouped"
                return result

            self._renumber(new_order)
            get_conn().commit()

            result["reordered"] = sum(1 for a, b in zip(old_order, new_order) if a != b)

            if new_order[0] != old_order[0]:
                self.queue.refresh_prefetch()

            qm_log.info(
                "Queue optimised: %d job(s) in %d group(s); model loads %d -> %d",
                len(items),
                len(groups),
                result["model_loads_before"],
                result["model_loads_after"],
            )
            self._notify({"reordered": result["reordered"], "optimized": True})

            return result

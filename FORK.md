# Fork additions

This is a private fork of [QuietNoise/comfyui_queue_manager](https://github.com/QuietNoise/comfyui_queue_manager),
based on upstream **v0.2.1**, with a handful of features added on top. Everything upstream
does is untouched: drag-and-drop reordering, selection, renaming, archive, gallery and the
rest behave exactly as documented in [README.md](README.md).

## What this fork adds

### FIFO / LIFO submit mode
**⋮ menu → "New jobs: FIFO / LIFO"**. In FIFO (the default, and upstream's behaviour) a new
job is appended at the end of the queue. In LIFO it jumps to the front and runs next.

LIFO gives the new job a negative `number`, the same scheme the native "front" / shift+Run
already uses, so it sorts before every pending job; since the server's job counter only
grows, each new LIFO job also sorts before the previous one. The setting is stored as the
`submit_order` option and survives restarts.

### Group by model / LoRA
**⋮ menu → "Group by model / LoRA"**. Reorders the pending queue so that jobs loading the
same checkpoint / UNET, VAE, CLIP and LoRA set run back to back, which saves reloading
several GB of weights between jobs.

Grouping is *stable*: groups are kept in order of first appearance, and so is the order of
jobs inside a group. The job that is about to run never moves, and nothing is pushed to the
back of the queue arbitrarily. The result reports how many model loads it saved, e.g.
`37 job(s) in 6 group(s) - model loads 31 -> 6`.

The signature of a job is read from the stored prompt: input names containing `lora` count
as LoRAs, `ckpt_name` / `unet_name` / `model_name` and friends as heavyweight models,
`vae_name` / `clip_name` and friends as companions. Node links and prompt text are ignored,
and nested widgets (power LoRA loaders) are walked.

### Invert queue order
**⋮ menu → "Invert queue order"**. Reverses the pending queue: the last job becomes the first.

### Queued / Finished columns
The **Queue** tab shows when each job was queued, the **Completed** tab when each job
finished. Timestamps travel as epoch seconds because SQLite's `CURRENT_TIMESTAMP` is UTC
without a zone marker, which a browser would otherwise read as local time.

### Elapsed time and completion estimate
The running job shows `running for 12m 03s — ~65% of 18m 20s`. The estimate is the median
execution time of the last 20 completed jobs of the same workflow (cached for a minute).
Counting executed nodes, which is what the progress bar does, says very little about video
jobs where a single node takes nearly all of the time. With no completed run of that
workflow yet, only the elapsed time is shown.

### Open with the default player / show in folder
Two buttons on every completed job: open the first output file with the system's default
application, or reveal it in the output folder. Both run **on the machine running ComfyUI**,
not on the machine running the browser.

### Delete a job for good
The red bin button on a completed job deletes the job *and its output files*. Files go to
the **Recycle Bin**; a failed Recycle Bin operation never falls back to deleting for good.
A confirmation dialog lists what will be deleted. A running job is refused (cancel it first).

Only files that are listed in that job's own `outputs` metadata **and** resolve inside
ComfyUI's output directory can ever be touched: a path escaping the output folder is
refused and logged. The job's entry in ComfyUI's history is removed as well.

## New API endpoints

| Endpoint | Body | Does |
|---|---|---|
| `POST /queue_manager/invert` | – | reverse the pending queue |
| `POST /queue_manager/optimize` | – | group pending jobs by model / LoRA |
| `POST /queue_manager/open_file` | `{id, filename, subfolder}` | open an output file with the default application |
| `POST /queue_manager/purge_preview` | `{items: [db_id]}` | what a purge would delete |
| `POST /queue_manager/purge` | `{items: [db_id], confirm: true, permanent?: false}` | delete jobs and their files |

`submit_order` (`"FIFO"` / `"LIFO"`) is a new value for the existing
`GET|POST /queue_manager/options` endpoint.

## One upstream fix carried here

`_pending_prompt` compared the takeover client's timestamp against `updated_at`. Reordering
a job rewrites its `number`, which bumps `updated_at`, so a reordered job silently lost the
takeover client. This fork compares against `created_at` (when the job was queued) instead.
Worth sending upstream.

## Layout

Almost everything added lives in two new modules, so that rebasing onto a newer upstream
stays easy:

- `src/comfyui_queue_manager/qm_order.py` — FIFO/LIFO, invert, grouping
- `src/comfyui_queue_manager/qm_files.py` — open with default application, purge with files

The hooks inside `qm_queue.py` and `qm_server.py` are deliberately small: wiring in
`__init__`, the LIFO branch in `queue_put`, `started_at` bookkeeping, the timestamp columns
in `get_current_queue`, and the new routes.

## Tests

```
python tests/test_qm_order.py
python tests/test_qm_files.py
python tests/test_qm_queue_order.py
```

They stub ComfyUI's `server`, `execution` and `folder_paths` modules and use a throwaway
sqlite database, so they run anywhere, without ComfyUI. `tests/test_qm_files.py` is the one
that matters most: it checks that an absolute path or a `../../..` subfolder in a job's
metadata can never lead to a file outside the output folder being deleted.

## Updating from upstream

```bash
git remote add upstream https://github.com/QuietNoise/comfyui_queue_manager
git fetch upstream --tags
git rebase <new tag>
```

Then rebuild the GUI (`cd src/gui && npm install && npm run build`) and rerun the tests.
The built bundle in `web/.gui` is committed on purpose: installing from git does not run npm.

// `src/gui/app/components/QueueItemRow.jsx`
"use client";

import React, { memo, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { apiCall, msgLoadWorkflow } from "../internals/functions";
import { AppContext } from "../internals/app-context";
import { MediaItem } from "../components/MediaItem";
import DragIndicatorIcon from "@mui/icons-material/DragIndicator";
import {useSelectionStore} from "../stores/selectionStore";
import PlayArrowOutlinedIcon from "@mui/icons-material/PlayArrowOutlined";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import PlayCircleOutlineIcon from "@mui/icons-material/PlayCircleOutline";
import DeleteForeverIcon from "@mui/icons-material/DeleteForever";
import { MediaOutputs } from "../models/MediaOutputs";
import { LoaderSpinner } from "../components/LoaderSpinner";
import {useAppStore} from "@/app/stores/appStore";
import { QueueItemName } from "./QueueItemName";
import {withWorkflowFilter} from "../internals/workflowFilters.mjs";
import {queueWorkflow} from "../internals/queueMetadata.mjs";

const two = (n) => String(n).padStart(2, "0");

function formatStamp(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${two(d.getDate())}/${two(d.getMonth() + 1)} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${two(m)}m ${two(s % 60)}s` : `${m}m ${two(s % 60)}s`;
}

// Ticks in its own component so the whole row does not re-render every second.
function RunningFor({ startedTs, estimate }) {
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, []);

  if (!startedTs) return null;

  const elapsed = Math.max(0, now - startedTs);
  const percent = estimate > 0 ? Math.min(99, Math.round((elapsed / estimate) * 100)) : null;

  return (
    <div
      className="running-for"
      title={estimate > 0
        ? `Estimate from the median of recent runs of this workflow (${formatDuration(estimate)})`
        : "No completed run of this workflow yet, so no estimate"}
    >
      running for {formatDuration(elapsed)}
      {percent !== null ? ` — ~${percent}% of ${formatDuration(estimate)}` : ""}
    </div>
  );
}

export const QueueItemRow = memo(
  function QueueItemRow({
    item,
    className,
    loader,
    index,
    mode,
    info,

    route,
    thumbMode,
    galleryOptions,
    filters,
    workflowFilterMode,
    selecting,
    selected,
    reorderBusy,
    dragging,
    reorderAnchor,
  }) {
    const { onMediaItemClick, fetchQueueItems, refreshQueueItems } = useContext(AppContext);

    const renaming = useSelectionStore((state) => !!state.rename);
    const dbId = item?.[3]?.db_id;
    const workflow = useMemo(() => queueWorkflow(item), [item]);
    const createdTs = item?.[3]?.created_ts;
    const finishedTs = item?.[3]?.finished_ts;
    const startedTs = item?.[3]?.started_ts;
    const estimate = item?.[3]?.estimated_duration;
    const selectable = !!dbId && mode !== "running";
    const canReorder = route === "queue" || route === "archive";

    const mediaOutputs = useMemo(() => {
      if (item?.[3]?.outputs && route === "completed") {
        return new MediaOutputs(item[3], galleryOptions);
      }
      return null;
    }, [item, route, galleryOptions]);

    const cancelQueueItem = useCallback(async () => {
      const cancelRoute = mode === "running" ? "interrupt" : "queue";
      await apiCall(`api/${cancelRoute}`, { delete: [item[1]] });
    }, [mode, item]);

    const loadQueueItem = useCallback(() => {
      if (workflow) {
        msgLoadWorkflow(workflow, item[0]);
      }
    }, [workflow, item]);

    const archiveQueueItem = useCallback(async () => {
      await apiCall(`queue_manager/archive`, {
        archive: [dbId],
        front: useAppStore.getState().shiftDown === true,
      });
    }, [dbId]);

    const playItem = useCallback(async () => {
      const { shiftDown, clientId } = useAppStore.getState();

      await apiCall(`queue_manager/play`, {
        items: [dbId],
        front: shiftDown === true,
        clientId,
      });
    }, [dbId]);

    const firstFile = useMemo(() => {
      const files = mediaOutputs?.files;
      if (files && files.length) return files[0];
      return mediaOutputs?.cover ?? null;
    }, [mediaOutputs]);

    const openInPlayer = useCallback(async () => {
      if (!firstFile) return;
      try {
        await apiCall(`queue_manager/open_file`, {
          id: dbId,
          filename: firstFile.filename,
          subfolder: firstFile.subfolder ?? "",
        });
      } catch (e) {
        window.alert("Could not open the file. It may have been moved or deleted.");
      }
    }, [dbId, firstFile]);

    const openFolder = useCallback(async () => {
      if (!firstFile) return;
      const args = new URLSearchParams({
        id: String(dbId),
        filename: firstFile.filename,
        subfolder: firstFile.subfolder ?? "",
      });
      await apiCall(`queue_manager/open_location?${args.toString()}`, null, "GET");
    }, [dbId, firstFile]);

    const purgeJob = useCallback(async () => {
      let preview = null;
      try {
        preview = await apiCall(`queue_manager/purge_preview`, { items: [dbId] });
      } catch (e) {
        window.alert("Could not read what would be deleted.");
        return;
      }

      const job = preview?.jobs?.[0];
      if (!job) return;

      if (job.running) {
        window.alert("This job is running. Cancel it first, then delete it.");
        return;
      }

      const names = job.files.slice(0, 10).join("\n");
      const more = job.total_files > 10 ? `\n...and ${job.total_files - 10} more` : "";
      const confirmed = window.confirm(
        `Delete "${job.name || "this job"}" for good?\n\n` +
        `${job.total_files} file(s) go to the Recycle Bin and the job is removed from the queue. ` +
        `This cannot be undone from Queue Manager.\n\n${names}${more}`
      );

      if (!confirmed) return;

      try {
        const result = await apiCall(`queue_manager/purge`, { items: [dbId], confirm: true });
        if (result?.failed_files) {
          window.alert(`${result.failed_files} file(s) could not be deleted.`);
        }
      } catch (e) {
        window.alert("Delete failed.");
      }
    }, [dbId]);

    const filterByWorkflow = useCallback(() => {
      if (!workflow || selecting) return;

      const nextFilters = withWorkflowFilter(filters, {id: workflow.id, name: workflow.workflow_name ?? ""}, workflowFilterMode);
      if (nextFilters === filters) return;

      fetchQueueItems({
        page: 0,
        filters: nextFilters,
        reload: true,
      });
    }, [fetchQueueItems, filters, workflow, workflowFilterMode, selecting]);

const executionTimeLabel = useMemo(() => {
  const t = item?.[3]?.execution_time;
  if (t == null) return null;

  const rawSeconds = Number(t);
  if (!Number.isFinite(rawSeconds) || rawSeconds < 0) return null;

  const totalSeconds = rawSeconds >= 60 ? Math.round(rawSeconds) : rawSeconds;

  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  const secondsLabel = rawSeconds >= 60 ? `${seconds}s` : `${seconds.toFixed(2)}s`;

  if (days > 0) return ` ${days}d ${hours}h ${minutes}m ${secondsLabel}`;
  if (hours > 0) return ` ${hours}h ${minutes}m ${secondsLabel}`;
  if (minutes > 0) return ` ${minutes}m ${secondsLabel}`;
  return ` ${rawSeconds.toFixed(2)}s`;
}, [item?.[3]?.execution_time]);

    const showCover =
      route === "completed" &&
      thumbMode === "cover" &&
      (galleryOptions.ShowImages || galleryOptions.ShowVideos);

    const showGrid =
      item?.[3]?.total_files > 0 &&
      route === "completed" &&
      thumbMode === "grid" &&
      (galleryOptions.ShowImages || galleryOptions.ShowVideos);

    const rowIndex =
      index === undefined || !info ? "" : index + 1 + info.page * info.page_size;

    return (
      <>
        {/*
        *
        * Queue Item Details
        *
        */}
        <tr className={(className ?? "") + (selected ? " selected" : "") + (dragging ? " reorder-moving" : "")}
            data-reorder-anchor={reorderAnchor}
            data-reorder-id={canReorder && selectable ? dbId : undefined}
            data-selection-id={selectable ? dbId : undefined}
            aria-selected={selecting && selectable ? selected : undefined}>
          <td className="px-3 py-1 serial">
            <span>{rowIndex}</span>
            {loader ? <LoaderSpinner /> : null}
          </td>

          {/* Thumbnail in Cover mode */}
          {showCover ? (
            <td className="px-3 py-1 cover">
              {mediaOutputs?.cover ? (
                <MediaItem
                  file={mediaOutputs.cover}
                  controls={false}
                  autoplay={false}
                  onClick={() => onMediaItemClick({ dbID: dbId, fileIndex: 0 })}
                  className="play-button"
                  title="Open gallery"
                />
              ) : null}
            </td>
          ) : null}

          {/* Workflow Name */}
          <td className="px-3 py-1 text-left name">
            <div className="name-cell">
              {mediaOutputs && item[3].total_files > 0 ? (
                <span
                  className="total shiny-button"
                  title={`Total file outputs: ${mediaOutputs.total}`}
                  onClick={() => onMediaItemClick({ dbID: dbId, fileIndex: 0 })}
                >
                  {mediaOutputs.total}
                </span>
              ) : null}

              <QueueItemName
                name={workflow?.workflow_name ?? ""}
                dbId={dbId}
                selecting={selecting}
                route={route}
                canRename={!!dbId && mode !== "running" && (route === "queue" || route === "archive")}
                onFilter={filterByWorkflow}
                filterTitle={workflowFilterMode === "workflow_name" ? "Filter view by this exact workflow name" : "Filter view by the workflow"}
                onRenamed={refreshQueueItems}
              />
            </div>
            {mode === "running" || mode === "external" ? (
              <RunningFor startedTs={startedTs} estimate={estimate} />
            ) : null}
          </td>

          {route === "queue" ? (
            <td
              className="px-3 py-1 text-left queued-at"
              title={createdTs ? new Date(createdTs * 1000).toLocaleString() : ""}
            >
              {formatStamp(createdTs)}
            </td>
          ) : null}

          {route === "completed" ? (
            <td
              className="px-3 py-1 text-left finished-at"
              title={finishedTs ? new Date(finishedTs * 1000).toLocaleString() : ""}
            >
              {formatStamp(finishedTs)}
            </td>
          ) : null}

          {route === "completed" &&
            <td className={'meta-info'}>
              {executionTimeLabel ? (
                <div className="execution-time" title="Execution time">
                  {executionTimeLabel}
                </div>
              ) : null}
            </td>
          }


          {/* Item Actions */}
          <td className="px-3 py-1 text-right actions">
            <div style={{ justifyContent: "flex-end" }}  className="buttons">
              <button
                className="delete red-button shiny-button"
                onClick={cancelQueueItem}
                title="Delete workflow from queue"
                data-selection-ignore
              >
                Delete
              </button>

              <button
                className="load green-button shiny-button"
                onClick={loadQueueItem}
                title={workflow.api_prompt ? "Load API prompt (automatic node layout)" : "Load workflow"}
                data-selection-ignore
              >
                Load
              </button>

              {route === "queue" && mode !== "running" ? (
                <button
                  className="archive yellow-button shiny-button"
                  onClick={archiveQueueItem}
                  title="Move to the archive (Shift: add at the start)"
                  data-selection-ignore
                >
                  Archive
                </button>
              ) : null}

              {route === "archive" ? (
                <button
                  className="run blue-button shiny-button"
                  onClick={playItem}
                  title="Move to queue"
                  data-selection-ignore
                >
                  <PlayArrowOutlinedIcon fontSize="small" />
                  Run
                </button>
              ) : null}

              {route === "completed" && firstFile ? (
                <>
                  <button
                    className="open-player green-button shiny-button"
                    onClick={openInPlayer}
                    title="Open with the system's default player"
                    data-selection-ignore
                  >
                    <PlayCircleOutlineIcon fontSize="small" />
                  </button>
                  <button
                    className="open-folder yellow-button shiny-button"
                    onClick={openFolder}
                    title="Show the file in the output folder"
                    data-selection-ignore
                  >
                    <FolderOpenIcon fontSize="small" />
                  </button>
                </>
              ) : null}

              {route === "completed" ? (
                <button
                  className="purge red-button shiny-button"
                  onClick={purgeJob}
                  title="Delete the job and its output files (Recycle Bin)"
                  data-selection-ignore
                >
                  <DeleteForeverIcon fontSize="small" />
                </button>
              ) : null}

              {route === "completed" ? (
                <button

                  className="view violet-button shiny-button"
                  onClick={() => onMediaItemClick({ dbID: dbId, fileIndex: 0 })}
                  title="View outputs in gallery"
                  data-selection-ignore
                >
                  View
                </button>
              ) : null}
              {selecting && <input type="checkbox" className="row-selection"
                aria-label={`Select ${workflow?.workflow_name || "job"}`}
                title={selectable ? "Select job (Shift: range, Alt: deselect)" : "Running jobs cannot be selected"}
                disabled={!selectable} checked={selected} onChange={() => {}} />}
            </div>
          </td>
          {canReorder && <td className="reorder-column" data-selection-ignore>
            <button type="button" className="reorder-handle" data-reorder-handle={selectable ? dbId : undefined}
              disabled={!selectable || reorderBusy || renaming}
              aria-label={`Reorder ${workflow?.workflow_name || "job"}`}
              title={!selectable ? "Running jobs cannot be reordered" : renaming ? "Finish renaming first" :
                selecting ? "Drag to move the selected group (or use Up/Down arrow keys)" : "Drag to reorder (or use Up/Down arrow keys)"}>
              <DragIndicatorIcon fontSize="small" />
            </button>
          </td>}
        </tr>

        {/*
        *
        * Queue Item Outputs
        *
        */}
        {showGrid ? (
          <tr className={"dark:odd:bg-neutral-900 odd:bg-neutral-100 gallery" + (selected ? " selected" : "")}
              data-selection-id={selectable ? dbId : undefined}>
            <td colSpan={100} className="px-3 py-1">
              <div className="flex flex-wrap gap-2 items">
                {mediaOutputs?.files?.length
                  ? mediaOutputs.files.map((file, fileIndex) => (
                      <MediaItem
                        key={`${file.filename}-${file.subfolder}`}
                        file={file}
                        autoplay={false}
                        onClick={() => onMediaItemClick({ dbID: dbId, fileIndex })}
                        title="Open gallery"
                      />
                    ))
                  : null}
              </div>
            </td>
          </tr>
        ) : null}
      </>
    );
  },
  (prev, next) => {
    // Custom compare: skip re\-render if the item identity changes but the content  hasnâ€™t.
    const prevId = prev.item?.[3]?.db_id;
    const nextId = next.item?.[3]?.db_id;
    if (prevId !== nextId) return false;

    return (
      prev.item?.[3]?.created_ts === next.item?.[3]?.created_ts &&
      prev.item?.[3]?.finished_ts === next.item?.[3]?.finished_ts &&
      prev.item?.[3]?.started_ts === next.item?.[3]?.started_ts &&
      prev.item?.[3]?.estimated_duration === next.item?.[3]?.estimated_duration &&
      prev.loader === next.loader &&
      prev.index === next.index &&
      prev.mode === next.mode &&
      prev.route === next.route &&
      prev.thumbMode === next.thumbMode &&
      prev.galleryOptions === next.galleryOptions &&
      prev.filters === next.filters &&
      prev.workflowFilterMode === next.workflowFilterMode &&
      prev.selecting === next.selecting &&
      prev.selected === next.selected &&
      prev.reorderBusy === next.reorderBusy &&
      prev.dragging === next.dragging &&
      prev.reorderAnchor === next.reorderAnchor &&
      prev.info?.page === next.info?.page &&
      prev.info?.page_size === next.info?.page_size &&
      prev.item?.[0] === next.item?.[0] &&
      queueWorkflow(prev.item).workflow_name === queueWorkflow(next.item).workflow_name &&
      queueWorkflow(prev.item).id === queueWorkflow(next.item).id
    );
  }
);

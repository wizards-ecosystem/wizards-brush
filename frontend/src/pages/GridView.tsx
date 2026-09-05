import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Asset, GridResult } from "../api/types";
import { AssetModal } from "../components/AssetModal";
import { Icon } from "../components/icons";
import { Button, EmptyState, PageHeader, Spinner } from "../components/ui";
import { useStore } from "../store/useStore";

/** Interactive result grid for an X/Y sweep: sticky axis labels, live cells. */
export function GridView() {
  const { groupId } = useParams();
  const jobs = useStore((s) => s.jobs);
  const [grid, setGrid] = useState<GridResult | null>(null);
  const [open, setOpen] = useState<Asset | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    if (!groupId) return;
    api
      .grid(groupId)
      .then((g) => {
        setGrid(g);
        setError("");
      })
      .catch((e) => setError(String(e)));
  }, [groupId]);

  useEffect(() => {
    load();
  }, [load]);

  const pending = useMemo(
    () => (grid?.cells || []).some((c) => c.status === "queued" || c.status === "running"),
    [grid],
  );

  // Re-fetch when any of this grid's jobs completes (WS keeps `jobs` fresh);
  // fall back to a slow poll while cells are pending.
  const cellJobStatuses = (grid?.cells || []).map((c) => jobs[c.job_id]?.status).join(",");
  useEffect(() => {
    if (!grid) return;
    // Debounce: a large sweep finishing cell-by-cell would otherwise fire one
    // full grid GET per cell transition on top of the slow poll below.
    const t = setTimeout(load, 500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cellJobStatuses]);
  useEffect(() => {
    if (!pending) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [pending, load]);

  if (error)
    return (
      <div className="page-pad flex min-h-full items-center justify-center">
        <EmptyState
          icon="alert"
          title="Grid not found"
          description={error}
          action={
            <Button icon="refresh" onClick={load}>
              Try again
            </Button>
          }
        />
      </div>
    );
  if (!grid)
    return (
      <div className="page-pad flex items-center gap-3 text-sm text-muted">
        <Spinner /> Loading result matrix…
      </div>
    );

  const xs = grid.x_values;
  const ys = grid.y_values?.length ? grid.y_values : [""];
  const cellMap = new Map(grid.cells.map((c) => [`${c.x}|${c.y}`, c]));
  const cellFor = (x: string, y: string) => cellMap.get(`${x}|${y}`);
  const doneAssets = grid.cells.map((c) => c.asset).filter(Boolean) as Asset[];

  return (
    <div className="page-pad h-full overflow-auto">
      <PageHeader
        kicker="Parameter study"
        title="Result grid"
        description={
          <span className="technical text-xs">
            {grid.kind} · X: {grid.x_param}
            {grid.y_param ? ` · Y: ${grid.y_param}` : ""}
          </span>
        }
        actions={
          pending ? (
            <span className="flex items-center gap-2 border-l-2 border-warn pl-3 text-xs text-warn">
              <Spinner /> Generating cells
            </span>
          ) : (
            <span className="flex items-center gap-2 text-xs text-ok">
              <Icon name="check" size={15} /> Study complete
            </span>
          )
        }
      />

      <div
        className="mt-6 inline-grid gap-2 border-b border-r border-edge bg-edge p-px"
        style={{ gridTemplateColumns: `auto repeat(${xs.length}, minmax(140px, 1fr))` }}
      >
        <div />
        {xs.map((x) => (
          <div
            key={`x${x}`}
            className="technical sticky top-0 z-10 truncate bg-panel px-2 py-2 text-center text-[10px] text-muted"
            title={`${grid.x_param} = ${x}`}
          >
            {x}
          </div>
        ))}
        {ys.map((y) => (
          <FragmentRow
            key={`row${y}`}
            y={y}
            xs={xs}
            yParam={grid.y_param}
            cellFor={cellFor}
            jobs={jobs}
            onOpen={setOpen}
          />
        ))}
      </div>

      {open && (
        <AssetModal asset={open} onClose={() => setOpen(null)} list={doneAssets} onNavigate={setOpen} />
      )}
    </div>
  );
}

function FragmentRow({
  y,
  xs,
  yParam,
  cellFor,
  jobs,
  onOpen,
}: {
  y: string;
  xs: string[];
  yParam?: string;
  cellFor: (x: string, y: string) => import("../api/types").GridCell | undefined;
  jobs: Record<number, import("../api/types").Job>;
  onOpen: (a: Asset) => void;
}) {
  return (
    <>
      <div
        className="technical sticky left-0 z-[5] flex max-w-32 items-center truncate bg-panel px-2 text-[10px] text-muted"
        title={yParam ? `${yParam} = ${y}` : ""}
      >
        {y}
      </div>
      {xs.map((x) => {
        const cell = cellFor(x, y);
        const live = cell ? jobs[cell.job_id] : undefined;
        const status = live?.status ?? cell?.status;
        return (
          <div key={`c${x}|${y}`} className="media-tile relative aspect-square min-w-[140px] bg-bg">
            {cell?.asset ? (
              <button className="w-full h-full" onClick={() => onOpen(cell.asset!)}>
                <img
                  src={cell.asset.thumb_url || cell.asset.url}
                  alt={`${x}/${y}`}
                  className="w-full h-full object-cover"
                  loading="lazy"
                />
              </button>
            ) : (
              <div className="technical flex h-full w-full flex-col items-center justify-center gap-2 text-[10px] text-muted">
                {status === "error" || status === "canceled" ? (
                  <span className="text-danger">{status}</span>
                ) : (
                  <>
                    <Spinner />
                    {live?.progress ? `${Math.round(live.progress * 100)}%` : status || "…"}
                  </>
                )}
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}

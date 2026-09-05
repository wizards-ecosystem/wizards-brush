import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Asset, Job } from "../api/types";
import { AssetModal } from "../components/AssetModal";
import { LoadStatus } from "../components/LoadStatus";
import { Icon } from "../components/icons";
import { ProgressBar } from "../components/ProgressBar";
import { Button, EmptyState, IconButton, PageHeader } from "../components/ui";
import { HIDE_KEYS, isRerunnable, laneOf, prefillableParams } from "../lib/generators";
import { useStore } from "../store/useStore";

const FILTERS = ["all", "running", "done", "error"] as const;

function fmtDuration(job: Job): string {
  if (!job.started_at || !job.finished_at) return "";
  const s = (new Date(job.finished_at).getTime() - new Date(job.started_at).getTime()) / 1000;
  if (s < 0) return "";
  return s >= 90 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;
}

export function Queue() {
  const jobs = useStore((s) => s.jobs);
  const previews = useStore((s) => s.previews);
  const assets = useStore((s) => s.assets);
  const loads = useStore((s) => s.loads);
  const specs = useStore((s) => s.specs);
  const refreshAssets = useStore((s) => s.refreshAssets);
  const refreshJobs = useStore((s) => s.refreshJobs);
  const toast = useStore((s) => s.toast);
  const navigate = useNavigate();
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const [selId, setSelId] = useState<number | null>(null);
  const [open, setOpen] = useState<Asset | null>(null);
  const [busy, setBusy] = useState(false);
  const [showErr, setShowErr] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  /** job id -> why it, and not the head of the list, runs next. */
  const [nextUp, setNextUp] = useState<Record<number, string | null>>({});

  const list = useMemo(() => {
    // Partitioned, not one comparator: a mixed queued/finished comparator is not
    // a total order (priority vs id cycles), and Array.sort output becomes
    // arbitrary. Queued float on top in run order; the rest newest-first.
    const values = Object.values(jobs).filter((j) => j.status !== "canceled");
    const queued = values
      .filter((j) => j.status === "queued")
      .sort((a, b) => {
        const pa = a.priority ?? 0,
          pb = b.priority ?? 0;
        if (pa !== pb) return pb - pa;
        return a.id - b.id;
      });
    const rest = values.filter((j) => j.status !== "queued").sort((a, b) => b.id - a.id);
    const all = [...queued, ...rest];
    if (filter === "all") return all;
    if (filter === "running") return all.filter((j) => j.status === "running" || j.status === "queued");
    return all.filter((j) => j.status === filter);
  }, [jobs, filter]);

  const sel = selId != null ? jobs[selId] : null;
  const selImage = sel
    ? (assets ?? []).find((asset) => asset.kind === "image" && asset.job_id === sel.id)
    : null;

  // A just-queued rerun isn't in `jobs` yet — remember it so the auto-select
  // below doesn't snap back to another job before its WS event arrives.
  const pendingSel = useRef<number | null>(null);

  // Auto-select the newest job, and re-select when the current pick is filtered out.
  useEffect(() => {
    if (pendingSel.current != null) {
      const pid = pendingSel.current;
      if (list.some((j) => j.id === pid)) {
        setSelId(pid);
        pendingSel.current = null;
        return;
      }
      // Job arrived but the active filter hides it (e.g. rerun while filtered to
      // "error") — stop waiting or auto-select would stay disabled all session.
      if (jobs[pid]) pendingSel.current = null;
      else return;
    }
    if (list.length && !list.some((j) => j.id === selId)) setSelId(list[0].id);
  }, [list, selId, jobs]);

  // The lane may run a later job first when its model is already loaded. The
  // list above is in queue order, so without this the page would confidently
  // display an order the scheduler is not going to follow.
  //
  // Polled rather than pushed: it is a derived hint, not a state transition, and
  // it changes only when the queue or the resident model does. Only while there
  // is something queued — an idle queue has no next pick to show.
  const queuedCount = useMemo(() => Object.values(jobs).filter((j) => j.status === "queued").length, [jobs]);
  useEffect(() => {
    // Nothing queued: no poll, and no need to clear either — the badge renders
    // only for a job still in `queued`, so a stale mark cannot show through.
    if (queuedCount === 0) return;
    let alive = true;
    const load = async () => {
      try {
        const r = await api.nextPicks();
        if (!alive) return;
        const marks: Record<number, string | null> = {};
        Object.values(r.lanes).forEach((p) => {
          marks[p.job_id] = p.reason;
        });
        setNextUp(marks);
      } catch {
        /* a hint that fails to load is not worth surfacing */
      }
    };
    void load();
    const t = setInterval(load, 4000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [queuedCount]);

  useEffect(() => {
    if (selId != null) {
      document.querySelector(`[data-jobid="${selId}"]`)?.scrollIntoView({ block: "nearest" });
    }
  }, [selId]);

  // Arrow-key navigation through the job list.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      const i = list.findIndex((j) => j.id === selId);
      if (i < 0) return;
      const next = list[i + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) {
        e.preventDefault();
        setSelId(next.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [list, selId]);

  const rerun = async (job: Job, reseed: boolean) => {
    setBusy(true);
    try {
      const r = await api.rerun(job.id, reseed);
      pendingSel.current = r.job_id!;
      setSelId(r.job_id!);
      // A failed Retry supersedes its old row. Refresh immediately as well as
      // listening for the websocket event, so a briefly disconnected tab never
      // leaves an obsolete failure visible.
      if (r.replaced_job_id) await refreshJobs();
      toast(r.model_warning || `Queued job #${r.job_id}`, r.model_warning ? "info" : "success");
    } catch (e) {
      toast(`Rerun failed: ${e}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const move = async (job: Job, dir: "up" | "down") => {
    try {
      await api.moveJob(job.id, dir);
      refreshJobs();
    } catch (e) {
      toast(`Reorder failed: ${e}`, "error");
    }
  };

  const runNext = async (job: Job) => {
    try {
      await api.prioritize(job.id);
      refreshJobs();
      toast(`#${job.id} will run next`, "success");
    } catch (e) {
      toast(`Prioritize failed: ${e}`, "error");
    }
  };

  const editQueued = (job: Job) => {
    const spec = specs.find((s) => s.id === job.kind);
    if (!spec) return;
    if (spec.needs_image || (spec.image_inputs?.length ?? 0) > 0) {
      toast("Re-add the input image after editing", "info");
    }
    navigate(`/g/${job.kind}`, {
      state: { prefillValues: prefillableParams(job.params), editOfJob: job.id },
    });
  };

  const openResult = async (job: Job) => {
    const ids = job.result?.asset_ids || [];
    if (!ids.length) return;
    try {
      setOpen(await api.asset(ids[0]));
    } catch {
      toast("Result asset no longer exists", "info");
    }
  };

  return (
    <div className="grid h-full grid-cols-1 bg-bg lg:grid-cols-[minmax(0,1fr)_390px]">
      <div className="overflow-y-auto page-pad">
        <PageHeader
          kicker="Run ledger"
          title="Queue"
          meta={`${list.length} shown`}
          description="Follow work across both compute lanes, change queued order, or reuse a finished run."
          actions={
            <div
              className="flex gap-1 border border-edge bg-panel p-1"
              role="group"
              aria-label="Queue status filter"
            >
              {FILTERS.map((f) => (
                <button
                  key={f}
                  className={`seg min-w-16 ${filter === f ? "seg-active" : ""}`}
                  onClick={() => setFilter(f)}
                  aria-pressed={filter === f}
                >
                  {f}
                </button>
              ))}
            </div>
          }
        />
        {list.length === 0 ? (
          <div className="flex min-h-[420px] items-center justify-center">
            <EmptyState
              icon="queue"
              title="No runs in this view"
              description={
                filter === "all"
                  ? "New generations will enter the ledger as soon as they are submitted."
                  : "Choose another status to inspect the rest of the ledger."
              }
            />
          </div>
        ) : (
          <div className="mt-6 divide-y divide-edge border-y border-edge">
            {list.map((j) => (
              <div
                key={j.id}
                data-jobid={j.id}
                className={`relative cursor-pointer py-3 pl-4 pr-3 transition-colors ${selId === j.id ? "bg-panel2" : "hover:bg-panel/70"}`}
              >
                <button
                  className="absolute inset-0 z-[1]"
                  aria-label={`Inspect job ${j.id}`}
                  aria-pressed={selId === j.id}
                  onClick={() => {
                    setSelId(j.id);
                    setDetailOpen(true);
                  }}
                />
                {selId === j.id && <span className="absolute inset-y-2 left-0 w-[3px] bg-accent" />}
                <div className="pointer-events-none relative z-0">
                  <ProgressBar job={j} />
                  {/* The lane runs this one next. Shown only when it is NOT the
                      top of the list, because that is the only case where the
                      displayed order and the actual order disagree — and the
                      reason is what makes the reordering legible rather than
                      arbitrary. Reprioritising remains the override. */}
                  {j.status === "queued" && j.id in nextUp && nextUp[j.id] && (
                    <div className="technical mt-1 flex items-center gap-1 text-[10px] text-accent">
                      <Icon name="arrow-up" size={11} />
                      Runs next — {nextUp[j.id]}
                    </div>
                  )}
                </div>
                {j.status === "queued" && (
                  <div className="absolute right-2 top-2 z-[2] flex gap-1">
                    <button
                      className="icon-btn size-7 min-h-0 bg-panel"
                      title="Move up"
                      aria-label="Move job up in queue"
                      onClick={(e) => {
                        e.stopPropagation();
                        move(j, "up");
                      }}
                    >
                      <Icon name="arrow-up" size={13} />
                    </button>
                    <button
                      className="icon-btn size-7 min-h-0 bg-panel"
                      title="Move down"
                      aria-label="Move job down in queue"
                      onClick={(e) => {
                        e.stopPropagation();
                        move(j, "down");
                      }}
                    >
                      <Icon name="arrow-down" size={13} />
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {detailOpen && (
        <button
          className="fixed inset-0 z-30 bg-black/55 lg:hidden"
          aria-label="Dismiss job details"
          onClick={() => setDetailOpen(false)}
        />
      )}
      <aside
        className={`${detailOpen ? "flex" : "hidden"} fixed inset-x-0 bottom-[calc(64px+env(safe-area-inset-bottom))] top-20 z-40 flex-col overflow-y-auto rounded-t-[14px] border-t border-edge bg-panel p-5 shadow-float lg:static lg:inset-auto lg:z-auto lg:flex lg:rounded-none lg:border-t-0 lg:border-l lg:shadow-none`}
        aria-label="Selected job details"
      >
        <div className="mb-4 flex items-center justify-between lg:hidden">
          <div>
            <div className="page-kicker">Inspector</div>
            <h2 className="font-semibold">Job details</h2>
          </div>
          <IconButton icon="close" label="Close job details" onClick={() => setDetailOpen(false)} />
        </div>
        {!sel ? (
          <EmptyState
            icon="queue"
            title="Select a run"
            description="Its inputs, parameters, progress, and next actions will appear here."
          />
        ) : (
          <div className="space-y-4">
            <div>
              <div className="flex items-center gap-2">
                <span
                  className={`technical text-[9px] uppercase ${laneOf(sel.kind, specs) === "local GPU" ? "text-accent" : "text-accent2"}`}
                >
                  {laneOf(sel.kind, specs)}
                </span>
                <h2 className="text-lg font-semibold tracking-[-0.02em]">
                  #{sel.id} · {sel.kind}
                </h2>
              </div>
              <div className="text-xs text-white/40">
                {sel.status}
                {sel.created_at && ` · ${new Date(sel.created_at).toLocaleString()}`}
                {fmtDuration(sel) && ` · took ${fmtDuration(sel)}`}
              </div>
            </div>

            {/* A model swap is not job progress and gets its own line, so a
                90-second load reads as "switching model" rather than as a
                progress bar that has stopped moving. */}
            <LoadStatus state={loads?.[sel.id]} />

            {selImage && (
              <img
                src={selImage.thumb_url || selImage.url}
                alt={`Result for job ${sel.id}`}
                className="media-tile max-h-80 w-full object-contain"
              />
            )}

            {sel.status === "running" && previews[sel.id] && !selImage && (
              <img
                src={previews[sel.id]}
                alt="live preview"
                className="media-tile max-h-64 w-full object-contain"
              />
            )}

            {sel.params?.prompt && (
              <div>
                <div className="label">Prompt</div>
                <div className="border-l-2 border-accent bg-panel2 p-3 text-sm leading-relaxed whitespace-pre-wrap">
                  {sel.params.prompt}
                </div>
              </div>
            )}
            {sel.params?.negative_prompt && (
              <div>
                <div className="label">Negative</div>
                <div className="max-h-24 overflow-y-auto border-l-2 border-edge bg-panel2 p-3 text-xs text-muted">
                  {sel.params.negative_prompt}
                </div>
              </div>
            )}
            <details>
              <summary className="label cursor-pointer select-none">Parameters</summary>
              <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                {Object.entries(sel.params || {})
                  .filter(([k]) => !HIDE_KEYS.has(k) && k !== "prompt" && k !== "negative_prompt")
                  .map(([k, v]) => (
                    <div key={k} className="flex justify-between border-b border-edge/40 py-0.5 gap-2">
                      <span className="text-white/40 shrink-0">{k}</span>
                      <span className="text-white/80 truncate text-right">{String(v)}</span>
                    </div>
                  ))}
              </div>
            </details>

            {sel.params?.grid_id && (
              <button className="btn w-full text-xs" onClick={() => navigate(`/grid/${sel.params.grid_id}`)}>
                View grid
              </button>
            )}

            {sel.error && (
              <div className="border-l-2 border-danger bg-danger/8 p-3 text-xs text-danger">
                {/* Lead with what to do about it. A Python traceback is the
                    right thing to keep and the wrong thing to open with. */}
                {sel.tip && <div className="mb-1.5 text-ink">{sel.tip}</div>}
                <div className={showErr ? "whitespace-pre-wrap max-h-56 overflow-y-auto" : "truncate"}>
                  {showErr ? sel.error : sel.error.split("\n")[0]}
                </div>
                <button className="text-white/40 hover:text-white mt-1" onClick={() => setShowErr((s) => !s)}>
                  {showErr ? "less" : "details"}
                </button>
              </div>
            )}

            <div className="space-y-2 pt-1">
              {sel.status === "queued" && (
                <div className="grid grid-cols-2 gap-2">
                  <Button icon="arrow-up" onClick={() => runNext(sel)}>
                    Run next
                  </Button>
                  {isRerunnable(sel.kind, specs) && (
                    <Button icon="edit" onClick={() => editQueued(sel)}>
                      Edit
                    </Button>
                  )}
                </div>
              )}
              {isRerunnable(sel.kind, specs) && (
                <div className="grid grid-cols-2 gap-2">
                  <button className="btn" disabled={busy} onClick={() => rerun(sel, false)} title="Same seed">
                    Retry
                  </button>
                  <button
                    className="btn-primary"
                    disabled={busy}
                    onClick={() => rerun(sel, true)}
                    title="New seed"
                  >
                    Generate more
                  </button>
                </div>
              )}
              {(sel.result?.asset_ids?.length || 0) > 0 && (
                <button className="btn w-full" onClick={() => openResult(sel)}>
                  View result
                </button>
              )}
              {/* Skip drops the item currently rendering and moves to the next
                  one in the batch. The backend has supported this all along;
                  nothing ever called it, so a batch where item 3 of 8 was
                  clearly going wrong had to be cancelled outright. */}
              {sel.status === "running" && (Number(sel.params?.batch) || 1) > 1 && (
                <Button
                  className="w-full"
                  icon="skip"
                  onClick={() =>
                    api
                      .skip(sel.id)
                      .then((r) => toast(r.changed ? "Skipping to the next item…" : "Nothing to skip"))
                      .catch(() => toast("Couldn't skip", "error"))
                  }
                >
                  Skip this item
                </Button>
              )}
              {(sel.status === "running" || sel.status === "queued") && (
                <button className="btn w-full text-danger" onClick={() => api.cancel(sel.id)}>
                  Cancel
                </button>
              )}
            </div>
          </div>
        )}
      </aside>

      {open && (
        <AssetModal
          asset={open}
          onClose={() => {
            setOpen(null);
            refreshAssets();
          }}
        />
      )}
    </div>
  );
}

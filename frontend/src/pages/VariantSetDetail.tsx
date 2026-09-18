import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Asset, VariantItem, VariantSetDetail as SetDetail } from "../api/types";
import { AssetModal } from "../components/AssetModal";
import { Icon } from "../components/icons";
import { Button, ConfirmDialog, EmptyState, PageHeader, SegmentedControl, Spinner } from "../components/ui";
import { ITEM_STATE_LABEL, liveState, settledFraction, stateTone } from "../lib/variantSets";
import { useStore } from "../store/useStore";

type Filter = "all" | "succeeded" | "problems" | "active" | "blocked" | "canceled";

const FILTERS: { value: Filter; label: string; match: (state: string) => boolean }[] = [
  { value: "all", label: "All", match: () => true },
  { value: "succeeded", label: "Done", match: (s) => s === "succeeded" },
  { value: "problems", label: "Failed · invalid", match: (s) => s === "failed" || s === "invalid" },
  {
    value: "active",
    label: "In progress",
    match: (s) => s === "queued" || s === "running" || s === "pending",
  },
  { value: "blocked", label: "Blocked", match: (s) => s === "blocked" },
  { value: "canceled", label: "Canceled", match: (s) => s === "canceled" },
];

const VALIDATION_TONE: Record<string, string> = {
  passed: "text-ok",
  warned: "text-warn",
  failed: "text-danger",
  pending: "text-muted",
  skipped: "text-muted",
};

function Stat({ label, value, tone = "text-ink" }: { label: string; value: number; tone?: string }) {
  return (
    <div className="min-w-20">
      <div className={`text-xl font-semibold ${tone}`}>{value}</div>
      <div className="technical text-[10px] uppercase tracking-wide text-muted">{label}</div>
    </div>
  );
}

/** One Variant Set: every combination, its live state, its validation, and the
 *  actions that respect it — retry what failed, rerun one on purpose, stop the
 *  rest, export what succeeded. */
export function VariantSetDetail() {
  const { id } = useParams();
  const setId = Number(id);
  const jobs = useStore((s) => s.jobs);
  const revision = useStore((s) => s.variantRevision);
  const toast = useStore((s) => s.toast);
  const [detail, setDetail] = useState<SetDetail | null>(null);
  const [error, setError] = useState("");
  const [stage, setStage] = useState(-1);
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<Asset | null>(null);
  const [busy, setBusy] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);

  const load = useCallback(() => {
    if (!Number.isFinite(setId)) return;
    api
      .variantSet(setId)
      .then((next) => {
        setDetail(next);
        setError("");
      })
      .catch((e) => setError(String(e).replace(/^Error: /, "")));
  }, [setId]);

  // Refresh when the set announces a change, or when one of its jobs does —
  // debounced, because a set finishing child by child would otherwise fetch
  // once per transition.
  const jobStatuses = (detail?.items ?? [])
    .map((item) => (item.job_id != null ? (jobs[item.job_id]?.status ?? "") : ""))
    .join(",");
  useEffect(() => {
    const timer = setTimeout(load, detail ? 400 : 0);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, revision, jobStatuses]);
  useEffect(() => {
    if (detail?.status !== "active") return;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [detail?.status, load]);

  const stages = useMemo(() => detail?.stages ?? [], [detail]);
  const lastStage = Math.max(0, stages.length - 1);
  const shownStage = stage < 0 ? lastStage : Math.min(stage, lastStage);
  const axes = useMemo(
    () => stages.slice(0, shownStage + 1).flatMap((s) => s.axes.map((a) => a.name)),
    [stages, shownStage],
  );
  const rows = useMemo(() => {
    const match = FILTERS.find((f) => f.value === filter)?.match ?? (() => true);
    return (detail?.items ?? [])
      .filter((item) => item.stage === shownStage)
      .filter((item) => match(liveState(item.state, jobs[item.job_id ?? -1]?.status ?? item.job_status)));
  }, [detail, shownStage, filter, jobs]);
  const outputs = rows.map((item) => item.asset).filter(Boolean) as Asset[];

  if (error && !detail) {
    return (
      <div className="page-pad">
        <EmptyState
          icon="alert"
          title="Variant set not found"
          description={error}
          action={
            <Link to="/variants" className="btn">
              All variant sets
            </Link>
          }
        />
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="page-pad flex items-center gap-3 text-sm text-muted">
        <Spinner /> Loading variant set…
      </div>
    );
  }

  const c = detail.counts;
  const failed = (c.failed || 0) + (c.invalid || 0);
  const act = async (label: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(label);
    try {
      await fn();
      toast(done, "success");
      load();
    } catch (e) {
      toast(`${label} failed: ${String(e).replace(/^Error: /, "")}`, "error");
    } finally {
      setBusy("");
    }
  };

  const rerun = (item: VariantItem, reseed: boolean) =>
    act(
      "Rerun",
      () => api.rerunVariantItem(setId, item.id, { reseed }),
      reseed ? `Rerunning ${item.key || "variant"} with a new seed` : `Rerunning ${item.key || "variant"}`,
    );

  return (
    <div className="page-pad h-full overflow-y-auto">
      <PageHeader
        kicker="Variant set"
        title={detail.name}
        meta={detail.status}
        description={
          <span className="technical text-xs">
            {detail.operation} · {stages.length} stage{stages.length === 1 ? "" : "s"} ·{" "}
            {new Date(detail.created_at).toLocaleString()}
          </span>
        }
        actions={
          <>
            {failed > 0 && (
              <Button
                icon="refresh"
                loading={busy === "Retry"}
                onClick={() =>
                  act("Retry", () => api.retryVariantSet(setId), `Retrying ${failed} variant(s)`)
                }
              >
                Retry failed · {failed}
              </Button>
            )}
            {(c.canceled || 0) > 0 && (
              <Button
                icon="play"
                loading={busy === "Resume"}
                onClick={() =>
                  act("Resume", () => api.retryVariantSet(setId, true), "Resuming canceled variants")
                }
              >
                Resume canceled · {c.canceled}
              </Button>
            )}
            {detail.status === "active" && (
              <Button variant="danger" icon="close" onClick={() => setConfirmCancel(true)}>
                Cancel remaining
              </Button>
            )}
            {(c.succeeded || 0) > 0 && (
              <Button
                variant="primary"
                icon="download"
                loading={busy === "Export"}
                onClick={() => act("Export", () => api.exportVariantSet(setId), "Export ready")}
              >
                Export ZIP
              </Button>
            )}
          </>
        }
      />

      <section className="mt-6 border-y border-edge py-4" aria-label="Set progress">
        <div className="flex flex-wrap gap-x-6 gap-y-3">
          <Stat label="Expected" value={detail.expected} />
          <Stat label="Done" value={c.succeeded || 0} tone="text-ok" />
          <Stat label="Running" value={c.running || 0} tone="text-accent2" />
          <Stat label="Queued" value={c.queued || 0} />
          <Stat label="Waiting" value={c.pending || 0} />
          <Stat label="Failed" value={c.failed || 0} tone={c.failed ? "text-danger" : "text-ink"} />
          <Stat label="Invalid" value={c.invalid || 0} tone={c.invalid ? "text-danger" : "text-ink"} />
          <Stat label="Blocked" value={c.blocked || 0} tone={c.blocked ? "text-warn" : "text-ink"} />
          <Stat label="Canceled" value={c.canceled || 0} />
        </div>
        <div
          className="mt-4 h-[3px] overflow-hidden bg-edge"
          role="progressbar"
          aria-label="Settled variants"
          aria-valuenow={Math.round(settledFraction(c) * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full bg-ok transition-[width]"
            style={{ width: `${settledFraction(c) * 100}%` }}
          />
        </div>
        {detail.collection_id != null && (
          <Link
            to={`/gallery?collection=${detail.collection_id}`}
            className="mt-3 inline-flex items-center gap-1 text-xs text-accent"
          >
            <Icon name="gallery" size={13} /> Open the results collection
          </Link>
        )}
      </section>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        {stages.length > 1 && (
          <SegmentedControl
            label="Stage"
            value={String(shownStage)}
            onChange={(next) => setStage(Number(next))}
            options={stages.map((s) => ({
              value: String(s.index),
              label: `Stage ${s.index + 1}${s.name ? ` · ${s.name}` : ""}`,
            }))}
          />
        )}
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter variants">
          {FILTERS.map((f) => (
            <button
              key={f.value}
              className={`chip ${filter === f.value ? "chip-active" : ""}`}
              aria-pressed={filter === f.value}
              onClick={() => setFilter(f.value)}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-4 overflow-x-auto border border-edge">
        <table className="w-full min-w-[720px] text-left text-xs">
          <thead className="bg-panel text-[10px] uppercase tracking-wide text-muted">
            <tr>
              <th className="px-3 py-2">Output</th>
              {axes.map((axis) => (
                <th key={axis} className="technical px-3 py-2">
                  {axis}
                </th>
              ))}
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Validation</th>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-edge/70">
            {rows.map((item) => {
              const job = item.job_id != null ? jobs[item.job_id] : undefined;
              const state = liveState(item.state, job?.status ?? item.job_status);
              const progress = job?.progress ?? item.progress ?? 0;
              const inFlight = state === "queued" || state === "running" || state === "pending";
              return (
                <tr key={item.id} className="align-top" data-testid="variant-row">
                  <td className="px-3 py-2">
                    {item.asset ? (
                      <button
                        className="media-tile block size-14"
                        onClick={() => setOpen(item.asset)}
                        aria-label={`Open output for ${item.key || "variant"}`}
                      >
                        <img
                          src={item.asset.thumb_url || item.asset.url}
                          alt=""
                          className="h-full w-full object-cover"
                        />
                      </button>
                    ) : (
                      <div className="flex size-14 items-center justify-center border border-edge bg-bg text-[10px] text-muted">
                        {state === "running" ? (
                          `${Math.round(progress * 100)}%`
                        ) : (
                          <Icon name="image" size={16} />
                        )}
                      </div>
                    )}
                  </td>
                  {axes.map((axis) => (
                    <td key={axis} className="px-3 py-2 text-ink/85">
                      {item.values[axis]}
                    </td>
                  ))}
                  <td className="px-3 py-2">
                    <div className={`font-medium ${stateTone(state)}`}>{ITEM_STATE_LABEL[state]}</div>
                    {item.state_reason && (
                      <div
                        className="mt-0.5 max-w-56 truncate text-[10px] text-muted"
                        title={item.state_reason}
                      >
                        {item.state_reason}
                      </div>
                    )}
                    {item.attempts > 1 && (
                      <div className="technical text-[10px] text-muted">attempt {item.attempts}</div>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <button
                      className={`technical text-[11px] ${VALIDATION_TONE[item.validation_state] || "text-muted"}`}
                      onClick={() => setExpanded(expanded === item.id ? null : item.id)}
                      aria-expanded={expanded === item.id}
                      disabled={!item.validation.length}
                    >
                      {item.validation_state}
                    </button>
                    {expanded === item.id && (
                      <ul className="mt-1 space-y-0.5 text-[10px]">
                        {item.validation.map((r) => (
                          <li
                            key={r.validator}
                            className={
                              VALIDATION_TONE[
                                r.status === "pass" ? "passed" : r.status === "warn" ? "warned" : "failed"
                              ]
                            }
                          >
                            {r.validator}: {r.message}
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                  <td
                    className="technical max-w-48 truncate px-3 py-2 text-[10px] text-muted"
                    title={item.output_name}
                  >
                    {item.output_name}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        variant="quiet"
                        disabled={inFlight || state === "blocked" || !!busy}
                        onClick={() => rerun(item, false)}
                        title="Run this variant again with the same settings"
                      >
                        Rerun
                      </Button>
                      <Button
                        size="sm"
                        variant="quiet"
                        disabled={inFlight || state === "blocked" || !!busy}
                        onClick={() => rerun(item, true)}
                        title="Run this variant again with a fresh seed"
                      >
                        New seed
                      </Button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!rows.length && <div className="p-6 text-sm text-muted">No variants match this view.</div>}
      </div>

      {open && <AssetModal asset={open} onClose={() => setOpen(null)} list={outputs} onNavigate={setOpen} />}
      {confirmCancel && (
        <ConfirmDialog
          title="Cancel the remaining variants?"
          message="Queued and waiting variants stop now; one that is running stops at its next step. Finished variants keep their results, and canceled ones can be resumed later."
          confirmLabel="Cancel remaining"
          onConfirm={() => act("Cancel", () => api.cancelVariantSet(setId), "Remaining variants canceled")}
          onClose={() => setConfirmCancel(false)}
        />
      )}
    </div>
  );
}

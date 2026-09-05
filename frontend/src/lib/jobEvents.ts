import type { Job, ModelLoadEvent } from "../api/types";

export interface JobEvent {
  type: string;
  id?: number;
  job_id?: number;
  kind?: string;
  status?: string;
  progress?: number;
  message?: string;
  error?: string;
  tip?: string;
  [k: string]: any;
}

/** What a job's model load is currently doing, and when we last heard about it.
 *  `at` is what lets the UI tell "slow" from "hung": the stage text does not
 *  change during a long download, but the timestamp does. */
export interface LoadState {
  stage: ModelLoadEvent["stage"];
  detail: string;
  hint?: string;
  at: number;
}

export interface ApplyResult {
  jobs: Record<number, Job>;
  /** Job finished — its preview entry (if any) should be dropped and its object
   * URL revoked. Previews themselves arrive as binary frames now, not in this
   * event stream; see lib/wsframe.ts. */
  clearPreview?: number;
  /** Model-load stage for this job, or null to clear it. Kept out of the jobs
   *  map for the same reason previews are: it changes on a timer. */
  load?: { id: number; state: LoadState | null };
}

const FINAL = new Set(["done", "error", "canceled"]);

/** Pure reducer for a jobs WebSocket event. Testable without the store. */
export function applyJobEvent(jobs: Record<number, Job>, e: JobEvent): ApplyResult | null {
  if (e.type === "model_load") return applyModelLoad(jobs, e as unknown as ModelLoadEvent);
  if (e.type !== "job") return null;
  const id = e.id ?? e.job_id;
  if (id == null) return null;
  const previous = jobs[id] || {};
  const patch: Partial<Job> = {};
  if (e.kind !== undefined) patch.kind = e.kind;
  if (e.status !== undefined) patch.status = e.status as Job["status"];
  if (e.progress !== undefined) patch.progress = e.progress;
  if (e.message !== undefined) patch.message = e.message;
  if (e.error !== undefined) patch.error = e.error;
  if (e.tip !== undefined) patch.tip = e.tip;
  const merged = { ...previous, ...patch, id } as Job;
  const out: ApplyResult = { jobs: { ...jobs, [id]: merged } };
  if (merged.status && FINAL.has(merged.status)) {
    out.clearPreview = id;
    // A finished job is not loading anything. Clearing here rather than waiting
    // for a "ready" stage means a job that fails mid-load does not leave a
    // spinner behind forever.
    out.load = { id, state: null };
  }
  return out;
}

/** A model-load stage. Deliberately does NOT touch the job's progress: a load
 *  has no meaningful fraction, and moving the bar for it would make a 90-second
 *  stall look like generation progress. */
function applyModelLoad(jobs: Record<number, Job>, e: ModelLoadEvent): ApplyResult {
  const out: ApplyResult = { jobs };
  if (e.id == null) return out;
  out.load = {
    id: e.id,
    state: e.stage === "ready" ? null : { stage: e.stage, detail: e.detail, hint: e.hint, at: Date.now() },
  };
  return out;
}

/** Human wording for a load stage. */
export const LOAD_STAGE_LABEL: Record<ModelLoadEvent["stage"], string> = {
  swapping: "Switching model",
  resolving: "Reading model config",
  downloading: "Downloading weights",
  quantizing: "Quantizing",
  loading: "Loading weights",
  placing: "Moving to GPU",
  ready: "Ready",
};

/** How long since the last event before a load should be called suspect.
 *  The heartbeat is every 2s, so 30s of silence is fifteen missed beats. */
export const LOAD_STALL_MS = 30_000;

export function loadLooksStalled(state: LoadState | null | undefined, now = Date.now()): boolean {
  return !!state && now - state.at > LOAD_STALL_MS;
}

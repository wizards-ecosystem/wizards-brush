import type { ExecutionPlan, PlanRequest, PreparedPlan } from "../api/types";

/**
 * A prepared run: a draft request, the server's plan for it, and a submission.
 *
 * The plan is server-authoritative. The draft is only what the person is
 * editing; nothing here guesses readiness, composes prompts or applies
 * dependent defaults. Each draft change bumps `revision`, and a preview answer
 * is accepted only for the revision it was asked for, so a slow answer for an
 * older draft can never replace the current one. A plan can be submitted only
 * while it is current: the draft has not moved on, and the install's
 * capability revision is still the one the plan was resolved against.
 */
export interface Problem {
  detail: string;
  code?: string;
  field?: string;
  retryable?: boolean;
  state?: Record<string, unknown>;
  suggestions?: string[];
}

export interface PreparedRunState {
  revision: number;
  request: PlanRequest | null;
  /** The seed the server chose for a random request, kept so previews and the
   *  submission stay one request until the person asks for a new seed. */
  pinnedSeed: number | null;
  preview: {
    status: "idle" | "loading" | "ready" | "error";
    forRevision: number;
    plan: ExecutionPlan | null;
    error: Problem | null;
  };
  /** The newest capability revision the server reported. */
  capabilityRevision: string | null;
  submit: { status: "idle" | "submitting" | "done" | "error"; error: Problem | null; jobIds: number[] };
}

export type PreparedRunAction =
  | { type: "draft"; request: PlanRequest | null }
  | { type: "newSeed" }
  | { type: "previewStart"; revision: number }
  | { type: "previewOk"; revision: number; prepared: PreparedPlan }
  | { type: "previewError"; revision: number; error: Problem }
  | { type: "capabilities"; revision: string | null }
  | { type: "submitStart" }
  | { type: "submitOk"; jobIds: number[] }
  | { type: "submitError"; error: Problem };

export const initialPreparedRun: PreparedRunState = {
  revision: 0,
  request: null,
  pinnedSeed: null,
  preview: { status: "idle", forRevision: -1, plan: null, error: null },
  capabilityRevision: null,
  submit: { status: "idle", error: null, jobIds: [] },
};

export function preparedRunReducer(state: PreparedRunState, action: PreparedRunAction): PreparedRunState {
  switch (action.type) {
    case "draft":
      return {
        ...state,
        revision: state.revision + 1,
        request: action.request,
        submit: { ...initialPreparedRun.submit },
      };
    case "newSeed":
      return { ...state, revision: state.revision + 1, pinnedSeed: null };
    case "previewStart":
      if (action.revision !== state.revision) return state;
      return { ...state, preview: { ...state.preview, status: "loading", error: null } };
    case "previewOk":
      if (action.revision !== state.revision) return state; // a late answer for an older draft
      return {
        ...state,
        pinnedSeed: state.request?.seed ?? action.prepared.seed,
        preview: { status: "ready", forRevision: action.revision, plan: action.prepared.plan, error: null },
      };
    case "previewError":
      if (action.revision !== state.revision) return state;
      return {
        ...state,
        preview: { ...state.preview, status: "error", forRevision: action.revision, error: action.error },
      };
    case "capabilities":
      return action.revision === state.capabilityRevision
        ? state
        : { ...state, capabilityRevision: action.revision };
    case "submitStart":
      return { ...state, submit: { status: "submitting", error: null, jobIds: [] } };
    case "submitOk":
      return { ...state, submit: { status: "done", error: null, jobIds: action.jobIds } };
    case "submitError":
      // The draft and its plan are untouched: a network failure or a refusal
      // must never cost the person what they were composing.
      return { ...state, submit: { status: "error", error: action.error, jobIds: [] } };
    default:
      return state;
  }
}

/** The request as it will be sent: the draft with the pinned seed applied. */
export function effectiveRequest(state: PreparedRunState): PlanRequest | null {
  if (!state.request) return null;
  if (state.request.seed !== undefined || state.pinnedSeed == null) return state.request;
  return { ...state.request, seed: state.pinnedSeed };
}

export type StaleReason =
  "no-plan" | "previewing" | "draft-changed" | "capabilities-changed" | "blocked" | "submitting" | null;

/** Why the current plan cannot be submitted, or null when it can. */
export function staleReason(state: PreparedRunState): StaleReason {
  const { preview } = state;
  if (state.submit.status === "submitting") return "submitting";
  if (!preview.plan) return preview.status === "loading" ? "previewing" : "no-plan";
  if (preview.forRevision !== state.revision || preview.status === "loading") return "draft-changed";
  if (state.capabilityRevision && preview.plan.capability_revision !== state.capabilityRevision) {
    return "capabilities-changed";
  }
  if (!preview.plan.readiness.submittable) return "blocked";
  return null;
}

export function canSubmit(state: PreparedRunState): boolean {
  return staleReason(state) === null;
}

/** Turn a thrown API error into the structured problem the server sent. */
export function asProblem(error: unknown): Problem {
  if (error && typeof error === "object" && "detail" in error) return error as Problem;
  const message = error instanceof Error ? error.message : String(error);
  try {
    const parsed = JSON.parse(message) as Problem;
    if (parsed && typeof parsed.detail === "string") return parsed;
  } catch {
    /* not a JSON body */
  }
  return { detail: message };
}

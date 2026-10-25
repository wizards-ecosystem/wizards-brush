import { useCallback, useEffect, useReducer, useRef } from "react";
import { api, isAbort } from "../api/client";
import type { PlanRequest } from "../api/types";
import { useStore } from "../store/useStore";
import {
  asProblem,
  canSubmit,
  effectiveRequest,
  initialPreparedRun,
  preparedRunReducer,
  staleReason,
} from "./preparedRun";

function newRequestId(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `wb_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
}

/**
 * Keep a server plan in step with a draft request.
 *
 * Every change to `request` (compared by value) is previewed after a short
 * debounce; the previous preview's fetch is aborted, and an answer that still
 * arrives for an older draft is ignored by the reducer. When the install's
 * capability revision moves, the current draft is previewed again, and until
 * then the old plan cannot be submitted. `submit` sends exactly the previewed
 * plan's hash; the server refuses it if what would run changed.
 */
export function usePreparedRun(request: PlanRequest | null, { debounceMs = 300 } = {}) {
  const [state, dispatch] = useReducer(preparedRunReducer, initialPreparedRun);
  const capability = useStore((s) => s.system?.capability_revision ?? null);
  const key = request ? JSON.stringify(request) : "";
  const requestId = useRef<string | null>(null);

  useEffect(() => {
    dispatch({ type: "draft", request: key ? (JSON.parse(key) as PlanRequest) : null });
    requestId.current = null; // a new request is a new submission identity
  }, [key]);

  useEffect(() => {
    dispatch({ type: "capabilities", revision: capability });
  }, [capability]);

  // The pinned seed is read at send time, not used as a trigger: pinning the
  // seed the server chose must not cause a second, identical preview.
  const latest = useRef(state);
  useEffect(() => {
    latest.current = state;
  });
  const revision = state.revision;

  useEffect(() => {
    if (!key) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const body = effectiveRequest(latest.current);
      if (!body) return;
      dispatch({ type: "previewStart", revision });
      api
        .preparePlan({ ...body, capability_revision: capability ?? undefined }, controller.signal)
        .then((prepared) => dispatch({ type: "previewOk", revision, prepared }))
        .catch((error) => {
          if (controller.signal.aborted || isAbort(error)) return;
          dispatch({ type: "previewError", revision, error: asProblem(error) });
        });
    }, debounceMs);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
    // `capability` re-previews the same draft when what can run changed.
  }, [key, revision, capability, debounceMs]);

  const submit = useCallback(async () => {
    const plan = state.preview.plan;
    const body = effectiveRequest(state);
    if (!plan || !body || !canSubmit(state)) return null;
    // One id per submission attempt of this request: a retry after a dropped
    // response returns the same job instead of queuing a second one.
    requestId.current ??= newRequestId();
    dispatch({ type: "submitStart" });
    try {
      const result = await api.submitPlan(body, plan.hashes.execution_plan_hash, requestId.current);
      dispatch({ type: "submitOk", jobIds: result.job_ids?.length ? result.job_ids : [result.job_id] });
      requestId.current = null;
      return result;
    } catch (error) {
      dispatch({ type: "submitError", error: asProblem(error) });
      return null;
    }
  }, [state]);

  const newSeed = useCallback(() => dispatch({ type: "newSeed" }), []);

  return { state, submit, newSeed, stale: staleReason(state), canSubmit: canSubmit(state) };
}

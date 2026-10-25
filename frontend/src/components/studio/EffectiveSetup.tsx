import type { ExecutionPlan, PlanIssue } from "../../api/types";
import type { Problem, StaleReason } from "../../lib/preparedRun";

const STATE_TEXT: Record<string, { label: string; tone: string }> = {
  ready: { label: "Ready", tone: "text-ok border-ok/50" },
  needs_resources: { label: "Needs a download", tone: "text-warn border-warn/50" },
  needs_check: { label: "Not checked yet", tone: "text-warn border-warn/50" },
  blocked: { label: "Cannot run here", tone: "text-danger border-danger/50" },
};

const STALE_TEXT: Partial<Record<NonNullable<StaleReason>, string>> = {
  previewing: "Working out the setup…",
  "draft-changed": "Updating to match your changes…",
  "capabilities-changed": "What this install can run changed; updating the setup…",
};

function Issue({ issue }: { issue: PlanIssue }) {
  return (
    <li className={`border-l-2 pl-2 ${issue.blocking ? "border-danger" : "border-warn"}`}>
      <span className="text-ink">{issue.message}</span>
      {issue.action && <span className="block text-muted">{issue.action}</span>}
    </li>
  );
}

/**
 * What will actually run, exactly as the server resolved it.
 *
 * This view never computes anything: the composed prompt, the effective
 * numbers and readiness are the plan's. A value the job builder changed (a
 * quality tier setting the step count, a size snapped to the model's bucket)
 * is listed, so the preview is the request that runs.
 */
export function EffectiveSetup({
  plan,
  stale,
  error,
}: {
  plan: ExecutionPlan | null;
  stale: StaleReason;
  error: Problem | null;
}) {
  if (error && (!plan || stale !== "draft-changed")) {
    return (
      <div
        role="alert"
        className="border-l-2 border-danger bg-panel2 px-3 py-2 text-xs"
        data-testid="plan-error"
      >
        <div className="text-ink">{error.detail}</div>
        {error.field && <div className="technical text-[10px] text-muted">{error.field}</div>}
        {error.suggestions?.map((s) => (
          <div key={s} className="text-muted">
            {s}
          </div>
        ))}
      </div>
    );
  }
  if (!plan) {
    return (
      <div className="text-xs text-muted">
        {STALE_TEXT[stale || "previewing"] ?? "Choose a preset to see what will run."}
      </div>
    );
  }
  const effective = plan.effective_params as Record<string, unknown>;
  const state = STATE_TEXT[plan.readiness.state] ?? STATE_TEXT.blocked;
  const size = effective.width && effective.height ? `${effective.width}×${effective.height}` : null;
  const facts = [
    size && ["Size", size],
    effective.steps != null && ["Steps", String(effective.steps)],
    effective.guidance != null && ["Guidance", String(effective.guidance)],
    effective.batch != null && ["Images", String(effective.batch)],
    ["Seed", String(plan.seed.base)],
  ].filter(Boolean) as [string, string][];
  return (
    <section className="space-y-3 text-xs" aria-label="What will run" data-testid="effective-setup">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`technical border px-1.5 py-0.5 text-[10px] uppercase ${state.tone}`}
          data-testid="readiness"
        >
          {state.label}
        </span>
        <span className="text-muted">
          {plan.target.resolved
            ? `${plan.target.resolved === "local" ? "This machine" : "Remote GPU"}`
            : "No target"}
          {plan.kind ? ` · ${plan.kind}` : ""}
        </span>
        {stale && STALE_TEXT[stale] && <span className="text-muted">· {STALE_TEXT[stale]}</span>}
      </div>
      <div className="text-[11px] text-muted" data-testid="target-reason">
        {plan.target.reason}
      </div>
      <div>
        <div className="label mb-1">Model</div>
        <div className="truncate text-ink" title={plan.model.resource}>
          {plan.model.repo || plan.model.resource}
          {plan.model.variant ? <span className="text-muted"> · {plan.model.variant}</span> : null}
        </div>
        {plan.model.verification !== "verified" && (
          <div className="text-[10px] text-muted">
            Revision {plan.model.revision || "not pinned"} ·{" "}
            {plan.model.verification === "legacy_unverified" ? "weights not hashed" : plan.model.verification}
          </div>
        )}
      </div>
      {plan.adapters.length > 0 && (
        <div>
          <div className="label mb-1">Adapters, in order</div>
          <ol className="space-y-0.5">
            {plan.adapters.map((a, index) => (
              <li
                key={`${a.resource}-${index}`}
                className={a.status === "blocked" ? "text-danger" : "text-ink"}
              >
                {index + 1}. {a.library_path || a.resource.slice(0, 28)} · {a.strength}
                {a.status === "blocked" && " · blocked"}
              </li>
            ))}
          </ol>
        </div>
      )}
      <div>
        <div className="label mb-1">Prompt that runs</div>
        <p className="whitespace-pre-wrap border-l-2 border-edge pl-2 text-ink" data-testid="composed-prompt">
          {plan.prompt.positive || <span className="text-muted">(empty)</span>}
        </p>
        {plan.prompt.negative && (
          <p className="mt-1 whitespace-pre-wrap border-l-2 border-edge pl-2 text-muted">
            <span className="technical text-[10px] uppercase">Avoid</span> {plan.prompt.negative}
          </p>
        )}
        {plan.prompt.enhancement.enabled && (
          <div className="mt-1 text-[10px] text-muted">
            Enhancement added: {plan.prompt.enhancement.modifications.join(", ")}
          </div>
        )}
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
        {facts.map(([label, value]) => (
          <div key={label} className="flex justify-between gap-2">
            <dt className="text-muted">{label}</dt>
            <dd className="technical text-ink">{value}</dd>
          </div>
        ))}
      </dl>
      {plan.normalized.length > 0 && (
        <details className="text-[11px]" data-testid="normalized">
          <summary className="cursor-pointer text-muted">
            Adjusted for this device ({plan.normalized.length})
          </summary>
          <ul className="mt-1 space-y-0.5">
            {plan.normalized.map((n) => (
              <li key={n.field}>
                <span className="technical">{n.field}</span> {String(n.requested)} → {String(n.effective)}
                <span className="text-muted"> · {n.reason}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {(plan.readiness.blockers.length > 0 || plan.warnings.length > 0) && (
        <ul className="space-y-1.5" data-testid="plan-issues">
          {plan.readiness.blockers.map((issue) => (
            <Issue key={`b-${issue.code}-${issue.field}`} issue={issue} />
          ))}
          {plan.warnings.map((issue, index) => (
            <Issue key={`w-${issue.code}-${index}`} issue={issue} />
          ))}
        </ul>
      )}
    </section>
  );
}

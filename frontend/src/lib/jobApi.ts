import { api } from "../api/client";
import type { GeneratorSpec, JobCreate, JobWait } from "../api/types";

/**
 * Queue one job through the unified job API and resolve once it settles.
 *
 * The UI's own small helpers (an automatic mask, say) use the same path a
 * script would — POST /api/jobs, then long-poll GET /api/jobs/{id}/wait — so
 * the documented API is exercised by the app itself, not only by examples.
 * `isCancelled` lets a component stop waiting when it unmounts; the job
 * itself keeps running and stays in the Queue.
 */
export async function runJob(body: JobCreate, isCancelled: () => boolean = () => false): Promise<JobWait> {
  const { job_id } = await api.createJob(body);
  for (;;) {
    const answer = await api.waitJob(job_id, 30);
    if (answer.settled || isCancelled()) return answer;
  }
}

/** The first line of a failed job's error, for a toast. */
export function jobFailure(answer: JobWait): string {
  const first = (answer.job.error || answer.job.message || answer.job.status).split("\n")[0];
  return first || "the job did not finish";
}

/** A request as a runnable curl command (docs/api.md). The token header is
 *  harmless when the app sets none, so one command works either way. */
export function apiRequestText(path: string, body: unknown, origin: string, notes: string[] = []): string {
  const json = JSON.stringify(body, null, 2).replace(/'/g, "'\\''");
  return [
    ...notes.map((note) => `# ${note}`),
    `curl -X POST ${origin}${path} \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H "X-API-Token: \${API_TOKEN:-}" \\`,
    `  -d '${json}'`,
  ].join("\n");
}

/**
 * A generator form as a `POST /api/jobs` request: only the kind's own
 * settings, which is all the strict API accepts, with the prompt as it will
 * actually be sent (styles applied). Inputs are asset ids there, not files, so
 * a kind that takes images comes with a note on how to get ids.
 */
export function jobRequestFor(
  spec: GeneratorSpec,
  values: Record<string, unknown>,
  prompt: string,
): { body: JobCreate; notes: string[] } {
  const names = new Set(spec.controls.map((control) => control.name));
  const params: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(values)) {
    if (names.has(name) && value !== undefined) params[name] = value;
  }
  if (names.has("prompt")) params.prompt = prompt;
  const body: JobCreate = { kind: spec.kind, params };
  const notes: string[] = [];
  if (spec.needs_image) {
    body.inputs = { images: [], ...(spec.needs_mask ? { mask: null } : {}) };
    notes.push(
      "Put input asset ids in inputs.images" +
        (spec.needs_mask ? " and the mask's id in inputs.mask" : "") +
        "; import local files with POST /api/assets/import" +
        (spec.needs_mask ? " (role=mask for the mask)." : "."),
    );
  }
  notes.push("Then wait for it: GET /api/jobs/{job_id}/wait (see docs/api.md).");
  return { body, notes };
}

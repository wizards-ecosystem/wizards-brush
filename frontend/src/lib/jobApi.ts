import { api } from "../api/client";
import type { JobCreate, JobWait } from "../api/types";

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

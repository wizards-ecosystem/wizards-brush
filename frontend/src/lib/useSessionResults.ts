import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { JobWait } from "../api/types";
import { useStore } from "../store/useStore";

const SETTLED = new Set(["done", "error", "canceled"]);

/**
 * The outputs of the jobs this session submitted, fetched by job id.
 *
 * The store's recent-assets list is a cache of the newest few hundred files;
 * a long session would watch its first results fall out of it. Asking each
 * job for its own outputs keeps every result, including the partial outputs
 * of a batch that failed partway.
 */
export function useSessionResults(jobIds: number[]): Record<number, JobWait> {
  const jobs = useStore((s) => s.jobs);
  const assetRevision = useStore((s) => s.assetRevision);
  const [results, setResults] = useState<Record<number, JobWait>>({});
  // Refetch a job when its status changes, or when an asset event lands.
  const signature = useMemo(
    () => jobIds.map((id) => `${id}:${jobs[id]?.status ?? "?"}`).join(","),
    [jobIds, jobs],
  );

  useEffect(() => {
    let alive = true;
    const wanted = jobIds.filter(
      (id) => !results[id] || !SETTLED.has(results[id].job.status) || !results[id].settled,
    );
    Promise.all(
      wanted.map((id) =>
        api
          .waitJob(id, 0)
          .then((view) => [id, view] as const)
          .catch(() => null),
      ),
    ).then((pairs) => {
      if (!alive) return;
      const fresh = pairs.filter((p): p is readonly [number, JobWait] => p !== null);
      if (fresh.length) setResults((current) => ({ ...current, ...Object.fromEntries(fresh) }));
    });
    return () => {
      alive = false;
    };
    // `results` is read to skip settled jobs, not as a trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, assetRevision]);

  return results;
}

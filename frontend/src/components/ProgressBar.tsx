import type { Job } from "../api/types";
import { laneOf } from "../lib/generators";
import { useStore } from "../store/useStore";
import { Icon } from "./icons";

function eta(job: Job): string {
  if (job.status !== "running" || !job.started_at || job.progress < 0.03) return "";
  const elapsed = (Date.now() - new Date(job.started_at).getTime()) / 1000;
  const remaining = elapsed / job.progress - elapsed;
  if (!isFinite(remaining) || remaining <= 0) return "";
  return remaining > 90 ? `~${Math.round(remaining / 60)}m left` : `~${Math.round(remaining)}s left`;
}

export function ProgressBar({ job, preview }: { job: Job; preview?: string }) {
  const specs = useStore((state) => state.specs);
  const percent = Math.round((job.progress || 0) * 100);
  const done = job.status === "done";
  const failed = job.status === "error" || job.status === "canceled";
  const lane = laneOf(job.kind, specs);
  const remaining = eta(job);
  const width = job.status === "queued" ? 4 : done ? 100 : percent;
  const colour = failed ? "bg-danger" : done ? "bg-ok" : lane === "A100" ? "bg-accent2" : "bg-accent";

  return (
    <div className="flex min-w-0 items-center gap-3">
      {preview && job.status === "running" && (
        <div className="media-tile size-14 shrink-0">
          <img src={preview} alt="Live generation preview" className="h-full w-full object-cover" />
          <span
            className={`absolute right-1 top-1 size-1.5 animate-pulse rounded-full ${lane === "A100" ? "bg-accent2" : "bg-accent"}`}
          />
        </div>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center justify-between gap-3">
          <span className="flex min-w-0 items-center gap-2">
            <Icon
              name={failed ? "alert" : done ? "check" : job.status === "running" ? "make" : "queue"}
              size={14}
              className={
                failed ? "text-danger" : done ? "text-ok" : lane === "A100" ? "text-accent2" : "text-accent"
              }
            />
            <span className="technical shrink-0 text-[9px] uppercase tracking-wide text-muted">{lane}</span>
            <span className="truncate text-xs text-ink">
              #{job.id} · {job.kind}
            </span>
          </span>
          <span className="technical shrink-0 text-[10px] text-muted">
            {remaining || job.message || `${percent}%`}
          </span>
        </div>
        <div
          className="mt-2 h-[3px] overflow-hidden bg-edge"
          role="progressbar"
          aria-label={`Job ${job.id} progress`}
          aria-valuenow={width}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className={`h-full ${colour} transition-[width] duration-200`}
            style={{ width: `${width}%` }}
          />
        </div>
        <div className="mt-1 flex justify-between gap-2 text-[10px]">
          <span className={failed ? "truncate text-danger" : "text-muted"}>
            {job.error ? job.error.split("\n")[0] : job.status}
          </span>
          <span className="technical text-white/28">{percent}%</span>
        </div>
      </div>
    </div>
  );
}

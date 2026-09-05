import { Link } from "react-router-dom";
import { api } from "../api/client";
import { laneOf } from "../lib/generators";
import { useStore } from "../store/useStore";
import { Icon } from "./icons";
import { ProgressBar } from "./ProgressBar";
import { Button, StatusIndicator } from "./ui";

/** Live compute telemetry presented as one workshop instrument strip. */
export function BackendPanel() {
  const system = useStore((state) => state.system);
  const specs = useStore((state) => state.specs);
  const jobs = useStore((state) => state.jobs);
  const previews = useStore((state) => state.previews);
  const refreshSystem = useStore((state) => state.refreshSystem);
  const toast = useStore((state) => state.toast);
  const gpu = system?.local_gpu;
  const remote = system?.remote_gpu;
  const running = Object.values(jobs).filter((job) => job.status === "running");
  const localJob = running.find((job) => laneOf(job.kind, specs) === "local GPU");
  const remoteJob = running.find((job) => laneOf(job.kind, specs) === "A100");
  const vramPct = gpu?.memory_total_mb
    ? Math.min(100, Math.round(((gpu.memory_used_mb || 0) / gpu.memory_total_mb) * 100))
    : 0;

  const reconnect = async () => {
    try {
      const response = await api.saveSettings({ remote_gpu_base_url: remote?.url || "" });
      await refreshSystem();
      toast(
        response.remote_gpu?.connected
          ? "Remote GPU reconnected"
          : `Still offline: ${response.remote_gpu?.reason || "unknown"}`,
        response.remote_gpu?.connected ? "success" : "info",
      );
    } catch (error) {
      toast(`Reconnect failed: ${error}`, "error");
    }
  };

  return (
    <div>
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <div className="page-kicker">Workshop instruments</div>
          <h2 className="display-title text-[28px] leading-none">Compute lanes</h2>
        </div>
        <Link to="/diagnosis" className="btn-ghost text-xs">
          Full diagnosis <Icon name="arrow-right" size={14} />
        </Link>
      </div>
      <div className="grid border-y border-edge md:grid-cols-2">
        <section className="min-w-0 py-4 pr-0 md:pr-6">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <StatusIndicator
                ok={!!gpu?.available}
                tone="local"
                label="Local GPU"
                detail={gpu?.available ? `${gpu.utilization_pct || 0}% load` : "offline"}
              />
              <div className="mt-1 truncate text-xs text-muted">{gpu?.name || "No device reported"}</div>
            </div>
            <Icon name="gpu" size={21} className="shrink-0 text-accent" />
          </div>
          {gpu?.available && (
            <div className="mt-4">
              <div className="technical flex justify-between text-[10px] text-muted">
                <span>
                  VRAM {Math.round((gpu.memory_used_mb || 0) / 1024)} /{" "}
                  {Math.round((gpu.memory_total_mb || 0) / 1024)} GB
                </span>
                <span>{gpu.temperature_c ?? "—"}°C</span>
              </div>
              <div className="mt-1.5 h-[3px] bg-edge">
                <div
                  className={`h-full ${vramPct > 90 ? "bg-danger" : "bg-accent"}`}
                  style={{ width: `${vramPct}%` }}
                />
              </div>
            </div>
          )}
          <div className="technical mt-3 truncate text-[10px] text-white/35">
            {system?.models.local_image || "Model unavailable"}
          </div>
          <div className="mt-3 border-t border-edge/70 pt-3">
            {localJob ? (
              <ProgressBar job={localJob} preview={previews[localJob.id]} />
            ) : (
              <span className="technical text-[10px] uppercase text-white/28">Lane idle</span>
            )}
          </div>
        </section>

        <section className="min-w-0 border-t border-edge py-4 md:border-t-0 md:border-l md:pl-6">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <StatusIndicator
                ok={!!remote?.connected}
                tone="remote"
                label="Remote GPU"
                detail={remote?.connected ? remote.gpu || "ready" : "disconnected"}
              />
              <div className="mt-1 truncate text-xs text-muted">
                {remote?.connected ? remote.url : remote?.reason || "No connection configured"}
              </div>
            </div>
            <Icon name="cloud" size={21} className="shrink-0 text-accent2" />
          </div>
          <div className="technical mt-4 truncate text-[10px] text-white/35">
            {system?.models.a100_image || "Image model unavailable"} ·{" "}
            {system?.models.video || "Video model unavailable"}
          </div>
          <div className="mt-3 border-t border-edge/70 pt-3">
            {remote?.connected ? (
              remoteJob ? (
                <ProgressBar job={remoteJob} preview={previews[remoteJob.id]} />
              ) : (
                <div className="text-[11px] text-muted">
                  {remote.build_stale ? (
                    <span className="text-warn">
                      Remote GPU worker is running an older build — rebuild and restart it.
                    </span>
                  ) : remote.worker_alive === false ? (
                    <span className="text-danger">Worker stopped — restart the Remote GPU service.</span>
                  ) : typeof remote.disk_free_gb === "number" && remote.disk_free_gb < 25 ? (
                    <span className="text-warn">
                      Only {remote.disk_free_gb} GB free; a model swap may fail.
                    </span>
                  ) : remote.queue_depth ? (
                    <span className="technical">Idle here · {remote.queue_depth} queued remotely</span>
                  ) : (
                    <span className="technical uppercase text-white/28">Lane idle</span>
                  )}
                </div>
              )
            ) : (
              <div className="flex flex-wrap gap-2">
                {remote?.url && (
                  <Button size="sm" onClick={reconnect}>
                    Reconnect
                  </Button>
                )}
                <Link to="/settings" className="btn text-xs">
                  Open settings
                </Link>
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

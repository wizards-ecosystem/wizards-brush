import { useStore } from "../store/useStore";
import { StatusIndicator } from "./ui";

export function StatusBadges() {
  const system = useStore((s) => s.system);
  const gpu = system?.local_gpu;
  const remote = system?.remote_gpu;
  const vram = gpu?.available
    ? `${Math.round((gpu.memory_used_mb || 0) / 1024)}/${Math.round((gpu.memory_total_mb || 0) / 1024)}GB`
    : "—";
  return (
    <div className="flex items-center gap-4">
      <span title={gpu?.name || "No local GPU"}>
        <StatusIndicator
          ok={!!gpu?.available}
          tone="local"
          label="Local GPU"
          detail={
            gpu?.available
              ? `${vram} · ${gpu.utilization_pct}%${gpu.temperature_c ? ` · ${gpu.temperature_c}°C` : ""}`
              : "offline"
          }
        />
      </span>
      <span title={remote?.url || "Not connected"}>
        <StatusIndicator
          ok={!!remote?.connected}
          tone="remote"
          label="Remote GPU"
          detail={remote?.connected ? remote.gpu || "ready" : "disconnected"}
        />
      </span>
    </div>
  );
}

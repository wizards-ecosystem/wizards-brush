import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { BackendInfo, BackendModel, BackendReport } from "../api/types";
import { Icon } from "../components/icons";
import { Button, EmptyState, PageHeader, Spinner, StatusIndicator } from "../components/ui";

type ModelState = NonNullable<BackendModel["status"]>;

const MODEL_STATE: Record<ModelState, { label: string; className: string; explanation: string }> = {
  ready: {
    label: "Ready",
    className: "text-ok",
    explanation: "The weights are present and can be loaded now.",
  },
  partial: {
    label: "Partial download",
    className: "text-warn",
    explanation: "Some files are cached, but the download must finish before this model can run.",
  },
  absent: {
    label: "Not downloaded",
    className: "text-muted",
    explanation: "The first run will download this model. This is not an error.",
  },
  unknown: {
    label: "Unknown remotely",
    className: "text-muted",
    explanation:
      "The Remote GPU worker supports this model but does not report whether its weights are cached.",
  },
};

function stateOf(model: BackendModel): ModelState {
  return model.status || (model.ready ? "ready" : "unknown");
}

function gb(value: number | null): string {
  return typeof value === "number" ? `${value.toFixed(value < 10 ? 1 : 0)} GB` : "Not reported";
}

function BackendCard({ backend }: { backend: BackendInfo }) {
  const localWithoutGpu = backend.kind === "local" && backend.health.vram_gb === 0;
  const usable = backend.health.connected && !localWithoutGpu;
  const trouble = backend.health.build_stale
    ? "Remote worker code is older than this app. Rebuild remote_gpu_filled.py and restart the remote service before generating."
    : localWithoutGpu
      ? "No CUDA GPU was detected. Local diffusion is unavailable; connect Remote GPU or check the NVIDIA driver."
      : !backend.health.connected
        ? backend.health.reason || "This compute lane has not answered yet."
        : "";

  return (
    <section className="border-t border-edge pt-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <StatusIndicator
            ok={usable}
            tone={backend.kind === "remote" ? "remote" : "local"}
            label={backend.label}
            detail={usable ? backend.health.device || "ready" : "unavailable"}
          />
          <p className="mt-1 text-xs text-muted">
            {backend.kind === "local" ? "Runs on this machine" : "Runs on the connected remote GPU"}
          </p>
        </div>
        <span className="technical text-[10px] uppercase text-muted">{backend.kind}</span>
      </div>

      {trouble && (
        <div
          className={`mt-4 border-l-2 px-3 py-2 text-xs leading-relaxed ${backend.health.build_stale ? "border-warn bg-warn/5 text-warn" : "border-danger bg-danger/5 text-danger"}`}
        >
          {trouble}
        </div>
      )}

      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-xs sm:grid-cols-4">
        <Metric label="Device" value={backend.health.device || "Not reported"} />
        <Metric label="VRAM" value={gb(backend.health.vram_gb)} />
        <Metric label="Free disk" value={gb(backend.health.disk_free_gb)} />
        <Metric label="Queue" value={`${backend.health.queue_depth} waiting`} />
      </dl>

      <div className="mt-5 overflow-x-auto border-y border-edge">
        <table className="w-full min-w-[620px] text-left text-xs">
          <thead className="technical text-[9px] uppercase tracking-wide text-muted">
            <tr>
              <th className="px-2 py-2 font-medium">Model</th>
              <th className="px-2 py-2 font-medium">Purpose</th>
              <th className="px-2 py-2 font-medium">Family</th>
              <th className="px-2 py-2 font-medium">Downloaded</th>
              <th className="px-2 py-2 text-right font-medium">Cache size</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-edge/70">
            {backend.models.map((model) => {
              const status = stateOf(model);
              const state = MODEL_STATE[status];
              return (
                <tr key={`${model.kind}:${model.id}`}>
                  <td className="max-w-[300px] px-2 py-2.5">
                    <div className="font-medium text-ink">{model.label || model.id}</div>
                    <div className="technical mt-0.5 truncate text-[10px] text-muted" title={model.id}>
                      {model.id}
                    </div>
                  </td>
                  <td className="px-2 py-2.5 text-muted">{model.kind}</td>
                  <td className="px-2 py-2.5 text-muted">{model.family || "unknown"}</td>
                  <td className={`px-2 py-2.5 ${state.className}`} title={state.explanation}>
                    <span className="inline-flex items-center gap-1.5">
                      <span className="size-1.5 rounded-full bg-current" /> {state.label}
                    </span>
                  </td>
                  <td className="technical px-2 py-2.5 text-right text-muted">{gb(model.size_gb)}</td>
                </tr>
              );
            })}
            {backend.models.length === 0 && (
              <tr>
                <td className="px-2 py-5 text-muted" colSpan={5}>
                  This backend did not return a model catalogue.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <details className="mt-3 text-xs text-muted">
        <summary className="cursor-pointer py-2 text-ink/75">Advanced capabilities</summary>
        <div className="flex flex-wrap gap-1.5 pb-2">
          {backend.health.features.length ? (
            backend.health.features.map((feature) => (
              <span key={feature} className="technical border border-edge bg-panel2 px-2 py-1 text-[9px]">
                {feature}
              </span>
            ))
          ) : (
            <span>No live capability list is available.</span>
          )}
        </div>
        {backend.health.models_loaded.length > 0 && (
          <p className="mt-1 break-words">
            Resident now: <span className="technical">{backend.health.models_loaded.join(", ")}</span>
          </p>
        )}
        {backend.health.build && (
          <p className="mt-1">
            Build: <span className="technical">{backend.health.build}</span>
          </p>
        )}
      </details>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="label mb-1">{label}</dt>
      <dd className="technical break-words text-ink/80">{value}</dd>
    </div>
  );
}

export function Diagnosis() {
  const [report, setReport] = useState<BackendReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setReport(await api.backends());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let current = true;
    api
      .backends()
      .then((next) => {
        if (current) setReport(next);
      })
      .catch((reason) => {
        if (current) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, []);

  const usable = report?.backends.filter(
    (backend) => backend.health.connected && !(backend.kind === "local" && backend.health.vram_gb === 0),
  );
  const stale = report?.backends.some((backend) => backend.health.build_stale);

  return (
    <div className="page-shell page-pad mx-auto max-w-6xl">
      <PageHeader
        kicker="Read-only workshop report"
        title="Diagnosis"
        description="See what can run now, what will download on first use, and why automatic performance limits were chosen. Nothing on this page changes your setup."
        actions={
          <Button icon="refresh" loading={loading} onClick={() => void load()}>
            Refresh live status
          </Button>
        }
      />

      {loading && !report && (
        <div className="mt-10 flex items-center gap-3 text-sm text-muted">
          <Spinner /> Checking this machine and the Remote GPU worker…
        </div>
      )}

      {error && !report && (
        <EmptyState
          className="mt-10 border-y border-edge py-8"
          icon="alert"
          title="Diagnosis could not be loaded"
          description={error}
          action={<Button onClick={() => void load()}>Try again</Button>}
        />
      )}

      {report && (
        <>
          <section
            className={`mt-7 border-l-[3px] px-4 py-3 ${usable?.length && !stale ? "border-ok bg-ok/5" : "border-warn bg-warn/5"}`}
          >
            <div className="flex items-start gap-3">
              <Icon
                name={usable?.length && !stale ? "check" : "alert"}
                className={usable?.length && !stale ? "text-ok" : "text-warn"}
              />
              <div>
                <h2 className="font-medium">
                  {usable?.length
                    ? `${usable.length} compute lane${usable.length === 1 ? " is" : "s are"} ready`
                    : "No GPU compute lane is ready"}
                </h2>
                <p className="mt-1 text-xs leading-relaxed text-muted">
                  {stale
                    ? "A connected Remote GPU worker needs its code refreshed before it is safe to use."
                    : usable?.length
                      ? "Models marked Not downloaded are available on demand; the first run will take longer."
                      : "Check the actions below, or connect a Remote GPU from Settings."}
                </p>
              </div>
            </div>
          </section>

          <section className="mt-8 border-t border-edge pt-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="page-kicker">Automatic performance envelope</div>
                <h2 className="text-lg font-semibold">{report.hardware.label} profile</h2>
                <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted">
                  Selected from{" "}
                  {report.hardware.source === "override"
                    ? "your HARDWARE_PROFILE override"
                    : `${report.hardware.device} with ${gb(report.hardware.vram_gb)} VRAM`}
                  . It limits canvas and batch sizes before a job can exhaust memory.
                </p>
              </div>
              <span className="technical border border-edge bg-panel2 px-2 py-1 text-[10px] uppercase text-muted">
                {report.hardware.source}
              </span>
            </div>
            <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
              <Metric
                label="Configured precision"
                value={report.hardware.quant === "none" ? "Native" : report.hardware.quant}
              />
              <Metric label="Configured offload" value={report.hardware.offload ? "Enabled" : "Disabled"} />
              <Metric label="Longest side" value={`${report.hardware.max_side} px`} />
              <Metric label="Maximum batch" value={String(report.hardware.max_batch)} />
            </dl>
            <details className="mt-3 text-xs text-muted">
              <summary className="cursor-pointer py-2 text-ink/75">Resolution budgets by quality</summary>
              <div className="flex flex-wrap gap-2 pb-2">
                {Object.entries(report.hardware.tier_mp).map(([tier, mp]) => (
                  <span key={tier} className="technical border border-edge bg-panel2 px-2 py-1 text-[10px]">
                    {tier}: {mp} MP
                  </span>
                ))}
              </div>
            </details>
          </section>

          <div className="mt-8 space-y-8">
            {report.backends.map((backend) => (
              <BackendCard key={backend.id} backend={backend} />
            ))}
          </div>

          <section className="mt-8 border-y border-edge py-5">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="flex items-start gap-3">
                <Icon
                  name={report.credentials.hf_token_set ? "check" : "info"}
                  className={report.credentials.hf_token_set ? "text-ok" : "text-muted"}
                />
                <div>
                  <h2 className="text-sm font-medium">
                    Hugging Face access token{" "}
                    {report.credentials.hf_token_set ? "is configured" : "is not configured"}
                  </h2>
                  <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted">
                    {report.credentials.hf_token_set
                      ? "Gated and private repositories may be downloaded when your account has access. The token itself is never shown here."
                      : "Public models still work. Add HF_TOKEN to .env only if a configured model is gated or private."}
                  </p>
                </div>
              </div>
              <Link to="/settings" className="btn text-xs">
                Connection settings <Icon name="arrow-right" size={14} />
              </Link>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

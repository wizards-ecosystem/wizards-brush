import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type {
  ImportInspection,
  ImportTask,
  RemotePrepare,
  ResourceLicense,
  ResourceView,
} from "../api/types";
import { Icon } from "../components/icons";
import { Button, EmptyState, PageHeader, Spinner, fmtBytes } from "../components/ui";
import { type UploadProgress, uploadResource } from "../lib/resourceUpload";
import { useStore } from "../store/useStore";

const ACTIVE_IMPORTS = new Set(["queued", "running", "canceling"]);
const IMPORT_POLL_MS = 2000;
const PREPARE_POLL_MS = 1500;

function licenseUnknown(license: ResourceLicense | undefined): boolean {
  return !license || !license.id || license.id === "unknown";
}

/** The licence as recorded, with an unknown one flagged rather than hidden. */
export function LicenseBadge({ license }: { license: ResourceLicense | undefined }) {
  if (licenseUnknown(license)) {
    return (
      <span className="technical border border-warn/60 px-1.5 py-0.5 text-[9px] uppercase text-warn">
        Licence unknown
      </span>
    );
  }
  return (
    <span className="technical border border-edge px-1.5 py-0.5 text-[9px] uppercase text-muted">
      {String(license?.name || license?.id)}
      {license?.reviewed ? " · reviewed" : ""}
    </span>
  );
}

function shortRevision(revision: string): string {
  return /^[0-9a-f]{40}$/.test(revision) ? revision.slice(0, 10) : revision;
}

// ---- add from a provider link ---------------------------------------------------------------

/**
 * Paste a link, read what it points at, then import one exact choice.
 *
 * Inspect downloads nothing. The import is a separate, explicit action, and it
 * needs a concrete file or version when the link offers several, and a
 * confirmation that the licence was reviewed when the provider declares none.
 */
function LinkImport({ onQueued }: { onQueued: () => void }) {
  const toast = useStore((s) => s.toast);
  const [link, setLink] = useState("");
  const [inspecting, setInspecting] = useState(false);
  const [inspection, setInspection] = useState<ImportInspection | null>(null);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState<string | null>(null);
  const [kind, setKind] = useState<"adapter" | "model">("adapter");
  const [acknowledged, setAcknowledged] = useState(false);
  const [creating, setCreating] = useState(false);

  const inspect = async () => {
    setInspecting(true);
    setError("");
    setInspection(null);
    try {
      const found = await api.inspectImport(link.trim());
      setInspection(found);
      setSelection(found.default_selection);
      setAcknowledged(false);
    } catch (e) {
      setError(`${e}`);
    } finally {
      setInspecting(false);
    }
  };

  const chosen = inspection?.candidates.find((c) => c.id === selection) ?? null;
  // A single file from a Hugging Face repository does not say what it is.
  const needsKind = chosen !== null && chosen.kind === null && chosen.type === "file";
  const ready =
    inspection !== null &&
    chosen !== null &&
    chosen.supported &&
    (!inspection.license_needs_acknowledgement || acknowledged);

  const create = async () => {
    if (!inspection || !chosen) return;
    setCreating(true);
    try {
      await api.createImport({
        link: link.trim(),
        selection: chosen.id,
        kind: needsKind ? kind : null,
        acknowledge_license: inspection.license_needs_acknowledgement ? acknowledged : false,
      });
      toast(`Importing ${chosen.name}`, "info");
      setInspection(null);
      setLink("");
      onQueued();
    } catch (e) {
      toast(`Could not import: ${e}`, "error");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="space-y-3" data-testid="link-import">
      <form
        className="flex flex-col gap-2 sm:flex-row"
        onSubmit={(event) => {
          event.preventDefault();
          if (link.trim()) void inspect();
        }}
      >
        <input
          className="input min-w-0 flex-1"
          aria-label="Model or adapter link"
          placeholder="https://huggingface.co/…, https://civitai.com/models/…, or hf:owner/repo"
          value={link}
          onChange={(event) => setLink(event.target.value)}
        />
        <Button type="submit" icon="search" loading={inspecting} disabled={!link.trim()}>
          Inspect
        </Button>
      </form>
      {error && <div className="border-l-2 border-danger pl-2 text-xs text-danger">{error}</div>}
      {inspection && (
        <div className="space-y-3 border border-edge p-3" data-testid="inspection">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{inspection.name}</span>
            <LicenseBadge license={inspection.license} />
            <span className="technical text-[10px] uppercase text-muted">
              {inspection.provider} · {shortRevision(inspection.revision)} · {inspection.access}
              {inspection.family && inspection.family !== "unknown" ? ` · ${inspection.family}` : ""}
            </span>
          </div>
          {inspection.notes.map((note) => (
            <div key={note} className="text-[11px] text-muted">
              {note}
            </div>
          ))}
          <div
            role="radiogroup"
            aria-label="What to import"
            className="divide-y divide-edge border border-edge"
          >
            {inspection.candidates.length === 0 && (
              <div className="px-3 py-2 text-xs text-muted">Nothing importable at this link.</div>
            )}
            {inspection.candidates.map((candidate) => (
              <label
                key={candidate.id}
                className={`flex items-start gap-2 px-3 py-2 text-xs ${candidate.supported ? "cursor-pointer" : "opacity-60"}`}
              >
                <input
                  type="radio"
                  name="import-candidate"
                  disabled={!candidate.supported}
                  checked={selection === candidate.id}
                  onChange={() => setSelection(candidate.id)}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{candidate.name}</span>
                  <span className="technical text-[10px] text-muted">
                    {candidate.kind ?? candidate.type}
                    {candidate.size ? ` · ${fmtBytes(candidate.size)}` : ""}
                    {candidate.sha256 ? " · sha256 known" : ""}
                  </span>
                  {candidate.reason && (
                    <span
                      className={`block text-[11px] ${candidate.supported ? "text-muted" : "text-danger"}`}
                    >
                      {candidate.reason}
                    </span>
                  )}
                </span>
              </label>
            ))}
          </div>
          {needsKind && (
            <label className="flex items-center gap-2 text-xs">
              This file is
              <select
                className="input w-auto text-xs"
                aria-label="Import as"
                value={kind}
                onChange={(event) => setKind(event.target.value as "adapter" | "model")}
              >
                <option value="adapter">an adapter (LoRA)</option>
                <option value="model">a model checkpoint</option>
              </select>
            </label>
          )}
          {inspection.license_needs_acknowledgement && (
            <label className="flex items-start gap-2 text-xs text-warn">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(event) => setAcknowledged(event.target.checked)}
              />
              The provider declares no licence. I have reviewed the terms at the source and may use this.
            </label>
          )}
          <div className="flex gap-2">
            <Button variant="primary" icon="download" loading={creating} disabled={!ready} onClick={create}>
              Import {chosen ? chosen.name : ""}
            </Button>
            <Button onClick={() => setInspection(null)}>Discard</Button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---- add from this computer -----------------------------------------------------------------

function FileImport({ onImported }: { onImported: () => void }) {
  const toast = useStore((s) => s.toast);
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState<"adapter" | "model">("adapter");
  const [label, setLabel] = useState("");
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const upload = async () => {
    if (!file) return;
    const abort = new AbortController();
    controller.current = abort;
    setProgress({ sent: 0, size: file.size, phase: "uploading" });
    try {
      const result = await uploadResource(file, kind, {
        label,
        signal: abort.signal,
        onProgress: setProgress,
      });
      toast(
        result.status === "already_present"
          ? `${result.resource.label} was already in the library`
          : `Imported ${result.resource.label}`,
        "success",
      );
      setFile(null);
      setLabel("");
      onImported();
    } catch (e) {
      if (!abort.signal.aborted) toast(`Upload failed: ${e}`, "error");
    } finally {
      controller.current = null;
      setProgress(null);
    }
  };

  const pct = progress ? Math.floor((100 * progress.sent) / Math.max(1, progress.size)) : 0;
  return (
    <div className="space-y-3" data-testid="file-import">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <input
          type="file"
          accept=".safetensors"
          aria-label="Choose a .safetensors file"
          className="text-xs"
          disabled={progress !== null}
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <select
          className="input w-auto text-xs"
          aria-label="File kind"
          value={kind}
          disabled={progress !== null}
          onChange={(event) => setKind(event.target.value as "adapter" | "model")}
        >
          <option value="adapter">Adapter (LoRA)</option>
          <option value="model">Model checkpoint</option>
        </select>
        <input
          className="input min-w-0 flex-1 text-xs"
          aria-label="Label"
          placeholder="Label (optional)"
          value={label}
          disabled={progress !== null}
          onChange={(event) => setLabel(event.target.value)}
        />
      </div>
      <p className="text-[11px] text-muted">
        Only .safetensors files are accepted: pickle formats can run code when loaded. The file is checked and
        hashed before it joins the library.
      </p>
      {progress ? (
        <div className="space-y-1" data-testid="upload-progress">
          <div className="h-1.5 bg-edge">
            <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
          </div>
          <div className="flex items-center justify-between text-[11px] text-muted">
            <span>
              {progress.phase === "uploading"
                ? `${fmtBytes(progress.sent)} of ${fmtBytes(progress.size)} (${pct}%)`
                : progress.phase === "verifying"
                  ? "Checking size and hash…"
                  : "Validating and adding to the library…"}
            </span>
            <Button
              size="sm"
              disabled={progress.phase !== "uploading"}
              onClick={() => controller.current?.abort()}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <Button variant="primary" icon="arrow-up" disabled={!file} onClick={() => void upload()}>
          Upload {file ? `(${fmtBytes(file.size)})` : ""}
        </Button>
      )}
    </div>
  );
}

// ---- import tasks ---------------------------------------------------------------------------

function ImportTasks({ tasks, onChange }: { tasks: ImportTask[]; onChange: () => void }) {
  const toast = useStore((s) => s.toast);
  if (tasks.length === 0) return null;
  const act = (call: Promise<unknown>, verb: string) =>
    call.then(onChange).catch((e) => toast(`Could not ${verb}: ${e}`, "error"));
  return (
    <div className="divide-y divide-edge border border-edge" data-testid="import-tasks">
      {tasks.map((task) => {
        const active = ACTIVE_IMPORTS.has(task.state);
        const pct = Math.round(100 * (task.progress || 0));
        return (
          <div key={task.id} className="space-y-1 px-3 py-2 text-xs" data-testid="import-task">
            <div className="flex items-center justify-between gap-2">
              <span className="min-w-0 truncate">
                {task.filename || task.provider_ref}
                <span className="technical ml-2 text-[10px] text-muted">
                  {task.provider} · {shortRevision(task.revision)}
                </span>
              </span>
              <span className="flex shrink-0 items-center gap-2">
                <span
                  className={`technical text-[10px] uppercase ${
                    task.state === "done"
                      ? "text-accent"
                      : task.state === "failed"
                        ? "text-danger"
                        : "text-muted"
                  }`}
                >
                  {task.state}
                </span>
                {(task.state === "queued" || task.state === "running") && (
                  <Button size="sm" onClick={() => act(api.cancelImport(task.id), "cancel")}>
                    Cancel
                  </Button>
                )}
                {task.retryable && (
                  <Button size="sm" icon="refresh" onClick={() => act(api.retryImport(task.id), "retry")}>
                    Retry
                  </Button>
                )}
              </span>
            </div>
            {active && (
              <div className="h-1 bg-edge">
                <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
              </div>
            )}
            {active && task.bytes_total ? (
              <div className="text-[11px] text-muted">
                {fmtBytes(task.bytes_done)} of {fmtBytes(task.bytes_total)}
              </div>
            ) : null}
            {task.error && <div className="text-[11px] text-danger">{task.error}</div>}
          </div>
        );
      })}
    </div>
  );
}

// ---- prepare an adapter on the Remote GPU ---------------------------------------------------

function PrepareRemote({ resource }: { resource: ResourceView }) {
  const toast = useStore((s) => s.toast);
  const [task, setTask] = useState<RemotePrepare | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .prepareStatus(resource.id)
      .then((t) => alive && setTask(t))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [resource.id]);

  const running = task?.state === "running";
  useEffect(() => {
    if (!running) return;
    let alive = true;
    const timer = window.setInterval(() => {
      api
        .prepareStatus(resource.id)
        .then((t) => alive && setTask(t))
        .catch(() => {});
    }, PREPARE_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [running, resource.id]);

  const start = () =>
    api
      .prepareRemote(resource.id)
      .then(setTask)
      .catch((e) => toast(`Could not prepare: ${e}`, "error"));
  const cancel = () =>
    api
      .cancelPrepare(resource.id)
      .then(() => api.prepareStatus(resource.id).then(setTask))
      .catch((e) => toast(`Could not cancel: ${e}`, "error"));

  if (running) {
    const pct = task.size ? Math.floor((100 * task.sent) / task.size) : 0;
    return (
      <span className="flex items-center gap-2 text-[11px] text-muted" data-testid="prepare-remote">
        <Spinner className="size-3" /> Sending to Remote GPU {pct}%
        <Button size="sm" onClick={() => void cancel()}>
          Cancel
        </Button>
      </span>
    );
  }
  const done = task?.state === "verified" || task?.state === "present";
  return (
    <span className="flex items-center gap-2 text-[11px]" data-testid="prepare-remote">
      {done && <span className="text-accent">On Remote GPU</span>}
      {task?.state === "failed" && <span className="text-danger">{task.error || "failed"}</span>}
      {task?.state === "canceled" && <span className="text-muted">canceled</span>}
      {!done && (
        <Button size="sm" icon="cloud" onClick={() => void start()}>
          Prepare on Remote GPU
        </Button>
      )}
    </span>
  );
}

// ---- inventory ------------------------------------------------------------------------------

function Inventory({ resources, remoteReady }: { resources: ResourceView[]; remoteReady: boolean }) {
  if (resources.length === 0) {
    return (
      <EmptyState
        icon="layers"
        title="Nothing here yet"
        description="Configured models appear after a rescan; import adapters from a link or this computer."
      />
    );
  }
  return (
    <div className="divide-y divide-edge border border-edge" data-testid="inventory">
      {resources.map((resource) => (
        <div key={resource.id} className="space-y-1 px-3 py-2.5" data-testid="resource-row">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="min-w-0 truncate text-sm" title={resource.id}>
              {resource.label || resource.provider_ref}
            </span>
            <span className="flex flex-wrap items-center gap-1.5">
              <LicenseBadge license={resource.license} />
              <span
                className={`technical border px-1.5 py-0.5 text-[9px] uppercase ${
                  resource.state === "installed"
                    ? "border-accent/60 text-accent"
                    : resource.state === "unavailable" || resource.state === "quarantined"
                      ? "border-danger/60 text-danger"
                      : "border-edge text-muted"
                }`}
              >
                {resource.state}
              </span>
            </span>
          </div>
          <div className="technical text-[10px] text-muted">
            {resource.kind}
            {resource.family && resource.family !== "unknown" ? ` · ${resource.family}` : ""}
            {resource.size_bytes ? ` · ${fmtBytes(resource.size_bytes)}` : ""}
            {` · ${resource.verification}`}
            {resource.provider && resource.provider !== "local"
              ? ` · ${resource.provider} ${shortRevision(resource.revision)}`
              : ""}
          </div>
          {resource.installs.length > 0 && (
            <div className="technical text-[10px] text-white/40">
              {resource.installs.map((install) => (
                <div key={`${install.target}:${install.root}:${install.path}`}>
                  {install.target}: {install.root}/{install.path} · {install.state}
                  {install.error ? ` · ${install.error}` : ""}
                </div>
              ))}
            </div>
          )}
          {resource.error && <div className="text-[11px] text-danger">{resource.error}</div>}
          {resource.kind === "adapter" &&
            remoteReady &&
            resource.installs.some((i) => i.target === "local") && <PrepareRemote resource={resource} />}
        </div>
      ))}
    </div>
  );
}

// ---- the page -------------------------------------------------------------------------------

export function Resources() {
  const toast = useStore((s) => s.toast);
  const remote = useStore((s) => s.system?.remote_gpu);
  const refreshControls = useStore((s) => s.refreshControls);
  const [resources, setResources] = useState<ResourceView[] | null>(null);
  const [tasks, setTasks] = useState<ImportTask[]>([]);
  const [loadError, setLoadError] = useState("");
  const [kindFilter, setKindFilter] = useState<"all" | "model" | "adapter">("all");
  const [source, setSource] = useState<"link" | "file">("link");
  const [scanning, setScanning] = useState(false);

  const loadResources = useCallback(
    () =>
      api
        .resources()
        .then((rows) => {
          setResources(rows);
          setLoadError("");
        })
        .catch((e) => setLoadError(`${e}`)),
    [],
  );
  const loadTasks = useCallback(
    () =>
      api
        .imports()
        .then(setTasks)
        .catch(() => {}),
    [],
  );

  useEffect(() => {
    void loadResources();
    void loadTasks();
  }, [loadResources, loadTasks]);

  // Poll imports only while one is in motion; refresh the inventory (and the
  // pickers' controls) when one finishes.
  const activeCount = tasks.filter((t) => ACTIVE_IMPORTS.has(t.state)).length;
  const previousActive = useRef(activeCount);
  useEffect(() => {
    if (previousActive.current > activeCount) {
      void loadResources();
      void refreshControls();
    }
    previousActive.current = activeCount;
  }, [activeCount, loadResources, refreshControls]);
  useEffect(() => {
    if (activeCount === 0) return;
    const timer = window.setInterval(() => void loadTasks(), IMPORT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [activeCount, loadTasks]);

  const rescan = async () => {
    setScanning(true);
    try {
      const counts = await api.rescanResources();
      await loadResources();
      void refreshControls();
      toast(
        `Rescanned: ${Object.entries(counts)
          .map(([k, v]) => `${v} ${k}`)
          .join(", ")}`,
        "success",
      );
    } catch (e) {
      toast(`Rescan failed: ${e}`, "error");
    } finally {
      setScanning(false);
    }
  };

  const shown = useMemo(
    () => (resources ?? []).filter((r) => kindFilter === "all" || r.kind === kindFilter),
    [resources, kindFilter],
  );
  const remoteReady = Boolean(remote?.connected && remote.features?.includes("resources"));
  const recentTasks = tasks
    .filter((t) => t.state !== "done")
    .concat(tasks.filter((t) => t.state === "done").slice(0, 5));

  return (
    <div className="page-shell page-pad mx-auto max-w-5xl">
      <PageHeader
        kicker="Library"
        title="Resources"
        description="Models and adapters by identity: where each came from, its licence, whether it is verified, and where it is installed."
        actions={
          <Button icon="refresh" loading={scanning} onClick={() => void rescan()}>
            Rescan
          </Button>
        }
      />

      <section className="mt-6 space-y-3" aria-labelledby="add-heading">
        <div className="flex items-center justify-between gap-2">
          <h2 id="add-heading" className="text-sm font-semibold">
            Add
          </h2>
          <div className="flex gap-1" role="tablist" aria-label="Where from">
            {(
              [
                ["link", "From a link"],
                ["file", "From this computer"],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={source === id}
                className={`chip text-[11px] ${source === id ? "border-accent text-ink" : ""}`}
                onClick={() => setSource(id)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        {source === "link" ? (
          <LinkImport onQueued={() => void loadTasks()} />
        ) : (
          <FileImport
            onImported={() => {
              void loadResources();
              void refreshControls();
            }}
          />
        )}
        <ImportTasks tasks={recentTasks} onChange={() => void loadTasks()} />
      </section>

      <section className="mt-10 space-y-3" aria-labelledby="inventory-heading">
        <div className="flex items-center justify-between gap-2">
          <h2 id="inventory-heading" className="text-sm font-semibold">
            Inventory
          </h2>
          <select
            className="input w-auto text-xs"
            aria-label="Show"
            value={kindFilter}
            onChange={(event) => setKindFilter(event.target.value as typeof kindFilter)}
          >
            <option value="all">Everything</option>
            <option value="model">Models</option>
            <option value="adapter">Adapters</option>
          </select>
        </div>
        {!remoteReady && (
          <p className="flex items-center gap-1.5 text-[11px] text-muted">
            <Icon name="info" size={12} />
            Adapters can be prepared on a Remote GPU once a worker that accepts resources is connected.
          </p>
        )}
        {loadError ? (
          <div className="border-l-2 border-danger pl-2 text-xs text-danger">{loadError}</div>
        ) : resources === null ? (
          <div className="flex items-center gap-2 text-sm text-muted">
            <Spinner /> Loading the library…
          </div>
        ) : (
          <Inventory resources={shown} remoteReady={remoteReady} />
        )}
      </section>
    </div>
  );
}

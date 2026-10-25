import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { PresetDefinition, PresetRead, PresetSummary } from "../api/types";
import { OriginBadge } from "../components/studio/PresetPicker";
import { Button, EmptyState, PageHeader } from "../components/ui";
import { useStore } from "../store/useStore";

const TEMPLATE: PresetDefinition = {
  name: "New preset",
  description: "",
  tags: [],
  operation: "text_to_image",
  model: { resource: "" },
  adapters: [],
  prompt: { template: "{{prompt}}", negative: "" },
  settings: {},
  finish: [],
};

/**
 * Studio presets: browse every revision, edit a custom one, clone or archive.
 *
 * The editor is the definition itself, checked by the server on save: the
 * same contract an AI client uses over MCP, so a preset written here and one
 * written by an agent are interchangeable. Curated presets are read-only; a
 * save from one becomes your copy.
 */
export function PresetLibrary() {
  const params = useParams();
  const presetId = params["*"] || null;
  const navigate = useNavigate();
  const toast = useStore((s) => s.toast);
  const [rows, setRows] = useState<PresetSummary[] | null>(null);
  const [archived, setArchived] = useState(false);
  const [current, setCurrent] = useState<PresetRead | null>(null);
  const [revisions, setRevisions] = useState<number[]>([]);
  const [text, setText] = useState("");
  const [error, setError] = useState<{ detail: string; field?: string } | null>(null);
  const [conflict, setConflict] = useState(false);
  const [creating, setCreating] = useState(false);

  const load = useCallback(() => {
    api
      .studioPresets(archived)
      .then(setRows)
      .catch(() => setRows([]));
  }, [archived]);
  useEffect(load, [load]);

  const show = useCallback((read: PresetRead) => {
    setCurrent(read);
    setText(JSON.stringify(read.definition, null, 2));
    setError(null);
    setConflict(false);
    setRevisions(Array.from({ length: read.head_revision }, (_, i) => i + 1));
  }, []);
  const open = useCallback(
    (id: string, revision?: number) =>
      api
        .studioPreset(id, revision)
        .then(show)
        .catch(() => setCurrent(null)),
    [show],
  );
  useEffect(() => {
    if (!presetId) return;
    let alive = true;
    api
      .studioPreset(presetId)
      .then((read) => alive && show(read))
      .catch(() => alive && setCurrent(null));
    return () => {
      alive = false;
    };
  }, [presetId, show]);

  const parse = (): PresetDefinition | null => {
    try {
      return JSON.parse(text) as PresetDefinition;
    } catch (caught) {
      setError({ detail: `Not valid JSON: ${caught instanceof Error ? caught.message : caught}` });
      return null;
    }
  };

  const fail = (caught: unknown) => {
    if (caught instanceof ApiError) {
      setConflict(caught.code === "revision_conflict");
      setError({ detail: caught.message, field: caught.field });
    } else {
      setError({ detail: caught instanceof Error ? caught.message : String(caught) });
    }
  };

  const save = async (onConflict: "error" | "copy" = "error") => {
    const definition = parse();
    if (!definition) return;
    try {
      const saved =
        creating || !current
          ? await api.createStudioPreset(definition)
          : await api.updateStudioPreset(current.id, definition, current.revision, onConflict);
      toast(
        saved.cloned_from
          ? `Saved as your copy: ${saved.name}`
          : `Saved ${saved.name} revision ${saved.revision}`,
        "success",
      );
      setCreating(false);
      load();
      navigate(`/presets/${saved.id}`);
      void open(saved.id);
    } catch (caught) {
      fail(caught);
    }
  };

  const clone = async () => {
    if (!current) return;
    try {
      const saved = await api.cloneStudioPreset(current.id, { revision: current.revision });
      load();
      navigate(`/presets/${saved.id}`);
    } catch (caught) {
      fail(caught);
    }
  };

  const archive = async () => {
    if (!current) return;
    try {
      const saved = await api.archiveStudioPreset(current.id, !current.archived);
      setCurrent({ ...current, archived: saved.archived });
      load();
    } catch (caught) {
      fail(caught);
    }
  };

  const readOnly = !creating && current?.origin === "curated";
  const atHead = current ? current.revision === current.head_revision : true;

  return (
    <div className="page-pad h-full overflow-y-auto">
      <PageHeader
        kicker="Workshop"
        title="Studio presets"
        description="Named setups of a model, adapters, prompt template and settings. Every save is a new revision; jobs keep the one they ran."
      />
      <div className="mt-2 flex gap-3 text-xs">
        <Link to="/recipes" className="text-muted underline-offset-2 hover:underline">
          Saved generators
        </Link>
        <span className="text-ink">Studio presets</span>
      </div>
      <div className="mt-6 grid items-start gap-6 lg:grid-cols-[minmax(220px,300px)_minmax(0,1fr)]">
        <nav aria-label="Presets" className="min-w-0 space-y-2">
          <div className="flex items-center justify-between">
            <label className="flex items-center gap-1 text-xs text-muted">
              <input
                type="checkbox"
                checked={archived}
                onChange={(event) => setArchived(event.target.checked)}
              />
              Show archived
            </label>
            <Button
              size="sm"
              icon="plus"
              onClick={() => {
                setCreating(true);
                setCurrent(null);
                setText(JSON.stringify(TEMPLATE, null, 2));
                setError(null);
              }}
            >
              New
            </Button>
          </div>
          {rows && rows.length === 0 ? (
            <EmptyState
              icon="bookmark"
              title="No presets yet"
              description="Save one from the Studio, or start a new definition here."
            />
          ) : (
            <ul className="divide-y divide-edge border border-edge">
              {(rows ?? []).map((row) => (
                <li key={row.id}>
                  <Link
                    to={`/presets/${row.id}`}
                    aria-current={row.id === presetId ? "page" : undefined}
                    className={`block px-3 py-2.5 hover:bg-panel2 ${row.id === presetId ? "bg-panel2" : ""}`}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm">{row.name}</span>
                      <OriginBadge preset={row} />
                    </span>
                    <span className="technical mt-0.5 block text-[10px] text-muted">
                      {row.id} · r{row.head_revision}
                      {row.archived ? " · archived" : ""}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </nav>
        <section className="min-w-0 space-y-3" aria-label="Preset definition">
          {!current && !creating ? (
            <EmptyState icon="bookmark" title="Choose a preset" description="Or start a new one." />
          ) : (
            <>
              {current && !creating && (
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="text-sm font-medium">{current.name}</span>
                  <OriginBadge preset={current} />
                  <label className="flex items-center gap-1">
                    Revision
                    <select
                      className="input w-auto py-0.5 text-xs"
                      value={current.revision}
                      onChange={(event) => void open(current.id, Number(event.target.value))}
                    >
                      {revisions.map((r) => (
                        <option key={r} value={r}>
                          {r}
                          {r === current.head_revision ? " (latest)" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Button
                    size="sm"
                    onClick={() =>
                      navigate("/", { state: { presetId: current.id, revision: current.revision } })
                    }
                  >
                    Open in Studio
                  </Button>
                  <Button size="sm" onClick={() => void clone()}>
                    Clone
                  </Button>
                  <Button size="sm" onClick={() => void archive()}>
                    {current.archived ? "Unarchive" : "Archive"}
                  </Button>
                </div>
              )}
              {current?.definition.curation && (
                <div className="border-l-2 border-edge pl-2 text-[11px] text-muted">
                  Review: {current.definition.curation.status ?? "unreviewed"}
                  {current.definition.curation.limitations?.length
                    ? ` · Limitations: ${current.definition.curation.limitations.join("; ")}`
                    : ""}
                </div>
              )}
              <textarea
                className="input min-h-[420px] font-mono text-[11px]"
                aria-label="Preset definition JSON"
                spellCheck={false}
                readOnly={readOnly}
                value={text}
                onChange={(event) => setText(event.target.value)}
              />
              {!atHead && !creating && (
                <p className="text-[11px] text-muted">
                  This is an older revision. Saving it makes a new revision from it.
                </p>
              )}
              {error && (
                <p role="alert" className="text-sm text-danger" data-testid="preset-error">
                  {error.detail}
                  {error.field && <span className="technical ml-2 text-[10px]">{error.field}</span>}
                </p>
              )}
              <div className="flex justify-end gap-2">
                {conflict && <Button onClick={() => void save("copy")}>Keep mine as a copy</Button>}
                <Button variant="primary" disabled={readOnly && !creating} onClick={() => void save()}>
                  {creating ? "Create preset" : readOnly ? "Curated presets are read-only" : "Save revision"}
                </Button>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

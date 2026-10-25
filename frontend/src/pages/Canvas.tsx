import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { Asset, CanvasLayer, CanvasRead, CanvasSummary } from "../api/types";
import { GalleryPicker } from "../components/GalleryPicker";
import { Icon } from "../components/icons";
import { Button, EmptyState, PageHeader, Spinner } from "../components/ui";
import {
  HISTORY_LIMIT,
  IDENTITY,
  docReducer,
  initialState,
  newDocument,
  toDocPoint,
  type DocState,
} from "../lib/canvasDoc";
import { useStore } from "../store/useStore";

const AUTOSAVE_MS = 800;

/** Gallery assets by id: the recent snapshot first, then a fetch per id. */
function useAssets(ids: number[]): Record<number, Asset | null> {
  const recent = useStore((s) => s.assets);
  const [fetched, setFetched] = useState<Record<number, Asset | null>>({});
  const key = [...new Set(ids)].sort((a, b) => a - b).join(",");
  useEffect(() => {
    let alive = true;
    const wanted = key ? key.split(",").map(Number) : [];
    for (const id of wanted) {
      if (recent.some((a) => a.id === id)) continue;
      api
        .asset(id)
        .then((asset) => alive && setFetched((f) => ({ ...f, [id]: asset })))
        .catch(() => alive && setFetched((f) => ({ ...f, [id]: null })));
    }
    return () => {
      alive = false;
    };
  }, [key, recent]);
  const out: Record<number, Asset | null> = { ...fetched };
  for (const a of recent) out[a.id] = a;
  return out;
}

/** The document as the editor shows it: CSS boxes with the same transform
 *  convention as the server renderer, scaled to fit. Flatten is the authority
 *  on pixels; this is the working view. */
function Stage({
  state,
  assets,
  missing,
  onSelect,
  onDrag,
}: {
  state: DocState;
  assets: Record<number, Asset | null>;
  missing: Set<number>;
  onSelect: (id: string | null) => void;
  onDrag: (id: string, x: number, y: number, merge: boolean) => void;
}) {
  const { doc, selected } = state;
  const stageRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: string; dx: number; dy: number; moved: boolean } | null>(null);

  const point = (event: React.PointerEvent) => {
    const rect = stageRef.current?.getBoundingClientRect();
    return rect ? toDocPoint(event.clientX, event.clientY, rect, doc) : { x: 0, y: 0 };
  };

  return (
    <div
      ref={stageRef}
      className="relative w-full overflow-hidden border border-edge"
      style={{
        aspectRatio: `${doc.width} / ${doc.height}`,
        background:
          doc.background === "transparent"
            ? "repeating-conic-gradient(#2a2a2a 0% 25%, #1f1f1f 0% 50%) 0 / 16px 16px"
            : doc.background,
      }}
      data-testid="canvas-stage"
      onPointerDown={(event) => {
        if (event.target === stageRef.current) onSelect(null);
      }}
      onPointerMove={(event) => {
        const active = drag.current;
        if (!active) return;
        const p = point(event);
        onDrag(active.id, Math.round(p.x - active.dx), Math.round(p.y - active.dy), active.moved);
        active.moved = true;
      }}
      onPointerUp={() => {
        drag.current = null;
      }}
      onPointerLeave={() => {
        drag.current = null;
      }}
    >
      {doc.layers.map((layer, index) => {
        if (!layer.visible) return null;
        const asset = assets[layer.asset_id];
        const mask = layer.mask_id ? assets[layer.mask_id] : null;
        const lost = missing.has(layer.asset_id);
        const w = (asset?.width ?? 64) * layer.transform.scale_x;
        const h = (asset?.height ?? 64) * layer.transform.scale_y;
        const maskUrl = mask?.url;
        return (
          <div
            key={layer.id}
            role="button"
            tabIndex={0}
            aria-label={`Layer ${layer.name || layer.id}`}
            className={`absolute touch-none select-none ${selected === layer.id ? "outline outline-2 outline-accent" : ""}`}
            style={{
              left: `${(layer.transform.x / doc.width) * 100}%`,
              top: `${(layer.transform.y / doc.height) * 100}%`,
              width: `${(w / doc.width) * 100}%`,
              height: `${(h / doc.height) * 100}%`,
              transform: `rotate(${layer.transform.rotation}deg)`,
              opacity: layer.opacity,
              mixBlendMode: layer.blend_mode === "normal" ? undefined : layer.blend_mode,
              zIndex: index + 1,
            }}
            onPointerDown={(event) => {
              event.stopPropagation();
              onSelect(layer.id);
              const p = point(event);
              drag.current = {
                id: layer.id,
                dx: p.x - layer.transform.x,
                dy: p.y - layer.transform.y,
                moved: false,
              };
              (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
            }}
          >
            {lost || !asset ? (
              <div className="flex size-full items-center justify-center border border-dashed border-danger/70 bg-danger/10 text-[10px] text-danger">
                {lost ? `missing #${layer.asset_id}` : "…"}
              </div>
            ) : (
              <img
                src={asset.url}
                alt=""
                draggable={false}
                className="pointer-events-none size-full"
                style={
                  maskUrl
                    ? {
                        maskImage: `url(${maskUrl})`,
                        maskMode: "luminance",
                        maskSize: "100% 100%",
                      }
                    : undefined
                }
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

function NumberField({
  label,
  value,
  step = 1,
  onChange,
}: {
  label: string;
  value: number;
  step?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="text-[11px] text-muted">
      {label}
      <input
        className="input mt-1 text-xs"
        type="number"
        step={step}
        value={Number.isFinite(value) ? value : 0}
        onChange={(event) => {
          const next = Number(event.target.value);
          if (Number.isFinite(next)) onChange(next);
        }}
      />
    </label>
  );
}

/** One open project: stage, layers, inspector, autosave and flatten. */
function Editor({ project, onSaved }: { project: CanvasRead; onSaved: (read: CanvasRead) => void }) {
  const toast = useStore((s) => s.toast);
  const [state, dispatch] = useReducer(docReducer, project.document, initialState);
  const [revision, setRevision] = useState(project.head_revision);
  const [saved, setSaved] = useState(project.document);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState<number | null>(null);
  const [missing, setMissing] = useState(new Set(project.missing));
  const [picking, setPicking] = useState<"layer" | "mask" | null>(null);
  const [flattening, setFlattening] = useState(false);
  const [flatAsset, setFlatAsset] = useState<Asset | null>(null);
  const ids = state.doc.layers.flatMap((l) => (l.mask_id ? [l.asset_id, l.mask_id] : [l.asset_id]));
  const assets = useAssets(ids);
  const layer = state.doc.layers.find((l) => l.id === state.selected) ?? null;
  const dirty = state.doc !== saved;

  // Autosave: the current document, debounced, from the revision it was based
  // on. A conflict stops autosave; nothing is overwritten.
  useEffect(() => {
    if (!dirty || conflict !== null || saving) return;
    const timer = window.setTimeout(() => {
      const doc = state.doc;
      setSaving(true);
      api
        .saveCanvas(project.id, doc, revision)
        .then((read) => {
          setRevision(read.head_revision);
          setSaved(doc);
          setMissing(new Set(read.missing));
          onSaved(read);
        })
        .catch((error) => {
          if (error instanceof ApiError && error.code === "revision_conflict") {
            setConflict(Number(error.state?.current_revision ?? 0));
          } else {
            toast(`Could not save: ${error}`, "error");
            setSaved(doc); // stop retrying this exact document; the next edit tries again
          }
        })
        .finally(() => setSaving(false));
    }, AUTOSAVE_MS);
    return () => window.clearTimeout(timer);
  }, [dirty, conflict, saving, state.doc, revision, project.id, onSaved, toast]);

  // Undo/redo shortcuts, scoped to this editor and not while typing.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "z") return;
      event.preventDefault();
      dispatch({ type: event.shiftKey ? "redo" : "undo" });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const reload = async () => {
    try {
      const read = await api.canvas(project.id);
      dispatch({ type: "load", doc: read.document });
      setSaved(read.document);
      setRevision(read.head_revision);
      setMissing(new Set(read.missing));
      setConflict(null);
      onSaved(read);
    } catch (error) {
      toast(`${error}`, "error");
    }
  };

  const flatten = async () => {
    setFlattening(true);
    try {
      const result = await api.flattenCanvas(project.id, { revision });
      setFlatAsset(result.asset);
      toast(`Flattened revision ${result.revision} into the gallery`, "success");
    } catch (error) {
      toast(`Could not flatten: ${error}`, "error");
    } finally {
      setFlattening(false);
    }
  };

  return (
    <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_280px]">
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted">
          <Button
            size="sm"
            icon="undo"
            disabled={!state.past.length}
            onClick={() => dispatch({ type: "undo" })}
          >
            Undo
          </Button>
          <Button
            size="sm"
            icon="redo"
            disabled={!state.future.length}
            onClick={() => dispatch({ type: "redo" })}
          >
            Redo
          </Button>
          <span className="technical" data-testid="canvas-save-state">
            {conflict !== null
              ? "not saved"
              : saving
                ? "saving…"
                : dirty
                  ? "unsaved changes"
                  : `saved · revision ${revision}`}
          </span>
          <span className="ml-auto technical">
            {state.doc.width}×{state.doc.height} · undo keeps {HISTORY_LIMIT} steps
          </span>
        </div>
        {conflict !== null && (
          <div className="border-l-2 border-warn pl-2 text-xs text-warn" data-testid="canvas-conflict">
            Revision {conflict} was saved elsewhere after this one was opened. Your changes were not saved and
            nothing was overwritten.{" "}
            <button type="button" className="underline" onClick={() => void reload()}>
              Load revision {conflict}
            </button>{" "}
            (discards the unsaved changes here).
          </div>
        )}
        <Stage
          state={state}
          assets={assets}
          missing={missing}
          onSelect={(id) => dispatch({ type: "select", id })}
          onDrag={(id, x, y, merge) => dispatch({ type: "transform", id, patch: { x, y }, merge })}
        />
        {missing.size > 0 && (
          <div className="text-[11px] text-danger">
            Assets {[...missing].join(", ")} are no longer in the gallery (restore them from the trash). Their
            layers are kept as placeholders.
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            icon="image"
            loading={flattening}
            disabled={dirty}
            onClick={() => void flatten()}
          >
            Flatten to gallery
          </Button>
          {dirty && <span className="text-[11px] text-muted">Flatten uses the saved revision.</span>}
          {flatAsset && (
            <Link to="/gallery" className="flex items-center gap-2 text-[11px] text-accent underline">
              <img src={flatAsset.thumb_url ?? flatAsset.url} alt="" className="size-8 border border-edge" />
              Asset #{flatAsset.id}
            </Link>
          )}
        </div>
      </div>

      <aside className="space-y-4">
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="label mb-0">Layers</span>
            <Button size="sm" icon="plus" onClick={() => setPicking("layer")}>
              Add
            </Button>
          </div>
          {state.doc.layers.length === 0 && <div className="text-xs text-muted">No layers yet.</div>}
          <ol className="divide-y divide-edge border border-edge" data-testid="canvas-layers">
            {[...state.doc.layers].reverse().map((l) => (
              <li
                key={l.id}
                className={`flex items-center gap-1 px-2 py-1.5 text-xs ${l.id === state.selected ? "bg-accent/12" : ""}`}
              >
                <button
                  type="button"
                  className="icon-btn size-6 min-h-0"
                  aria-label={`${l.visible ? "Hide" : "Show"} ${l.name || l.id}`}
                  aria-pressed={l.visible}
                  onClick={() => dispatch({ type: "update", id: l.id, patch: { visible: !l.visible } })}
                >
                  <Icon name={l.visible ? "check" : "close"} size={11} />
                </button>
                <button
                  type="button"
                  className="min-w-0 flex-1 truncate text-left"
                  onClick={() => dispatch({ type: "select", id: l.id })}
                >
                  {l.name || l.id}
                  {missing.has(l.asset_id) && <span className="ml-1 text-danger">(missing)</span>}
                </button>
                <button
                  type="button"
                  className="icon-btn size-6 min-h-0"
                  aria-label={`Raise ${l.name || l.id}`}
                  onClick={() => dispatch({ type: "move", id: l.id, by: 1 })}
                >
                  <Icon name="arrow-up" size={11} />
                </button>
                <button
                  type="button"
                  className="icon-btn size-6 min-h-0"
                  aria-label={`Lower ${l.name || l.id}`}
                  onClick={() => dispatch({ type: "move", id: l.id, by: -1 })}
                >
                  <Icon name="arrow-down" size={11} />
                </button>
                <button
                  type="button"
                  className="icon-btn size-6 min-h-0"
                  aria-label={`Delete ${l.name || l.id}`}
                  onClick={() => dispatch({ type: "remove", id: l.id })}
                >
                  <Icon name="trash" size={11} />
                </button>
              </li>
            ))}
          </ol>
        </div>

        {layer && (
          <div className="space-y-2 border border-edge p-3" data-testid="canvas-inspector">
            <label className="block text-[11px] text-muted">
              Name
              <input
                className="input mt-1 text-xs"
                value={layer.name}
                onChange={(event) =>
                  dispatch({ type: "update", id: layer.id, patch: { name: event.target.value } })
                }
              />
            </label>
            <div className="grid grid-cols-2 gap-2">
              {(
                [
                  ["x", "X", 1],
                  ["y", "Y", 1],
                  ["scale_x", "Scale X", 0.05],
                  ["scale_y", "Scale Y", 0.05],
                  ["rotation", "Rotation°", 1],
                ] as const
              ).map(([key, label, step]) => (
                <NumberField
                  key={key}
                  label={label}
                  step={step}
                  value={layer.transform[key]}
                  onChange={(value) => dispatch({ type: "transform", id: layer.id, patch: { [key]: value } })}
                />
              ))}
            </div>
            <label className="block text-[11px] text-muted">
              Opacity {Math.round(layer.opacity * 100)}%
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                className="w-full"
                value={layer.opacity}
                onChange={(event) =>
                  dispatch({ type: "update", id: layer.id, patch: { opacity: Number(event.target.value) } })
                }
              />
            </label>
            <label className="block text-[11px] text-muted">
              Blend
              <select
                className="input mt-1 text-xs"
                value={layer.blend_mode}
                onChange={(event) =>
                  dispatch({
                    type: "update",
                    id: layer.id,
                    patch: { blend_mode: event.target.value as CanvasLayer["blend_mode"] },
                  })
                }
              >
                <option value="normal">Normal</option>
                <option value="multiply">Multiply</option>
                <option value="screen">Screen</option>
              </select>
            </label>
            <div className="flex flex-wrap gap-1">
              <Button size="sm" onClick={() => setPicking("mask")}>
                {layer.mask_id ? "Change mask" : "Set mask"}
              </Button>
              {layer.mask_id && (
                <Button
                  size="sm"
                  onClick={() => dispatch({ type: "update", id: layer.id, patch: { mask_id: null } })}
                >
                  Remove mask
                </Button>
              )}
              <Button
                size="sm"
                onClick={() => dispatch({ type: "transform", id: layer.id, patch: IDENTITY })}
              >
                Reset transform
              </Button>
            </div>
            {layer.lineage?.job_id && (
              <div className="technical text-[10px] text-muted">
                from job #{layer.lineage.job_id}
                {layer.lineage.from_layer ? ` editing ${layer.lineage.from_layer}` : ""}
              </div>
            )}
          </div>
        )}
      </aside>

      {picking && (
        <GalleryPicker
          title={picking === "mask" ? "Choose a mask the size of this layer's image" : "Add a layer"}
          filter={(a) => (picking === "mask" ? a.generator === "mask" : a.generator !== "mask")}
          onClose={() => setPicking(null)}
          onPick={(asset) => {
            if (picking === "mask" && layer) {
              const source = assets[layer.asset_id];
              if (source && (source.width !== asset.width || source.height !== asset.height)) {
                toast("A mask must be the same size as its layer's image", "error");
                return;
              }
              dispatch({ type: "update", id: layer.id, patch: { mask_id: asset.id } });
            } else {
              dispatch({
                type: "add",
                layer: {
                  name: `Asset ${asset.id}`,
                  asset_id: asset.id,
                  mask_id: null,
                  transform: { ...IDENTITY },
                  opacity: 1,
                  visible: true,
                  blend_mode: "normal",
                },
              });
            }
            setPicking(null);
          }}
        />
      )}
    </div>
  );
}

/**
 * Canvas: layered documents over gallery assets, saved as revisions.
 * Sources are never modified; flattening writes a new gallery image.
 */
export function Canvas() {
  const { id } = useParams();
  const navigate = useNavigate();
  const toast = useStore((s) => s.toast);
  const [projects, setProjects] = useState<CanvasSummary[] | null>(null);
  const [open, setOpen] = useState<CanvasRead | null>(null);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [size, setSize] = useState({ width: 1024, height: 1024 });

  const load = useCallback(
    () =>
      api
        .canvases()
        .then(setProjects)
        .catch((e) => setError(`${e}`)),
    [],
  );
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (!id) return;
    let alive = true;
    api
      .canvas(Number(id))
      .then((read) => alive && setOpen(read))
      .catch((e) => alive && setError(`${e}`));
    return () => {
      alive = false;
    };
  }, [id]);
  const current = open !== null && String(open.id) === id ? open : null;

  const create = async () => {
    try {
      const made = await api.createCanvas(
        name.trim() || "Untitled canvas",
        newDocument(size.width, size.height),
      );
      setName("");
      void load();
      navigate(`/canvas/${made.id}`);
    } catch (e) {
      toast(`Could not create: ${e}`, "error");
    }
  };

  const remove = async (project: CanvasRead) => {
    if (!window.confirm(`Delete "${project.name}"? Its gallery images stay in the gallery.`)) return;
    try {
      await api.deleteCanvas(project.id);
      void load();
      navigate("/canvas");
    } catch (e) {
      toast(`${e}`, "error");
    }
  };

  return (
    <div className="page-shell page-pad mx-auto max-w-7xl">
      <PageHeader
        kicker="Compose"
        title="Canvas"
        description="Layer gallery images, mask and arrange them, and flatten the result into a new image. Sources are never changed."
      />
      {error && <div className="mt-4 border-l-2 border-danger pl-2 text-xs text-danger">{error}</div>}
      <div className="mt-6 grid items-start gap-8 xl:grid-cols-[240px_minmax(0,1fr)]">
        <div className="space-y-3">
          <div className="space-y-2 border border-edge p-3">
            <input
              className="input text-xs"
              aria-label="New canvas name"
              placeholder="New canvas name"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
            <div className="grid grid-cols-2 gap-2">
              <NumberField
                label="Width"
                value={size.width}
                onChange={(width) => setSize((s) => ({ ...s, width }))}
              />
              <NumberField
                label="Height"
                value={size.height}
                onChange={(height) => setSize((s) => ({ ...s, height }))}
              />
            </div>
            <Button size="sm" icon="plus" onClick={() => void create()}>
              New canvas
            </Button>
          </div>
          {projects === null ? (
            <div className="flex items-center gap-2 text-sm text-muted">
              <Spinner /> Loading…
            </div>
          ) : projects.length === 0 ? (
            <div className="text-xs text-muted">No canvases yet.</div>
          ) : (
            <div className="divide-y divide-edge border border-edge" data-testid="canvas-projects">
              {projects.map((p) => (
                <Link
                  key={p.id}
                  to={`/canvas/${p.id}`}
                  className={`block px-3 py-2 text-sm ${String(p.id) === id ? "bg-accent/12" : "hover:bg-panel2"}`}
                >
                  <span className="block truncate">{p.name}</span>
                  <span className="technical text-[10px] text-muted">
                    {p.width}×{p.height} · r{p.head_revision}
                  </span>
                </Link>
              ))}
            </div>
          )}
        </div>
        <div className="min-w-0">
          {current ? (
            <>
              <div className="mb-3 flex items-center justify-between gap-2">
                <h2 className="truncate text-sm font-semibold">{current.name}</h2>
                <Button size="sm" icon="trash" onClick={() => void remove(current)}>
                  Delete
                </Button>
              </div>
              <Editor key={current.id} project={current} onSaved={() => void load()} />
            </>
          ) : (
            <EmptyState
              icon="layers"
              title="Open or create a canvas"
              description="A canvas keeps layers, masks and transforms as a document you can reopen; your gallery images are only referenced."
            />
          )}
        </div>
      </div>
    </div>
  );
}

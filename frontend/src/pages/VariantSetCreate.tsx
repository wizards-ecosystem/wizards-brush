import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, authHeaders } from "../api/client";
import type { Asset, VariantCapabilities, VariantPreview, VariantRecipeSpec } from "../api/types";
import { GalleryPicker } from "../components/GalleryPicker";
import { Icon } from "../components/icons";
import { MaskEditor } from "../components/MaskEditor";
import { VariantStageEditor, operationControls } from "../components/VariantStageEditor";
import {
  Button,
  EmptyState,
  IconButton,
  NameDialog,
  PageHeader,
  SegmentedControl,
  Spinner,
} from "../components/ui";
import { coerceValues } from "../lib/generators";
import {
  axisProblems,
  countCombinations,
  formula,
  fromRecipe,
  newDraft,
  newStage,
  toRecipe,
  type SetDraft,
} from "../lib/variantSets";
import { useStore } from "../store/useStore";

const DRAFT_KEY = "variant-set-draft-v1";

function loadDraft(): SetDraft | null {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    return raw ? (JSON.parse(raw) as SetDraft) : null;
  } catch {
    return null;
  }
}

function requestId(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `wb_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
}

/** Build a new Variant Set: sources, ordered stages of named axes, templates,
 *  the operation's own registry controls, finishing, validation and names —
 *  with a live combination count and a server-checked preview. */
export function VariantSetCreate() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const specs = useStore((s) => s.specs);
  const presets = useStore((s) => s.presets);
  const system = useStore((s) => s.system);
  const assets = useStore((s) => s.assets);
  const toast = useStore((s) => s.toast);

  const [caps, setCaps] = useState<VariantCapabilities | null>(null);
  const [draft, setDraft] = useState<SetDraft | null>(null);
  const [preview, setPreview] = useState<VariantPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [picking, setPicking] = useState(false);
  const [naming, setNaming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [maskFile, setMaskFile] = useState<File | null>(null);
  const [maskImage, setMaskImage] = useState<{ assetId: number; file: File } | null>(null);
  const submitting = useRef(false);

  useEffect(() => {
    api
      .variantCapabilities()
      .then(setCaps)
      .catch((e) => toast(`Couldn't load Variant Set options: ${e}`, "error"));
  }, [toast]);

  // Initial draft: a saved recipe (?recipe=), else the last unsent draft, else
  // new. The local cases are settled during render, the React way to derive
  // state from a value that just arrived; only a recipe needs a fetch.
  const recipeParam = params.get("recipe");
  const firstKind = caps?.operations[0]?.kind || "image_edit";
  if (caps && !draft && !recipeParam) {
    const saved = loadDraft();
    const known = new Set(caps.operations.map((op) => op.kind));
    setDraft(saved && saved.stages?.every((s) => known.has(s.operation)) ? saved : newDraft(firstKind));
  }
  useEffect(() => {
    if (!caps || !recipeParam) return;
    let live = true;
    api
      .variantRecipe(Number(recipeParam))
      .then((r) => live && setDraft(fromRecipe(r.recipe, r.name)))
      .catch((e) => {
        if (!live) return;
        toast(`Couldn't load that recipe: ${e}`, "error");
        setDraft(newDraft(firstKind));
      });
    return () => {
      live = false;
    };
  }, [caps, recipeParam, firstKind, toast]);

  useEffect(() => {
    if (!draft) return;
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    } catch {
      /* private mode or quota: the form still works for this visit */
    }
  }, [draft]);

  const opByKind = useMemo(() => new Map((caps?.operations ?? []).map((op) => [op.kind, op])), [caps]);
  const specByKind = useMemo(() => new Map(specs.map((spec) => [spec.kind, spec])), [specs]);
  const maskOps = useMemo(
    () => new Set((caps?.operations ?? []).filter((op) => op.mask === "required").map((op) => op.kind)),
    [caps],
  );

  // The recipe as the backend will see it: every registry control's value, as
  // the generator form sends, so a hidden control still ships its default.
  const buildRecipe = useCallback(
    (current: SetDraft): VariantRecipeSpec =>
      toRecipe(
        {
          ...current,
          stages: current.stages.map((stage) => {
            const controls = operationControls(specByKind.get(stage.operation), caps);
            const { values } = coerceValues(controls, stage.params);
            delete values.style_ids;
            return { ...stage, params: values };
          }),
        },
        maskOps,
      ),
    [caps, specByKind, maskOps],
  );
  const recipe = useMemo(() => (draft && caps ? buildRecipe(draft) : null), [draft, caps, buildRecipe]);

  const counts = useMemo(() => (draft ? countCombinations(draft.stages) : null), [draft]);
  const problems = useMemo(() => (draft ? axisProblems(draft.stages) : []), [draft]);
  const cap = caps?.cap ?? 1000;
  const firstOp = draft ? opByKind.get(draft.stages[0].operation) : undefined;
  const needsMask = !!draft && draft.stages.some((stage) => maskOps.has(stage.operation));

  // Server-checked preview, debounced. The backend is the authority on every
  // rule; this is where unknown placeholders, collisions and caps surface.
  // While the form has problems of its own there is nothing to ask the server,
  // and an earlier answer is simply not shown.
  const previewable = !!recipe && !!counts && !problems.length && counts.total > 0 && counts.total <= cap;
  useEffect(() => {
    if (!recipe || !previewable) return;
    let live = true;
    const timer = setTimeout(() => {
      setPreviewing(true);
      api
        .previewVariantSet(recipe, 60)
        .then((result) => {
          if (!live) return;
          setPreview(result);
          setPreviewError("");
        })
        .catch((e) => {
          if (!live) return;
          setPreview(null);
          setPreviewError(String(e).replace(/^Error: /, ""));
        })
        .finally(() => live && setPreviewing(false));
    }, 450);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [recipe, previewable]);

  // The mask is painted over the first source image.
  const firstSource = draft?.sources[0];
  const firstSourceAsset = assets.find((a) => a.id === firstSource);
  const maskSource =
    needsMask && firstSourceAsset && maskImage?.assetId === firstSourceAsset.id ? maskImage.file : null;
  useEffect(() => {
    if (!needsMask || !firstSourceAsset) return;
    let live = true;
    fetch(firstSourceAsset.url, { headers: authHeaders() })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(`${r.status}`))))
      .then((blob) => {
        if (!live) return;
        const file = new File([blob], firstSourceAsset.filename, { type: blob.type || "image/png" });
        setMaskImage({ assetId: firstSourceAsset.id, file });
      })
      .catch(() => toast("Couldn't load the source image for the mask", "error"));
    return () => {
      live = false;
    };
  }, [needsMask, firstSourceAsset, toast]);

  const remoteBlocked =
    !!draft &&
    draft.stages.some((stage) => opByKind.get(stage.operation)?.lane === "remote") &&
    !system?.remote_gpu?.connected;
  const unavailable = draft?.stages
    .map((stage) => specByKind.get(stage.operation)?.unavailable_reason)
    .find(Boolean);
  const blockedReason =
    unavailable ||
    (remoteBlocked
      ? "Remote GPU is disconnected. Start the remote service and update the connection in Settings."
      : "");
  const sourceProblem =
    firstOp &&
    draft &&
    (draft.sources.length < firstOp.min_sources || draft.sources.length > firstOp.max_sources)
      ? firstOp.max_sources === 0
        ? "This operation takes no source image."
        : `Choose ${firstOp.min_sources === firstOp.max_sources ? firstOp.min_sources : `${firstOp.min_sources}-${firstOp.max_sources}`} source image(s).`
      : "";
  const maskProblem =
    needsMask && !maskFile && draft?.maskAssetId == null ? "Paint the region to change." : "";
  const overCap = !!counts && counts.total > cap;
  const canCreate =
    !!recipe &&
    !!counts &&
    counts.total > 0 &&
    !overCap &&
    !problems.length &&
    !sourceProblem &&
    !maskProblem &&
    previewable &&
    !!preview &&
    !preview.collisions.length &&
    !previewError &&
    !blockedReason &&
    !busy;

  const update = (next: SetDraft) => setDraft(next);

  const withMask = async (current: SetDraft): Promise<SetDraft> => {
    if (!needsMask || !maskFile) return current;
    const { asset_id } = await api.uploadVariantMask(maskFile);
    const next = { ...current, maskAssetId: asset_id };
    setDraft(next);
    setMaskFile(null);
    return next;
  };

  const create = async () => {
    if (!draft || !canCreate || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    try {
      const current = await withMask(draft);
      const made = await api.createVariantSet({
        name: current.name,
        recipe: buildRecipe(current),
        request_id: requestId(),
        collection: { mode: current.collection ? "new" : "none" },
      });
      toast(`Queued ${made.expected} variants`, "success");
      try {
        localStorage.removeItem(DRAFT_KEY);
      } catch {
        /* nothing to clean */
      }
      navigate(`/variants/${made.id}`);
    } catch (e) {
      toast(`Couldn't create the set: ${String(e).replace(/^Error: /, "")}`, "error");
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };

  const saveRecipe = async (name: string) => {
    if (!draft) return;
    try {
      const current = await withMask(draft);
      await api.saveVariantRecipe({ name, recipe: buildRecipe(current) });
      toast("Recipe saved", "success");
    } catch (e) {
      toast(`Couldn't save the recipe: ${String(e).replace(/^Error: /, "")}`, "error");
    }
  };

  if (!caps || !draft || !counts) {
    return (
      <div className="page-pad flex items-center gap-3 text-sm text-muted">
        <Spinner /> Preparing the variant set editor…
      </div>
    );
  }
  if (!caps.operations.length) {
    return (
      <div className="page-pad">
        <EmptyState
          icon="layers"
          title="No operation is available"
          description="Variant Sets drive the existing image operations. None is enabled on this installation."
        />
      </div>
    );
  }

  const chosen = draft.sources.map((id) => assets.find((a) => a.id === id));
  const totalLabel =
    draft.stages.length > 1 ? counts.totals.join(" + ") + ` = ${counts.total}` : `${counts.total}`;

  return (
    <div className="grid h-full min-h-0 grid-cols-1 bg-bg lg:grid-cols-[minmax(0,1fr)_clamp(320px,30vw,420px)]">
      <div className="min-h-0 overflow-y-auto page-pad">
        <PageHeader
          kicker="Variant set"
          title="New variant set"
          description="Choose an operation and its sources, name the axes to vary, and write the instruction once. Every combination becomes its own tracked job."
        />

        <div className="mt-6 max-w-3xl space-y-5">
          <div>
            <label className="label" htmlFor="variant-set-name">
              Name
            </label>
            <input
              id="variant-set-name"
              className="input"
              maxLength={120}
              placeholder="e.g. Finish study"
              value={draft.name}
              onChange={(e) => update({ ...draft, name: e.target.value })}
            />
          </div>

          {firstOp && firstOp.max_sources > 0 && (
            <div>
              <div className="flex items-baseline justify-between">
                <div className="label mb-0">Source images</div>
                <span className="technical text-[10px] text-muted">
                  {draft.sources.length}/{firstOp.max_sources}
                </span>
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                {chosen.map((asset, i) => (
                  <div key={draft.sources[i]} className="media-tile relative size-24">
                    {asset ? (
                      <img src={asset.thumb_url || asset.url} alt="" className="h-full w-full object-cover" />
                    ) : (
                      <div className="technical flex h-full items-center justify-center text-[10px] text-muted">
                        #{draft.sources[i]}
                      </div>
                    )}
                    <IconButton
                      icon="close"
                      label={`Remove source ${draft.sources[i]}`}
                      className="absolute right-0 top-0 bg-black/60 text-white"
                      onClick={() =>
                        update({
                          ...draft,
                          sources: draft.sources.filter((_, j) => j !== i),
                          maskAssetId: null,
                        })
                      }
                    />
                  </div>
                ))}
                {draft.sources.length < firstOp.max_sources && (
                  <button
                    type="button"
                    className="flex size-24 flex-col items-center justify-center gap-1 border border-dashed border-edge text-xs text-muted hover:text-ink"
                    onClick={() => setPicking(true)}
                  >
                    <Icon name="plus" size={16} /> Add from gallery
                  </button>
                )}
              </div>
              <div className="mt-1 text-[11px] text-muted">
                Every variant of the first stage uses these same images.
              </div>
            </div>
          )}

          {needsMask && maskSource && (
            <div>
              <div className="label">Region to change</div>
              {draft.maskAssetId != null && !maskFile ? (
                <div className="flex items-center gap-2 text-xs text-ok">
                  <Icon name="check" size={14} /> Mask saved (asset #{draft.maskAssetId}).
                  <button
                    type="button"
                    className="text-accent underline"
                    onClick={() => update({ ...draft, maskAssetId: null })}
                  >
                    Paint again
                  </button>
                </div>
              ) : (
                <MaskEditor image={maskSource} onMask={setMaskFile} />
              )}
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <div className="label">Seeds</div>
              <SegmentedControl
                label="Seed policy"
                value={draft.seedMode}
                onChange={(seedMode) => update({ ...draft, seedMode })}
                options={[
                  { value: "fixed", label: "Shared seed" },
                  { value: "per_variant", label: "Seed per variant" },
                ]}
              />
              <div className="mt-1 text-[11px] text-muted">
                {draft.seedMode === "fixed"
                  ? "Variants differ only in what their axes change."
                  : "Each variant gets its own stable seed, derived from the base seed and its key."}
              </div>
            </div>
            <div>
              <label className="label" htmlFor="variant-set-seed">
                Base seed (-1 random)
              </label>
              <input
                id="variant-set-seed"
                className="input"
                type="number"
                value={draft.seed}
                onChange={(e) =>
                  update({ ...draft, seed: e.target.value === "" ? -1 : Number(e.target.value) })
                }
              />
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={draft.collection}
              onChange={(e) => update({ ...draft, collection: e.target.checked })}
            />
            Collect the results in a new gallery collection
          </label>

          {draft.stages.map((stage, index) => (
            <VariantStageEditor
              key={stage.uid}
              stage={stage}
              index={index}
              stages={draft.stages}
              operations={caps.operations}
              caps={caps}
              spec={specByKind.get(stage.operation)}
              presets={presets}
              onChange={(next) =>
                update({ ...draft, stages: draft.stages.map((s, j) => (j === index ? next : s)) })
              }
              onRemove={
                index > 0
                  ? () => update({ ...draft, stages: draft.stages.filter((_, j) => j !== index) })
                  : undefined
              }
            />
          ))}
          {draft.stages.length < caps.limits.stages && (
            <button
              type="button"
              className="btn text-xs"
              onClick={() => {
                const derivable = caps.operations.find((op) => op.takes_source);
                if (derivable) update({ ...draft, stages: [...draft.stages, newStage(derivable.kind)] });
              }}
            >
              <Icon name="plus" size={13} /> Add a derived stage
            </button>
          )}
        </div>
      </div>

      <aside
        className="flex min-h-0 flex-col border-t border-edge bg-panel lg:border-l lg:border-t-0"
        aria-label="Variant set preview"
      >
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
          <div>
            <div className="page-kicker">Combinations</div>
            <div
              className={`text-3xl font-semibold ${overCap ? "text-danger" : "text-ink"}`}
              data-testid="variant-total"
            >
              {counts.total} variant{counts.total === 1 ? "" : "s"}
            </div>
            <div className="technical mt-1 text-[11px] text-muted">
              {draft.stages.map((stage, i) => (
                <div key={stage.uid}>
                  Stage {i + 1}: {formula(stage.axes)}
                  {i > 0 ? ` × ${counts.totals[i - 1]} inputs` : ""} = {counts.totals[i]}
                </div>
              ))}
              {draft.stages.length > 1 && <div>Total: {totalLabel}</div>}
            </div>
            {overCap && (
              <div className="mt-2 text-xs text-danger">
                Over the {cap}-generation limit for one set. Remove values or split the work.
              </div>
            )}
          </div>

          {[...problems, sourceProblem, maskProblem].filter(Boolean).length > 0 && (
            <div className="space-y-1 border-l-2 border-warn pl-3 text-xs text-warn">
              {[...problems, sourceProblem, maskProblem].filter(Boolean).map((p) => (
                <div key={p}>{p}</div>
              ))}
            </div>
          )}
          {previewable && previewError && (
            <div className="border-l-2 border-danger pl-3 text-xs text-danger" role="alert">
              {previewError}
            </div>
          )}
          {previewable && preview && preview.collisions.length > 0 && (
            <div className="border-l-2 border-danger pl-3 text-xs text-danger">
              {preview.collisions.length} output name{preview.collisions.length > 1 ? "s are" : " is"} shared
              by several variants — e.g. {preview.collisions[0].name}. Include every axis in the name
              template.
            </div>
          )}
          {(previewable ? (preview?.warnings ?? []) : []).map((w) => (
            <div key={w} className="border-l-2 border-warn pl-3 text-xs text-warn">
              {w}
            </div>
          ))}

          <div>
            <div className="flex items-center justify-between">
              <div className="label mb-0">Preview</div>
              {previewable && previewing && <Spinner />}
            </div>
            {previewable && preview ? (
              <ol className="mt-2 space-y-2">
                {preview.items.map((item) => (
                  <li key={`${item.stage}:${item.key}`} className="border-b border-edge/60 pb-2">
                    <div className="technical truncate text-[10px] text-accent" title={item.output_name}>
                      {item.output_name}
                    </div>
                    <div className="text-xs text-ink/85">{item.prompt || <em>(empty instruction)</em>}</div>
                  </li>
                ))}
                {preview.total > preview.items.length && (
                  <li className="text-[11px] text-muted">
                    …and {preview.total - preview.items.length} more.
                  </li>
                )}
              </ol>
            ) : (
              <div className="mt-2 text-xs text-muted">
                Name an axis and give it values to see every combination.
              </div>
            )}
          </div>
        </div>
        <div className="shrink-0 space-y-2 border-t border-edge p-4">
          {blockedReason && (
            <div className="flex items-start gap-2 text-xs leading-relaxed text-warn">
              <Icon name="alert" size={15} className="mt-0.5 shrink-0" />
              {blockedReason}
            </div>
          )}
          <Button variant="primary" className="w-full" loading={busy} disabled={!canCreate} onClick={create}>
            {busy ? "Creating…" : `Create set · ${counts.total}`}
          </Button>
          <Button
            className="w-full"
            icon="bookmark"
            disabled={!recipe || !!problems.length}
            onClick={() => setNaming(true)}
          >
            Save as recipe
          </Button>
        </div>
      </aside>

      {picking && (
        <GalleryPicker
          title="Choose a source image"
          filter={(asset: Asset) => asset.generator !== "mask"}
          onClose={() => setPicking(false)}
          onPick={(asset) => {
            setPicking(false);
            if (!draft.sources.includes(asset.id)) {
              update({ ...draft, sources: [...draft.sources, asset.id], maskAssetId: null });
            }
          }}
        />
      )}
      {naming && (
        <NameDialog
          title="Save this recipe as…"
          placeholder="recipe name"
          initial={draft.name}
          onSubmit={saveRecipe}
          onClose={() => setNaming(false)}
        />
      )}
    </div>
  );
}

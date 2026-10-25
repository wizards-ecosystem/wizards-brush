import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Asset, PresetRead, PresetSummary, TargetsView } from "../api/types";
import { AssetCard } from "../components/AssetCard";
import { AssetModal } from "../components/AssetModal";
import { DynamicControls } from "../components/DynamicControls";
import { FinishingStepsEditor } from "../components/FinishingStepsEditor";
import { ProgressBar } from "../components/ProgressBar";
import { AdapterStack } from "../components/studio/AdapterStack";
import { EffectiveSetup } from "../components/studio/EffectiveSetup";
import { InputPickers } from "../components/studio/InputPickers";
import { OriginBadge, PresetPicker } from "../components/studio/PresetPicker";
import { SavePresetDialog } from "../components/studio/SavePresetDialog";
import { Button, EmptyState, SegmentedControl } from "../components/ui";
import {
  customizations,
  loadDraft,
  resetToPreset,
  saveDraft,
  switchPreset,
  toRequest,
  type StudioDraft,
} from "../lib/studioDraft";
import { usePreparedRun } from "../lib/usePreparedRun";
import { useSessionResults } from "../lib/useSessionResults";
import { useStore } from "../store/useStore";

/** Settings the plan owns through their own fields, not as plain controls. */
const MANAGED = new Set([
  "prompt",
  "negative_prompt",
  "loras",
  "finish_steps",
  "model_variant",
  "engine",
  "seed",
]);
const VIDEO_OPERATIONS = new Set(["text_to_video", "image_to_video", "long_video"]);
const ROLE_FOR: Record<string, string> = {
  edit: "edit",
  text_to_video: "video",
  image_to_video: "video",
  long_video: "video",
};

const STALE_REASON: Record<string, string> = {
  "no-plan": "Choose a preset and write a prompt.",
  previewing: "Working out the setup…",
  "draft-changed": "Updating to match your changes…",
  "capabilities-changed": "Updating: what this install can run changed.",
  blocked: "This setup cannot run here yet; see the notes on the right.",
  submitting: "Submitting…",
};

/**
 * The preset-first studio: choose a setup, describe the image, Generate.
 *
 * A preset supplies the model, adapters, prompt template and settings; the
 * person adds a subject and, only if they want to, changes anything else in
 * Settings. The server resolves every draft into the plan shown under "What
 * will run" and that plan is exactly what Generate submits. The classic
 * per-process generators stay one link away, for video tools and everything
 * a preset does not cover.
 */
export function Studio() {
  const toast = useStore((s) => s.toast);
  const specById = useStore((s) => s.specById);
  const presetsLibrary = useStore((s) => s.presets);
  const jobs = useStore((s) => s.jobs);
  const previews = useStore((s) => s.previews);
  const controlsRevision = useStore((s) => s.controlsRevision);

  const [initial] = useState(() => loadDraft(localStorage));
  const [draft, setDraft] = useState<StudioDraft>(initial.draft);
  const [draftWarning, setDraftWarning] = useState(initial.warning);
  const [undo, setUndo] = useState<{ draft: StudioDraft; dropped: string[]; from: string } | null>(null);
  const [presets, setPresets] = useState<PresetSummary[] | null>(null);
  const [preset, setPreset] = useState<PresetRead | null>(null);
  const [targets, setTargets] = useState<TargetsView | null>(null);
  const [output, setOutput] = useState<"image" | "video">("image");
  const [showSettings, setShowSettings] = useState(false);
  const [choosing, setChoosing] = useState(!initial.draft.presetId);
  const [saving, setSaving] = useState(false);
  const [submitted, setSubmitted] = useState<number[]>([]);
  const [open, setOpen] = useState<Asset | null>(null);
  const composeRef = useRef<HTMLElement>(null);
  const location = useLocation();
  const navigate = useNavigate();

  const request = useMemo(() => toRequest(draft), [draft]);
  const run = usePreparedRun(request);
  const plan = run.state.preview.plan;
  const results = useSessionResults(submitted);

  useEffect(() => saveDraft(localStorage, draft), [draft]);

  const reloadPresets = useCallback(() => {
    api
      .studioPresets()
      .then(setPresets)
      .catch(() => setPresets([]));
  }, []);
  useEffect(reloadPresets, [reloadPresets, controlsRevision]);
  useEffect(() => {
    api
      .targets()
      .then(setTargets)
      .catch(() => setTargets(null));
  }, [controlsRevision]);
  useEffect(() => {
    if (!draft.presetId) return;
    let alive = true;
    api
      .studioPreset(draft.presetId, draft.revision ?? undefined)
      .then((read) => alive && setPreset(read))
      .catch(() => alive && setPreset(null));
    return () => {
      alive = false;
    };
  }, [draft.presetId, draft.revision]);

  const summary = presets?.find((p) => p.id === draft.presetId) ?? null;
  // The loaded definition counts only while it is the draft's preset.
  const definition = preset && preset.id === draft.presetId ? preset.definition : null;
  const operation = definition?.operation ?? "text_to_image";
  const changed = customizations(draft);
  const upgradeTo =
    summary && draft.revision != null && summary.head_revision > draft.revision
      ? summary.head_revision
      : null;
  const spec = plan?.kind ? specById(plan.kind) : undefined;
  const controls = (spec?.controls ?? []).filter((c) => !MANAGED.has(c.name));
  const quick = controls.filter((c) => c.name === "aspect" || c.name === "batch");
  const values = { ...(plan?.settings ?? {}), ...draft.settings } as Record<string, unknown>;

  const update = (patch: Partial<StudioDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const setSetting = (name: string, value: unknown) =>
    update({ settings: { ...draft.settings, [name]: value } });

  const choose = async (picked: PresetSummary) => {
    try {
      const read = await api.studioPreset(picked.id);
      const { draft: next, dropped } = switchPreset(draft, read);
      if (draft.presetId && draft.presetId !== picked.id) {
        setUndo({ draft, dropped, from: summary?.name ?? draft.presetId });
      }
      setPreset(read);
      setDraft(next);
      setChoosing(false);
    } catch (error) {
      toast(`Couldn't open that preset: ${error}`, "error");
    }
  };

  // "Open in Studio" from the preset library hands over a preset revision.
  const handed = (location.state as { presetId?: string; revision?: number } | null) ?? null;
  useEffect(() => {
    if (!handed?.presetId) return;
    let alive = true;
    api
      .studioPreset(handed.presetId, handed.revision)
      .then((read) => {
        if (!alive) return;
        const { draft: next } = switchPreset(draft, read);
        setPreset(read);
        setDraft(next);
        setChoosing(false);
        navigate(location.pathname, { replace: true, state: null });
      })
      .catch(() => toast("Couldn't open that preset", "error"));
    return () => {
      alive = false;
    };
    // Runs once per hand-over; the draft it switches from is the current one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handed?.presetId, handed?.revision]);

  const generate = useCallback(async () => {
    const result = await run.submit();
    if (result) {
      const ids = result.job_ids?.length ? result.job_ids : [result.job_id];
      setSubmitted((s) => [...s, ...ids.filter((id) => !s.includes(id))]);
      toast(result.duplicate ? `Already queued as #${result.job_id}` : `Queued #${result.job_id}`, "success");
    }
  }, [run, toast]);

  // Ctrl/Cmd+Enter submits from the compose panel only, never from a dialog.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key !== "Enter" || event.defaultPrevented) return;
      const panel = composeRef.current;
      if (!panel || !(event.target instanceof Node) || !panel.contains(event.target)) return;
      if (event.target instanceof Element && event.target.closest('[role="dialog"]')) return;
      event.preventDefault();
      void generate();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [generate]);

  const createStarter = async () => {
    const model = targets?.local.models.find((m) => m.role === "image");
    if (!model) {
      toast("No local image model is configured to start from.", "error");
      return;
    }
    try {
      const created = await api.createStudioPreset({
        name: "Starter",
        description: `${model.repo} with no adapters: the model's own look.`,
        tags: ["starter"],
        operation: "text_to_image",
        model: { resource: model.resource },
      });
      reloadPresets();
      await choose(created);
    } catch (error) {
      toast(`Couldn't create a starter preset: ${error}`, "error");
    }
  };

  const modelChoices = (
    targets
      ? [
          ...targets.local.models.map((m) => ({ ...m, where: "This machine" })),
          ...targets.remote.models.map((m) => ({ ...m, where: "Remote GPU" })),
        ]
      : []
  ).filter((m) => m.role === (ROLE_FOR[operation] ?? "image"));
  const sessionJobs = submitted.map((id) => jobs[id] ?? results[id]?.job).filter(Boolean);
  const active = sessionJobs.filter((j) => j && (j.status === "queued" || j.status === "running"));
  const outputs = submitted
    .flatMap((id) => results[id]?.assets ?? [])
    .slice()
    .reverse();
  const failed = submitted.map((id) => results[id]?.job).filter((j) => j && j.status === "error");

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_clamp(380px,32vw,460px)]">
        <aside
          ref={composeRef}
          className="flex min-h-0 flex-col bg-panel lg:col-start-2 lg:row-start-1 lg:border-l lg:border-edge"
          aria-label="Compose"
        >
          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-5 sm:px-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="page-kicker">Studio</div>
                <h1 className="text-[25px] font-semibold tracking-[-0.025em]">
                  Make {output === "image" ? "an image" : "a video"}
                </h1>
              </div>
              <Link to="/processes" className="text-xs text-muted underline-offset-2 hover:underline">
                All processes
              </Link>
            </div>
            <SegmentedControl
              className="w-full"
              label="What to make"
              value={output}
              onChange={(next) => {
                setOutput(next);
                if (summary && (next === "video") !== VIDEO_OPERATIONS.has(String(summary.operation)))
                  setChoosing(true);
              }}
              options={[
                { value: "image", label: "Image" },
                { value: "video", label: "Video" },
              ]}
            />
            {draftWarning && (
              <p className="border-l-2 border-warn pl-2 text-[11px] text-warn">
                {draftWarning}{" "}
                <button className="underline" onClick={() => setDraftWarning("")}>
                  Dismiss
                </button>
              </p>
            )}

            <section aria-label="Preset">
              {presets && presets.length === 0 ? (
                <EmptyState
                  icon="spark"
                  title="No presets yet"
                  description="A preset is a model, its adapters and settings that work together. Start from the configured model, or build one in Recipes."
                  action={
                    <Button variant="primary" onClick={() => void createStarter()}>
                      Create a starter preset
                    </Button>
                  }
                />
              ) : choosing || !summary ? (
                <PresetPicker
                  presets={presets ?? []}
                  selected={draft.presetId}
                  onSelect={(p) => void choose(p)}
                  output={output}
                />
              ) : (
                <div className="flex items-start justify-between gap-2 border border-edge bg-panel2 px-3 py-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium" data-testid="preset-name">
                        {summary.name}
                      </span>
                      <OriginBadge preset={summary} />
                      {changed.length > 0 && (
                        <span
                          className="technical border border-warn/60 px-1.5 py-0.5 text-[9px] uppercase text-warn"
                          data-testid="customized"
                        >
                          Customized
                        </span>
                      )}
                    </div>
                    <div className="technical mt-0.5 text-[10px] text-muted">
                      revision {draft.revision ?? summary.head_revision}
                    </div>
                  </div>
                  <Button size="sm" onClick={() => setChoosing(true)}>
                    Change
                  </Button>
                </div>
              )}
              {undo && (
                <p
                  className="mt-2 border-l-2 border-accent pl-2 text-[11px] text-muted"
                  data-testid="switch-summary"
                >
                  Switched from {undo.from}.
                  {undo.dropped.length ? ` Not carried over: ${undo.dropped.join(", ")}.` : ""}{" "}
                  <button
                    className="text-accent underline"
                    onClick={() => {
                      setDraft(undo.draft);
                      setUndo(null);
                    }}
                  >
                    Undo
                  </button>
                </p>
              )}
              {(changed.length > 0 || upgradeTo) && summary && (
                <div className="mt-2 flex flex-wrap gap-2 text-xs">
                  {changed.length > 0 && (
                    <button className="text-accent underline" onClick={() => setDraft(resetToPreset(draft))}>
                      Reset to preset ({changed.join(", ")})
                    </button>
                  )}
                  {upgradeTo && (
                    <button className="text-accent underline" onClick={() => update({ revision: upgradeTo })}>
                      Upgrade to revision {upgradeTo}
                    </button>
                  )}
                </div>
              )}
            </section>

            {summary && (
              <>
                <div>
                  <label className="label" htmlFor="studio-prompt">
                    Prompt
                  </label>
                  <textarea
                    id="studio-prompt"
                    className="input min-h-[120px] resize-y text-sm"
                    placeholder="Describe what you want to see"
                    value={draft.prompt}
                    onChange={(event) => update({ prompt: event.target.value })}
                  />
                </div>
                {(definition?.prompt?.styles?.length ?? 0) > 0 && (
                  <div role="group" aria-label="Styles">
                    <div className="label">Styles</div>
                    <div className="flex flex-wrap gap-1.5">
                      {definition!.prompt!.styles!.map((style) => {
                        const selected = (draft.styles ?? definition!.prompt!.default_styles ?? []).includes(
                          style.id,
                        );
                        return (
                          <button
                            key={style.id}
                            type="button"
                            className={`chip text-[11px] ${selected ? "border-accent text-ink" : ""}`}
                            aria-pressed={selected}
                            title={style.text}
                            onClick={() => {
                              const current = draft.styles ?? definition!.prompt!.default_styles ?? [];
                              update({
                                styles: selected
                                  ? current.filter((s) => s !== style.id)
                                  : [...current, style.id],
                              });
                            }}
                          >
                            {style.label || style.id}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
                {(definition?.prompt?.enhancement?.length ?? 0) > 0 && (
                  <label className="flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={draft.enhance ?? definition!.prompt!.enhance_by_default ?? false}
                      onChange={(event) => update({ enhance: event.target.checked })}
                    />
                    Add this preset's enhancement ({definition!.prompt!.enhancement!.join(", ")})
                  </label>
                )}
                {(definition?.prompt?.template ?? "").includes("{{notes}}") && (
                  <div>
                    <label className="label" htmlFor="studio-notes">
                      Notes kept in the prompt
                    </label>
                    <input
                      id="studio-notes"
                      className="input text-xs"
                      value={draft.notes}
                      onChange={(event) => update({ notes: event.target.value })}
                    />
                  </div>
                )}
                <InputPickers
                  operation={operation}
                  definition={definition}
                  images={draft.images}
                  mask={draft.mask}
                  references={draft.references}
                  onImages={(images) => update({ images })}
                  onMask={(mask) => update({ mask })}
                  onReferences={(references) => update({ references })}
                />
                {quick.length > 0 && (
                  <DynamicControls
                    controls={quick}
                    values={values}
                    onChange={setSetting}
                    presets={presetsLibrary}
                  />
                )}
                <div>
                  <SegmentedControl
                    className="w-full"
                    label="Where to run"
                    value={draft.target}
                    onChange={(target) => update({ target })}
                    options={[
                      { value: "auto", label: "Auto" },
                      { value: "local", label: "This machine" },
                      { value: "remote", label: "Remote GPU" },
                    ]}
                  />
                  <label className="mt-2 flex items-center gap-2 text-[11px] text-muted">
                    <input
                      type="checkbox"
                      checked={draft.allowRemote}
                      onChange={(event) => update({ allowRemote: event.target.checked })}
                    />
                    Allow this request to use the Remote GPU and send it its images (never starts a rental)
                  </label>
                </div>

                <details
                  open={showSettings}
                  onToggle={(event) => setShowSettings((event.target as HTMLDetailsElement).open)}
                >
                  <summary className="cursor-pointer text-sm">Settings</summary>
                  <div className="mt-3 space-y-4">
                    <div>
                      <label className="label" htmlFor="studio-model">
                        Model
                      </label>
                      <select
                        id="studio-model"
                        className="input text-xs"
                        value={draft.model ?? definition?.model.resource ?? ""}
                        onChange={(event) =>
                          update({
                            model:
                              event.target.value === definition?.model.resource ? null : event.target.value,
                          })
                        }
                      >
                        {definition &&
                          !modelChoices.some((m) => m.resource === definition.model.resource) && (
                            <option value={definition.model.resource}>
                              {definition.model.resource} (preset)
                            </option>
                          )}
                        {modelChoices.map((m) => (
                          <option key={`${m.where}-${m.resource}`} value={m.resource}>
                            {m.repo} · {m.variant} · {m.where}
                          </option>
                        ))}
                      </select>
                    </div>
                    {operation !== "edit" && !VIDEO_OPERATIONS.has(operation) && (
                      <div>
                        <div className="label">Adapters</div>
                        <AdapterStack
                          planAdapters={plan?.adapters ?? []}
                          value={draft.adapters}
                          onChange={(adapters) => update({ adapters })}
                        />
                      </div>
                    )}
                    <div>
                      <div className="label">Finishing, in order</div>
                      <FinishingStepsEditor
                        steps={(draft.finish ?? definition?.finish ?? []) as { processor: string }[]}
                        processors={spec?.controls.find((c) => c.name === "finish_steps")?.processors ?? []}
                        onChange={(steps) => update({ finish: steps })}
                      />
                    </div>
                    <div>
                      <label className="label" htmlFor="studio-negative">
                        Negative prompt
                      </label>
                      <textarea
                        id="studio-negative"
                        className="input min-h-[52px] text-xs"
                        value={draft.negative ?? definition?.prompt?.negative ?? ""}
                        onChange={(event) => update({ negative: event.target.value })}
                      />
                    </div>
                    <div className="flex items-end gap-2">
                      <div className="flex-1">
                        <label className="label" htmlFor="studio-seed">
                          Seed
                        </label>
                        <input
                          id="studio-seed"
                          className="input text-xs"
                          inputMode="numeric"
                          placeholder={run.state.pinnedSeed != null ? String(run.state.pinnedSeed) : "random"}
                          value={draft.seed ?? ""}
                          onChange={(event) => {
                            const n = Number(event.target.value);
                            update({
                              seed:
                                event.target.value === "" || !Number.isFinite(n)
                                  ? null
                                  : Math.max(0, Math.floor(n)),
                            });
                          }}
                        />
                      </div>
                      <Button
                        size="sm"
                        icon="refresh"
                        onClick={() => {
                          update({ seed: null });
                          run.newSeed();
                        }}
                      >
                        New seed
                      </Button>
                    </div>
                    {controls.length > 0 && (
                      <DynamicControls
                        controls={controls.filter((c) => !quick.includes(c))}
                        values={values}
                        onChange={setSetting}
                        presets={presetsLibrary}
                      />
                    )}
                  </div>
                </details>

                <section aria-label="What will run" className="border-t border-edge pt-4">
                  <div className="label">What will run</div>
                  <EffectiveSetup plan={plan} stale={run.stale} error={run.state.preview.error} />
                </section>
              </>
            )}
          </div>

          <div className="shrink-0 border-t border-edge bg-panel px-4 py-3 sm:px-5">
            {run.state.submit.error && (
              <p role="alert" className="mb-2 text-xs text-danger" data-testid="submit-error">
                {run.state.submit.error.detail}
              </p>
            )}
            {!run.canSubmit && run.stale && summary && (
              <p className="mb-2 text-[11px] text-muted" data-testid="submit-blocked">
                {STALE_REASON[run.stale]}
              </p>
            )}
            <div className="flex gap-2">
              <Button disabled={!plan || !summary} onClick={() => setSaving(true)}>
                Save preset
              </Button>
              <Button
                variant="primary"
                className="flex-1"
                loading={run.state.submit.status === "submitting"}
                disabled={!run.canSubmit}
                onClick={() => void generate()}
              >
                Generate
              </Button>
            </div>
            <div className="technical mt-2 text-center text-[9px] uppercase tracking-wide text-white/28">
              Ctrl / ⌘ + Enter
            </div>
          </div>
        </aside>

        <section className="flex min-h-0 flex-col bg-bg lg:col-start-1 lg:row-start-1" aria-label="Session">
          <div className="flex shrink-0 items-end justify-between border-b border-edge px-4 py-4 sm:px-6">
            <div>
              <div className="page-kicker">This session</div>
              <h2 className="text-lg font-semibold">Results</h2>
            </div>
            <div className="technical text-[10px] uppercase text-muted">
              {outputs.length} results · {active.length} active
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
            {active.length > 0 && (
              <div className="mb-6 grid gap-3 border-l-[3px] border-accent bg-panel px-4 py-3 xl:grid-cols-2">
                {active.map((job) => (
                  <div key={job!.id} className="min-w-0">
                    <ProgressBar job={job!} preview={previews[job!.id]} />
                    <Button
                      variant="quiet"
                      size="sm"
                      className="mt-1 text-danger"
                      onClick={() => void api.cancel(job!.id)}
                    >
                      Cancel #{job!.id}
                    </Button>
                  </div>
                ))}
              </div>
            )}
            {failed.length > 0 && (
              <ul className="mb-4 space-y-1 text-xs" data-testid="session-failures">
                {failed.map((job) => (
                  <li key={job!.id} className="border-l-2 border-danger pl-2">
                    #{job!.id} failed{job!.tip ? `: ${job!.tip}` : ""}
                  </li>
                ))}
              </ul>
            )}
            {outputs.length === 0 ? (
              <div className="flex min-h-[380px] items-center justify-center">
                <EmptyState
                  icon="spark"
                  title={active.length ? "The first result is on its way" : "Nothing made yet"}
                  description="Pick a preset, describe the image and press Generate. Results from this session collect here."
                />
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-4">
                {outputs.map((asset) => (
                  <div key={asset.id} className="space-y-1">
                    <AssetCard asset={asset} onClick={setOpen} />
                    <div className="flex flex-wrap gap-1 text-[10px]">
                      <button
                        className="text-muted underline-offset-2 hover:text-ink hover:underline"
                        onClick={() =>
                          update({
                            prompt: String(asset.meta.raw_prompt ?? asset.meta.prompt ?? draft.prompt),
                          })
                        }
                      >
                        Reuse prompt
                      </button>
                      {asset.job_id != null && (
                        <button
                          className="text-muted underline-offset-2 hover:text-ink hover:underline"
                          onClick={() =>
                            api
                              .replayJob(asset.job_id!, "exact")
                              .then((r) => {
                                setSubmitted((s) => [...s, r.job_id]);
                                toast(`Replaying as #${r.job_id}`, "success");
                              })
                              .catch((error) => toast(`${error}`, "error"))
                          }
                        >
                          Replay exactly
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      </div>

      {open && <AssetModal asset={open} onClose={() => setOpen(null)} list={outputs} onNavigate={setOpen} />}
      {saving && plan && (
        <SavePresetDialog
          preset={preset}
          plan={plan}
          draft={draft}
          onClose={() => setSaving(false)}
          onSaved={(saved) => {
            setSaving(false);
            toast(`Saved ${saved.name}`, "success");
            reloadPresets();
            setDraft((d) => ({ ...resetToPreset(d), presetId: saved.id, revision: saved.revision }));
            setPreset(saved);
          }}
        />
      )}
    </div>
  );
}

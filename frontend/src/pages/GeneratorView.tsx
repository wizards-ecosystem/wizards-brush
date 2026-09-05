import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { api, authHeaders } from "../api/client";
import type { Asset, GridConfig, ImageSlot, PromptHistoryItem, WildcardInfo } from "../api/types";
import { AssetCard } from "../components/AssetCard";
import { AssetModal } from "../components/AssetModal";
import { DynamicControls, overriddenHidden, styleMap } from "../components/DynamicControls";
import { Icon } from "../components/icons";
import { MaskEditor } from "../components/MaskEditor";
import { MultiImageDropzone } from "../components/MultiImageDropzone";
import { ProgressBar } from "../components/ProgressBar";
import { XYGridPanel, GRID_HARD_CAP } from "../components/XYGridPanel";
import { Button, EmptyState, IconButton, NameDialog, SegmentedControl, Spinner } from "../components/ui";
import {
  applyDependentDefaults,
  coerceValues,
  effectiveControlDefault,
  prefillableParams,
} from "../lib/generators";
import { COMBO_CAP, estimateCombinations } from "../lib/wildcards";
import { useStore } from "../store/useStore";

const DEPTH_KEY = "gen-control-depth";
const DRAFT_EPOCH_KEY = "wizard-brush-draft-epoch";
const DRAFT_EPOCH = "3";

/**
 * Prompt history has a second, intentionally separate copy in per-generator
 * browser drafts. When the durable history is reset, leaving those behind
 * makes an old prompt reappear the next time that generator opens. Epoch the
 * keys once and remove only generation drafts; API credentials and UI
 * preferences in the same localStorage are deliberately untouched.
 */
function currentDraftEpoch(): string {
  try {
    if (localStorage.getItem(DRAFT_EPOCH_KEY) !== DRAFT_EPOCH) {
      const stale: string[] = [];
      for (let i = 0; i < localStorage.length; i += 1) {
        const key = localStorage.key(i);
        if (key?.startsWith("gen-draft-")) stale.push(key);
      }
      stale.forEach((key) => localStorage.removeItem(key));
      localStorage.setItem(DRAFT_EPOCH_KEY, DRAFT_EPOCH);
    }
  } catch {
    /* private mode / denied storage: drafts were unavailable anyway */
  }
  return DRAFT_EPOCH;
}

export function GeneratorView() {
  const { id } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const spec = useStore((s) => s.specById(id || ""));
  const presets = useStore((s) => s.presets);
  const system = useStore((s) => s.system);
  const jobs = useStore((s) => s.jobs);
  const previews = useStore((s) => s.previews);
  const assets = useStore((s) => s.assets);
  const toast = useStore((s) => s.toast);
  const refreshPresets = useStore((s) => s.refreshPresets);
  const refreshJobs = useStore((s) => s.refreshJobs);

  const [values, setValues] = useState<Record<string, any>>({});
  const [files, setFiles] = useState<Record<string, File | null>>({});
  const [mask, setMask] = useState<File | null>(null);
  const [grid, setGrid] = useState<GridConfig | null>(null);
  const [submitted, setSubmitted] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  // State disables the button on the next render; the ref closes the smaller
  // same-frame window where a second click/keyboard event could still submit.
  const submittingRef = useRef(false);
  const [open, setOpen] = useState<Asset | null>(null);
  const [history, setHistory] = useState<PromptHistoryItem[]>([]);
  const [showHist, setShowHist] = useState(false);
  const [naming, setNaming] = useState(false);
  const [wildcards, setWildcards] = useState<WildcardInfo[]>([]);
  const [showWc, setShowWc] = useState(false);
  const [mobileView, setMobileView] = useState<"compose" | "session">("compose");
  const [adjustedState, setAdjustedState] = useState<string[]>([]);
  const [metadataNote, setMetadataNote] = useState("");
  const metadataRef = useRef<HTMLInputElement>(null);
  const [draftEpoch] = useState(currentDraftEpoch);
  // Simple is the default form, and the preference is global rather than
  // per-generator: "how much do you want to be asked" is a fact about the
  // person, not about whether they are inpainting today.
  const [depth, setDepth] = useState<"simple" | "full">(() => {
    try {
      return localStorage.getItem(DEPTH_KEY) === "full" ? "full" : "simple";
    } catch {
      return "simple";
    }
  });
  const simple = depth === "simple";

  const draftKey = spec ? `gen-draft-${draftEpoch}-${spec.id}` : "";
  const editOfJob: number | undefined = (location.state as any)?.editOfJob;

  // Declared image slots (multi-image generators) or the legacy single slot.
  const slots: ImageSlot[] = useMemo(() => {
    if (!spec) return [];
    if (spec.image_inputs?.length) return spec.image_inputs;
    if (spec.needs_image) return [{ name: "image", label: "Input image", required: true }];
    return [];
  }, [spec]);

  // Initialize form: navigation prefill > saved draft > generator defaults.
  // Done with React's adjust-state-during-render pattern (not an effect) so the
  // prefilled form paints in one pass. Session state (results, files, mask,
  // grid) resets only when the GENERATOR changes — a same-page navigate (e.g.
  // submit's state cleanup after editing a queued job) mints a new location.key
  // and must not wipe the job the user just queued out of the results pane.
  const initKey = spec ? `${spec.id}|${location.key}` : "";
  const [prevInitKey, setPrevInitKey] = useState("");
  const [prevSpecId, setPrevSpecId] = useState<string | null>(null);
  if (spec && prevInitKey !== initKey) {
    setPrevInitKey(initKey);
    const prefill = (location.state as any)?.prefillValues;
    let raw: Record<string, any> = {};
    if (prefill) {
      raw = prefillableParams(prefill);
    } else {
      try {
        const saved = localStorage.getItem(draftKey);
        if (saved) raw = JSON.parse(saved);
      } catch {
        /* ignore bad draft */
      }
    }
    const validStyles = new Set(Object.keys(styleMap(presets)));
    const coerced = coerceValues(spec.controls, raw, validStyles);
    setValues(coerced.values);
    setAdjustedState(coerced.adjusted);
    const fromAsset = (location.state as any)?.fromAsset;
    if (prevSpecId !== spec.id) {
      setPrevSpecId(spec.id);
      setMobileView("compose");
      setFiles({});
      setMask(null);
      setGrid(null);
      setSubmitted([]);
      setMetadataNote("");
    } else if (prefill || fromAsset) {
      // Same generator but an explicit "send to" / "edit job" navigation: the
      // inputs belong to the previous source — a stale mask drawn for image A
      // must not be uploaded with image B. Session results stay.
      setFiles({});
      setMask(null);
      setGrid(null);
    }
  }

  // "Send to img2img/video" passes the source asset to load as the input image.
  // The fetch is the only part that needs an effect.
  useEffect(() => {
    const fromAsset = (location.state as any)?.fromAsset;
    if (!slots.length || !fromAsset?.url) return;
    // The frame endpoint lives under /api — token-gated when API_TOKEN is set.
    fetch(fromAsset.url, { headers: authHeaders() })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(`${r.status}`))))
      .then((b) =>
        setFiles((f) => ({
          ...f,
          image: new File([b], fromAsset.filename || "input.png", { type: b.type || "image/png" }),
        })),
      )
      .catch(() => toast("Couldn't load the source image", "error"));
  }, [location.state, slots, toast]);

  // Persist a draft as the user edits (skip files).
  useEffect(() => {
    if (spec && Object.keys(values).length) {
      try {
        localStorage.setItem(draftKey, JSON.stringify(values));
      } catch {
        /* quota — ignore */
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values]);

  useEffect(() => {
    try {
      localStorage.setItem(DEPTH_KEY, depth);
    } catch {
      /* quota — the switch still works for this session */
    }
  }, [depth]);

  useEffect(() => {
    api
      .history(60)
      .then(setHistory)
      .catch(() => {});
    api
      .wildcards()
      .then(setWildcards)
      .catch(() => setWildcards([]));
  }, [spec?.id]);

  const remoteBlocked = spec?.needs_remote && !system?.remote_gpu?.connected;
  const blockedReason =
    spec?.unavailable_reason ||
    (remoteBlocked
      ? "Remote GPU is disconnected. Start the remote service and update the connection in Settings."
      : "");
  const results = useMemo(
    () => assets.filter((a) => a.job_id != null && submitted.includes(a.job_id)),
    [assets, submitted],
  );
  // Every still-active job from this session — local + Remote GPU lanes can run in
  // parallel, so several progress bars may be live at once.
  const activeJobs = useMemo(
    () =>
      submitted.map((jid) => jobs[jid]).filter((j) => j && (j.status === "running" || j.status === "queued")),
    [jobs, submitted],
  );

  const set = (name: string, value: any) =>
    setValues((v) => ({
      ...v,
      [name]: value,
      // A LoRA belongs to the selected model family. Reset the list before the
      // next render so a model switch cannot submit stale adapters.
      ...(name === "model_variant" ? { loras: [] } : {}),
      // Controls declaring defaults_by follow this field when the user has not
      // overridden them — e.g. guidance tracks model_variant, so picking
      // "quality" moves the slider off Turbo's CFG 1.0 instead of silently
      // running a CFG model at CFG 1.0.
      ...applyDependentDefaults(spec?.controls ?? [], name, v[name], value, v),
    }));

  const composedPrompt = () => {
    const map = styleMap(presets);
    const styleTexts = (values.style_ids || []).map((sid: string) => map[sid]).filter(Boolean);
    return [values.prompt, ...styleTexts].filter(Boolean).join(", ");
  };

  // Settings the Simple form does not show but is still sending, because the
  // user changed them in Full mode and switched back.
  const hiddenOverrides = useMemo(
    () => (simple && spec ? overriddenHidden(spec.controls, values) : []),
    [simple, spec, values],
  );
  const clearHidden = () =>
    setValues((v) => ({
      ...v,
      ...Object.fromEntries(hiddenOverrides.map((c) => [c.name, effectiveControlDefault(c, v)])),
    }));

  // Switching to Simple hides the grid panel, so it must also drop any grid
  // already configured — otherwise Generate would silently queue N jobs from a
  // panel that is no longer on screen.
  const changeDepth = (next: "simple" | "full") => {
    setDepth(next);
    if (next === "simple") setGrid(null);
  };

  const combos = estimateCombinations(values.prompt || "");
  const missingRequired = slots.some((s) => s.required && !files[s.name]);
  const missingMask = !!spec?.needs_mask && !mask;
  const gridCells = grid ? grid.x.values.length * (grid.y ? grid.y.values.length : 1) : 0;
  const gridBlocked = gridCells > GRID_HARD_CAP;

  const submit = async () => {
    if (!spec || submittingRef.current || busy || blockedReason || gridBlocked) return;
    if (missingRequired || missingMask) {
      toast(
        missingMask ? "Paint the area to regenerate first." : "Add the required input image first.",
        "error",
      );
      return;
    }
    submittingRef.current = true;
    setBusy(true);
    try {
      const requestId =
        typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `wb_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
      const payload: Record<string, any> = {
        ...values,
        request_id: requestId,
        raw_prompt: values.prompt || "",
        prompt: composedPrompt(),
      };

      if (grid && !slots.length) {
        const r = await api.generateGrid(spec.id, payload, grid);
        setSubmitted((s) => [...s, ...r.job_ids]);
        toast(`Queued grid of ${r.job_ids.length} jobs`, "success");
        refreshJobs();
        navigate(`/grid/${r.group_id}`);
        return;
      }

      const uploads: Record<string, File | null | undefined> = { ...files, mask: mask || undefined };
      const r = await api.generate(spec.endpoint, payload, uploads);
      const ids = r.job_ids?.length ? r.job_ids : [r.job_id];
      setSubmitted((s) => [...s, ...ids]);
      setMobileView("session");
      const n = Number(values.batch) || 1;
      toast(
        ids.length > 1
          ? `Queued ${ids.length} combination jobs`
          : `Queued job #${r.job_id}${n > 1 ? ` · batch ×${n}` : ""}`,
        "success",
      );
      if (editOfJob != null) {
        try {
          await api.cancel(editOfJob);
          toast(`Canceled original #${editOfJob}`, "info");
        } catch {
          /* original may have started — leave it */
        }
        navigate(location.pathname, { replace: true, state: {} });
      }
      api
        .history(60)
        .then(setHistory)
        .catch(() => {});
    } catch (e) {
      toast(`Failed: ${e}`, "error");
    } finally {
      submittingRef.current = false;
      setBusy(false);
    }
  };

  // Ctrl/Cmd+Enter to generate.
  const submitRef = useRef(submit);
  useEffect(() => {
    submitRef.current = submit; // kept fresh in an effect — refs must not be written in render
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        submitRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const savePrompt = async (name: string) => {
    await api.createPreset({ type: "prompt", name, payload: { text: values.prompt } });
    await refreshPresets();
    toast("Prompt saved to presets", "success");
  };

  const recall = (h: PromptHistoryItem) => {
    const raw = Object.keys(h.params || {}).length
      ? prefillableParams(h.params)
      : { prompt: h.prompt, negative_prompt: h.negative };
    const next = coerceValues(spec?.controls ?? [], raw, new Set(Object.keys(styleMap(presets))));
    setValues(next.values);
    setAdjustedState(next.adjusted);
    setShowHist(false);
  };

  const insertWildcard = (name: string) => {
    setValues((v) => ({ ...v, prompt: `${v.prompt || ""} __${name}__`.trim() }));
    setShowWc(false);
  };

  const loadImageMetadata = async (file: File) => {
    if (!spec) return;
    try {
      const imported = await api.readMetadata(file);
      if (imported.scheme === "none" || !Object.keys(imported.params).length) {
        toast("No reusable generation settings were found in that image.", "info");
        return;
      }
      // A foreign block is often partial. Merge over the current form first so
      // absent fields stay exactly as they are, then validate every resulting
      // value against this generator's live schema.
      const next = coerceValues(
        spec.controls,
        { ...values, ...imported.params },
        new Set(Object.keys(styleMap(presets))),
      );
      setValues(next.values);
      setAdjustedState(next.adjusted);
      const skipped = imported.unresolved.length;
      setMetadataNote(
        `Loaded ${imported.scheme === "wizards-brush" ? "The Wizard's Brush" : "A1111-compatible"} settings${skipped ? `; skipped ${skipped} unavailable value${skipped === 1 ? "" : "s"}` : ""}.`,
      );
      toast("Image settings loaded", "success");
    } catch (error) {
      toast(`Couldn't read image settings: ${error}`, "error");
    }
  };

  if (!spec) return <SkeletonView />;

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      <div className="shrink-0 border-b border-edge bg-panel px-3 py-2 lg:hidden">
        <SegmentedControl
          label="Generator workspace"
          value={mobileView}
          onChange={setMobileView}
          options={[
            { value: "compose", label: "Compose" },
            {
              value: "session",
              label: `Session${results.length || activeJobs.length ? ` · ${results.length + activeJobs.length}` : ""}`,
            },
          ]}
        />
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_clamp(380px,31vw,440px)]">
        <aside
          className={`${mobileView === "compose" ? "flex" : "hidden"} min-h-0 flex-col bg-panel lg:col-start-2 lg:row-start-1 lg:flex lg:border-l lg:border-edge`}
          aria-label="Generation controls"
        >
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-5">
            <div className="relative mb-6">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="page-kicker">
                    {spec.output === "image" ? "Image process" : "Motion process"}
                  </div>
                  <h1 className="text-[25px] font-semibold tracking-[-0.025em]">{spec.title}</h1>
                  <p className="mt-1 text-xs leading-relaxed text-muted">{spec.subtitle}</p>
                  {editOfJob != null && (
                    <p className="mt-2 border-l-2 border-warn pl-2 text-[11px] text-warn">
                      Editing queued job #{editOfJob}; submitting cancels the original.
                    </p>
                  )}
                  {adjustedState.length > 0 && (
                    <p className="mt-2 border-l-2 border-warn pl-2 text-[11px] text-warn">
                      Updated stale saved settings to match what is available now: {adjustedState.join(", ")}.
                    </p>
                  )}
                  {metadataNote && (
                    <p className="mt-2 border-l-2 border-accent pl-2 text-[11px] text-muted">
                      {metadataNote}
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 gap-0.5">
                  <IconButton
                    icon="history"
                    label="Recall a recent prompt"
                    aria-expanded={showHist}
                    onClick={() => {
                      setShowHist((shown) => !shown);
                      setShowWc(false);
                    }}
                  />
                  {wildcards.length > 0 && (
                    <IconButton
                      icon="braces"
                      label="Insert a wildcard"
                      aria-expanded={showWc}
                      onClick={() => {
                        setShowWc((shown) => !shown);
                        setShowHist(false);
                      }}
                    />
                  )}
                  <IconButton
                    icon="image"
                    label="Load generation settings from an image"
                    onClick={() => metadataRef.current?.click()}
                  />
                  <input
                    ref={metadataRef}
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    className="hidden"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void loadImageMetadata(file);
                      event.target.value = "";
                    }}
                  />
                  <IconButton
                    icon="bookmark"
                    label="Save current prompt"
                    disabled={!values.prompt?.trim()}
                    onClick={() => setNaming(true)}
                  />
                </div>
              </div>

              <SegmentedControl
                className="mt-3 w-full"
                label="How many controls to show"
                value={depth}
                onChange={changeDepth}
                options={[
                  { value: "simple", label: "Simple" },
                  { value: "full", label: "Full controls" },
                ]}
              />

              {showHist && (
                <div className="absolute inset-x-0 top-full z-20 mt-2 max-h-64 overflow-y-auto border border-edge bg-panel3 p-2 shadow-float pop-in">
                  <div className="flex items-center justify-between px-2 pb-2 pt-1">
                    <span className="label mb-0">Recent prompts</span>
                    <IconButton
                      icon="close"
                      label="Close prompt history"
                      onClick={() => setShowHist(false)}
                    />
                  </div>
                  {history.length === 0 ? (
                    <div className="px-2 py-4 text-xs text-muted">No prompt history yet.</div>
                  ) : (
                    history.map((item) => (
                      <button
                        key={item.id}
                        className="block w-full truncate border-t border-edge/60 px-2 py-2.5 text-left text-xs text-muted hover:bg-panel2 hover:text-ink"
                        title={item.prompt}
                        onClick={() => recall(item)}
                      >
                        {item.prompt || "(empty prompt)"}
                      </button>
                    ))
                  )}
                </div>
              )}

              {showWc && (
                <div className="absolute inset-x-0 top-full z-20 mt-2 max-h-60 overflow-y-auto border border-edge bg-panel3 p-3 shadow-float pop-in">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="label mb-0">Wildcards</span>
                    <IconButton icon="close" label="Close wildcard menu" onClick={() => setShowWc(false)} />
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {wildcards.map((wildcard) => (
                      <button
                        key={wildcard.name}
                        className="chip technical"
                        title={`${wildcard.count} entries`}
                        onClick={() => insertWildcard(wildcard.name)}
                      >
                        __{wildcard.name}__ <span className="text-white/30">{wildcard.count}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div className="space-y-5">
              {slots.length > 0 && (
                <MultiImageDropzone
                  slots={slots}
                  files={files}
                  values={values}
                  onChange={(name, file) => setFiles((previous) => ({ ...previous, [name]: file }))}
                />
              )}
              {spec.needs_mask && files.image && <MaskEditor image={files.image} onMask={setMask} />}
              <DynamicControls
                controls={spec.controls}
                values={values}
                onChange={set}
                presets={presets}
                simple={simple}
              />

              {hiddenOverrides.length > 0 && (
                <div className="border-l-2 border-warn bg-panel2/60 px-3 py-2 text-[11px] leading-relaxed text-muted">
                  {hiddenOverrides.length} setting{hiddenOverrides.length > 1 ? "s" : ""} from Full controls{" "}
                  {hiddenOverrides.length > 1 ? "are" : "is"} still applied:{" "}
                  <span className="text-ink">{hiddenOverrides.map((c) => c.label).join(", ")}</span>.
                  <button className="ml-1 text-accent underline underline-offset-2" onClick={clearHidden}>
                    Reset them
                  </button>
                </div>
              )}

              {combos > 1 && (
                <div
                  className={`technical border-l-2 pl-2 text-[10px] ${combos >= COMBO_CAP ? "border-warn text-warn" : "border-edge text-muted"}`}
                >
                  Approximately {combos} prompt combinations
                  {values.combinatorial
                    ? combos > COMBO_CAP
                      ? `; the first ${COMBO_CAP} will queue.`
                      : "; all will queue."
                    : "; one deterministic option per image, in order."}
                </div>
              )}

              {!slots.length && !simple && (
                <XYGridPanel key={spec.id} spec={spec} grid={grid} onGrid={setGrid} />
              )}
            </div>
          </div>

          <div className="shrink-0 border-t border-edge bg-panel px-4 py-3 sm:px-5">
            {blockedReason && (
              <div className="mb-3 flex items-start gap-2 text-xs leading-relaxed text-warn">
                <Icon name="alert" size={15} className="mt-0.5 shrink-0" />
                {blockedReason}
              </div>
            )}
            <Button
              variant="primary"
              className="w-full"
              loading={busy}
              disabled={!!blockedReason || missingRequired || missingMask || gridBlocked}
              onClick={submit}
            >
              {busy
                ? "Submitting…"
                : grid
                  ? `Generate grid · ${gridCells}`
                  : `Generate${values.batch > 1 ? ` × ${values.batch}` : ""}`}
            </Button>
            <div className="technical mt-2 text-center text-[9px] uppercase tracking-wide text-white/28">
              Ctrl / ⌘ + Enter
            </div>
          </div>
        </aside>

        <section
          className={`${mobileView === "session" ? "flex" : "hidden"} min-h-0 flex-col bg-bg lg:col-start-1 lg:row-start-1 lg:flex`}
          aria-label="Session results"
        >
          <div className="flex shrink-0 items-end justify-between border-b border-edge px-4 py-4 sm:px-6">
            <div>
              <div className="page-kicker">Current run</div>
              <h2 className="text-lg font-semibold">Session canvas</h2>
            </div>
            <div className="technical text-[10px] uppercase text-muted">
              {results.length} results · {activeJobs.length} active
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
            {activeJobs.length > 0 && (
              <div className="mb-6 grid gap-3 border-l-[3px] border-accent bg-panel px-4 py-3 xl:grid-cols-2">
                {activeJobs.map((job) => (
                  <div key={job.id} className="min-w-0">
                    <ProgressBar job={job} preview={previews[job.id]} />
                    <Button
                      variant="quiet"
                      size="sm"
                      className="mt-1 text-danger"
                      onClick={() => api.cancel(job.id)}
                    >
                      Cancel #{job.id}
                    </Button>
                  </div>
                ))}
              </div>
            )}

            {results.length === 0 ? (
              <div className="flex min-h-[420px] items-center justify-center">
                <EmptyState
                  icon={spec.output}
                  title={activeJobs.length ? "The first result is taking shape" : "A clear session canvas"}
                  description={
                    activeJobs.length
                      ? "Live progress is above. Finished work will settle here without leaving this workspace."
                      : "Compose on the right, then generate. Work made during this visit will collect here for quick inspection."
                  }
                  action={
                    <button className="btn lg:hidden" onClick={() => setMobileView("compose")}>
                      Return to compose <Icon name="arrow-right" size={15} />
                    </button>
                  }
                />
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
                {results
                  .slice()
                  .reverse()
                  .map((asset) => (
                    <AssetCard key={asset.id} asset={asset} onClick={setOpen} />
                  ))}
              </div>
            )}
          </div>
        </section>
      </div>

      {open && (
        <AssetModal
          asset={open}
          onClose={() => setOpen(null)}
          list={results.slice().reverse()}
          onNavigate={setOpen}
        />
      )}
      {naming && (
        <NameDialog
          title="Save this prompt as…"
          placeholder="preset name"
          onSubmit={savePrompt}
          onClose={() => setNaming(false)}
        />
      )}
    </div>
  );
}

function SkeletonView() {
  return (
    <div className="grid h-full grid-cols-1 lg:grid-cols-[minmax(0,1fr)_420px]">
      <div className="hidden items-center justify-center lg:flex">
        <div className="flex items-center gap-3 text-sm text-muted">
          <Spinner /> Preparing session canvas…
        </div>
      </div>
      <div className="space-y-5 border-l border-edge bg-panel p-5">
        <div className="skeleton h-7 w-44" />
        <div className="skeleton h-24 w-full" />
        <div className="skeleton h-10 w-full" />
        <div className="skeleton h-10 w-full" />
        <div className="skeleton h-10 w-full" />
      </div>
    </div>
  );
}

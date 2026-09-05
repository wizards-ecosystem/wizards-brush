import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, markUsed } from "../api/client";
import type { Asset, HumanGrade, HumanGradeInput } from "../api/types";
import { GEN_TO_SPEC, reuseValues } from "../lib/generators";
import { useStore } from "../store/useStore";
import { Icon } from "./icons";
import { Button, ConfirmDialog, IconButton, Modal, StarRating } from "./ui";
import { VideoPlayer } from "./VideoPlayer";
import { ZoomableImage } from "./ZoomableImage";

// Rendered on their own above, so they must not also appear in the generic
// key/value dump below.
const META_HIDE = new Set([
  "auto_tags",
  "warnings",
  "ignored_params",
  "post",
  "generation_width",
  "generation_height",
  "width",
  "height",
]);

type PostRecord = {
  operation: string;
  input_size?: { width?: number; height?: number };
  output_size?: { width?: number; height?: number };
  scale?: number;
  factor?: number;
  regions?: number;
  clamped?: boolean;
};

function postRecord(value: unknown): PostRecord | null {
  if (typeof value === "string") {
    const scale = /^upscaled_x(\d+)$/.exec(value);
    if (scale) return { operation: "upscale", scale: Number(scale[1]) };
    return {
      operation: value === "detailed" ? "detail" : value === "face_restored" ? "face_restore" : value,
    };
  }
  if (value && typeof value === "object" && typeof (value as PostRecord).operation === "string") {
    return value as PostRecord;
  }
  return null;
}

function postLabel(record: PostRecord): string {
  const name = record.operation.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  if (record.scale) return `${name} ×${record.scale}${record.clamped ? " · capped" : ""}`;
  if (record.factor) return `${name} ×${record.factor} frames`;
  if (record.regions) return `${name} · ${record.regions} region${record.regions === 1 ? "" : "s"}`;
  return name;
}

function sizeLabel(size?: PostRecord["input_size"]): string | null {
  return size?.width && size?.height ? `${size.width}×${size.height}` : null;
}

const RUBRIC: {
  key: keyof Pick<HumanGradeInput, "prompt_fidelity" | "visual_quality">;
  label: string;
  hint: string;
}[] = [
  {
    key: "visual_quality",
    label: "Looks good",
    hint: "Lighting, composition, anatomy, texture, clarity, and overall polish.",
  },
  {
    key: "prompt_fidelity",
    label: "Matches the prompt",
    hint: "Did it deliver the requested subject, pose, scene, and framing?",
  },
];

export function AssetModal({
  asset,
  onClose,
  list,
  onNavigate,
}: {
  asset: Asset;
  onClose: () => void;
  list?: Asset[];
  onNavigate?: (a: Asset) => void;
}) {
  const refreshAssets = useStore((s) => s.refreshAssets);
  const refreshStats = useStore((s) => s.refreshStats);
  const toast = useStore((s) => s.toast);
  const navigate = useNavigate();
  const [fav, setFav] = useState(asset.favorite);
  const [rating, setRating] = useState(asset.rating);
  const [grade, setGrade] = useState<HumanGrade>({ ...(asset.grade || {}) });
  const [tags, setTags] = useState<string[]>(asset.tags || []);
  const [tagInput, setTagInput] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  // Adjust-state-during-render (the React-docs pattern) instead of an effect:
  // navigating ‹/› swaps the asset prop, and its fav/rating/tags must replace
  // the previous asset's before anything paints.
  const [prevId, setPrevId] = useState(asset.id);
  if (prevId !== asset.id) {
    setPrevId(asset.id);
    setFav(asset.favorite);
    setRating(asset.rating);
    setGrade({ ...(asset.grade || {}) });
    setTags(asset.tags || []);
    setTagInput("");
  }

  const idx = list?.findIndex((a) => a.id === asset.id) ?? -1;
  const go = (d: number) => {
    if (!list || idx < 0 || !onNavigate) return;
    const next = list[idx + d];
    if (next) onNavigate(next);
  };
  const toggleFav = async () => {
    const prev = fav;
    const nv = !fav;
    setFav(nv);
    try {
      await api.favorite(asset.id, nv);
      refreshAssets();
      refreshStats();
    } catch (e) {
      setFav(prev); // roll back the optimistic flip — the server never changed
      toast(`Couldn't update favorite: ${e}`, "error");
    }
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return; // typing a tag
      if (e.key === "ArrowLeft") go(-1);
      if (e.key === "ArrowRight") go(1);
      if (e.key.toLowerCase() === "f") toggleFav();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list, idx, fav]);

  const runTool = async (kind: "upscale" | "face-restore" | "interpolate" | "detail") => {
    try {
      await api.tool(kind, { asset_id: asset.id });
      toast(`${kind} queued`, "success");
      onClose();
    } catch (e) {
      toast(`${kind} failed: ${e}`, "error");
    }
  };
  const doDelete = async () => {
    try {
      await api.deleteAsset(asset.id);
      await refreshAssets();
      refreshStats();
      toast("Moved to Trash", "info");
      onClose();
    } catch (e) {
      toast(`Delete failed: ${e}`, "error");
    }
  };
  const rate = async (n: number) => {
    const prev = rating;
    setRating(n);
    try {
      await api.rate(asset.id, n);
      refreshAssets();
    } catch (e) {
      setRating(prev);
      toast(`Couldn't set rating: ${e}`, "error");
    }
  };
  const setGradeValue = (key: keyof HumanGradeInput, value: number | null) => {
    setGrade((current) => ({ ...current, [key]: current[key] === value ? null : value }));
  };
  const canSaveGrade = grade.prompt_fidelity != null && grade.visual_quality != null;
  const saveGrade = async () => {
    if (!canSaveGrade) return;
    const input: HumanGradeInput = {
      prompt_fidelity: Number(grade.prompt_fidelity),
      visual_quality: Number(grade.visual_quality),
      notes: grade.notes || "",
    };
    const previous = grade;
    try {
      const result = await api.grade(asset.id, input);
      setGrade(result.grade);
      setRating(result.rating);
      refreshAssets();
      toast("Review saved", "success");
    } catch (e) {
      setGrade(previous);
      toast(`Couldn't save review: ${e}`, "error");
    }
  };
  const commitTags = async (next: string[]) => {
    const prev = tags;
    setTags(next);
    try {
      await api.tag(asset.id, next);
      refreshAssets();
    } catch (e) {
      setTags(prev);
      toast(`Couldn't update tags: ${e}`, "error");
    }
  };
  const addTag = () => {
    const t = tagInput.trim();
    if (t && !tags.includes(t)) commitTags([...tags, t]);
    setTagInput("");
  };

  const meta = asset.meta || {};
  const autoTags: string[] = Array.isArray(meta.auto_tags) ? meta.auto_tags : [];
  /** Things that went wrong without failing the job — an adapter that could not
   *  be applied, a control that was sent and never read. The image exists, which
   *  is why these are not errors, and why they would otherwise be invisible:
   *  the output simply does not match what was asked for and nothing says so. */
  const warnings: string[] = Array.isArray(meta.warnings) ? meta.warnings : [];
  const ignored: string[] = Array.isArray(meta.ignored_params) ? meta.ignored_params : [];
  const postHistory = (Array.isArray(meta.post) ? meta.post : [])
    .map(postRecord)
    .filter((record): record is PostRecord => record !== null);
  const generationWidth =
    meta.generation_width ??
    (postHistory.length > 0 && typeof meta.post?.[0] === "string" ? meta.width : null);
  const generationHeight =
    meta.generation_height ??
    (postHistory.length > 0 && typeof meta.post?.[0] === "string" ? meta.height : null);
  const specId = GEN_TO_SPEC[asset.generator];
  const copy = (text: string, label: string) => {
    navigator.clipboard?.writeText(text);
    toast(`${label} copied`, "info");
  };
  // Each of these is a moment the asset was *used*, not merely looked at:
  // reused as settings, rerun, fed into another generator, or taken away as a
  // file. That is the behaviour `used_count` exists to record — see the model.
  const prefill = (values: Record<string, any>) => {
    if (!specId) return;
    markUsed(asset.id);
    navigate(`/g/${specId}`, { state: { prefillValues: values } });
  };
  const moreLikeThis = async () => {
    if (asset.job_id == null) return;
    markUsed(asset.id);
    try {
      const r = await api.rerun(asset.job_id, true);
      if (r.error) throw new Error(r.error);
      toast(r.model_warning || `Queued job #${r.job_id}`, r.model_warning ? "info" : "success");
      onClose();
    } catch (e) {
      toast(`Rerun failed: ${e}`, "error");
    }
  };
  const sendTo = (target: "img2img" | "i2v" | "inpaint") => {
    markUsed(asset.id);
    navigate(`/g/${target}`, {
      state: {
        prefillValues: { prompt: meta.prompt || "" },
        fromAsset: { url: asset.url, filename: asset.filename },
      },
    });
  };
  const extendVideo = () => {
    markUsed(asset.id);
    navigate("/g/i2v", {
      state: {
        prefillValues: { prompt: meta.prompt || "" },
        fromAsset: { url: `/api/assets/${asset.id}/frame?which=last`, filename: "last-frame.png" },
      },
    });
  };

  return (
    <Modal onClose={onClose} size="fullscreen" label={`${asset.kind} by ${asset.generator}`}>
      <div className="flex min-h-0 flex-1 flex-col bg-bg lg:flex-row">
        <div className="relative flex min-h-[52dvh] flex-1 items-center justify-center bg-black lg:min-h-0">
          <div className="absolute left-3 top-3 z-20 flex items-center gap-2 rounded-[6px] bg-black/70 px-2.5 py-1.5 text-[10px] text-white/75">
            <Icon name={asset.kind} size={13} />
            <span className="technical uppercase">
              {idx >= 0 && list ? `${idx + 1} / ${list.length}` : asset.kind}
            </span>
          </div>
          <IconButton
            icon="close"
            label="Close asset viewer"
            className="absolute right-3 top-3 z-20 border-white/15 bg-black/70 text-white"
            onClick={onClose}
          />
          {asset.kind === "video" ? (
            <VideoPlayer src={asset.url} fps={Number(meta.fps) || 20} />
          ) : (
            <ZoomableImage src={asset.url} alt={meta.prompt || asset.filename} />
          )}
          {list && idx > 0 && (
            <button
              className="icon-btn absolute left-3 top-1/2 z-20 -translate-y-1/2 border-white/15 bg-black/70 text-white"
              onClick={() => go(-1)}
              aria-label="Previous asset"
            >
              <Icon name="chevron-left" />
            </button>
          )}
          {list && idx >= 0 && idx < list.length - 1 && (
            <button
              className="icon-btn absolute right-3 top-1/2 z-20 -translate-y-1/2 border-white/15 bg-black/70 text-white"
              onClick={() => go(1)}
              aria-label="Next asset"
            >
              <Icon name="chevron-right" />
            </button>
          )}
        </div>

        <aside
          className="flex max-h-[48dvh] w-full shrink-0 flex-col gap-4 overflow-y-auto border-t border-edge bg-panel p-4 lg:max-h-none lg:w-[370px] lg:border-t-0 lg:border-l lg:p-5"
          aria-label="Asset inspector"
        >
          <div className="flex items-center justify-between">
            <div>
              <div className="page-kicker">Asset inspector</div>
              <div className="flex items-center gap-2 text-base font-semibold">{asset.generator}</div>
            </div>
            <button
              className={`icon-btn ${fav ? "border-accent/40 bg-accent/10 text-accent" : "text-muted"}`}
              onClick={toggleFav}
              aria-label={fav ? "Remove from favorites" : "Add to favorites"}
              aria-pressed={fav}
            >
              <Icon name="heart" />
            </button>
          </div>
          <div className="technical border-y border-edge py-2 text-[10px] text-muted">
            {asset.width}×{asset.height}
            {generationWidth &&
              generationHeight &&
              (generationWidth !== asset.width || generationHeight !== asset.height) && (
                <>
                  {" "}
                  <span className="text-white/35">
                    (generated {generationWidth}×{generationHeight})
                  </span>
                </>
              )}{" "}
            · {new Date(asset.created_at).toLocaleString()}
          </div>
          <StarRating value={rating} onChange={rate} />

          <div className="border-y border-edge py-3">
            <div className="flex items-baseline justify-between gap-3">
              <div>
                <div className="label mb-0">Quick review</div>
                <p className="mt-1 text-[11px] leading-relaxed text-muted">
                  Two answers, then save. Quality counts 60%; prompt match counts 40%. LoRA impact is
                  calculated from paired base-model reviews.
                </p>
              </div>
              {typeof grade.overall === "number" && (
                <span className="technical shrink-0 text-xs text-accent">{grade.overall.toFixed(2)} / 5</span>
              )}
            </div>
            <div className="mt-3 space-y-2.5">
              {RUBRIC.map((criterion) => {
                const value = grade[criterion.key];
                return (
                  <div key={criterion.key}>
                    <div className="flex items-center justify-between gap-2">
                      <label className="text-xs text-white/85" title={criterion.hint}>
                        {criterion.label}
                      </label>
                    </div>
                    <div className="mt-1 flex gap-1">
                      {[1, 2, 3, 4, 5].map((n) => (
                        <button
                          key={n}
                          className={`flex h-7 flex-1 items-center justify-center border text-[11px] transition-colors ${value === n ? "border-accent bg-accent/20 text-accent" : "border-edge text-muted hover:bg-panel2"}`}
                          onClick={() => setGradeValue(criterion.key, n)}
                          aria-label={`${criterion.label}: ${n} out of 5`}
                          aria-pressed={value === n}
                          title={criterion.hint}
                        >
                          {n}
                        </button>
                      ))}
                    </div>
                  </div>
                );
              })}
              <textarea
                className="input min-h-16 text-xs"
                placeholder="Optional review note: what passed, what to fix, or why this setting won"
                value={grade.notes || ""}
                maxLength={800}
                onChange={(e) => setGrade((current) => ({ ...current, notes: e.target.value }))}
              />
              <Button size="sm" onClick={saveGrade} disabled={!canSaveGrade}>
                Save review
              </Button>
            </div>
          </div>

          {asset.caption && (
            <div className="border-l-2 border-accent pl-3 text-sm italic leading-relaxed text-muted">
              {asset.caption}
            </div>
          )}

          {(warnings.length > 0 || ignored.length > 0) && (
            <div className="border-l-2 border-warn bg-warn/8 py-2 pl-3 pr-2 text-xs leading-relaxed text-warn">
              {warnings.map((w) => (
                <div key={w}>{w}</div>
              ))}
              {ignored.length > 0 && (
                <div title="Sent with the job, but the generator never read them.">
                  Had no effect: {ignored.join(", ")}
                </div>
              )}
            </div>
          )}

          {/* tags */}
          <div>
            <div className="label">Tags</div>
            <div className="flex flex-wrap gap-1 mb-1">
              {tags.map((t) => (
                <button
                  key={t}
                  className="chip"
                  onClick={() => commitTags(tags.filter((x) => x !== t))}
                  aria-label={`Remove tag ${t}`}
                >
                  {t} <Icon name="close" size={11} />
                </button>
              ))}
            </div>
            {autoTags.filter((t) => !tags.includes(t)).length > 0 && (
              <div className="flex flex-wrap gap-1 mb-1 opacity-60">
                {autoTags
                  .filter((t) => !tags.includes(t))
                  .map((t) => (
                    <button
                      key={t}
                      className="chip"
                      title="Auto tag — click to keep"
                      onClick={() => commitTags([...tags, t])}
                    >
                      <Icon name="plus" size={11} /> {t}
                    </button>
                  ))}
              </div>
            )}
            <input
              className="input text-xs"
              placeholder="add tag + Enter"
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addTag()}
            />
          </div>

          {/* metadata */}
          <div className="divide-y divide-edge/60 border-y border-edge text-xs">
            {postHistory.length > 0 && (
              <div className="py-2.5">
                <div className="technical mb-2 text-[10px] text-muted">Processing history</div>
                <div className="space-y-2">
                  {postHistory.map((record, index) => {
                    const before = sizeLabel(record.input_size);
                    const after = sizeLabel(record.output_size);
                    return (
                      <div
                        key={`${record.operation}-${index}`}
                        className="flex items-baseline justify-between gap-3"
                      >
                        <span className="text-white/80">{postLabel(record)}</span>
                        {before && after && (
                          <span className="technical shrink-0 text-[10px] text-muted">
                            {before} → {after}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            {Object.entries(meta)
              .filter(([k]) => !META_HIDE.has(k))
              .map(([k, v]) => (
                <div key={k} className="grid grid-cols-[100px_minmax(0,1fr)] gap-2 py-1.5">
                  <span className="technical shrink-0 text-[10px] text-muted">{k}</span>
                  <span className="break-words text-right text-white/80">{String(v)}</span>
                </div>
              ))}
          </div>

          <div className="mt-auto space-y-4 border-t border-edge pt-4">
            <div className="label mb-0">Copy</div>
            <div className="grid grid-cols-2 gap-2">
              {meta.prompt && (
                <Button size="sm" icon="copy" onClick={() => copy(meta.prompt, "Prompt")}>
                  Prompt
                </Button>
              )}
              {meta.seed != null && (
                <Button size="sm" icon="copy" onClick={() => copy(String(meta.seed), "Seed")}>
                  Seed
                </Button>
              )}
            </div>
            {specId && (
              <div>
                <div className="label">Reuse</div>
                <div className="grid grid-cols-2 gap-2">
                  <Button size="sm" icon="refresh" onClick={() => prefill(reuseValues(meta))}>
                    All settings
                  </Button>
                  <Button
                    size="sm"
                    icon="make"
                    disabled={asset.job_id == null}
                    onClick={moreLikeThis}
                    title="Same settings, new seed"
                  >
                    More like this
                  </Button>
                  {meta.prompt && (
                    <Button
                      size="sm"
                      onClick={() =>
                        prefill({ prompt: meta.prompt, negative_prompt: meta.negative_prompt || "" })
                      }
                    >
                      Prompt only
                    </Button>
                  )}
                  {meta.seed != null && (
                    <Button size="sm" onClick={() => prefill({ seed: meta.seed })}>
                      Seed only
                    </Button>
                  )}
                </div>
              </div>
            )}
            {asset.kind === "image" && (
              <div>
                <div className="label">Send to</div>
                <div className="grid grid-cols-3 gap-2">
                  <Button size="sm" onClick={() => sendTo("img2img")}>
                    Img2Img
                  </Button>
                  <Button size="sm" onClick={() => sendTo("inpaint")}>
                    Inpaint
                  </Button>
                  <Button size="sm" onClick={() => sendTo("i2v")}>
                    Video
                  </Button>
                </div>
              </div>
            )}
            {asset.kind === "image" ? (
              <div>
                <div className="label">Enhance</div>
                <div className="grid grid-cols-3 gap-2">
                  <Button size="sm" onClick={() => runTool("upscale")}>
                    Upscale
                  </Button>
                  <Button size="sm" onClick={() => runTool("face-restore")}>
                    Faces
                  </Button>
                  <Button size="sm" onClick={() => runTool("detail")}>
                    Detail
                  </Button>
                </div>
              </div>
            ) : (
              <div>
                <div className="label">Enhance</div>
                <div className="grid grid-cols-2 gap-2">
                  <Button size="sm" onClick={() => runTool("interpolate")}>
                    Smooth ×2
                  </Button>
                  <Button size="sm" icon="arrow-right" onClick={extendVideo}>
                    Extend
                  </Button>
                </div>
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <a
                className="btn text-center text-xs"
                href={asset.url}
                download
                onClick={() => markUsed(asset.id)}
              >
                <Icon name="download" size={15} /> Download
              </a>
              <Button size="sm" variant="danger" icon="trash" onClick={() => setConfirmingDelete(true)}>
                Move to Trash
              </Button>
            </div>
          </div>
        </aside>
      </div>
      {confirmingDelete && (
        <ConfirmDialog
          title="Move this asset to Trash?"
          message="The file stays on disk and can be restored until Trash is permanently emptied."
          confirmLabel="Move to Trash"
          onConfirm={doDelete}
          onClose={() => setConfirmingDelete(false)}
        />
      )}
    </Modal>
  );
}

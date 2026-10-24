import { useRef, useState } from "react";
import { api } from "../api/client";
import type { Control, SavedRecipe, StyleFragment } from "../api/types";
import { GalleryPicker } from "./GalleryPicker";
import { Button, Modal } from "./ui";

type Choice = { name: string; label: string };

/** Pick a name and which fields a later run may change. Settings not listed stay frozen. */
export function SaveRecipeDialog({
  kind,
  controls,
  values,
  styles = [],
  needsImage,
  needsMask,
  needsLastFrame,
  onClose,
  onSaved,
}: {
  kind: string;
  controls: Control[];
  values: Record<string, unknown>;
  /** Selected style chips, frozen with their current text. A run composes them
   * onto its prompt the way Generate does, so the two produce the same text. */
  styles?: StyleFragment[];
  needsImage: boolean;
  needsMask: boolean;
  needsLastFrame: boolean;
  onClose: () => void;
  onSaved: (recipe: SavedRecipe) => void;
}) {
  const nameRef = useRef<HTMLInputElement>(null);
  const choices: Choice[] = [
    ...controls.map((control) => ({ name: control.name, label: control.label })),
    ...(needsImage ? [{ name: "images", label: "Source images" }] : []),
    ...(needsMask ? [{ name: "mask", label: "Mask" }] : []),
    ...(needsLastFrame ? [{ name: "last_frame", label: "Last frame" }] : []),
  ];
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    if (controls.some((control) => control.name === "prompt")) initial.add("prompt");
    if (needsImage) initial.add("images");
    if (needsMask) initial.add("mask");
    if (needsLastFrame) initial.add("last_frame");
    return initial;
  });
  const [frozenImage, setFrozenImage] = useState<number | null>(null);
  const [frozenMask, setFrozenMask] = useState<number | null>(null);
  const [picking, setPicking] = useState<"images" | "mask" | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const toggle = (field: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(field)) next.delete(field);
      else next.add(field);
      return next;
    });
  };

  const save = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Name the recipe.");
      nameRef.current?.focus();
      return;
    }
    if (needsImage && !selected.has("images") && frozenImage == null) {
      setError("Expose source images, or choose a gallery image to keep.");
      return;
    }
    if (needsMask && !selected.has("mask") && frozenMask == null) {
      setError("Expose the mask, or choose a gallery mask to keep.");
      return;
    }
    const params: Record<string, unknown> = {};
    for (const control of controls) {
      if (values[control.name] !== undefined) params[control.name] = values[control.name];
    }
    setSaving(true);
    setError("");
    try {
      const recipe = await api.saveRecipe({
        name: trimmed,
        description: description.trim(),
        kind,
        params,
        expose: choices.filter((choice) => selected.has(choice.name)).map((choice) => choice.name),
        styles,
        inputs: {
          images: !needsImage || selected.has("images") || frozenImage == null ? [] : [frozenImage],
          mask: !needsMask || selected.has("mask") ? null : frozenMask,
          last_frame: null,
        },
      });
      onSaved(recipe);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Couldn't save the recipe.");
      setSaving(false);
    }
  };

  return (
    <Modal onClose={onClose} label="Save as recipe" initialFocusRef={nameRef}>
      <form
        className="space-y-4 p-5"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <div>
          <div className="page-kicker">Reuse</div>
          <h2 className="text-lg font-semibold">Save as recipe</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted">
            Freeze this setup. A later run only asks for the fields you check. Everything else stays as it is
            now.
          </p>
        </div>
        <div>
          <label className="label" htmlFor="recipe-name">
            Name
          </label>
          <input
            id="recipe-name"
            ref={nameRef}
            className="input"
            value={name}
            maxLength={80}
            autoComplete="off"
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <div>
          <label className="label" htmlFor="recipe-description">
            Description
          </label>
          <textarea
            id="recipe-description"
            className="input min-h-[64px] resize-y"
            value={description}
            maxLength={2000}
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>
        <fieldset>
          <legend className="label">Fields the next run can change</legend>
          <div className="max-h-56 space-y-1 overflow-y-auto pr-1">
            {choices.map((choice) => (
              <label key={choice.name} className="flex items-center gap-2 py-1 text-sm">
                <input
                  type="checkbox"
                  checked={selected.has(choice.name)}
                  onChange={() => toggle(choice.name)}
                />
                <span>{choice.label}</span>
                <span className="technical text-[10px] text-muted">{choice.name}</span>
              </label>
            ))}
          </div>
        </fieldset>
        {styles.length > 0 && (
          <p className="text-[11px] leading-relaxed text-muted" data-testid="recipe-styles">
            Styles kept with the prompt: {styles.map((style) => style.id).join(", ")}. Each run adds their
            text after its prompt, as Generate does.
          </p>
        )}
        {needsImage && !selected.has("images") && (
          <div>
            <div className="label">Gallery image to keep</div>
            <div className="flex items-center gap-2">
              <Button type="button" onClick={() => setPicking("images")}>
                {frozenImage == null ? "Choose image" : `Image #${frozenImage}`}
              </Button>
              {frozenImage != null && (
                <button
                  type="button"
                  className="text-xs text-muted underline"
                  onClick={() => setFrozenImage(null)}
                >
                  Clear
                </button>
              )}
            </div>
          </div>
        )}
        {needsMask && !selected.has("mask") && (
          <div>
            <div className="label">Gallery mask to keep</div>
            <Button type="button" onClick={() => setPicking("mask")}>
              {frozenMask == null ? "Choose mask" : `Mask #${frozenMask}`}
            </Button>
          </div>
        )}
        {needsImage && (
          <p className="text-[11px] leading-relaxed text-muted">
            The image on the form is not stored. Expose source images, or pick one from the gallery.
          </p>
        )}
        {error && (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" loading={saving}>
            Save recipe
          </Button>
        </div>
      </form>
      {picking && (
        <GalleryPicker
          title={picking === "mask" ? "Choose a mask" : "Choose an image to keep"}
          onClose={() => setPicking(null)}
          onPick={(asset) => {
            if (picking === "mask") setFrozenMask(asset.id);
            else setFrozenImage(asset.id);
            setPicking(null);
          }}
        />
      )}
    </Modal>
  );
}

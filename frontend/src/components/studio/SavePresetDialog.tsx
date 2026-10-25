import { useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { ExecutionPlan, PresetDefinition, PresetRead } from "../../api/types";
import type { StudioDraft } from "../../lib/studioDraft";
import { Button, Modal } from "../ui";

/**
 * The definition the current setup stands for: the preset's authored content
 * with this draft's explicit choices applied. Only authored and overridden
 * values are written; dependent defaults and runtime values stay derived, so
 * the saved preset keeps following the model it names.
 */
export function definitionFrom(
  preset: PresetRead | null,
  plan: ExecutionPlan,
  draft: StudioDraft,
  meta: { name: string; description: string; tags: string[] },
): PresetDefinition {
  const base: PresetDefinition = preset?.definition ?? {
    name: meta.name,
    operation: plan.operation,
    model: { resource: plan.model.resource },
  };
  // A trigger is authored guidance about an adapter; it follows the adapter
  // when only its strength or position changed.
  const triggers = new Map((base.adapters ?? []).map((a) => [a.resource, a.trigger ?? ""]));
  return {
    name: meta.name,
    description: meta.description,
    tags: meta.tags,
    operation: plan.operation,
    model: { resource: draft.model ?? base.model.resource },
    adapters: (draft.adapters ?? base.adapters ?? []).map((a) => {
      const trigger = triggers.get(a.resource);
      return trigger
        ? { resource: a.resource, strength: a.strength, trigger }
        : { resource: a.resource, strength: a.strength };
    }),
    prompt: {
      ...(base.prompt ?? {}),
      negative: draft.negative ?? base.prompt?.negative ?? "",
      default_styles: draft.styles ?? base.prompt?.default_styles ?? [],
      enhance_by_default: draft.enhance ?? base.prompt?.enhance_by_default ?? false,
    },
    settings: { ...(base.settings ?? {}), ...draft.settings },
    finish: draft.finish ?? base.finish ?? [],
    references: base.references ?? [],
    validation: base.validation ?? [],
    fields: base.fields ?? [],
  };
}

export function SavePresetDialog({
  preset,
  plan,
  draft,
  onClose,
  onSaved,
}: {
  preset: PresetRead | null;
  plan: ExecutionPlan;
  draft: StudioDraft;
  onClose: () => void;
  onSaved: (saved: PresetRead) => void;
}) {
  const nameRef = useRef<HTMLInputElement>(null);
  const editable = preset?.origin === "custom";
  const [name, setName] = useState(preset ? (editable ? preset.name : `${preset.name} (mine)`) : "");
  const [description, setDescription] = useState(preset?.description ?? "");
  const [tags, setTags] = useState((preset?.tags ?? []).join(", "));
  const [mode, setMode] = useState<"new" | "update">(editable ? "update" : "new");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);

  const save = async (onConflict: "error" | "copy" = "error") => {
    if (!name.trim()) {
      setError("Name the preset.");
      nameRef.current?.focus();
      return;
    }
    const definition = definitionFrom(preset, plan, draft, {
      name: name.trim(),
      description: description.trim(),
      tags: tags
        .split(",")
        .map((t) => t.trim().toLowerCase())
        .filter(Boolean),
    });
    setSaving(true);
    setError("");
    try {
      const saved =
        mode === "update" && preset
          ? await api.updateStudioPreset(
              preset.id,
              { ...definition, id: preset.id },
              draft.revision ?? preset.revision,
              onConflict,
            )
          : await api.createStudioPreset(definition);
      onSaved(saved);
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "revision_conflict") {
        setConflict(true);
        setError(
          "Someone saved a newer revision of this preset since you started. Keep yours as a copy, or cancel.",
        );
      } else {
        setError(caught instanceof Error ? caught.message : "Couldn't save the preset.");
      }
      setSaving(false);
    }
  };

  return (
    <Modal onClose={onClose} label="Save preset" initialFocusRef={nameRef}>
      <form
        className="space-y-4 p-5"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <div>
          <div className="page-kicker">Reuse</div>
          <h2 className="text-lg font-semibold">Save preset</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted">
            Saves the model, adapters, prompt template and settings shown on the right, as a new revision.
            Jobs that already ran keep the revision they used.
          </p>
        </div>
        {editable && (
          <fieldset className="flex gap-3 text-sm">
            <label className="flex items-center gap-1">
              <input type="radio" checked={mode === "update"} onChange={() => setMode("update")} /> Update{" "}
              {preset?.name}
            </label>
            <label className="flex items-center gap-1">
              <input type="radio" checked={mode === "new"} onChange={() => setMode("new")} /> Save as new
            </label>
          </fieldset>
        )}
        {!editable && preset && (
          <p className="text-[11px] text-muted">
            {preset.name} is curated and never changes; this saves your version as a new preset.
          </p>
        )}
        <div>
          <label className="label" htmlFor="preset-name">
            Name
          </label>
          <input
            id="preset-name"
            ref={nameRef}
            className="input"
            value={name}
            maxLength={80}
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <div>
          <label className="label" htmlFor="preset-description">
            Description
          </label>
          <textarea
            id="preset-description"
            className="input min-h-[56px]"
            value={description}
            maxLength={2000}
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>
        <div>
          <label className="label" htmlFor="preset-tags">
            Tags (comma separated)
          </label>
          <input
            id="preset-tags"
            className="input"
            value={tags}
            onChange={(event) => setTags(event.target.value)}
          />
        </div>
        {error && (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          {conflict ? (
            <Button variant="primary" type="button" loading={saving} onClick={() => void save("copy")}>
              Keep mine as a copy
            </Button>
          ) : (
            <Button variant="primary" type="submit" loading={saving}>
              Save preset
            </Button>
          )}
        </div>
      </form>
    </Modal>
  );
}

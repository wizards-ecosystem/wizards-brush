import { useEffect, useId, useState } from "react";
import type { RecipeField, SavedRecipe } from "../api/types";
import { initialRecipeInputs, recipeFieldRequired, recipeInputMissing } from "../lib/recipes";
import { GalleryPicker } from "./GalleryPicker";
import { Button } from "./ui";

type Values = Record<string, unknown>;

/** A run form built from a recipe's exposed fields. The same schema agents read. */
export function RecipeForm({
  recipe,
  busy = false,
  onRun,
}: {
  recipe: SavedRecipe;
  busy?: boolean;
  onRun: (inputs: Values, dryRun: boolean) => Promise<void> | void;
}) {
  const formId = useId();
  const [values, setValues] = useState<Values>(() => initialRecipeInputs(recipe.fields));
  const [error, setError] = useState("");
  const [picking, setPicking] = useState<string | null>(null);

  const set = (name: string, value: unknown) => {
    setValues((current) => ({ ...current, [name]: value }));
  };

  const submit = (dryRun: boolean) => {
    const missing = recipeInputMissing(recipe.fields, values);
    if (missing) {
      setError(missing);
      return;
    }
    setError("");
    void onRun(values, dryRun);
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        submit(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // submit closes over the latest values; the effect rebinds with them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values, recipe.fields, onRun]);

  return (
    <form
      aria-label={`Run ${recipe.name}`}
      className="space-y-5"
      onSubmit={(event) => {
        event.preventDefault();
        submit(false);
      }}
    >
      <div>
        <div className="page-kicker">Recipe</div>
        <h2 className="text-xl font-semibold tracking-[-0.02em]">{recipe.name}</h2>
        <p className="mt-1 text-xs text-muted">
          {recipe.kind}
          {recipe.description ? ` · ${recipe.description}` : ""}
        </p>
      </div>

      {recipe.fields.length === 0 ? (
        <p className="text-sm text-muted">Nothing to change. Running uses the saved settings as they are.</p>
      ) : (
        <div className="space-y-4">
          {recipe.fields.map((field) => (
            <Field
              key={field.name}
              field={field}
              formId={formId}
              value={values[field.name]}
              onChange={(value) => set(field.name, value)}
              onPick={() => setPicking(field.name)}
            />
          ))}
        </div>
      )}

      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <Button variant="primary" type="submit" loading={busy} disabled={busy}>
          Run recipe
        </Button>
        <Button type="button" disabled={busy} onClick={() => submit(true)}>
          Check
        </Button>
      </div>
      <p className="technical text-[10px] uppercase tracking-wide text-white/35">Ctrl / ⌘ + Enter</p>

      {picking && (
        <GalleryPicker
          title={`Choose ${recipe.fields.find((field) => field.name === picking)?.label ?? "an image"}`}
          onClose={() => setPicking(null)}
          onPick={(asset) => {
            const field = recipe.fields.find((item) => item.name === picking);
            if (field?.type === "images") {
              const current = Array.isArray(values[field.name]) ? (values[field.name] as number[]) : [];
              const max = field.constraints.max_items ?? 1;
              const next = current.includes(asset.id) ? current : [...current, asset.id].slice(0, max);
              set(field.name, next);
            } else if (field) {
              set(field.name, asset.id);
            }
            setPicking(null);
          }}
        />
      )}
    </form>
  );
}

function Field({
  field,
  formId,
  value,
  onChange,
  onPick,
}: {
  field: RecipeField;
  formId: string;
  value: unknown;
  onChange: (value: unknown) => void;
  onPick: () => void;
}) {
  const id = `${formId}-${field.name}`;
  const hintId = field.description ? `${id}-hint` : undefined;
  const required = recipeFieldRequired(field);

  if (field.type === "textarea") {
    return (
      <div>
        <label className="label" htmlFor={id}>
          {field.label}
          {required ? " (required)" : ""}
        </label>
        <textarea
          id={id}
          className="input min-h-[96px] resize-y"
          value={typeof value === "string" ? value : ""}
          aria-describedby={hintId}
          onChange={(event) => onChange(event.target.value)}
        />
        {field.description && (
          <p id={hintId} className="mt-1 text-[11px] text-muted">
            {field.description}
          </p>
        )}
      </div>
    );
  }

  if (field.type === "toggle") {
    return (
      <label className="flex cursor-pointer items-center justify-between gap-3 border-y border-edge py-2.5">
        <span className="text-sm">{field.label}</span>
        <input
          id={id}
          type="checkbox"
          className="size-4 accent-accent"
          checked={value === true}
          onChange={(event) => onChange(event.target.checked)}
        />
      </label>
    );
  }

  if (field.type === "select" || field.type === "segmented" || field.type === "aspect") {
    const options = field.constraints.options ?? [];
    return (
      <div>
        <label className="label" htmlFor={id}>
          {field.label}
        </label>
        <select
          id={id}
          className="input"
          value={value === undefined || value === null ? "" : String(value)}
          aria-describedby={hintId}
          onChange={(event) => {
            const match = options.find((option) => String(option) === event.target.value);
            onChange(match === undefined ? event.target.value : match);
          }}
        >
          {options.map((option) => (
            <option key={String(option)} value={String(option)}>
              {String(option)}
            </option>
          ))}
        </select>
        {field.description && (
          <p id={hintId} className="mt-1 text-[11px] text-muted">
            {field.description}
          </p>
        )}
      </div>
    );
  }

  if (field.type === "number" || field.type === "slider") {
    return (
      <div>
        <label className="label" htmlFor={id}>
          {field.label}
        </label>
        <input
          id={id}
          className="input"
          type="number"
          min={field.constraints.min}
          max={field.constraints.max}
          step={field.constraints.step ?? "any"}
          value={typeof value === "number" ? value : ""}
          aria-describedby={hintId}
          onChange={(event) => {
            const next = event.target.value;
            onChange(next === "" ? "" : Number(next));
          }}
        />
        {field.description && (
          <p id={hintId} className="mt-1 text-[11px] text-muted">
            {field.description}
          </p>
        )}
      </div>
    );
  }

  if (field.type === "multiselect") {
    const options = (field.constraints.options ?? []).map((option) => String(option));
    const selected = Array.isArray(value) ? value.map((item) => String(item)) : [];
    return (
      <fieldset>
        <legend className="label">{field.label}</legend>
        <div className="space-y-1">
          {options.map((option) => (
            <label key={option} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={selected.includes(option)}
                onChange={(event) => {
                  const next = event.target.checked
                    ? [...selected, option]
                    : selected.filter((item) => item !== option);
                  onChange(next);
                }}
              />
              {option}
            </label>
          ))}
        </div>
      </fieldset>
    );
  }

  if (field.type === "images" || field.type === "asset") {
    const ids = field.type === "images" ? (Array.isArray(value) ? value : []) : value == null ? [] : [value];
    return (
      <div>
        <label className="label" htmlFor={id}>
          {field.label}
          {required ? " (required)" : ""}
        </label>
        <div className="flex gap-2">
          <input
            id={id}
            className="input"
            inputMode="numeric"
            placeholder="Asset id"
            aria-describedby={hintId}
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              event.preventDefault();
              const parsed = Number((event.target as HTMLInputElement).value);
              if (!Number.isInteger(parsed) || parsed < 1) return;
              if (field.type === "images") {
                const max = field.constraints.max_items ?? 1;
                const current = Array.isArray(value) ? (value as number[]) : [];
                onChange([...current, parsed].slice(0, max));
              } else {
                onChange(parsed);
              }
              (event.target as HTMLInputElement).value = "";
            }}
          />
          <Button type="button" onClick={onPick}>
            Gallery
          </Button>
        </div>
        {ids.length > 0 && (
          <ul className="mt-2 flex flex-wrap gap-1" aria-label={`${field.label} assets`}>
            {ids.map((assetId) => (
              <li key={String(assetId)}>
                <button
                  type="button"
                  className="chip"
                  onClick={() => {
                    if (field.type === "images") {
                      onChange((Array.isArray(value) ? value : []).filter((item) => item !== assetId));
                    } else {
                      onChange(null);
                    }
                  }}
                >
                  #{String(assetId)} remove
                </button>
              </li>
            ))}
          </ul>
        )}
        {field.description && (
          <p id={hintId} className="mt-1 text-[11px] text-muted">
            {field.description}
          </p>
        )}
      </div>
    );
  }

  return (
    <div>
      <label className="label" htmlFor={id}>
        {field.label} (JSON)
      </label>
      <textarea
        id={id}
        className="input min-h-[72px] font-mono text-xs"
        defaultValue={JSON.stringify(value ?? null, null, 2)}
        key={`${field.name}:${JSON.stringify(value ?? null)}`}
        onBlur={(event) => {
          try {
            onChange(JSON.parse(event.target.value));
          } catch {
            event.target.setAttribute("aria-invalid", "true");
          }
        }}
      />
    </div>
  );
}

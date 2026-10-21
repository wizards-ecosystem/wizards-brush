import type { RecipeField } from "../api/types";

/** A field the saved default does not already satisfy, so a run must send it. */
export function recipeFieldRequired(field: RecipeField): boolean {
  if (field.type === "images") {
    const min = field.constraints.min_items ?? 0;
    const count = Array.isArray(field.default) ? field.default.length : 0;
    return count < min;
  }
  if (field.type === "asset") {
    return field.constraints.required === true && (field.default === null || field.default === undefined);
  }
  return false;
}

/** Form state for a run. Array defaults are copied so edits do not mutate the recipe. */
export function initialRecipeInputs(fields: RecipeField[]): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const field of fields) {
    values[field.name] = Array.isArray(field.default) ? [...field.default] : field.default;
  }
  return values;
}

export function recipeInputMissing(fields: RecipeField[], values: Record<string, unknown>): string | null {
  for (const field of fields) {
    if (!recipeFieldRequired(field)) continue;
    const value = values[field.name];
    if (field.type === "images" && (!Array.isArray(value) || value.length === 0)) {
      return `${field.label} is required.`;
    }
    if (field.type === "asset" && (value === null || value === undefined || value === "")) {
      return `${field.label} is required.`;
    }
  }
  return null;
}

import { describe, expect, it } from "vitest";
import { initialRecipeInputs, recipeFieldRequired, recipeInputMissing } from "./recipes";
import type { RecipeField } from "../api/types";

const prompt: RecipeField = {
  name: "prompt",
  type: "textarea",
  label: "Prompt",
  description: "",
  default: "a cup",
  constraints: {},
};

const images: RecipeField = {
  name: "images",
  type: "images",
  label: "Source images",
  description: "",
  default: [],
  constraints: { min_items: 1, max_items: 1, asset_kind: "image" },
};

describe("recipe form values", () => {
  it("treats an empty image list as required and copies array defaults", () => {
    expect(recipeFieldRequired(prompt)).toBe(false);
    expect(recipeFieldRequired(images)).toBe(true);
    const values = initialRecipeInputs([images]);
    expect(values.images).toEqual([]);
    expect(values.images).not.toBe(images.default);
    expect(recipeInputMissing([images], values)).toBe("Source images is required.");
    expect(recipeInputMissing([images], { images: [4] })).toBeNull();
  });
});

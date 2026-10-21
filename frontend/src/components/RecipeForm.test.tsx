import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { SavedRecipe } from "../api/types";
import { RecipeForm } from "./RecipeForm";

const recipe: SavedRecipe = {
  id: 3,
  name: "Ceramic cup",
  slug: "ceramic-cup",
  description: "A studio setup",
  kind: "image_local",
  fields: [
    {
      name: "prompt",
      type: "textarea",
      label: "Prompt",
      description: "What to draw.",
      default: "a cup",
      constraints: {},
    },
    {
      name: "scale",
      type: "select",
      label: "Scale",
      description: "",
      default: 4,
      constraints: { options: [2, 4] },
    },
  ],
  input_schema: {},
  client: "ui",
  created_at: "2026-10-21T00:00:00Z",
  updated_at: "2026-10-21T00:00:00Z",
  params: {},
  inputs: { images: [], mask: null, last_frame: null },
};

const withImages: SavedRecipe = {
  ...recipe,
  fields: [
    {
      name: "images",
      type: "images",
      label: "Source images",
      description: "Gallery image ids.",
      default: [],
      constraints: { min_items: 1, max_items: 1, asset_kind: "image" },
    },
  ],
};

describe("RecipeForm", () => {
  it("runs from the button, from Check, and from the keyboard", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn().mockResolvedValue(undefined);
    render(<RecipeForm recipe={recipe} onRun={onRun} />);

    expect(screen.getByRole("form", { name: "Run Ceramic cup" })).toBeInTheDocument();
    expect(screen.getByLabelText("Prompt")).toHaveValue("a cup");
    await user.clear(screen.getByLabelText("Prompt"));
    await user.type(screen.getByLabelText("Prompt"), "a blue cup");
    await user.selectOptions(screen.getByLabelText("Scale"), "2");
    await user.click(screen.getByRole("button", { name: "Check" }));
    expect(onRun).toHaveBeenCalledWith({ prompt: "a blue cup", scale: 2 }, true);

    await user.click(screen.getByRole("button", { name: "Run recipe" }));
    expect(onRun).toHaveBeenLastCalledWith({ prompt: "a blue cup", scale: 2 }, false);

    await user.click(screen.getByLabelText("Prompt"));
    await user.keyboard("{Control>}{Enter}{/Control}");
    expect(onRun).toHaveBeenCalledTimes(3);
    expect(onRun).toHaveBeenLastCalledWith({ prompt: "a blue cup", scale: 2 }, false);
  });

  it("asks for a required image before it runs, and accepts an asset id", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    render(<RecipeForm recipe={withImages} onRun={onRun} />);

    await user.click(screen.getByRole("button", { name: "Run recipe" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Source images is required.");
    expect(onRun).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText(/Source images/), "12{Enter}");
    expect(screen.getByRole("list", { name: "Source images assets" })).toHaveTextContent("#12");
    await user.click(screen.getByRole("button", { name: "Run recipe" }));
    expect(onRun).toHaveBeenCalledWith({ images: [12] }, false);
  });
});

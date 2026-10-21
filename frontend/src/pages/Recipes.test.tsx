import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SavedRecipe } from "../api/types";
import { Recipes } from "./Recipes";

const recipe: SavedRecipe = {
  id: 8,
  name: "Ceramic cup",
  slug: "ceramic-cup",
  description: "Studio",
  kind: "image_local",
  fields: [
    {
      name: "prompt",
      type: "textarea",
      label: "Prompt",
      description: "",
      default: "a cup",
      constraints: {},
    },
  ],
  input_schema: { type: "object", properties: { prompt: { type: "string" } }, additionalProperties: false },
  client: "ui",
  created_at: "2026-10-21T00:00:00Z",
  updated_at: "2026-10-21T00:00:00Z",
  params: { prompt: "a cup", seed: 7 },
  inputs: { images: [], mask: null, last_frame: null },
};

const mocks = vi.hoisted(() => ({
  recipes: vi.fn(),
  recipe: vi.fn(),
  runRecipe: vi.fn(),
  deleteRecipe: vi.fn(),
  toast: vi.fn(),
  refreshJobs: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    recipes: mocks.recipes,
    recipe: mocks.recipe,
    runRecipe: mocks.runRecipe,
    deleteRecipe: mocks.deleteRecipe,
  },
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      toast: mocks.toast,
      refreshJobs: mocks.refreshJobs,
    }),
}));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/recipes" element={<Recipes />} />
        <Route path="/recipes/:key" element={<Recipes />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Recipes page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.recipes.mockResolvedValue([recipe]);
    mocks.recipe.mockResolvedValue(recipe);
    mocks.deleteRecipe.mockResolvedValue({ ok: true });
    mocks.runRecipe.mockImplementation(async (_key: string, body: { dry_run?: boolean }) =>
      body.dry_run
        ? { ...recipe, dry_run: true, would_queue: 1, duplicate: false, job_id: null, job_ids: [] }
        : { ...recipe, dry_run: false, duplicate: false, job_id: 19, job_ids: [19], group_id: null },
    );
  });

  it("shows an empty state when nothing is saved", async () => {
    mocks.recipes.mockResolvedValue([]);
    renderAt("/recipes");
    expect(await screen.findByText("No recipes yet")).toBeInTheDocument();
    expect(screen.getByText("Select a recipe to run it, or save one from a generator.")).toBeInTheDocument();
  });

  it("checks and runs the selected recipe", async () => {
    const user = userEvent.setup();
    renderAt("/recipes/ceramic-cup");
    expect(await screen.findByRole("form", { name: "Run Ceramic cup" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Check" }));
    await waitFor(() =>
      expect(mocks.runRecipe).toHaveBeenCalledWith("ceramic-cup", {
        inputs: { prompt: "a cup" },
        dry_run: true,
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("Check passed");
    await user.click(screen.getByRole("button", { name: "Run recipe" }));
    await waitFor(() =>
      expect(mocks.runRecipe).toHaveBeenCalledWith("ceramic-cup", {
        inputs: { prompt: "a cup" },
        dry_run: false,
      }),
    );
    expect(mocks.refreshJobs).toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "job #19" })).toHaveAttribute("href", "/queue");
  });

  it("says when the recipe is not on this machine", async () => {
    mocks.recipe.mockRejectedValue(new Error("missing"));
    renderAt("/recipes/missing");
    expect(await screen.findByRole("alert")).toHaveTextContent("not on this machine");
  });

  it("deletes the open recipe", async () => {
    const user = userEvent.setup();
    renderAt("/recipes/ceramic-cup");
    await user.click(await screen.findByRole("button", { name: "Delete recipe" }));
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mocks.deleteRecipe).toHaveBeenCalledWith("ceramic-cup"));
  });
});

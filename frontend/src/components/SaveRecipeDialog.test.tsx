import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Control } from "../api/types";
import { SaveRecipeDialog } from "./SaveRecipeDialog";

const controls: Control[] = [
  { name: "prompt", label: "Prompt", type: "textarea", default: "" },
  { name: "seed", label: "Seed", type: "number", default: -1 },
];

const mocks = vi.hoisted(() => ({
  saveRecipe: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: { saveRecipe: mocks.saveRecipe },
}));

describe("SaveRecipeDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.saveRecipe.mockResolvedValue({
      id: 4,
      name: "Studio cup",
      slug: "studio-cup",
      description: "",
      kind: "image_local",
      fields: [],
      input_schema: {},
      client: "ui",
      created_at: "",
      updated_at: "",
      params: {},
      inputs: { images: [], mask: null, last_frame: null },
    });
  });

  it("saves the checked fields and leaves the rest frozen", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    render(
      <SaveRecipeDialog
        kind="image_local"
        controls={controls}
        values={{ prompt: "a ceramic cup", seed: 7 }}
        needsImage={false}
        needsMask={false}
        needsLastFrame={false}
        onClose={() => {}}
        onSaved={onSaved}
      />,
    );

    expect(screen.getByRole("checkbox", { name: /Prompt/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Seed/ })).not.toBeChecked();
    await user.type(screen.getByLabelText("Name"), "Studio cup");
    await user.type(screen.getByLabelText("Description"), "Studio lighting");
    await user.click(screen.getByRole("button", { name: "Save recipe" }));

    expect(mocks.saveRecipe).toHaveBeenCalledWith({
      name: "Studio cup",
      description: "Studio lighting",
      kind: "image_local",
      params: { prompt: "a ceramic cup", seed: 7 },
      styles: [],
      expose: ["prompt"],
      inputs: { images: [], mask: null, last_frame: null },
    });
    expect(onSaved).toHaveBeenCalled();
  });

  it("requires a gallery image when source images are not exposed", async () => {
    const user = userEvent.setup();
    render(
      <SaveRecipeDialog
        kind="inpaint"
        controls={controls}
        values={{ prompt: "mend" }}
        needsImage
        needsMask={false}
        needsLastFrame={false}
        onClose={() => {}}
        onSaved={() => {}}
      />,
    );

    expect(screen.getByText(/image on the form is not stored/i)).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /Source images/ }));
    await user.type(screen.getByLabelText("Name"), "Mend");
    await user.click(screen.getByRole("button", { name: "Save recipe" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Expose source images");
    expect(mocks.saveRecipe).not.toHaveBeenCalled();
  });
});

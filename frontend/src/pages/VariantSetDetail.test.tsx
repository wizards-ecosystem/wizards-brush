import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { VariantItem, VariantSetDetail } from "../api/types";
import { VariantSetDetail as DetailPage } from "./VariantSetDetail";

const mocks = vi.hoisted(() => ({
  variantSet: vi.fn(),
  retryVariantSet: vi.fn(),
  rerunVariantItem: vi.fn(),
  cancelVariantSet: vi.fn(),
  exportVariantSet: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    variantSet: mocks.variantSet,
    retryVariantSet: mocks.retryVariantSet,
    rerunVariantItem: mocks.rerunVariantItem,
    cancelVariantSet: mocks.cancelVariantSet,
    exportVariantSet: mocks.exportVariantSet,
  },
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      jobs: { 3: { id: 3, status: "running", progress: 0.4 } },
      variantRevision: 0,
      toast: mocks.toast,
    }),
}));
vi.mock("../components/AssetModal", () => ({ AssetModal: () => null }));

function item(
  id: number,
  state: VariantItem["state"],
  values: Record<string, string>,
  extra = {},
): VariantItem {
  return {
    id,
    stage: 0,
    ordinal: id,
    key: Object.entries(values)
      .map(([k, v]) => `${k}=${v}`)
      .join(","),
    values,
    state,
    state_reason: state === "failed" ? "remote exploded" : "",
    job_id: id,
    job_status: null,
    progress: null,
    attempts: 1,
    parent_item_id: null,
    source_asset_ids: [12],
    asset_ids: [],
    asset: null,
    validation_state: state === "invalid" ? "failed" : state === "succeeded" ? "passed" : "pending",
    validation:
      state === "invalid"
        ? [{ validator: "alpha", status: "fail", message: "an alpha channel is required", details: {} }]
        : [],
    output_name: `${Object.values(values).join("_")}.png`,
    prompt: "p",
    seed: 1,
    history: [],
    ...extra,
  };
}

const set: VariantSetDetail = {
  id: 9,
  name: "Finish study",
  status: "active",
  operation: "image_edit",
  recipe_id: null,
  group_id: "vset-x",
  expected: 5,
  counts: {
    total: 5,
    succeeded: 1,
    failed: 1,
    invalid: 1,
    canceled: 1,
    queued: 1,
    running: 0,
    pending: 0,
    blocked: 0,
  },
  collection_id: 4,
  source_asset_ids: [12],
  stages: [
    {
      index: 0,
      name: "",
      operation: "image_edit",
      axes: [
        { name: "material", values: [] },
        { name: "finish", values: [] },
      ],
    },
  ],
  replay: {},
  canceled_at: null,
  created_at: "2026-09-18T10:00:00Z",
  updated_at: "2026-09-18T10:00:00Z",
  items: [
    item(1, "succeeded", { material: "wood", finish: "matte" }),
    item(2, "failed", { material: "wood", finish: "gloss" }),
    item(3, "queued", { material: "steel", finish: "matte" }),
    item(4, "invalid", { material: "steel", finish: "gloss" }),
    item(5, "canceled", { material: "glass", finish: "matte" }),
  ],
};

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={["/variants/9"]}>
      <Routes>
        <Route path="/variants/:id" element={<DetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Variant Set detail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.variantSet.mockResolvedValue(set);
    mocks.retryVariantSet.mockResolvedValue({ retried: 2, submitted: 2 });
    mocks.rerunVariantItem.mockResolvedValue(set.items[0]);
    mocks.cancelVariantSet.mockResolvedValue({ canceled: 1 });
    mocks.exportVariantSet.mockResolvedValue(undefined);
  });

  it("shows every combination with its values, live state and validation", async () => {
    renderDetail();
    expect(await screen.findByText("Finish study")).toBeInTheDocument();
    const rows = screen.getAllByTestId("variant-row");
    expect(rows).toHaveLength(5);
    expect(screen.getByRole("columnheader", { name: "material" })).toBeInTheDocument();
    // The queued item's job is running: the live job state wins.
    expect(within(rows[2]).getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("remote exploded")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "failed" }));
    expect(screen.getByText(/alpha: an alpha channel is required/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /results collection/ })).toHaveAttribute(
      "href",
      "/gallery?collection=4",
    );
  });

  it("retries only failed and invalid variants, and resumes canceled ones on request", async () => {
    renderDetail();
    await userEvent.click(await screen.findByRole("button", { name: "Retry failed · 2" }));
    expect(mocks.retryVariantSet).toHaveBeenCalledWith(9);
    await userEvent.click(screen.getByRole("button", { name: "Resume canceled · 1" }));
    expect(mocks.retryVariantSet).toHaveBeenLastCalledWith(9, true);
  });

  it("reruns one variant on purpose, with or without a new seed", async () => {
    renderDetail();
    await screen.findByText("Finish study");
    const rerun = screen.getAllByRole("button", { name: "Rerun" });
    // In-flight variants cannot be rerun.
    expect(rerun[2]).toBeDisabled();
    await userEvent.click(rerun[0]);
    expect(mocks.rerunVariantItem).toHaveBeenCalledWith(9, 1, { reseed: false });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "New seed" })[0]).toBeEnabled());
    await userEvent.click(screen.getAllByRole("button", { name: "New seed" })[0]);
    expect(mocks.rerunVariantItem).toHaveBeenLastCalledWith(9, 1, { reseed: true });
  });

  it("cancels the remaining work after confirmation and exports what succeeded", async () => {
    renderDetail();
    await userEvent.click(await screen.findByRole("button", { name: "Cancel remaining" }));
    await userEvent.click(screen.getAllByRole("button", { name: "Cancel remaining" })[1]);
    expect(mocks.cancelVariantSet).toHaveBeenCalledWith(9);
    await userEvent.click(screen.getByRole("button", { name: "Export ZIP" }));
    expect(mocks.exportVariantSet).toHaveBeenCalledWith(9);
  });

  it("filters to the problems", async () => {
    renderDetail();
    await userEvent.click(await screen.findByRole("button", { name: "Failed · invalid" }));
    expect(screen.getAllByTestId("variant-row")).toHaveLength(2);
  });
});

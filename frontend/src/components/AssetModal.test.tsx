import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Asset } from "../api/types";
import { AssetModal } from "./AssetModal";

const asset: Asset = {
  id: 7,
  kind: "image",
  filename: "atelier.png",
  url: "/media/atelier.png",
  thumb_url: "/media/atelier-thumb.png",
  width: 1024,
  height: 1024,
  job_id: 3,
  generator: "image_local",
  meta: { prompt: "A studio study" },
  created_at: "2026-08-28T12:00:00Z",
  favorite: false,
  rating: 0,
  tags: [],
};

const mocks = vi.hoisted(() => ({
  createJob: vi.fn(),
  tool: vi.fn(),
  toast: vi.fn(),
  onClose: vi.fn(),
  markUsed: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    createJob: mocks.createJob,
    tool: mocks.tool,
  },
  markUsed: mocks.markUsed,
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      refreshAssets: vi.fn(),
      refreshStats: vi.fn(),
      toast: mocks.toast,
    }),
}));

vi.mock("./ZoomableImage", () => ({
  ZoomableImage: ({
    alt,
    pickMode,
    onPick,
  }: {
    alt: string;
    pickMode?: boolean;
    onPick?: (x: number, y: number) => void;
  }) => (
    <button type="button" disabled={!pickMode} onClick={() => onPick?.(48, 12)}>
      {alt}
    </button>
  ),
}));

function Path() {
  return <div data-testid="path">{useLocation().pathname}</div>;
}

function renderModal() {
  return render(
    <MemoryRouter>
      <Routes>
        <Route path="/" element={<AssetModal asset={asset} onClose={mocks.onClose} />} />
        <Route path="/g/:id" element={<Path />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("AssetModal refine and wand", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.createJob.mockResolvedValue({ job_id: 99, job_ids: [99], group_id: null, duplicate: false });
    mocks.tool.mockResolvedValue({ job_id: 41 });
  });

  it("keeps Refine disabled until there is a sentence", () => {
    renderModal();
    expect(screen.getByRole("button", { name: "Refine" })).toBeDisabled();
  });

  it("queues a refine job with the sentence, optional region, and wand point", async () => {
    const user = userEvent.setup();
    renderModal();
    const box = screen.getByPlaceholderText(/closed-mouth smile/);
    // Modal focus trap swallows userEvent.type; the sentence is what we submit.
    fireEvent.change(box, { target: { value: "low nape ponytail" } });
    await user.selectOptions(screen.getByLabelText("Region"), "hair");
    await user.click(screen.getByRole("button", { name: "Click a point" }));
    expect(screen.getByRole("button", { name: "Click the image…" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "A studio study" }));
    expect(screen.getByRole("button", { name: "Point 48,12" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByPlaceholderText(/closed-mouth smile/)).toHaveValue("low nape ponytail");
    expect(screen.getByRole("button", { name: "Refine" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Refine" }));
    await waitFor(() =>
      expect(mocks.createJob).toHaveBeenCalledWith({
        kind: "refine",
        params: { prompt: "low nape ponytail", region: "hair", click_x: 48, click_y: 12 },
        inputs: { images: [7] },
      }),
    );
    expect(mocks.toast).toHaveBeenCalledWith("Queued refine #99", "success");
    expect(mocks.onClose).toHaveBeenCalled();
  });

  it("saves an SCHP mask through the region-mask tool", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.selectOptions(screen.getByLabelText("Region"), "upper-clothes");
    await user.click(screen.getByRole("button", { name: "Save mask" }));
    expect(mocks.tool).toHaveBeenCalledWith("region-mask", {
      asset_id: 7,
      region: "upper-clothes",
    });
    expect(mocks.toast).toHaveBeenCalledWith("Region mask queued (#41)", "success");
  });

  it("sends the picture to A100 inpaint", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.click(screen.getByRole("button", { name: "A100 inpaint" }));
    expect(screen.getByTestId("path")).toHaveTextContent("/g/inpaint_remote");
  });
});

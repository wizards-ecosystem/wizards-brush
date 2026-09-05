import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Asset } from "../api/types";
import { Gallery } from "./Gallery";

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
  assets: vi.fn(),
  bulkDelete: vi.fn(),
  exportZip: vi.fn(),
  refreshAssets: vi.fn(),
  refreshStats: vi.fn(),
  toast: vi.fn(),
  assetRevision: 0,
}));

vi.mock("../api/client", () => ({
  api: {
    assets: mocks.assets,
    bulkDelete: mocks.bulkDelete,
    exportZip: mocks.exportZip,
  },
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      refreshAssets: mocks.refreshAssets,
      refreshStats: mocks.refreshStats,
      toast: mocks.toast,
      assetRevision: mocks.assetRevision,
    }),
}));

vi.mock("../components/CollectionBar", () => ({
  CollectionBar: () => <button>Collections</button>,
}));
vi.mock("../components/SavedSearches", () => ({
  SavedSearches: () => <button>Saved views</button>,
}));
vi.mock("../components/AssetModal", () => ({ AssetModal: () => null }));
vi.mock("../components/CompareModal", () => ({ CompareModal: () => null }));

describe("Gallery workstation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.assets.mockResolvedValue({ items: [asset], total: 1, limit: 60, offset: 0 });
    mocks.bulkDelete.mockResolvedValue({ ok: true, deleted: 1 });
    mocks.exportZip.mockResolvedValue(undefined);
    mocks.refreshAssets.mockResolvedValue(undefined);
    mocks.refreshStats.mockResolvedValue(undefined);
    mocks.assetRevision = 0;
  });

  it("keeps selection actions in a contextual shelf and describes soft deletion accurately", async () => {
    const user = userEvent.setup();
    render(<Gallery />);

    await screen.findByRole("button", { name: /Open image:/ });
    await user.click(screen.getByRole("button", { name: "Select asset" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Move to Trash" }));
    const dialog = screen.getByRole("dialog", { name: "Move 1 selected item to Trash?" });
    expect(dialog).toHaveAccessibleDescription(
      /can be restored from Trash until they are permanently purged/i,
    );

    await user.click(within(dialog).getByRole("button", { name: "Move to Trash" }));
    await waitFor(() => expect(mocks.bulkDelete).toHaveBeenCalledWith([7]));
  });

  it("opens secondary filters in an accessible refinement sheet", async () => {
    const user = userEvent.setup();
    render(<Gallery />);
    await screen.findByRole("button", { name: /Open image:/ });

    await user.click(screen.getByRole("button", { name: /Refine/ }));
    expect(screen.getByRole("dialog", { name: "Gallery refinements" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Media type" })).toBeInTheDocument();
  });

  it("reloads its filtered query when a new asset is announced", async () => {
    const view = render(<Gallery />);
    await screen.findByRole("button", { name: /Open image:/ });
    const initialCalls = mocks.assets.mock.calls.length;

    mocks.assetRevision = 1;
    view.rerender(<Gallery />);
    await waitFor(() => expect(mocks.assets.mock.calls.length).toBeGreaterThan(initialCalls));
  });
});

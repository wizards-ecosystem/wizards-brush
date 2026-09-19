import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Asset } from "../api/types";
import { Tools } from "./Tools";

const asset: Asset = {
  id: 12,
  kind: "image",
  filename: "source.png",
  url: "/files/images/source.png",
  thumb_url: null,
  width: 64,
  height: 64,
  job_id: null,
  generator: "local_image:txt2img",
  meta: { prompt: "A studio study" },
  created_at: "2026-09-18T10:00:00Z",
  favorite: false,
  rating: 0,
  tags: [],
};

const mocks = vi.hoisted(() => ({
  jobKinds: vi.fn(),
  tool: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    jobKinds: mocks.jobKinds,
    tool: mocks.tool,
    asset: vi.fn(),
  },
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      assets: [asset],
      jobs: {},
      toast: mocks.toast,
    }),
}));

describe("Tools region mask", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.jobKinds.mockResolvedValue([
      { kind: "matte", available: true },
      { kind: "region_mask", available: true },
    ]);
    mocks.tool.mockResolvedValue({ job_id: 18 });
  });

  it("queues an SCHP region mask for the selected image", async () => {
    const user = userEvent.setup();
    render(<Tools />);
    await user.click(await screen.findByRole("button", { name: /Open image/ }));
    await user.selectOptions(screen.getByLabelText("Parsing region"), "hair");
    await user.click(screen.getByRole("button", { name: "Save SCHP mask" }));
    expect(mocks.tool).toHaveBeenCalledWith("region-mask", { asset_id: 12, region: "hair" });
    expect(mocks.toast).toHaveBeenCalledWith("region-mask queued", "success");
  });
});

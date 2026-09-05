import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job } from "../api/types";
import { Queue } from "./Queue";

const job = (id: number, status: Job["status"], priority?: number): Job => ({
  id,
  kind: "image_local",
  status,
  priority,
  progress: status === "done" ? 100 : 20,
  message: status,
  params: { prompt: `Prompt ${id}` },
  result: {},
  error: "",
  created_at: `2026-08-28T12:0${id}:00Z`,
  started_at: null,
  finished_at: null,
});

const mocks = vi.hoisted(() => ({
  moveJob: vi.fn(),
  refreshJobs: vi.fn(),
  refreshAssets: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    moveJob: mocks.moveJob,
    prioritize: vi.fn(),
    rerun: vi.fn(),
    asset: vi.fn(),
    cancel: vi.fn(),
    skip: vi.fn(),
  },
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      jobs: {
        1: job(1, "done"),
        2: job(2, "queued", 10),
        3: job(3, "running"),
      },
      previews: {},
      specs: [
        {
          id: "image_local",
          needs_image: false,
          image_inputs: [],
          needs_remote: false,
        },
      ],
      refreshAssets: mocks.refreshAssets,
      refreshJobs: mocks.refreshJobs,
      toast: mocks.toast,
    }),
}));

describe("Queue run ledger", () => {
  beforeAll(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.moveJob.mockResolvedValue({ ok: true });
  });

  it("filters runs, supports arrow-key selection, and reorders queued work", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <Queue />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Inspect job 2" })).toHaveAttribute("aria-pressed", "true"),
    );
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("button", { name: "Inspect job 3" })).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: "Move job down in queue" }));
    expect(mocks.moveJob).toHaveBeenCalledWith(2, "down");

    await user.click(screen.getByRole("button", { name: "done" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Inspect job 1" })).toHaveAttribute("aria-pressed", "true"),
    );
    expect(screen.queryByRole("button", { name: "Inspect job 2" })).not.toBeInTheDocument();
  });

  it("opens selected job details as a dismissible mobile sheet", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <Queue />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Inspect job 3" }));
    expect(screen.getByLabelText("Selected job details")).toHaveClass("flex");
    expect(screen.getByRole("button", { name: "Close job details" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close job details" }));
    expect(screen.getByLabelText("Selected job details")).toHaveClass("hidden");
  });
});

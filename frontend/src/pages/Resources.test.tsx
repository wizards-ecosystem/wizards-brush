import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ImportInspection, ImportTask, ResourceView } from "../api/types";
import { Resources } from "./Resources";

const mocks = vi.hoisted(() => ({
  resources: vi.fn(),
  imports: vi.fn(),
  inspectImport: vi.fn(),
  createImport: vi.fn(),
  retryImport: vi.fn(),
  prepareStatus: vi.fn(),
  prepareRemote: vi.fn(),
  toast: vi.fn(),
  system: { remote_gpu: { connected: false, url: "" } } as Record<string, unknown>,
}));

vi.mock("../api/client", () => ({
  ApiError: class extends Error {},
  api: {
    resources: mocks.resources,
    imports: mocks.imports,
    inspectImport: mocks.inspectImport,
    createImport: mocks.createImport,
    retryImport: mocks.retryImport,
    cancelImport: vi.fn(),
    rescanResources: vi.fn().mockResolvedValue({ models: 1 }),
    prepareStatus: mocks.prepareStatus,
    prepareRemote: mocks.prepareRemote,
    cancelPrepare: vi.fn(),
  },
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      toast: mocks.toast,
      system: mocks.system,
      refreshControls: vi.fn().mockResolvedValue(undefined),
    }),
}));

const adapter = (over: Partial<ResourceView> = {}): ResourceView => ({
  id: "local:sha256:" + "a".repeat(64),
  kind: "adapter",
  family: "flux",
  provider: "local",
  provider_ref: "",
  revision: "",
  content_hash: "a".repeat(64),
  format: "safetensors",
  size_bytes: 2048,
  label: "ink-wash",
  license: {},
  access: "public",
  state: "installed",
  verification: "verified",
  compat: {},
  constraints: {},
  error: "",
  installs: [
    {
      target: "local",
      root: "loras",
      path: "ink-wash.safetensors",
      state: "verified",
      size_bytes: 2048,
      error: "",
    },
  ],
  updated_at: "",
  ...over,
});

const inspection = (over: Partial<ImportInspection> = {}): ImportInspection => ({
  provider: "hf",
  provider_ref: "owner/repo",
  revision: "0123456789abcdef0123456789abcdef01234567",
  url: "https://huggingface.co/owner/repo",
  name: "owner/repo",
  license: { id: "apache-2.0" },
  access: "public",
  family: "flux",
  candidates: [
    {
      id: "file:a.safetensors",
      type: "file",
      name: "a.safetensors",
      kind: null,
      size: 100,
      supported: true,
      reason: "",
    },
    {
      id: "file:b.safetensors",
      type: "file",
      name: "b.safetensors",
      kind: null,
      size: 100,
      supported: true,
      reason: "",
    },
    {
      id: "file:c.ckpt",
      type: "file",
      name: "c.ckpt",
      kind: null,
      supported: false,
      reason: "pickle-based formats can run code",
    },
  ],
  selection_required: true,
  default_selection: null,
  license_needs_acknowledgement: false,
  notes: [],
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.system = { remote_gpu: { connected: false, url: "" } };
  mocks.resources.mockResolvedValue([adapter()]);
  mocks.imports.mockResolvedValue([]);
  mocks.prepareStatus.mockResolvedValue(null);
});

describe("Resources", () => {
  it("lists the inventory with state, and flags an unknown licence", async () => {
    render(<Resources />);
    const row = await screen.findByTestId("resource-row");
    expect(row).toHaveTextContent("ink-wash");
    expect(row).toHaveTextContent(/licence unknown/i);
    expect(row).toHaveTextContent("loras/ink-wash.safetensors");
    // No worker that accepts resources: no prepare button.
    expect(screen.queryByTestId("prepare-remote")).not.toBeInTheDocument();
  });

  it("inspects first, and imports only an explicit, supported choice", async () => {
    mocks.inspectImport.mockResolvedValue(inspection());
    mocks.createImport.mockResolvedValue({ id: 1 });
    render(<Resources />);
    fireEvent.change(screen.getByLabelText(/model or adapter link/i), { target: { value: "hf:owner/repo" } });
    fireEvent.click(screen.getByRole("button", { name: /inspect/i }));
    await screen.findByTestId("inspection");
    expect(mocks.createImport).not.toHaveBeenCalled();
    const importButton = screen.getByRole("button", { name: /^import/i });
    expect(importButton).toBeDisabled(); // two candidates: nothing is chosen for the person
    expect(screen.getByRole("radio", { name: /c\.ckpt/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: /b\.safetensors/ }));
    fireEvent.change(screen.getByLabelText(/import as/i), { target: { value: "model" } });
    fireEvent.click(screen.getByRole("button", { name: /^import/i }));
    await waitFor(() =>
      expect(mocks.createImport).toHaveBeenCalledWith({
        link: "hf:owner/repo",
        selection: "file:b.safetensors",
        kind: "model",
        acknowledge_license: false,
      }),
    );
  });

  it("an unknown licence needs an acknowledgement before import", async () => {
    mocks.inspectImport.mockResolvedValue(
      inspection({
        license: {},
        license_needs_acknowledgement: true,
        candidates: [
          { id: "civitai:v1", type: "version", name: "v1", kind: "adapter", supported: true, reason: "" },
        ],
        default_selection: "civitai:v1",
        selection_required: false,
      }),
    );
    mocks.createImport.mockResolvedValue({ id: 2 });
    render(<Resources />);
    fireEvent.change(screen.getByLabelText(/model or adapter link/i), {
      target: { value: "civitai:model/1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /inspect/i }));
    await screen.findByTestId("inspection");
    expect(screen.getByRole("button", { name: /^import/i })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /reviewed the terms/i }));
    fireEvent.click(screen.getByRole("button", { name: /^import/i }));
    await waitFor(() =>
      expect(mocks.createImport).toHaveBeenCalledWith(expect.objectContaining({ acknowledge_license: true })),
    );
  });

  it("shows import tasks with progress and offers retry for a failed one", async () => {
    const task = (over: Partial<ImportTask>): ImportTask =>
      ({
        id: 1,
        provider: "hf",
        provider_ref: "o/r",
        revision: "main",
        filename: "a.safetensors",
        state: "failed",
        progress: 0.4,
        bytes_done: 40,
        bytes_total: 100,
        error: "connection reset",
        retryable: true,
        ...over,
      }) as ImportTask;
    mocks.imports.mockResolvedValue([
      task({}),
      task({ id: 2, filename: "b.safetensors", state: "running", retryable: false }),
    ]);
    mocks.retryImport.mockResolvedValue(task({ state: "queued" }));
    render(<Resources />);
    const rows = await screen.findAllByTestId("import-task");
    expect(rows[0]).toHaveTextContent("connection reset");
    expect(rows[1]).toHaveTextContent("40 B of 100 B");
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(mocks.retryImport).toHaveBeenCalledWith(1));
  });

  it("offers Remote GPU preparation only when a worker accepts resources", async () => {
    mocks.system = { remote_gpu: { connected: true, url: "x", features: ["resources"] } };
    mocks.prepareRemote.mockResolvedValue({
      resource_id: "x",
      state: "running",
      sent: 512,
      size: 2048,
      error: "",
    });
    render(<Resources />);
    fireEvent.click(await screen.findByRole("button", { name: /prepare on remote gpu/i }));
    expect(await screen.findByText(/sending to remote gpu 25%/i)).toBeInTheDocument();
  });
});

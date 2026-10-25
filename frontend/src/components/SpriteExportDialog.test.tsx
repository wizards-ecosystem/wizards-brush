import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SpriteExportDialog } from "./SpriteExportDialog";

const mocks = vi.hoisted(() => ({
  previewSprites: vi.fn(),
  exportSprites: vi.fn(),
  downloadArtifact: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("../api/client", () => ({ api: mocks }));
vi.mock("../store/useStore", () => ({
  useStore: (select: (s: unknown) => unknown) => select({ toast: mocks.toast }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  mocks.previewSprites.mockResolvedValue({
    pages: [{ width: 198, height: 134 }],
    frames: [{ index: 0, key: "a", page: 0, x: 2, y: 2, w: 96, h: 64 }],
    missing: [{ index: 1, key: "b", reason: "failed" }],
    grid: { columns: 2, cell_width: 96, cell_height: 64 },
  });
});

describe("SpriteExportDialog", () => {
  it("previews pages and missing frames before anything is written", async () => {
    render(<SpriteExportDialog setId={4} onClose={vi.fn()} />);
    const preview = await screen.findByTestId("sprite-preview");
    await waitFor(() => expect(preview).toHaveTextContent("1 frame on 1 page: 198×134"));
    expect(preview).toHaveTextContent("Missing: #1 b");
    expect(mocks.exportSprites).not.toHaveBeenCalled();
  });

  it("switching to an atlas re-previews with the new profile", async () => {
    render(<SpriteExportDialog setId={4} onClose={vi.fn()} />);
    await screen.findByText(/198×134/);
    fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "atlas" } });
    fireEvent.click(screen.getByLabelText(/trim transparent borders/i));
    await waitFor(() =>
      expect(mocks.previewSprites).toHaveBeenLastCalledWith(
        4,
        expect.objectContaining({ mode: "atlas", trim: true }),
      ),
    );
  });

  it("builds, downloads, and keeps the artifact's checksum on screen", async () => {
    const artifact = {
      id: 9,
      kind: "sprite_grid",
      filename: "sheet-4.zip",
      mime_type: "application/zip",
      size_bytes: 2048,
      sha256: "ab".repeat(32),
      source: "variant_set:4",
      meta: {},
      created_at: "",
      expires_at: "2026-11-01T00:00:00Z",
      file_url: "/api/artifacts/9/file",
      manifest: {},
    };
    mocks.exportSprites.mockResolvedValue(artifact);
    mocks.downloadArtifact.mockResolvedValue(undefined);
    render(<SpriteExportDialog setId={4} onClose={vi.fn()} />);
    await screen.findByText(/198×134/);
    fireEvent.click(screen.getByRole("button", { name: /build and download/i }));
    await waitFor(() => expect(mocks.downloadArtifact).toHaveBeenCalledWith(artifact));
    expect(screen.getByTestId("sprite-artifact")).toHaveTextContent("sha256 abababababababab");
  });
});

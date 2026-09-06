import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Settings } from "./Settings";

const mocks = vi.hoisted(() => ({
  getSettings: vi.fn(),
  saveBrowserSession: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    getSettings: mocks.getSettings,
    userPresets: vi.fn().mockResolvedValue([]),
    saveSettings: vi.fn(),
    deletePreset: vi.fn(),
  },
  saveBrowserSession: mocks.saveBrowserSession,
}));

vi.mock("../store/useStore", () => ({
  useStore: (select: (state: unknown) => unknown) =>
    select({
      refreshSystem: vi.fn().mockResolvedValue(undefined),
      system: null,
      refreshPresets: vi.fn().mockResolvedValue(undefined),
      stats: null,
      refreshStats: vi.fn().mockResolvedValue(undefined),
      toast: mocks.toast,
    }),
}));

describe("Settings browser access", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getSettings.mockRejectedValue(new Error("unauthorized"));
    mocks.saveBrowserSession.mockImplementation(() => new Promise(() => undefined));
  });

  it("keeps the token form available when protected settings cannot load", async () => {
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <Settings />
      </MemoryRouter>,
    );

    expect(await screen.findByText(/protected settings could not be loaded/i)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("API token"), { target: { value: "new token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));

    expect(mocks.saveBrowserSession).toHaveBeenCalledWith("new token");
  });
});

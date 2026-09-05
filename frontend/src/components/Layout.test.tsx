import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { Layout } from "./Layout";

vi.mock("../store/useStore", () => ({
  useStore: (sel: (s: unknown) => unknown) =>
    sel({
      specs: [
        {
          id: "txt2img",
          title: "Text to image",
          subtitle: "Local image",
          output: "image",
          needs_remote: false,
        },
        { id: "t2v", title: "Text to video", subtitle: "Remote motion", output: "video", needs_remote: true },
      ],
      jobs: { 12: { id: 12, status: "running" } },
      system: { local_gpu: { available: true }, remote_gpu: { connected: false }, models: {} },
      toasts: [],
      dismissToast: () => {},
    }),
}));

const renderAt = (path = "/") =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Layout />
    </MemoryRouter>,
  );

describe("Layout navigation drawer", () => {
  it("offers a menu button for narrow screens", () => {
    renderAt();
    expect(screen.getByLabelText("Open navigation")).toBeInTheDocument();
  });

  it("opens and closes the drawer", async () => {
    renderAt();
    const open = screen.getByLabelText("Open navigation");
    expect(open).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(open);
    expect(screen.getByLabelText("Open navigation")).toHaveAttribute("aria-expanded", "true");

    // the scrim closes it again
    await userEvent.click(screen.getByLabelText("Close navigation"));
    expect(screen.getByLabelText("Open navigation")).toHaveAttribute("aria-expanded", "false");
  });

  it("still renders the workspace links", () => {
    renderAt();
    for (const label of ["Tools", "Gallery", "Queue", "Trash", "Settings"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });

  it("opens the live generator launcher and exposes lane availability", async () => {
    renderAt();
    await userEvent.click(screen.getAllByLabelText("Open generator launcher")[0]);
    expect(screen.getByRole("dialog", { name: "Generator launcher" })).toBeInTheDocument();
    expect(screen.getByText("Text to image")).toBeInTheDocument();
    expect(screen.getByText("Text to video")).toBeInTheDocument();
    expect(screen.getByText("offline")).toBeInTheDocument();
    expect(screen.getAllByLabelText("1 active job")).toHaveLength(2);

    await userEvent.click(screen.getByText("Text to image"));
    expect(screen.queryByRole("dialog", { name: "Generator launcher" })).not.toBeInTheDocument();
  });
});

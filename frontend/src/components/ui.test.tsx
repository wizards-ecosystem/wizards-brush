import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Modal } from "./ui";

function ModalHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open inspector</button>
      {open && (
        <Modal label="Test inspector" onClose={() => setOpen(false)}>
          <div>
            <button>First action</button>
            <button>Last action</button>
          </div>
        </Modal>
      )}
    </>
  );
}

function NestedModalHarness() {
  const [outer, setOuter] = useState(false);
  const [inner, setInner] = useState(false);
  return (
    <>
      <button onClick={() => setOuter(true)}>Open outer</button>
      {outer && (
        <Modal label="Outer dialog" onClose={() => setOuter(false)}>
          <button onClick={() => setInner(true)}>Open confirmation</button>
          {inner && (
            <Modal label="Nested confirmation" onClose={() => setInner(false)}>
              <button>Confirm</button>
            </Modal>
          )}
        </Modal>
      )}
    </>
  );
}

describe("Modal", () => {
  it("names the dialog, traps focus, closes with Escape, and restores focus", async () => {
    const user = userEvent.setup();
    render(<ModalHarness />);
    const trigger = screen.getByText("Open inspector");
    await user.click(trigger);

    expect(screen.getByRole("dialog", { name: "Test inspector" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("First action")).toHaveFocus());

    await user.tab({ shift: true });
    expect(screen.getByText("Last action")).toHaveFocus();
    await user.tab();
    expect(screen.getByText("First action")).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Test inspector" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("dismisses from the backdrop and keeps nested Escape handling scoped to the top dialog", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ModalHarness />);
    await user.click(screen.getByText("Open inspector"));
    const dialog = screen.getByRole("dialog", { name: "Test inspector" });
    fireEvent.mouseDown(dialog.parentElement!);
    expect(screen.queryByRole("dialog", { name: "Test inspector" })).not.toBeInTheDocument();

    rerender(<NestedModalHarness />);
    await user.click(screen.getByText("Open outer"));
    await user.click(screen.getByText("Open confirmation"));
    expect(screen.getByRole("dialog", { name: "Nested confirmation" })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Nested confirmation" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Outer dialog" })).toBeInTheDocument();
  });
});

import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { LoadStatus } from "./LoadStatus";
import type { LoadState } from "../lib/jobEvents";

const state = (over: Partial<LoadState> = {}): LoadState => ({
  stage: "downloading",
  detail: "8.4 GB",
  at: Date.now(),
  ...over,
});

afterEach(() => vi.useRealTimers());

describe("LoadStatus", () => {
  it("shows nothing when nothing is loading", () => {
    const { container } = render(<LoadStatus state={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names the stage in words a person can read", () => {
    render(<LoadStatus state={state()} />);
    expect(screen.getByText("Downloading weights")).toBeInTheDocument();
    expect(screen.getByText(/8\.4 GB/)).toBeInTheDocument();
  });

  it("shows no progress bar", () => {
    // Deliberate: nothing in the load path knows how far along it is, and a bar
    // that stalls at 80% makes a promise the loader cannot keep.
    const { container } = render(<LoadStatus state={state()} />);
    expect(container.querySelector('[role="progressbar"]')).toBeNull();
    expect(container.querySelector("progress")).toBeNull();
  });

  it("reads as alive while heartbeats are recent", () => {
    render(<LoadStatus state={state()} />);
    expect(screen.queryByText(/may have stalled/)).not.toBeInTheDocument();
  });

  it("warns after a long silence, without a new event arriving", async () => {
    // The case that matters: no further event is coming, so the warning has to
    // appear on a timer rather than in response to a message.
    vi.useFakeTimers();
    const at = Date.now();
    render(<LoadStatus state={state({ at })} />);
    await act(async () => {
      vi.advanceTimersByTime(45_000);
    });
    expect(screen.getByText(/may have stalled/)).toBeInTheDocument();
  });

  it("announces itself to assistive technology", () => {
    render(<LoadStatus state={state()} />);
    const el = screen.getByRole("status");
    expect(el).toHaveAttribute("aria-live", "polite");
  });

  it("shows a hint when the backend supplies one", () => {
    render(<LoadStatus state={state({ hint: "first use downloads several GB" })} />);
    expect(screen.getByText(/downloads several GB/)).toBeInTheDocument();
  });
});

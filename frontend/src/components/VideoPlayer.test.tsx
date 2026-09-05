import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { VideoPlayer } from "./VideoPlayer";

/** jsdom implements no media pipeline, so currentTime/duration/play/pause are
 *  stubbed onto the prototype. */
function stubMedia(duration = 1.25) {
  let t = 0;
  Object.defineProperty(HTMLMediaElement.prototype, "duration", {
    configurable: true,
    get: () => duration,
  });
  Object.defineProperty(HTMLMediaElement.prototype, "currentTime", {
    configurable: true,
    get: () => t,
    set: (v: number) => {
      t = v;
    },
  });
  const play = vi.fn().mockResolvedValue(undefined);
  const pause = vi.fn();
  HTMLMediaElement.prototype.play = play;
  HTMLMediaElement.prototype.pause = pause;
  return { now: () => t, play, pause };
}

describe("VideoPlayer", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("offers frame-level transport, not just play/pause", () => {
    stubMedia();
    render(<VideoPlayer src="/v.mp4" fps={20} />);
    for (const label of ["First frame", "Previous frame", "Next frame", "Last frame", "Scrub video"]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
  });

  it("steps exactly one frame at the clip's fps", () => {
    const { now } = stubMedia();
    render(<VideoPlayer src="/v.mp4" fps={25} />);
    fireEvent.click(screen.getByLabelText("Next frame"));
    expect(now()).toBeCloseTo(1 / 25, 5);
  });

  it("never seeks before the start", () => {
    const { now } = stubMedia();
    render(<VideoPlayer src="/v.mp4" fps={20} />);
    fireEvent.click(screen.getByLabelText("Previous frame"));
    expect(now()).toBe(0);
  });

  it("lands on the last decoded frame rather than past the end", () => {
    /* Seeking to exactly duration shows black — the frame you actually want is
       the one you feed back in to extend a shot. */
    const { now } = stubMedia(2.0);
    render(<VideoPlayer src="/v.mp4" fps={20} />);
    fireEvent.click(screen.getByLabelText("Last frame"));
    expect(now()).toBeLessThan(2.0);
    expect(now()).toBeGreaterThan(1.9);
  });

  it("seeks to the scrubbed position", () => {
    const { now, pause } = stubMedia(2.0);
    const { container } = render(<VideoPlayer src="/v.mp4" fps={20} />);
    // jsdom never fires this, so the scrubber's max would stay 0 and any value
    // written to it would clamp back to 0.
    fireEvent.loadedMetadata(container.querySelector("video")!);
    fireEvent.change(screen.getByLabelText("Scrub video"), { target: { value: "0.5" } });
    expect(now()).toBeCloseTo(0.5, 5);
    // and pauses, so playback does not fight the drag
    expect(pause).toHaveBeenCalled();
  });
});

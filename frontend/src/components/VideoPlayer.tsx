import { useCallback, useEffect, useRef, useState } from "react";
import { Icon } from "./icons";

/**
 * Video transport for the lightbox.
 *
 * The native `<video controls>` gives you play and a scrub bar and nothing else.
 * For generated clips the questions are different: does the motion hold up
 * frame to frame, where exactly does an artefact appear, and what does the last
 * frame look like — that last one being the frame you feed back in to extend
 * the shot. So this adds frame stepping, a frame counter, and jump-to-ends.
 */
export function VideoPlayer({
  src,
  fps = 20,
  className = "",
}: {
  src: string;
  fps?: number;
  className?: string;
}) {
  const ref = useRef<HTMLVideoElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);

  const step = fps > 0 ? 1 / fps : 1 / 24;
  const frame = Math.round(time * fps);
  const frames = duration ? Math.max(1, Math.round(duration * fps)) : 0;

  const seek = useCallback((to: number) => {
    const v = ref.current;
    if (!v) return;
    v.currentTime = Math.max(0, Math.min(to, v.duration || 0));
  }, []);

  const nudge = useCallback(
    (frames: number) => {
      const v = ref.current;
      if (!v) return;
      v.pause();
      seek(v.currentTime + frames * step);
    },
    [seek, step],
  );

  const toggle = useCallback(() => {
    const v = ref.current;
    if (!v) return;
    if (v.paused) void v.play();
    else v.pause();
  }, []);

  // Arrow keys step a frame; space toggles. Guarded so typing in the tag editor
  // beside the player does not scrub the video.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      if (e.key === ",") nudge(-1);
      else if (e.key === ".") nudge(1);
      else if (e.key === " ") {
        e.preventDefault();
        toggle();
      } else return;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [nudge, toggle]);

  return (
    <div className={`flex flex-col items-center gap-2 ${className}`}>
      <video
        ref={ref}
        src={src}
        autoPlay
        loop
        muted
        playsInline
        className="max-h-[80vh] max-w-full"
        onLoadedMetadata={(e) => setDuration(e.currentTarget.duration || 0)}
        onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onClick={toggle}
      />

      <div className="w-full max-w-3xl px-2">
        <input
          type="range"
          min={0}
          max={duration || 0}
          step={step}
          value={time}
          aria-label="Scrub video"
          onChange={(e) => {
            ref.current?.pause();
            seek(Number(e.target.value));
          }}
        />
        <div className="flex items-center justify-center gap-2 text-xs text-white/60">
          <button className="btn-ghost" aria-label="First frame" onClick={() => seek(0)}>
            <Icon name="skip" size={15} className="rotate-180" />
          </button>
          <button className="btn-ghost" aria-label="Previous frame" onClick={() => nudge(-1)}>
            <Icon name="chevron-left" size={15} />
          </button>
          <button className="btn-ghost w-8" aria-label={playing ? "Pause" : "Play"} onClick={toggle}>
            <Icon name={playing ? "pause" : "play"} size={15} />
          </button>
          <button className="btn-ghost" aria-label="Next frame" onClick={() => nudge(1)}>
            <Icon name="chevron-right" size={15} />
          </button>
          <button
            className="btn-ghost"
            aria-label="Last frame"
            /* Nudged back a hair: seeking to exactly duration lands past the
               last decoded frame and shows black. */
            onClick={() => seek((ref.current?.duration || 0) - step / 2)}
          >
            <Icon name="skip" size={15} />
          </button>
          <span className="tabular-nums ml-2">
            frame {frames ? Math.min(frame + 1, frames) : 0}/{frames || "?"}
          </span>
          <span className="tabular-nums text-white/35">{time.toFixed(2)}s</span>
        </div>
      </div>
    </div>
  );
}

import { useEffect, useState } from "react";
import { LOAD_STAGE_LABEL, loadLooksStalled } from "../lib/jobEvents";
import type { LoadState } from "../lib/jobEvents";
import { Icon } from "./icons";

/**
 * What a model load is doing right now.
 *
 * A local model swap takes 60-120 seconds, during which the app used to show
 * nothing at all and looked wedged. There is deliberately **no progress bar**:
 * nothing in the load path knows how far along it is, and a bar that stalls at
 * 80% is worse than no bar because it makes a promise the loader cannot keep.
 *
 * Liveness comes from the heartbeat instead. The backend re-sends the current
 * stage every two seconds; the stage text does not change but its arrival time
 * does, so a spinner that keeps turning is evidence the load is alive. After
 * thirty seconds of silence — fifteen missed beats — that becomes a warning
 * rather than a lie.
 */
export function LoadStatus({ state }: { state: LoadState | null | undefined }) {
  // The clock lives in state, not in a Date.now() call during render. Reading
  // the clock while rendering is impure — two renders of the same props would
  // disagree — and the value only needs to advance on the heartbeat interval
  // anyway. Seeded from the event's own timestamp so the first render is never
  // spuriously "stalled".
  const [ticked, setTicked] = useState(0);

  useEffect(() => {
    if (!state) return;
    const t = setInterval(() => setTicked(Date.now()), 2000);
    return () => clearInterval(t);
  }, [state]);

  if (!state) return null;
  // max() rather than resetting state when a new stage arrives: a fresh event
  // always has the later timestamp, so it un-stalls the display on its own and
  // the effect never has to write state synchronously to keep up.
  const now = Math.max(ticked, state.at);
  const stalled = loadLooksStalled(state, now);
  const waited = Math.max(0, Math.round((now - state.at) / 1000));

  return (
    <div
      className={`flex items-start gap-2 border-l-2 p-2.5 text-xs ${
        stalled ? "border-warn bg-warn/8 text-warn" : "border-accent bg-panel2 text-muted"
      }`}
      role="status"
      aria-live="polite"
    >
      <span className={stalled ? "" : "animate-spin"}>
        <Icon name={stalled ? "alert" : "refresh"} size={13} />
      </span>
      <span className="min-w-0">
        <span className="text-ink">{LOAD_STAGE_LABEL[state.stage]}</span>
        {state.detail && <span className="ml-1 truncate">· {state.detail}</span>}
        {state.hint && !stalled && <div className="mt-0.5 text-[10px] text-white/40">{state.hint}</div>}
        {stalled && (
          <div className="mt-0.5 text-[10px]">
            No update for {waited}s. Still working, or the load may have stalled — check the server log.
          </div>
        )}
      </span>
    </div>
  );
}

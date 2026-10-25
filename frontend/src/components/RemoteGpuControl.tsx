import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import type { RemoteSession } from "../api/types";
import { useStore } from "../store/useStore";
import { Button } from "./ui";

/** Poll fast while something is in motion, slowly while the pod is just up:
 *  a worker can drop, and a pod can be stopped from the provider's console. */
const SETTLING_MS = 5000;
const RUNNING_MS = 30000;
const UNRESOLVED = new Set(["stop_unconfirmed", "stop_failed"]);

function age(observedAt: number | undefined, now: number): string | null {
  if (!observedAt) return null;
  const seconds = Math.max(0, Math.round(now / 1000 - observedAt));
  return seconds < 90 ? `${seconds} s ago` : `${Math.round(seconds / 60)} min ago`;
}

/**
 * Start and stop rented Remote GPU hardware.
 *
 * Renders nothing unless this install is configured to provision, so the
 * self-hosted default sees no trace of a feature it does not use.
 *
 * The running cost is on screen the whole time it is up. If the app is going to
 * spend money on someone's behalf, the amount belongs in front of them rather
 * than in a billing page they have to go looking for. A stop the provider did
 * not confirm stays on screen too, as possibly billing, until it is retried or
 * the pod is confirmed gone and forgotten.
 */
export function RemoteGpuControl() {
  const [session, setSession] = useState<RemoteSession | null>(null);
  // When this panel last read the session: the provider's answer is aged
  // against that, not against a clock read during render.
  const [readAt, setReadAt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [forgetting, setForgetting] = useState(false);
  const [confirmId, setConfirmId] = useState("");
  const toast = useStore((state) => state.toast);
  const refreshSystem = useStore((state) => state.refreshSystem);
  // Every request takes a ticket; only the newest answer is applied, so a slow
  // poll can never overwrite the result of a Stop that finished after it.
  const ticket = useRef(0);
  const alive = useRef(true);

  const apply = useCallback((mine: number, next: RemoteSession | null) => {
    if (alive.current && mine === ticket.current) {
      setSession(next);
      setReadAt(Date.now());
    }
  }, []);

  const load = useCallback(async () => {
    const mine = ++ticket.current;
    try {
      apply(mine, await api.remoteSession());
    } catch {
      apply(mine, null); // an unreachable provider must not break the panel
    }
  }, [apply]);

  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
      ticket.current += 1;
    };
  }, [load]);

  const state = session?.state;
  const delay = !session?.running
    ? null
    : state === "starting" || state === "stopping"
      ? SETTLING_MS
      : RUNNING_MS;
  useEffect(() => {
    if (delay === null) return;
    const timer = window.setInterval(() => {
      void load().then(() => refreshSystem());
    }, delay);
    return () => window.clearInterval(timer);
  }, [delay, load, refreshSystem]);

  if (!session || (!session.can_provision && !session.running)) return null;

  const start = async () => {
    setBusy(true);
    const mine = ++ticket.current;
    try {
      apply(mine, await api.startRemoteSession());
      toast("Renting a GPU — this takes a few minutes", "info");
    } catch (error) {
      toast(`Could not start: ${error}`, "error");
      void load();
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    const mine = ++ticket.current;
    try {
      apply(mine, await api.stopRemoteSession());
      await refreshSystem();
      toast("Remote GPU terminated", "success");
    } catch (error) {
      // An unconfirmed stop carries the session it left behind: show that,
      // never a blank "off" that would hide hardware that may still bill.
      if (error instanceof ApiError && error.state && "running" in error.state) {
        apply(mine, error.state as unknown as RemoteSession);
      } else {
        void load();
      }
      toast(`Stop not confirmed: ${error}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const forget = async () => {
    setBusy(true);
    const mine = ++ticket.current;
    try {
      apply(mine, await api.forgetRemoteSession(confirmId.trim()));
      setForgetting(false);
      setConfirmId("");
      await refreshSystem();
      toast("Forgot the recorded pod. The app can no longer stop it.", "info");
    } catch (error) {
      toast(`Could not forget: ${error}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const unresolved = UNRESOLVED.has(session.state);
  const minutes = Math.floor(session.elapsed_s / 60);
  const cost = session.cost_estimate_usd !== null ? `~$${session.cost_estimate_usd.toFixed(2)}` : null;
  const rate = session.hourly_usd !== null ? `$${session.hourly_usd.toFixed(2)}/hr` : null;
  const checked = age(session.observed_at, readAt);

  return (
    <div className="mt-3 rounded border border-white/10 p-3 text-sm" data-testid="remote-gpu-control">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="font-medium">
            {session.label}
            {session.gpu ? ` · ${session.gpu}` : ""}
          </div>
          <div className="text-xs opacity-70">
            {!session.running && "Not running — costs nothing while off"}
            {session.state === "starting" && (session.detail || "starting")}
            {session.state === "ready" &&
              (session.worker_connected ? "Ready" : "Pod up — waiting for the worker")}
            {session.state === "stopping" && "Terminating"}
            {session.state === "stop_unconfirmed" && "Stop sent, not confirmed — the pod may still exist"}
            {session.state === "stop_failed" && "Stop refused — the pod is still recorded"}
            {session.state === "error" && (session.detail || "error")}
          </div>
        </div>
        {session.running ? (
          <Button onClick={stop} disabled={busy}>
            {unresolved ? "Retry stop" : "Stop"}
          </Button>
        ) : (
          <Button onClick={start} disabled={busy || !session.can_provision}>
            Start GPU
          </Button>
        )}
      </div>
      {session.running ? (
        <div className="mt-2 text-xs opacity-70" data-testid="remote-gpu-cost">
          {minutes} min{rate ? ` · ${rate}` : ""}
          {cost ? ` · ${cost} so far (estimate)` : ""}
          {session.possibly_billing && unresolved ? " · may still be billing" : ""}
        </div>
      ) : (
        rate && <div className="mt-2 text-xs opacity-70">Billed per second while running, about {rate}.</div>
      )}
      {session.running && (checked || session.observe_error) && (
        <div className="mt-1 text-[11px] opacity-60" data-testid="remote-gpu-observed">
          {checked ? `Provider last answered ${checked}` : "The provider has not answered yet"}
          {session.observe_error ? ` · latest check failed: ${session.observe_error}` : ""}
        </div>
      )}
      {unresolved && (
        <div
          className="mt-2 space-y-2 border-l-2 border-danger pl-2 text-xs"
          data-testid="remote-gpu-unresolved"
        >
          {session.stop_error && <div>{session.stop_error}</div>}
          <div className="opacity-70">
            Retry stop asks the provider whether pod <span className="technical">{session.pod_id}</span> still
            exists before terminating it again
            {session.stop_attempts
              ? ` (${session.stop_attempts} attempt${session.stop_attempts > 1 ? "s" : ""} so far)`
              : ""}
            . If the console shows it is gone, forget it here.
          </div>
          {forgetting ? (
            <div className="flex flex-wrap items-center gap-2">
              <input
                className="input w-56 text-xs"
                aria-label="Type the pod id to confirm"
                placeholder={session.pod_id}
                value={confirmId}
                onChange={(event) => setConfirmId(event.target.value)}
              />
              <Button size="sm" onClick={forget} disabled={busy || confirmId.trim() !== session.pod_id}>
                Forget pod
              </Button>
              <Button size="sm" onClick={() => setForgetting(false)}>
                Cancel
              </Button>
            </div>
          ) : (
            <Button size="sm" onClick={() => setForgetting(true)}>
              It is gone — forget it
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

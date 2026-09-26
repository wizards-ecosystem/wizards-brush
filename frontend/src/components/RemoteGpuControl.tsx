import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { RemoteSession } from "../api/types";
import { useStore } from "../store/useStore";
import { Button } from "./ui";

/**
 * Start and stop rented Remote GPU hardware.
 *
 * Renders nothing unless this install is configured to provision, so the
 * self-hosted default sees no trace of a feature it does not use.
 *
 * The running cost is on screen the whole time it is up. If the app is going to
 * spend money on someone's behalf, the amount belongs in front of them rather
 * than in a billing page they have to go looking for.
 */
export function RemoteGpuControl() {
  const [session, setSession] = useState<RemoteSession | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useStore((state) => state.toast);
  const refreshSystem = useStore((state) => state.refreshSystem);
  const timer = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      setSession(await api.remoteSession());
    } catch {
      setSession(null); // an unreachable provider must not break the panel
    }
  }, []);

  useEffect(() => {
    // Guarded rather than fire-and-forget: the panel can unmount mid-request
    // when someone navigates away from Settings while a pull is in progress.
    let cancelled = false;
    api
      .remoteSession()
      .then((next) => {
        if (!cancelled) setSession(next);
      })
      .catch(() => {
        if (!cancelled) setSession(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll only while something is in motion. A pod that is up and answering does
  // not need a request every few seconds, but a pull in progress does.
  const settling = session?.state === "starting" || session?.state === "stopping";
  useEffect(() => {
    if (!settling) return;
    timer.current = window.setInterval(() => {
      void load().then(() => refreshSystem());
    }, 5000);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [settling, load, refreshSystem]);

  if (!session || (!session.can_provision && !session.running)) return null;

  const start = async () => {
    setBusy(true);
    try {
      setSession(await api.startRemoteSession());
      toast("Renting a GPU — this takes a few minutes", "info");
    } catch (error) {
      toast(`Could not start: ${error}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    try {
      setSession(await api.stopRemoteSession());
      await refreshSystem();
      toast("Remote GPU terminated", "success");
    } catch (error) {
      toast(`Could not stop: ${error}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const minutes = Math.floor(session.elapsed_s / 60);
  const cost = session.cost_estimate_usd !== null ? `~$${session.cost_estimate_usd.toFixed(2)}` : null;
  const rate = session.hourly_usd !== null ? `$${session.hourly_usd.toFixed(2)}/hr` : null;

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
            {session.state === "error" && (session.detail || "error")}
          </div>
        </div>
        {session.running ? (
          <Button onClick={stop} disabled={busy}>
            Stop
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
          {cost ? ` · ${cost} so far` : ""}
        </div>
      ) : (
        rate && <div className="mt-2 text-xs opacity-70">Billed per second while running, about {rate}.</div>
      )}
    </div>
  );
}

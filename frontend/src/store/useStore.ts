import { create } from "zustand";
import { api, jobSocket, prepareBrowserSession } from "../api/client";
import type { Asset, GeneratorSpec, Job, Presets, Stats, SystemStatus } from "../api/types";
import { applyJobEvent } from "../lib/jobEvents";
import { previewObjectUrl } from "../lib/wsframe";
import type { LoadState } from "../lib/jobEvents";

export type ToastKind = "info" | "success" | "error";
export interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}

let _toastId = 0;
let _sysTimer: ReturnType<typeof setInterval> | null = null;

interface State {
  system: SystemStatus | null;
  specs: GeneratorSpec[];
  presets: Presets | null;
  stats: Stats | null;
  jobs: Record<number, Job>;
  previews: Record<number, string>; // job id → live-preview data URL (transient)
  /** job id → what its model load is doing. Kept out of `jobs` for the same
   *  reason previews are: it updates on a heartbeat, and merging it into the
   *  job row would re-render every consumer of that row twice a second. */
  loads: Record<number, LoadState>;
  assets: Asset[]; // recent snapshot for pickers/dashboard/tools
  /** Monotonic signal for pages with their own paginated asset query. The
   * recent-assets snapshot is data, not a reliable invalidation channel. */
  assetRevision: number;
  toasts: Toast[];
  ws: WebSocket | null;
  booted: boolean;

  boot: () => Promise<void>;
  refreshSystem: () => Promise<void>;
  refreshAssets: () => Promise<void>;
  refreshJobs: () => Promise<void>;
  refreshPresets: () => Promise<void>;
  refreshStats: () => Promise<void>;
  specById: (id: string) => GeneratorSpec | undefined;
  toast: (text: string, kind?: ToastKind) => void;
  dismissToast: (id: number) => void;
}

export const useStore = create<State>((set, get) => ({
  system: null,
  specs: [],
  presets: null,
  stats: null,
  jobs: {},
  previews: {},
  loads: {},
  assets: [],
  assetRevision: 0,
  toasts: [],
  ws: null,
  booted: false,

  boot: async () => {
    if (get().booted) return;
    set({ booted: true });
    try {
      await prepareBrowserSession();
      const [specs, presets] = await Promise.all([api.models(), api.presets()]);
      set({ specs, presets });
    } catch (e) {
      // Config load failed (backend down mid-boot): don't strand the app with an
      // empty sidebar and no retry — reset the guard and try again shortly.
      get().toast(`${e} — retrying…`, "error");
      set({ booted: false });
      setTimeout(() => get().boot(), 3000);
      return;
    }
    await Promise.all([
      get().refreshSystem(),
      get().refreshJobs(),
      get().refreshAssets(),
      get().refreshStats(),
    ]);

    // Live job updates via WebSocket; refresh assets/stats when a job finishes.
    let jobsRefreshTimer: ReturnType<typeof setTimeout> | null = null;
    let doneRefreshTimer: ReturnType<typeof setTimeout> | null = null;
    let assetRefreshTimer: ReturnType<typeof setTimeout> | null = null;
    let hadOpen = false;
    let reconnectAttempt = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    const connect = () => {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      // Tear down any prior socket first so an HMR/StrictMode re-boot doesn't
      // leave a second live connection (and its 2s reconnect loop) behind.
      const stale = get().ws;
      if (stale) {
        stale.onclose = null; // don't let the old socket schedule another reconnect
        stale.close();
      }
      const ws = jobSocket(
        (e) => {
          // An asset finished mid-job. A batch of eight is ONE job, so waiting for
          // its terminal event means nothing appears until the last image is done;
          // the backend announces each one as it lands. Debounced because a fast
          // batch would otherwise fire one full asset fetch per image.
          if (e.type === "asset") {
            set((state) => ({ assetRevision: state.assetRevision + 1 }));
            if (!assetRefreshTimer) {
              assetRefreshTimer = setTimeout(() => {
                assetRefreshTimer = null;
                get().refreshAssets();
              }, 250);
            }
            return;
          }
          if (e.type !== "job" && e.type !== "model_load") return;
          const id = e.id ?? e.job_id;
          const known = !!get().jobs[id];
          set((s) => {
            const r = applyJobEvent(s.jobs, e);
            if (!r) return {};
            const previews = { ...s.previews };
            if (r.clearPreview != null) {
              const done = previews[r.clearPreview];
              if (done) URL.revokeObjectURL(done);
              delete previews[r.clearPreview];
            }
            const loads = { ...s.loads };
            if (r.load) {
              if (r.load.state) loads[r.load.id] = r.load.state;
              else delete loads[r.load.id];
            }
            return { jobs: r.jobs, previews, loads };
          });
          // A model_load event is not a job row, so it must not trigger the
          // "first sight of this job" fetch below.
          if (e.type === "model_load") return;
          if (!known && !jobsRefreshTimer) {
            // First sight of this job was a WS event — pull the full row (params etc.).
            jobsRefreshTimer = setTimeout(() => {
              jobsRefreshTimer = null;
              get().refreshJobs();
            }, 300);
          }
          if (e.status === "done" || e.status === "error") {
            if (e.status === "error") {
              get().toast(`Job #${id} failed: ${(e.error || "error").split("\n")[0]}`, "error");
            }
            // Debounced: a grid/batch finishing cell-by-cell must not fire three
            // HTTP refreshes per done event.
            if (!doneRefreshTimer) {
              doneRefreshTimer = setTimeout(() => {
                doneRefreshTimer = null;
                get().refreshAssets();
                get().refreshJobs();
                get().refreshStats();
              }, 400);
            }
          }
        },
        (frame) => {
          // A preview frame. Object URLs are not garbage collected, and one
          // arrives several times a second, so the previous URL for this job must
          // be revoked as it is replaced or the tab leaks steadily.
          set((s) => {
            const previews = { ...s.previews };
            const stale = previews[frame.jobId];
            if (stale) URL.revokeObjectURL(stale);
            previews[frame.jobId] = previewObjectUrl(frame);
            return { previews };
          });
        },
      );
      ws.onopen = () => {
        // Reconnect: any terminal event that fired while the socket was down is
        // gone for good — resync or jobs stay "running" in the UI forever.
        if (hadOpen) {
          get().refreshJobs();
          get().refreshAssets();
          get().refreshStats();
        }
        hadOpen = true;
        reconnectAttempt = 0;
      };
      ws.onclose = () => {
        const delay = Math.min(30_000, 1_000 * 2 ** Math.min(reconnectAttempt, 5));
        reconnectAttempt += 1;
        reconnectTimer = setTimeout(connect, delay);
      };
      set({ ws });
    };
    connect();

    if (_sysTimer) clearInterval(_sysTimer); // avoid stacking pollers across re-boots
    _sysTimer = setInterval(() => get().refreshSystem(), 5000);
  },

  refreshSystem: async () => {
    try {
      set({ system: await api.system() });
    } catch {
      /* transient */
    }
  },
  refreshAssets: async () => {
    try {
      const page = await api.assets({ limit: 300, sort: "newest" });
      set({ assets: page.items });
    } catch {
      /* transient */
    }
  },
  refreshJobs: async () => {
    try {
      const list = await api.jobs(150);
      const jobs = Object.fromEntries(list.map((jb) => [jb.id, jb]));
      set((s) => {
        // Drop previews whose job is no longer running — a data URL for a job
        // whose final event was missed would otherwise leak until reload.
        // A load entry for a job that is no longer running is stale — the job
        // ended without a "ready" stage (cancelled, or it failed mid-load).
        const loads: Record<number, LoadState> = {};
        Object.entries(s.loads).forEach(([k, st]) => {
          const jb = jobs[Number(k)];
          if (jb && jb.status === "running") loads[Number(k)] = st;
        });
        const previews: Record<number, string> = {};
        Object.entries(s.previews).forEach(([k, url]) => {
          const jb = jobs[Number(k)];
          if (jb && (jb.status === "running" || jb.status === "queued")) {
            previews[Number(k)] = url;
          } else {
            URL.revokeObjectURL(url); // dropped here, so release it here
          }
        });
        return { jobs, previews, loads };
      });
    } catch {
      /* transient */
    }
  },
  refreshPresets: async () => {
    try {
      set({ presets: await api.presets() });
    } catch {
      /* transient */
    }
  },
  refreshStats: async () => {
    try {
      set({ stats: await api.stats() });
    } catch {
      /* transient */
    }
  },
  specById: (id) => get().specs.find((s) => s.id === id),

  toast: (text, kind = "info") => {
    const id = ++_toastId;
    set((s) => ({ toasts: [...s.toasts, { id, kind, text }] }));
    setTimeout(() => get().dismissToast(id), kind === "error" ? 7000 : 4000);
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

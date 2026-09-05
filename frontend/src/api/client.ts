import type {
  AppSettings,
  Asset,
  AssetPage,
  AssetQuery,
  BackendReport,
  CollectionInfo,
  GeneratorSpec,
  GridConfig,
  GridResult,
  HumanGrade,
  HumanGradeInput,
  ImportedMetadata,
  Job,
  LoraCatalog,
  OkResponse,
  Presets,
  PromptHistoryItem,
  Stats,
  SystemStatus,
  UserPreset,
  NextPicks,
  WildcardInfo,
} from "./types";
import { decodeFrame } from "../lib/wsframe";
import type { BinaryFrame } from "../lib/wsframe";

/** Extract FastAPI's {detail} from an error response, falling back to raw text/status. */
async function errorDetail(r: Response): Promise<string> {
  let detail = await r.text();
  try {
    detail = JSON.parse(detail).detail || detail;
  } catch {
    /* keep raw text */
  }
  return detail || `${r.status}`;
}

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(await errorDetail(r));
  return r.json();
}

/** fetch wrapper that turns a network-level reject (backend down/offline) into a
 * friendly message instead of a raw "TypeError: Failed to fetch" in toasts. */
async function safeFetch(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch {
    throw new Error("Can't reach the backend — is it running?");
  }
}

export function authHeaders(): Record<string, string> {
  const tok = localStorage.getItem("api_token");
  syncMediaToken(tok || "");
  return tok ? { "X-API-Token": tok } : {};
}

/** Keep browser-only auth out of URLs. Media elements and WebSockets cannot
 * attach the API header, so each gets a narrowly scoped SameSite cookie. */
export function syncMediaToken(token: string): void {
  if (typeof document === "undefined") return;
  const secure = typeof location !== "undefined" && location.protocol === "https:" ? "; Secure" : "";
  document.cookie = token
    ? `wb_media_token=${encodeURIComponent(token)}; Path=/files; SameSite=Strict${secure}`
    : "wb_media_token=; Path=/files; SameSite=Strict; Max-Age=0";
  document.cookie = token
    ? `wb_ws_token=${encodeURIComponent(token)}; Path=/api/jobs/ws; SameSite=Strict${secure}`
    : "wb_ws_token=; Path=/api/jobs/ws; SameSite=Strict; Max-Age=0";
}

const get = (url: string) => safeFetch(url, { headers: authHeaders() });

const postJson = (url: string, body: unknown) =>
  safeFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });

const del = (url: string) => safeFetch(url, { method: "DELETE", headers: authHeaders() });

/** Query-string builder: drops undefined/null/empty values. Exported for tests. */
export function buildQuery(query: Record<string, unknown>): string {
  const p = new URLSearchParams();
  Object.entries(query).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  });
  return p.toString();
}

export const api = {
  system: () => get("/api/system").then((r) => j<SystemStatus>(r)),
  backends: () => get("/api/backends").then((r) => j<BackendReport>(r)),
  models: () => get("/api/models").then((r) => j<GeneratorSpec[]>(r)),
  presets: () => get("/api/presets").then((r) => j<Presets>(r)),
  stats: () => get("/api/stats").then((r) => j<Stats>(r)),

  jobs: (limit = 100) => get(`/api/jobs?limit=${limit}`).then((r) => j<Job[]>(r)),
  cancel: (id: number) =>
    safeFetch(`/api/jobs/${id}/cancel`, { method: "POST", headers: authHeaders() }).then((r) =>
      j<OkResponse>(r),
    ),
  skip: (id: number) =>
    safeFetch(`/api/jobs/${id}/skip`, { method: "POST", headers: authHeaders() }).then((r) =>
      j<OkResponse>(r),
    ),
  rerun: (id: number, reseed: boolean) =>
    safeFetch(`/api/jobs/${id}/rerun?reseed=${reseed}`, { method: "POST", headers: authHeaders() }).then(
      (r) => j<{ job_id?: number; replaced_job_id?: number; model_warning?: string; error?: string }>(r),
    ),
  prioritize: (id: number) => postJson(`/api/jobs/${id}/front`, {}).then((r) => j<OkResponse>(r)),
  moveJob: (id: number, dir: "up" | "down") =>
    postJson(`/api/jobs/${id}/move`, { dir }).then((r) => j<OkResponse>(r)),

  assets: (query: AssetQuery = {}) =>
    get(`/api/assets?${buildQuery(query as Record<string, unknown>)}`).then((r) => j<AssetPage>(r)),
  asset: (id: number) => get(`/api/assets/${id}`).then((r) => j<Asset>(r)),
  deleteAsset: (id: number) => del(`/api/assets/${id}`).then((r) => j<OkResponse>(r)),
  // Deletes are soft: these move rows to the trash, purge is what unlinks files.
  trash: (limit = 120, offset = 0) =>
    get(`/api/assets/trash?limit=${limit}&offset=${offset}`).then((r) => j<AssetPage>(r)),
  restoreAssets: (ids: number[]) =>
    postJson("/api/assets/restore", { ids }).then((r) => j<OkResponse & { restored: number }>(r)),
  purgeTrash: (ids: number[] = []) =>
    postJson("/api/assets/trash/purge", { ids }).then((r) => j<OkResponse & { purged: number }>(r)),
  bulkDelete: (ids: number[]) =>
    postJson("/api/assets/bulk-delete", { ids }).then((r) => j<OkResponse & { deleted: number }>(r)),
  favorite: (id: number, favorite: boolean) =>
    postJson(`/api/assets/${id}/favorite`, { favorite }).then((r) => j<OkResponse>(r)),
  rate: (id: number, rating: number) =>
    postJson(`/api/assets/${id}/rating`, { rating }).then((r) => j<OkResponse>(r)),
  grade: (id: number, grade: HumanGradeInput) =>
    postJson(`/api/assets/${id}/grade`, grade).then((r) =>
      j<OkResponse & { rating: number; grade: HumanGrade }>(r),
    ),
  tag: (id: number, tags: string[]) =>
    postJson(`/api/assets/${id}/tags`, { tags }).then((r) => j<OkResponse & { tags: string[] }>(r)),
  exportZip: async (ids: number[]) => {
    const r = await postJson("/api/assets/export", { ids });
    if (!r.ok) throw new Error(await errorDetail(r));
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "gen-export.zip";
    a.click();
    URL.revokeObjectURL(a.href);
    ids.forEach(markUsed);
  },

  /** Which job each lane will actually run next, and why it is not the head.
   *  The Queue page lists jobs in queue order — priority then age — because that
   *  is the order the user controls. The local lane may run a LATER job first
   *  when it needs the model already loaded, and silently reordering a list that
   *  displays an order is how that optimisation turns into a bug report. */
  nextPicks: () => get("/api/jobs/next").then((r) => j<NextPicks>(r)),

  history: (limit = 100) => get(`/api/history?limit=${limit}`).then((r) => j<PromptHistoryItem[]>(r)),

  userPresets: (type?: string) =>
    get(`/api/presets/user${type ? `?type=${type}` : ""}`).then((r) => j<UserPreset[]>(r)),
  createPreset: (body: { type: string; name: string; payload: Record<string, any>; scope?: string }) =>
    postJson("/api/presets/user", body).then((r) => j<UserPreset>(r)),
  deletePreset: (id: number) => del(`/api/presets/user/${id}`).then((r) => j<OkResponse>(r)),

  getSettings: () => get("/api/settings").then((r) => j<AppSettings>(r)),
  saveSettings: (body: Partial<AppSettings> & { remote_gpu_shared_secret?: string }) =>
    postJson("/api/settings", body).then((r) => j<AppSettings>(r)),
  readMetadata: (image: File) => {
    const body = new FormData();
    body.append("image", image);
    return safeFetch("/api/metadata/read", {
      method: "POST",
      body,
      headers: authHeaders(),
    }).then((r) => j<ImportedMetadata>(r));
  },

  loras: (modelVariant?: string) =>
    get(`/api/loras${modelVariant ? `?model_variant=${encodeURIComponent(modelVariant)}` : ""}`).then((r) =>
      j<LoraCatalog>(r),
    ),
  collections: () => get("/api/collections").then((r) => j<CollectionInfo[]>(r)),
  createCollection: (name: string) =>
    postJson("/api/collections", { name }).then((r) => j<CollectionInfo>(r)),
  renameCollection: (id: number, name: string) =>
    postJson(`/api/collections/${id}/rename`, { name }).then((r) => j<OkResponse>(r)),
  deleteCollection: (id: number) => del(`/api/collections/${id}`).then((r) => j<OkResponse>(r)),
  addToCollection: (id: number, asset_ids: number[]) =>
    postJson(`/api/collections/${id}/add`, { asset_ids }).then((r) => j<OkResponse & { added: number }>(r)),
  removeFromCollection: (id: number, asset_ids: number[]) =>
    postJson(`/api/collections/${id}/remove`, { asset_ids }).then((r) =>
      j<OkResponse & { removed: number }>(r),
    ),
  wildcards: () => get("/api/wildcards").then((r) => j<WildcardInfo[]>(r)),
  wildcard: (name: string) =>
    get(`/api/wildcards/${name}`).then((r) => j<{ name: string; items: string[] }>(r)),
  saveWildcard: (name: string, items: string[]) =>
    postJson(`/api/wildcards/${name}`, { items }).then((r) => j<OkResponse>(r)),
  deleteWildcard: (name: string) => del(`/api/wildcards/${name}`).then((r) => j<OkResponse>(r)),

  grid: (groupId: string) => get(`/api/grids/${groupId}`).then((r) => j<GridResult>(r)),
  generateGrid: (kind: string, payload: Record<string, any>, grid: GridConfig) =>
    postJson("/api/generate/grid", { kind, payload, x: grid.x, y: grid.y ?? null }).then((r) =>
      j<{ group_id: string; job_ids: number[] }>(r),
    ),

  // Generation: multipart with a JSON `payload` field + optional named image files.
  generate: (
    endpoint: string,
    payload: Record<string, any>,
    files?: Record<string, File | null | undefined>,
  ) => {
    const fd = new FormData();
    fd.append("payload", JSON.stringify(payload));
    Object.entries(files || {}).forEach(([name, f]) => {
      if (f) fd.append(name, f);
    });
    return safeFetch(endpoint, { method: "POST", body: fd, headers: authHeaders() }).then((r) =>
      j<{ job_id: number; job_ids?: number[]; group_id?: string }>(r),
    );
  },

  tool: (
    kind: "upscale" | "face-restore" | "interpolate" | "detail" | "extend-video",
    body: Record<string, any>,
  ) => postJson(`/api/tools/${kind}`, body).then((r) => j<{ job_id: number }>(r)),
};

/**
 * The live job socket. Carries two kinds of message:
 *
 *  - JSON events (job state, progress, model-load stages) -> `onEvent`
 *  - binary frames (previews) -> `onFrame`
 *
 * `binaryType = "arraybuffer"` so a binary message arrives as an ArrayBuffer
 * whose header can be read synchronously, rather than as a Blob that would need
 * an async read before we even know what it is.
 */
/**
 * Record that an asset was actually used — exported, downloaded, remixed, or fed
 * back in as an input.
 *
 * Deliberately fire-and-forget, and deliberately outside `api`: it is a usage
 * signal, not a transaction. Awaiting it would put a network round trip in front
 * of a download, and letting it throw would turn a bookkeeping failure into a
 * failed export. `favorite` records what you said you liked; this records what
 * you actually did, and the two disagree often enough to be worth both.
 */
export function markUsed(id: number): void {
  void postJson(`/api/assets/${id}/used`, {}).catch(() => {
    /* a signal that does not arrive costs a ranking nudge, nothing more */
  });
}

export function jobSocket(onEvent: (e: any) => void, onFrame?: (frame: BinaryFrame) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const tok = localStorage.getItem("api_token");
  syncMediaToken(tok || "");
  const ws = new WebSocket(`${proto}://${location.host}/api/jobs/ws`);
  ws.binaryType = "arraybuffer";
  ws.onmessage = (m) => {
    if (m.data instanceof ArrayBuffer) {
      const frame = decodeFrame(m.data);
      // A frame type this build does not know is skipped, not an error: the
      // server may be newer than the page the browser has cached.
      if (frame && onFrame) onFrame(frame);
      return;
    }
    try {
      onEvent(JSON.parse(m.data));
    } catch {
      /* ignore */
    }
  };
  return ws;
}

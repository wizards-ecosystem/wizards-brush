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
  JobCreate,
  JobKindInfo,
  JobSubmission,
  JobWait,
  RecipeRun,
  RecipeWrite,
  SavedRecipe,
  SavedRecipeSummary,
  LoraCatalog,
  NextPicks,
  OkResponse,
  PlanRequest,
  PreparedPlan,
  PresetDefinition,
  PresetRead,
  PresetSummary,
  Presets,
  PromptHistoryItem,
  RemoteSession,
  ResourceView,
  RoutingPolicy,
  Stats,
  SystemStatus,
  TargetsView,
  UserPreset,
  VariantCapabilities,
  VariantItem,
  VariantPreview,
  VariantRecipe,
  VariantRecipeSpec,
  VariantSetDetail,
  VariantSetSummary,
  WildcardInfo,
} from "./types";
import { decodeFrame } from "../lib/wsframe";
import type { BinaryFrame } from "../lib/wsframe";

// v0.1.0 stored the browser token in localStorage. Keep it in memory only long
// enough to exchange it for backend-issued HttpOnly cookies, then remove the
// old persistent copy. Fresh sessions never write a reusable token here.
let transitionalApiToken = "";
if (typeof localStorage !== "undefined") {
  transitionalApiToken = localStorage.getItem("api_token") || "";
  localStorage.removeItem("api_token");
}
if (typeof document !== "undefined") {
  const secure = typeof location !== "undefined" && location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `wb_media_token=; Path=/files; SameSite=Strict; Max-Age=0${secure}`;
  document.cookie = `wb_ws_token=; Path=/api/jobs/ws; SameSite=Strict; Max-Age=0${secure}`;
}

/** Extract FastAPI's {detail} from an error response, falling back to raw text/status. */
/** A refused request. The message is the server's `detail`, exactly as before;
 *  the structured fields (code, field, retryable, state, suggestions) ride
 *  along for callers that act on them. Named "Error" so `${error}` reads as it
 *  always has in toasts. */
export class ApiError extends Error {
  status: number;
  code?: string;
  field?: string;
  retryable?: boolean;
  state?: Record<string, unknown>;
  suggestions?: string[];

  constructor(status: number, body: Record<string, unknown> | null, detail: string) {
    super(detail);
    this.name = "Error";
    this.status = status;
    if (body) {
      if (typeof body.code === "string") this.code = body.code;
      if (typeof body.field === "string") this.field = body.field;
      if (typeof body.retryable === "boolean") this.retryable = body.retryable;
      if (body.state && typeof body.state === "object") this.state = body.state as Record<string, unknown>;
      if (Array.isArray(body.suggestions)) this.suggestions = body.suggestions as string[];
    }
  }

  get detail(): string {
    return this.message;
  }
}

async function errorParts(r: Response): Promise<{ body: Record<string, unknown> | null; detail: string }> {
  const text = await r.text();
  let body: Record<string, unknown> | null = null;
  let detail = text;
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      body = parsed as Record<string, unknown>;
      if (typeof body.detail === "string" && body.detail) detail = body.detail;
    }
  } catch {
    /* keep raw text */
  }
  return { body, detail: detail || `${r.status}` };
}

async function errorDetail(r: Response): Promise<string> {
  return (await errorParts(r)).detail;
}

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const { body, detail } = await errorParts(r);
    throw new ApiError(r.status, body, detail);
  }
  return r.json();
}

/** Preset ids carry one "/" ("custom/my-look"); each part is encoded on its own. */
function presetPath(id: string): string {
  return id.split("/").map(encodeURIComponent).join("/");
}

/** fetch wrapper that turns a network-level reject (backend down/offline) into a
 * friendly message instead of a raw "TypeError: Failed to fetch" in toasts. */
async function safeFetch(url: string, init?: RequestInit): Promise<Response> {
  let failure: unknown;
  try {
    return await fetch(url, init);
  } catch (error) {
    failure = error;
  }
  // A caller that aborted a stale request must see the abort, not an outage.
  if (isAbort(failure)) throw failure;
  throw new Error("Can't reach the backend — is it running?");
}

/** True for a fetch the caller canceled with an AbortController. */
export function isAbort(error: unknown): boolean {
  return (error as { name?: unknown } | null)?.name === "AbortError";
}

export function authHeaders(): Record<string, string> {
  return transitionalApiToken ? { "X-API-Token": transitionalApiToken } : {};
}

async function exchangeBrowserToken(token: string): Promise<void> {
  const response = await safeFetch("/api/auth/session", {
    method: "POST",
    headers: { "X-API-Token": token },
  });
  await j<OkResponse & { authenticated: boolean }>(response);
  transitionalApiToken = "";
  localStorage.removeItem("api_token");
}

/** Upgrade the pre-v0.1.1 browser token without breaking the first API load. */
export async function prepareBrowserSession(): Promise<void> {
  if (transitionalApiToken) await exchangeBrowserToken(transitionalApiToken);
}

/** Store browser authentication only in backend-issued HttpOnly cookies. */
export async function saveBrowserSession(token: string): Promise<void> {
  if (token) {
    await exchangeBrowserToken(token);
    return;
  }
  const response = await safeFetch("/api/auth/session", {
    method: "DELETE",
    headers: authHeaders(),
  });
  await j<OkResponse & { authenticated: boolean }>(response);
  transitionalApiToken = "";
  localStorage.removeItem("api_token");
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
  remoteSession: () => get("/api/remote-gpu/session").then((r) => j<RemoteSession>(r)),
  startRemoteSession: () => postJson("/api/remote-gpu/session", {}).then((r) => j<RemoteSession>(r)),
  stopRemoteSession: () => del("/api/remote-gpu/session").then((r) => j<RemoteSession>(r)),
  models: () => get("/api/models").then((r) => j<GeneratorSpec[]>(r)),
  presets: () => get("/api/presets").then((r) => j<Presets>(r)),
  stats: () => get("/api/stats").then((r) => j<Stats>(r)),

  jobs: (limit = 100) => get(`/api/jobs?limit=${limit}`).then((r) => j<Job[]>(r)),
  job: (id: number, signal?: AbortSignal) =>
    safeFetch(`/api/jobs/${id}`, { headers: authHeaders(), signal }).then((r) => j<Job>(r)),
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
    kind: "upscale" | "face-restore" | "interpolate" | "detail" | "extend-video" | "matte" | "region-mask",
    body: Record<string, any>,
  ) => postJson(`/api/tools/${kind}`, body).then((r) => j<{ job_id: number }>(r)),

  // ---- the unified job API (docs/api.md) ------------------------------------
  jobKinds: () => get("/api/jobs/kinds").then((r) => j<JobKindInfo[]>(r)),
  createJob: (body: JobCreate) => postJson("/api/jobs", body).then((r) => j<JobSubmission>(r)),
  /** Long-poll: resolves when the job settles or after `timeout` seconds. */
  waitJob: (id: number, timeout = 30) =>
    get(`/api/jobs/${id}/wait?timeout=${timeout}`).then((r) => j<JobWait>(r)),
  /** Bring a local image into the library; transparency is kept. */
  importAsset: (file: File, options: { role?: "source" | "mask"; tags?: string[] } = {}) => {
    const body = new FormData();
    body.append("file", file);
    if (options.role) body.append("role", options.role);
    if (options.tags?.length) body.append("tags", options.tags.join(","));
    return safeFetch("/api/assets/import", { method: "POST", body, headers: authHeaders() }).then((r) =>
      j<Asset>(r),
    );
  },

  // ---- saved generation recipes (distinct from variant-set recipes) ----------
  recipes: (limit = 100) => get(`/api/recipes?limit=${limit}`).then((r) => j<SavedRecipeSummary[]>(r)),
  recipe: (key: string) => get(`/api/recipes/${encodeURIComponent(key)}`).then((r) => j<SavedRecipe>(r)),
  saveRecipe: (body: RecipeWrite) => postJson("/api/recipes", body).then((r) => j<SavedRecipe>(r)),

  // ---- studio presets, plans, resources and targets ----
  studioPresets: (includeArchived = false) =>
    get(`/api/studio/presets${includeArchived ? "?include_archived=true" : ""}`).then((r) =>
      j<PresetSummary[]>(r),
    ),
  studioPreset: (id: string, revision?: number) =>
    get(`/api/studio/presets/${presetPath(id)}${revision ? `?revision=${revision}` : ""}`).then((r) =>
      j<PresetRead>(r),
    ),
  createStudioPreset: (definition: PresetDefinition) =>
    postJson("/api/studio/presets", definition).then((r) => j<PresetRead>(r)),
  updateStudioPreset: (
    id: string,
    definition: PresetDefinition,
    baseRevision: number,
    onConflict: "error" | "copy" = "error",
  ) =>
    safeFetch(`/api/studio/presets/${presetPath(id)}`, {
      method: "PUT",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ definition, base_revision: baseRevision, on_conflict: onConflict }),
    }).then((r) => j<PresetRead>(r)),
  cloneStudioPreset: (id: string, body: { revision?: number; name?: string } = {}) =>
    postJson(`/api/studio/presets/${presetPath(id)}/clone`, body).then((r) => j<PresetRead>(r)),
  archiveStudioPreset: (id: string, archived = true) =>
    postJson(`/api/studio/presets/${presetPath(id)}/${archived ? "archive" : "unarchive"}`, {}).then((r) =>
      j<PresetRead>(r),
    ),
  /** Resolve a preset request into its plan. Nothing is queued. Pass a signal
   *  so a newer draft can cancel an older preview. */
  preparePlan: (body: PlanRequest, signal?: AbortSignal) =>
    safeFetch("/api/jobs", {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, dry_run: true }),
      signal,
    }).then((r) => j<PreparedPlan>(r)),
  /** Submit exactly the previewed plan; a plan that changed since is refused. */
  submitPlan: (body: PlanRequest, expectedPlanHash: string, requestId: string) =>
    postJson("/api/jobs", { ...body, expected_plan_hash: expectedPlanHash, request_id: requestId }).then(
      (r) => j<{ job_id: number; job_ids: number[]; duplicate: boolean }>(r),
    ),
  replayJob: (jobId: number, mode: "exact" | "latest" = "exact") =>
    postJson(`/api/jobs/${jobId}/replay`, { mode }).then((r) => j<{ job_id: number; job_ids: number[] }>(r)),
  resources: (kind?: "model" | "adapter") =>
    get(`/api/resources${kind ? `?kind=${kind}` : ""}`).then((r) => j<ResourceView[]>(r)),
  targets: () => get("/api/targets").then((r) => j<TargetsView>(r)),
  setRoutingPolicy: (changes: Partial<RoutingPolicy>) =>
    safeFetch("/api/targets/policy", {
      method: "PUT",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }).then((r) => j<RoutingPolicy>(r)),
  updateRecipe: (key: string, body: RecipeWrite) =>
    safeFetch(`/api/recipes/${encodeURIComponent(key)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    }).then((r) => j<SavedRecipe>(r)),
  deleteRecipe: (key: string) => del(`/api/recipes/${encodeURIComponent(key)}`).then((r) => j<OkResponse>(r)),
  runRecipe: (
    key: string,
    body: { inputs?: Record<string, unknown>; dry_run?: boolean; request_id?: string },
  ) => postJson(`/api/recipes/${encodeURIComponent(key)}/run`, body).then((r) => j<RecipeRun>(r)),

  // ---- Variant Sets ----------------------------------------------------------
  variantCapabilities: () => get("/api/variant-sets/capabilities").then((r) => j<VariantCapabilities>(r)),
  variantRecipes: () => get("/api/variant-recipes").then((r) => j<VariantRecipe[]>(r)),
  saveVariantRecipe: (body: { name: string; description?: string; recipe: VariantRecipeSpec }, id?: number) =>
    (id == null
      ? postJson("/api/variant-recipes", body)
      : safeFetch(`/api/variant-recipes/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: JSON.stringify(body),
        })
    ).then((r) => j<VariantRecipe>(r)),
  cloneVariantRecipe: (id: number) =>
    postJson(`/api/variant-recipes/${id}/clone`, {}).then((r) => j<VariantRecipe>(r)),
  deleteVariantRecipe: (id: number) => del(`/api/variant-recipes/${id}`).then((r) => j<OkResponse>(r)),
  variantRecipe: (id: number) => get(`/api/variant-recipes/${id}`).then((r) => j<VariantRecipe>(r)),
  previewVariantSet: (recipe: VariantRecipeSpec, limit = 60) =>
    postJson("/api/variant-sets/preview", { recipe, limit }).then((r) => j<VariantPreview>(r)),
  createVariantSet: (body: {
    name: string;
    recipe: VariantRecipeSpec;
    request_id: string;
    collection?: { mode: "none" | "new" | "existing"; id?: number };
  }) => postJson("/api/variant-sets", body).then((r) => j<VariantSetSummary>(r)),
  variantSets: (limit = 100) =>
    get(`/api/variant-sets?limit=${limit}`).then((r) => j<VariantSetSummary[]>(r)),
  variantSet: (id: number) => get(`/api/variant-sets/${id}`).then((r) => j<VariantSetDetail>(r)),
  retryVariantSet: (id: number, includeCanceled = false) =>
    postJson(`/api/variant-sets/${id}/retry`, { include_canceled: includeCanceled }).then((r) =>
      j<{ retried: number; submitted: number }>(r),
    ),
  rerunVariantItem: (setId: number, itemId: number, options: { reseed?: boolean; cascade?: boolean } = {}) =>
    postJson(`/api/variant-sets/${setId}/items/${itemId}/rerun`, options).then((r) => j<VariantItem>(r)),
  cancelVariantSet: (id: number) =>
    postJson(`/api/variant-sets/${id}/cancel`, {}).then((r) => j<{ canceled: number }>(r)),
  deleteVariantSet: (id: number) => del(`/api/variant-sets/${id}`).then((r) => j<OkResponse>(r)),
  exportVariantSet: async (id: number, includeIntermediate = false) => {
    const r = await postJson(`/api/variant-sets/${id}/export`, { include_intermediate: includeIntermediate });
    if (!r.ok) throw new Error(await errorDetail(r));
    const name = /filename="?([^";]+)"?/.exec(r.headers.get("content-disposition") || "")?.[1];
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name || `variant-set-${id}.zip`;
    a.click();
    URL.revokeObjectURL(a.href);
  },
  uploadVariantMask: (mask: File) => {
    const body = new FormData();
    body.append("mask", mask);
    return safeFetch("/api/variant-sets/masks", { method: "POST", body, headers: authHeaders() }).then((r) =>
      j<{ asset_id: number }>(r),
    );
  },
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

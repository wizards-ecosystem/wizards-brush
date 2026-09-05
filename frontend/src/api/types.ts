export interface ShowIf {
  field: string;
  equals: any | any[]; // array = "any of these values"
}

export interface Control {
  name: string;
  label: string;
  type: "textarea" | "slider" | "number" | "select" | "toggle" | "segmented" | "aspect" | "lora";
  default: any;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
  /** value -> display text. The value is what gets submitted and stored; the
   *  label is only what the picker shows, so it can follow configuration. */
  option_labels?: Record<string, string>;
  /** Per-option explanation shown for the current choice. Unlike the compact
   * label this may describe cost, CFG behavior, access, or quality tradeoffs. */
  option_hints?: Record<string, string>;
  preset_group?: string;
  hint_key?: string;
  /** Select help text from the current value of another control, with
   *  hint_key as the fallback. Guidance uses this when the model changes. */
  hint_keys_by?: HintKeysBy;
  styles?: boolean; // attach the style-chip picker (prompt field)
  section?: string; // "Advanced" → collapsible group
  /** "basic" → part of the Simple form. Anything else is only rendered once the
   *  user switches the panel to Full; its default still ships with the request. */
  tier?: string;
  show_if?: ShowIf; // conditional visibility
  defaults_by?: DefaultsBy; // default depends on another control's value
  /** Relative cost of changing this value. 10 = a model reload, 0 = normal,
   *  negative = cheaper than normal. Used to order grid axes so the expensive
   *  one is the outer loop. */
  change_weight?: number;
  /** Only offered when a backend advertises this capability. */
  feature_flag?: string;
  /** This control may be used as a scalar X/Y study axis. Declared by the
   * backend so the editor can never offer a value the route will reject. */
  sweepable?: boolean;
  /** Another control temporarily fixes this control to a truthful effective
   * value. The underlying choice is retained and returns when the constraint
   * turns off. */
  constrained_by?: {
    field: string;
    equals: any | any[];
    value: any;
    reason: string;
  };
  /** Presentation and legal range selected by another control. This is the
   * narrowing state between hiding a meaningless control and disabling a
   * temporarily seized one. The server remains the authority on replay. */
  overrides_by?: {
    field: string;
    map: Record<
      string,
      {
        label?: string;
        min?: number;
        max?: number;
        step?: number;
        options?: string[];
        option_labels?: Record<string, string>;
        option_hints?: Record<string, string>;
      }
    >;
  };
}

/** A control whose sensible default changes with another field — e.g. guidance,
 *  which wants ~1.0 on the distilled Turbo model and ~4.0 on the CFG one. */
export interface DefaultsBy {
  field: string;
  map: Record<string, any>;
}

export interface HintKeysBy {
  field: string;
  map: Record<string, string>;
}

/** Named image-input slot for multi-image generators (slot 1 is always "image"). */
export interface ImageSlot {
  name: string;
  label: string;
  required?: boolean;
  show_if?: ShowIf;
}

export interface GeneratorSpec {
  id: string;
  kind: string;
  title: string;
  subtitle: string;
  endpoint: string;
  output: "image" | "video";
  group?: string;
  device?: string;
  needs_image: boolean;
  needs_mask?: boolean;
  needs_remote: boolean;
  /** A live, actionable reason this process cannot currently start. Omitted
   * while capability is usable or still being detected. */
  unavailable_reason?: string;
  image_inputs?: ImageSlot[];
  controls: Control[];
  /** Other names this generator can be found by: what it used to be called, and
   *  what other tools call it. Search matches these as well as the title. */
  aliases?: string[];
}

export interface Job {
  id: number;
  kind: string;
  status: "queued" | "running" | "done" | "error" | "canceled";
  progress: number;
  message: string;
  params: Record<string, any>;
  result: { asset_ids?: number[] };
  error: string;
  /** One actionable sentence about a failure. The traceback stays in `error`
   *  behind a disclosure — lead with what to do, not with a Python stack. */
  tip?: string;
  priority?: number;
  /** Which local model this job needs. null = it never touches the pipeline. */
  model_key?: string | null;
  group_id?: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** What a load stage looks like on the wire. Not job progress: a model load has
 *  no meaningful fraction, and must not move the progress bar. */
export interface ModelLoadEvent {
  type: "model_load";
  id: number;
  kind: string;
  stage: "swapping" | "resolving" | "downloading" | "quantizing" | "loading" | "placing" | "ready";
  detail: string;
  /** true = a heartbeat repeating the current stage, proving the load is alive
   *  rather than wedged. The message does not change; the arrival does. */
  beat: boolean;
  hint?: string;
}

export interface Estimate {
  seconds: number | null;
  human: string;
  confidence: "measured" | "estimated" | "estimating" | "unknown";
  includes_swap?: boolean;
}

export interface LaneEta {
  seconds: number | null;
  human: string;
  confidence: Estimate["confidence"];
  jobs: Record<string, Estimate>;
}

/** Which job a lane will actually run next, and why it is not the head. */
export interface NextPick {
  job_id: number;
  reason: string | null;
}

export interface LoraItem {
  name: string;
  label: string;
  filename: string;
  path: string;
  size_bytes: number;
  /** Architecture read from the file's own header. "unknown" is a real answer
   *  and must not be treated as a mismatch. */
  family: string;
  arch: string;
  rank: number | null;
  compatible: boolean;
  is_adapter?: boolean;
  adapter_type?: string;
  selectable?: boolean;
  unavailable_reason?: string;
  /** .pt and .bin execute code from whoever built them. Accepted deliberately;
   *  surfaced so the choice is visible. */
  pickled: boolean;
}

export interface BackendHealth {
  connected: boolean;
  reason: string;
  device: string;
  vram_gb: number | null;
  build: string;
  build_stale: boolean;
  queue_depth: number;
  disk_free_gb: number | null;
  features: string[];
  models_loaded: string[];
}

export interface BackendModel {
  id: string;
  label: string;
  kind: string;
  ready: boolean;
  /** `unknown` is deliberately distinct from absent: a Remote GPU worker can
   *  report what it is configured to run without exposing its download cache. */
  status?: "ready" | "partial" | "absent" | "unknown";
  size_gb: number | null;
  family: string;
}

export interface BackendInfo {
  id: string;
  label: string;
  kind: "local" | "remote";
  health: BackendHealth;
  models: BackendModel[];
}

export interface HardwareProfile {
  device: string;
  vram_gb: number | null;
  profile: string;
  label: string;
  source: "detected" | "override";
  quant: string;
  offload: boolean;
  max_side: number;
  tier_mp: Record<string, number>;
  max_batch: number;
  available: { name: string; label: string }[];
}

export interface BackendReport {
  backends: BackendInfo[];
  hardware: HardwareProfile;
  credentials: { hf_token_set: boolean };
}

export interface Asset {
  id: number;
  kind: "image" | "video";
  filename: string;
  url: string;
  thumb_url: string | null;
  width: number | null;
  height: number | null;
  job_id: number | null;
  generator: string;
  meta: Record<string, any>;
  created_at: string;
  favorite: boolean;
  rating: number;
  /** A completed rubric review; absent when served by an older backend. */
  grade?: HumanGrade;
  tags: string[];
  caption?: string;
  /** Behaviour, as distinct from the `favorite` intent: incremented when an
   *  asset is exported, downloaded, remixed or used as an input. */
  used_count?: number;
  last_used_at?: string | null;
  /** How many live assets share these exact bytes, including this one.
   *  1 = unique. Supplied per page, not computed per tile. */
  copies?: number;
  /** The file is gone from disk. A state to show, not an error to throw. */
  is_missing?: boolean;
}

export interface HumanGrade {
  version?: number;
  prompt_fidelity?: number | null;
  visual_quality?: number | null;
  /** Present on v1 reviews saved before the two-question rubric. */
  anatomy_detail?: number | null;
  lora_effect?: number | null;
  overall?: number;
  notes?: string;
  reviewed_at?: string;
}

export interface HumanGradeInput {
  prompt_fidelity: number;
  visual_quality: number;
  notes?: string;
}

export interface AssetPage {
  items: Asset[];
  total: number;
  limit: number;
  offset: number;
  /** Present when the page was ranked by meaning rather than keyword. */
  ranked_by?: "meaning";
}

export interface SystemStatus {
  version?: string;
  local_gpu: {
    available: boolean;
    name?: string;
    memory_total_mb?: number;
    memory_used_mb?: number;
    utilization_pct?: number;
    temperature_c?: number;
  };
  remote_gpu: {
    connected: boolean;
    url: string;
    reason?: string;
    gpu?: string;
    features?: string[];
    // `ok` now reflects the thing that actually runs jobs, not merely that HTTP
    // answered — a dead worker used to report ok:true while every submitted job
    // sat forever.
    worker_alive?: boolean;
    queue_depth?: number;
    disk_free_gb?: number;
    // True when the remote worker is running an older remote_gpu.py than this repo.
    // Undefined for workers predating the build marker — unknown, not stale.
    build_stale?: boolean;
    speed_loaded?: { image?: boolean; edit?: boolean; video?: boolean };
    image_model?: string;
    video_model?: string;
  };
  models: { local_image: string; a100_image: string; video: string };
}

export interface StyleItem {
  id: string;
  label?: string;
  text: string;
}
export interface StyleProfile {
  category: string;
  items: StyleItem[];
}

export interface UserPreset {
  id: number;
  type: string;
  name: string;
  payload: Record<string, any>;
  scope: string;
}

export interface Presets {
  negative: { id: string; label: string; text: string }[];
  prompt: { id: string; category?: string; label: string; text: string }[];
  settings?: {
    id: string;
    label: string;
    description: string;
    requires?: string[];
    values: Record<string, any>;
  }[];
  style_profiles: StyleProfile[];
  aspects: string[];
  quality_tiers: string[];
  hints: Record<string, string>;
  user: UserPreset[];
}

export interface PromptHistoryItem {
  id: number;
  kind: string;
  prompt: string;
  negative: string;
  params: Record<string, any>;
  favorite: boolean;
  created_at: string;
}

export interface Stats {
  images: number;
  videos: number;
  favorites: number;
  today: number;
  disk_bytes: number;
}

export interface AssetQuery {
  limit?: number;
  offset?: number;
  kind?: string;
  generator?: string;
  favorite?: boolean;
  min_rating?: number;
  q?: string;
  since_days?: number;
  sort?: string;
  collection_id?: number;
}

export interface OkResponse {
  ok: boolean;
  [k: string]: any;
}

export interface AppSettings {
  remote_gpu_base_url: string;
  remote_gpu_shared_secret_set?: boolean; // matches the /api/settings response key
  hf_token_hint?: string;
  embed_metadata?: boolean;
  embed_provenance?: boolean;
  configuration_warning?: string;
  models?: {
    label: string;
    id: string;
    lane: "local" | "remote_gpu";
    kind: "image" | "image-edit" | "video";
  }[];
  local?: { quant: string; offload: boolean };
  remote_gpu?: { connected: boolean; reason?: string; url?: string };
  storage?: {
    output: string;
    huggingface: string;
    weights: string;
    loras: string;
    runtime: string;
  };
  [k: string]: any;
}

export interface ImportedMetadata {
  scheme: "wizards-brush" | "a1111" | "none";
  params: Record<string, any>;
  unresolved: string[];
  app_version?: string;
}

// ---- wildcards -------------------------------------------------------------
export interface WildcardInfo {
  name: string;
  count: number;
}

// ---- X/Y grids --------------------------------------------------------------
export interface GridAxis {
  param: string;
  values: (string | number | boolean)[];
  sr_search?: string; // for param === "prompt_sr": the substring to replace
}
export interface GridConfig {
  x: GridAxis;
  y?: GridAxis;
}
export interface GridCell {
  x: string;
  y: string;
  job_id: number;
  status: Job["status"];
  asset?: Asset | null;
}
export interface GridResult {
  group_id: string;
  kind: string;
  x_param: string;
  x_values: string[];
  y_param?: string;
  y_values?: string[];
  cells: GridCell[];
}

/** One adapter in LORA_DIR, as listed by GET /api/loras. */
export interface LoraInfo {
  name: string;
  label: string;
  filename: string;
  path: string;
  size_bytes: number;
  /** Architecture read from the file's own header. "unknown" means we could not
   *  identify it — which is NOT a mismatch and must not be warned about. */
  family: string;
  arch: string;
  rank: number | null;
  /** Whether this suits the model currently selected. True when either side is
   *  unknown: absence of evidence is not evidence of a mismatch. */
  compatible: boolean;
  /** False for a readable full checkpoint/VAE/text encoder placed in LORA_DIR. */
  selectable?: boolean;
  unavailable_reason?: string;
  is_adapter?: boolean;
  adapter_type?: string;
  /** .pt and .bin are pickle formats that execute code from whoever built them.
   *  Accepted deliberately; surfaced so the choice is visible. */
  pickled: boolean;
  /** Author- or test-derived starting strength. Undefined means use 1.0. */
  recommended_weight?: number | null;
  /** Short provenance note from the optional sidecar. */
  why?: string;
}

/** The catalogue plus the limits the backend enforces, so the UI does not
 *  duplicate the caps. */
export interface LoraCatalog {
  items: LoraInfo[];
  max_active: number;
  weight_min: number;
  weight_max: number;
  /** The model compatibility was judged against, if any. */
  model?: string | null;
}

/** A named grouping of assets. A view over them, never a container that owns
 *  them — deleting a collection never deletes an asset. */
export interface CollectionInfo {
  id: number;
  name: string;
  count: number;
  created_at: string;
}

/** Response of GET /api/jobs/next — see `api.nextPicks`. */
export interface NextPicks {
  lanes: Record<string, { job_id: number; reason: string | null }>;
}

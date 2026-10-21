import { api } from "../api/client";
import type { GeneratorSpec, JobCreate, JobWait } from "../api/types";

/**
 * Queue one job through the unified job API and resolve once it settles.
 *
 * The UI's own small helpers (an automatic mask, say) use the same path a
 * script would — POST /api/jobs, then long-poll GET /api/jobs/{id}/wait — so
 * the documented API is exercised by the app itself, not only by examples.
 * `isCancelled` lets a component stop waiting when it unmounts; the job
 * itself keeps running and stays in the Queue.
 */
export async function runJob(body: JobCreate, isCancelled: () => boolean = () => false): Promise<JobWait> {
  const { job_id } = await api.createJob(body);
  for (;;) {
    const answer = await api.waitJob(job_id, 30);
    if (answer.settled || isCancelled()) return answer;
  }
}

/** The first line of a failed job's error, for a toast. */
export function jobFailure(answer: JobWait): string {
  const first = (answer.job.error || answer.job.message || answer.job.status).split("\n")[0];
  return first || "the job did not finish";
}

/** A request as a runnable curl command (docs/api.md). The token header is
 *  harmless when the app sets none, so one command works either way. */
export function apiRequestText(path: string, body: unknown, origin: string, notes: string[] = []): string {
  const json = JSON.stringify(body, null, 2).replace(/'/g, "'\\''");
  return [
    ...notes.map((note) => `# ${note}`),
    `curl -X POST ${origin}${path} \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H "X-API-Token: \${API_TOKEN:-}" \\`,
    `  -d '${json}'`,
  ].join("\n");
}

/**
 * A generator form as a `POST /api/jobs` request: only the kind's own
 * settings, which is all the strict API accepts, with the prompt as it will
 * actually be sent (styles applied). Inputs are asset ids there, not files, so
 * a kind that takes images comes with a note on how to get ids.
 */
export function jobRequestFor(
  spec: GeneratorSpec,
  values: Record<string, unknown>,
  prompt: string,
): { body: JobCreate; notes: string[] } {
  const names = new Set(spec.controls.map((control) => control.name));
  const params: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(values)) {
    if (names.has(name) && value !== undefined) params[name] = value;
  }
  if (names.has("prompt")) params.prompt = prompt;
  const body: JobCreate = { kind: spec.kind, params };
  const notes: string[] = [];
  if (spec.needs_image) {
    body.inputs = { images: [], ...(spec.needs_mask ? { mask: null } : {}) };
    notes.push(
      "Put input asset ids in inputs.images" +
        (spec.needs_mask ? " and the mask's id in inputs.mask" : "") +
        "; import local files with POST /api/assets/import" +
        (spec.needs_mask ? " (role=mask for the mask)." : "."),
    );
  }
  notes.push("Then wait for it: GET /api/jobs/{job_id}/wait (see docs/api.md).");
  return { body, notes };
}

/** Image kinds the OpenAI image API can queue. Anything else has no snippet. */
const OPENAI_KINDS = new Set([
  "image_local",
  "image_colab",
  "img2img",
  "image_edit",
  "inpaint",
  "inpaint_remote",
]);

export function openaiMapped(kind: string): boolean {
  return OPENAI_KINDS.has(kind);
}

function quoted(value: string): string {
  return JSON.stringify(value);
}

/**
 * The same form as an MCP `generate` or `edit` call. `wait: true` is in the
 * call, so the job API's "then wait" note is left off. Import notes stay.
 */
export function mcpCallText(
  spec: GeneratorSpec,
  values: Record<string, unknown>,
  prompt: string,
): { text: string; notes: string[] } {
  const { body, notes } = jobRequestFor(spec, values, prompt);
  const params = { ...(body.params ?? {}) };
  const spoken = String(params.prompt ?? prompt);
  delete params.prompt;
  const call = spec.needs_image
    ? {
        tool: "edit",
        arguments: {
          kind: spec.kind,
          instruction: spoken,
          images: ["<asset id>"],
          ...(spec.needs_mask ? { mask: "<mask asset id>" } : {}),
          ...(Object.keys(params).length ? { params } : {}),
          wait: true,
        },
      }
    : {
        tool: "generate",
        arguments: {
          kind: spec.kind,
          prompt: spoken,
          ...(Object.keys(params).length ? { params } : {}),
          wait: true,
        },
      };
  return {
    text: JSON.stringify(call, null, 2),
    notes: notes.filter((note) => !note.startsWith("Then wait")),
  };
}

function openaiLane(kind: string): "local" | "remote" {
  if (kind === "image_colab" || kind === "image_edit" || kind === "inpaint_remote") return "remote";
  return "local";
}

/** A raw id (`local/turbo`, `remote/quality`) so the snippet does not depend on friendly names. */
function openaiModel(kind: string, values: Record<string, unknown>): string {
  const lane = openaiLane(kind);
  const fallback = lane === "remote" ? "quality" : "turbo";
  const variant =
    typeof values.model_variant === "string" && values.model_variant ? values.model_variant : fallback;
  return `${lane}/${variant}`;
}

function openaiSize(spec: GeneratorSpec, values: Record<string, unknown>): string | null {
  if (!spec.controls.some((control) => control.name === "aspect")) return null;
  const aspect = values.aspect;
  if (typeof aspect !== "string" || aspect === "" || aspect === "Match input") return null;
  if (aspect === "Custom") {
    const width = Number(values.width);
    const height = Number(values.height);
    if (!Number.isFinite(width) || !Number.isFinite(height)) return null;
    return `${width}x${height}`;
  }
  return aspect;
}

/**
 * A Python snippet for `images.generate` or `images.edit`. Null when this kind
 * has no OpenAI mapping. `size` is the ratio or WxH this server accepts.
 * `Match input` omits `size`.
 */
export function openaiCallText(
  spec: GeneratorSpec,
  values: Record<string, unknown>,
  prompt: string,
): string | null {
  if (!openaiMapped(spec.kind)) return null;
  const { body } = jobRequestFor(spec, values, prompt);
  const params = body.params ?? {};
  const spoken = String(params.prompt ?? prompt);
  const batch = Number(params.batch);
  const size = openaiSize(spec, values);
  const lines = [
    "import os",
    "",
    "from openai import OpenAI",
    "",
    "client = OpenAI(",
    '    base_url="http://127.0.0.1:8000/api/openai/v1",',
    '    api_key=os.environ["BRUSH_API_TOKEN"],',
    ")",
  ];
  const kwargs = [`    model=${quoted(openaiModel(spec.kind, values))},`, `    prompt=${quoted(spoken)},`];
  if (size) kwargs.push(`    size=${quoted(size)},`);
  if (Number.isFinite(batch) && batch > 1) kwargs.push(`    n=${batch},`);
  if (spec.needs_image) {
    lines.push("# http URLs are refused. Pass a local file.");
    lines.push('source = open("image.png", "rb")');
    if (spec.needs_mask) lines.push('mask = open("mask.png", "rb")');
    lines.push("image = client.images.edit(", "    image=source,");
    if (spec.needs_mask) lines.push("    mask=mask,");
  } else {
    lines.push("image = client.images.generate(");
  }
  lines.push(...kwargs, ")");
  return lines.join("\n");
}

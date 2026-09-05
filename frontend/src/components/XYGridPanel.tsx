import { useMemo, useState } from "react";
import type { Control, GeneratorSpec, GridAxis, GridConfig } from "../api/types";
import { Icon } from "./icons";

const SOFT_WARN = 16;
export const GRID_HARD_CAP = 36;

interface AxisOption {
  key: string;
  label: string;
  control?: Control;
}

function axisOptions(spec: GeneratorSpec): AxisOption[] {
  const opts: AxisOption[] = [
    { key: "prompt_sr", label: "Prompt S/R (search & replace)" },
    { key: "seed", label: "Seed" },
  ];
  spec.controls
    .filter((c) => c.sweepable && c.name !== "seed")
    .forEach((c) => opts.push({ key: c.name, label: c.label, control: c }));
  return opts;
}

function splitCsv(raw: string): string[] {
  const fields: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < raw.length; i += 1) {
    const char = raw[i];
    if (char === '"') {
      if (quoted && raw[i + 1] === '"') {
        field += '"';
        i += 1;
      } else {
        quoted = !quoted;
      }
    } else if (char === "," && !quoted) {
      fields.push(field);
      field = "";
    } else {
      field += char;
    }
  }
  fields.push(field);
  return fields;
}

const NUMBER = "[-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)";
const STEP_RANGE = new RegExp(
  `^\\s*(${NUMBER})\\s*-\\s*(${NUMBER})\\s*\\(\\s*\\+\\s*(${NUMBER})\\s*\\)\\s*$`,
);
const COUNT_RANGE = new RegExp(`^\\s*(${NUMBER})\\s*-\\s*(${NUMBER})\\s*\\[\\s*(\\d+)\\s*\\]\\s*$`);

function expandNumeric(value: string): number[] | null {
  const step = value.match(STEP_RANGE);
  if (step) {
    const start = Number(step[1]);
    const end = Number(step[2]);
    const amount = Number(step[3]);
    if (!(amount > 0) || end < start) return [Number.NaN];
    const result: number[] = [];
    for (let current = start; current <= end + amount * 1e-9; current += amount) {
      result.push(Number(current.toPrecision(12)));
      if (result.length > GRID_HARD_CAP) break;
    }
    return result;
  }
  const count = value.match(COUNT_RANGE);
  if (count) {
    const start = Number(count[1]);
    const end = Number(count[2]);
    const total = Number(count[3]);
    if (total < 2 || end < start || total > GRID_HARD_CAP) return [Number.NaN];
    return Array.from({ length: total }, (_, index) =>
      Number((start + ((end - start) * index) / (total - 1)).toPrecision(12)),
    );
  }
  return null;
}

export function parseValues(raw: string, control?: Control): (string | number | boolean)[] {
  return splitCsv(raw)
    .map((s) => s.trim())
    .filter(Boolean)
    .flatMap((s) => {
      if (control?.type === "toggle") return s.toLowerCase() === "true";
      if (control && (control.type === "slider" || control.type === "number")) {
        return expandNumeric(s) ?? Number(s);
      }
      return s;
    });
}

interface AxisDraft {
  param: string;
  raw: string;
  sr: string;
}

/** The single home for draft → GridAxis conversion (editor, cell count, submit). */
function buildAxis(ax: AxisDraft, spec: GeneratorSpec): GridAxis | null {
  if (!ax.param) return null;
  const control = axisOptions(spec).find((o) => o.key === ax.param)?.control;
  const values = parseValues(ax.raw, control);
  if (!values.length) return null;
  const axis: GridAxis = { param: ax.param, values };
  if (ax.param === "prompt_sr") axis.sr_search = ax.sr.trim();
  return axis;
}

function AxisEditor({
  title,
  spec,
  axis,
  onChange,
}: {
  title: string;
  spec: GeneratorSpec;
  axis: AxisDraft;
  onChange: (a: AxisDraft) => void;
}) {
  const opts = axisOptions(spec);
  const control = opts.find((o) => o.key === axis.param)?.control;
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wide text-white/40 w-3">{title}</span>
        <select
          className="input text-xs flex-1"
          value={axis.param}
          onChange={(e) => onChange({ ...axis, param: e.target.value, raw: "" })}
        >
          <option value="">— none —</option>
          {opts.map((o) => (
            <option key={o.key} value={o.key}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {axis.param === "prompt_sr" && (
        <input
          className="input text-xs"
          placeholder="text in prompt to replace"
          value={axis.sr}
          onChange={(e) => onChange({ ...axis, sr: e.target.value })}
        />
      )}
      {axis.param &&
        (control?.options ? (
          <div className="flex flex-wrap gap-1">
            {control.options.map((o) => {
              const active = parseValues(axis.raw, control).map(String).includes(o);
              return (
                <button
                  key={o}
                  className={`chip ${active ? "chip-active" : ""}`}
                  aria-pressed={active}
                  onClick={() => {
                    const cur = axis.raw
                      ? axis.raw
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean)
                      : [];
                    const next = active ? cur.filter((x) => x !== o) : [...cur, o];
                    onChange({ ...axis, raw: next.join(", ") });
                  }}
                >
                  {o}
                </button>
              );
            })}
          </div>
        ) : (
          <input
            className="input text-xs"
            placeholder={
              axis.param === "prompt_sr"
                ? "replacements, comma-separated (first = original)"
                : control && (control.type === "slider" || control.type === "number")
                  ? "values, or 1-7 [5] / 1-7 (+0.5)"
                  : "values, comma-separated"
            }
            value={axis.raw}
            onChange={(e) => onChange({ ...axis, raw: e.target.value })}
          />
        ))}
    </div>
  );
}

/** Collapsible X/Y sweep config. Produces a GridConfig for /api/generate/grid. */
export function XYGridPanel({
  spec,
  grid,
  onGrid,
}: {
  spec: GeneratorSpec;
  grid: GridConfig | null;
  onGrid: (g: GridConfig | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [x, setX] = useState<AxisDraft>({ param: "", raw: "", sr: "" });
  const [y, setY] = useState<AxisDraft>({ param: "", raw: "", sr: "" });

  const cells = useMemo(() => {
    const gx = buildAxis(x, spec);
    const gy = buildAxis(y, spec);
    return gx ? gx.values.length * (gy ? gy.values.length : 1) : 0;
  }, [x, y, spec]);

  const sync = (nx: AxisDraft, ny: AxisDraft) => {
    setX(nx);
    setY(ny);
    const bx = buildAxis(nx, spec);
    const by = buildAxis(ny, spec);
    onGrid(bx ? { x: bx, y: by ?? undefined } : null);
  };

  return (
    <div className="border-t border-edge pt-4">
      <button
        className="flex w-full items-center justify-between text-xs text-muted hover:text-ink"
        onClick={() => setOpen((s) => !s)}
        aria-expanded={open}
      >
        <span className="flex items-center gap-2">
          <Icon name="layers" size={14} /> Parameter study
        </span>
        <span className="flex items-center gap-2">
          {grid && <span className="technical text-[9px] text-accent">{cells} cells</span>}
          <Icon
            name="chevron-right"
            size={14}
            className={`transition-transform ${open ? "rotate-90" : ""}`}
          />
        </span>
      </button>
      {open && (
        <div className="mt-4 border-l-2 border-edge pl-4">
          <div className="space-y-3">
            <AxisEditor title="X" spec={spec} axis={x} onChange={(a) => sync(a, y)} />
            <AxisEditor title="Y" spec={spec} axis={y} onChange={(a) => sync(x, a)} />
            {cells > 0 && (
              <div
                className={`text-[11px] ${cells > GRID_HARD_CAP ? "text-danger" : cells >= SOFT_WARN ? "text-warn" : "text-white/50"}`}
              >
                {cells} generation{cells === 1 ? "" : "s"}
                {cells > GRID_HARD_CAP && ` — over the ${GRID_HARD_CAP}-cell cap`}
              </div>
            )}
            {grid && (
              <button
                className="btn-ghost min-h-0 px-1 text-[11px] text-muted hover:text-danger"
                onClick={() => {
                  setX({ param: "", raw: "", sr: "" });
                  setY({ param: "", raw: "", sr: "" });
                  onGrid(null);
                }}
              >
                <Icon name="close" size={12} /> Clear study
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

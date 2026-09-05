import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { LoraInfo, LoraCatalog } from "../api/types";
import { Icon } from "./icons";

export type LoraSelection = { path: string; weight: number; te_weight?: number };

/** One row of the picker. Shows what the file is, so a mismatch is explicable
 *  rather than mysterious. */
function LoraRow({
  lora,
  on,
  disabled,
  onToggle,
}: {
  lora: LoraInfo;
  on: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      className={`w-full text-left px-2 py-1.5 text-xs flex items-center justify-between gap-2
        ${on ? "bg-accent/15" : "hover:bg-panel2"}
        ${disabled ? "opacity-40 cursor-not-allowed" : ""}
        ${lora.compatible === false ? "opacity-70" : ""}`}
      aria-pressed={on}
      disabled={disabled}
      onClick={onToggle}
      title={
        lora.selectable === false
          ? `${lora.path}\n${lora.unavailable_reason || "This is not a selectable adapter."}`
          : lora.compatible === false
            ? `${lora.path}\nBuilt for ${lora.arch}. It can still be applied, but it will probably not work.`
            : lora.path
      }
    >
      <span className="flex min-w-0 items-center gap-2 truncate">
        <span
          className={`flex size-4 shrink-0 items-center justify-center border ${on ? "border-accent bg-accent text-bg" : "border-edge text-transparent"}`}
        >
          <Icon name="check" size={10} />
        </span>
        <span className="truncate">{lora.label}</span>
        {lora.compatible === false && <span className="shrink-0 text-warn">⚠</span>}
      </span>
      <span className="flex shrink-0 items-center gap-2 text-white/40">
        {lora.family && lora.family !== "unknown" && <span className="technical">{lora.arch}</span>}
        {lora.adapter_type && <span className="technical">{lora.adapter_type}</span>}
        {lora.rank ? <span className="technical">r{lora.rank}</span> : null}
        <span>{fmtSize(lora.size_bytes)}</span>
      </span>
    </button>
  );
}

/** Module-level cache keyed by model variant: the catalogue is a directory
 *  listing that changes rarely, every local generator mounts this picker, and
 *  compatibility is judged against whichever model is selected. */
const cache = new Map<string, LoraCatalog>();

function fmtSize(bytes: number): string {
  const mb = bytes / 1024 / 1024;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

/**
 * Pick and weight local LoRA adapters.
 *
 * Weights are per-adapter and allow negatives, which push away from the trained
 * concept — a real technique, not a mistake, so the slider spans both signs.
 */
export function LoraPicker({
  value,
  onChange,
  modelVariant,
}: {
  value: LoraSelection[];
  onChange: (next: LoraSelection[]) => void;
  /** Which model these adapters will be applied to. Drives the compatible /
   *  not-for-this-model split. Omit and everything reads as compatible. */
  modelVariant?: string;
}) {
  const key = modelVariant ?? "";
  // Fetched results are keyed, so switching model variant does not briefly show
  // the previous model's compatibility judgements.
  const [fetched, setFetched] = useState<{ key: string; cat: LoraCatalog } | null>(null);
  const [err, setErr] = useState(false);
  const [open, setOpen] = useState(false);
  const [showIncompatible, setShowIncompatible] = useState(false);
  const [showUnavailable, setShowUnavailable] = useState(false);
  const selected = value ?? [];

  // Read the cache during render rather than syncing it into state from an
  // effect: a cache hit needs no render pass to become visible, and setState in
  // an effect body just to copy a value across is a cascading render for nothing.
  const cat = cache.get(key) ?? (fetched?.key === key ? fetched.cat : null);

  useEffect(() => {
    if (cache.has(key)) return;
    let alive = true;
    api
      .loras(modelVariant)
      .then((c) => {
        cache.set(key, c);
        // Guarded: a slow response for a model the user has already switched
        // away from must not overwrite the one they are looking at now.
        if (alive) setFetched({ key, cat: c });
      })
      .catch(() => {
        if (alive) setErr(true);
      });
    return () => {
      alive = false;
    };
  }, [key, modelVariant]);

  const atCap = cat != null && selected.length >= cat.max_active;

  const toggle = (l: LoraInfo) => {
    if (l.compatible === false || l.selectable === false) return;
    const i = selected.findIndex((s) => s.path === l.path);
    if (i >= 0) onChange(selected.filter((_, n) => n !== i));
    else if (!atCap)
      onChange([
        ...selected,
        {
          path: l.path,
          // The source card or local validation may identify a safer point than
          // the generic 1.0. Keep 1.0 as the backwards-compatible fallback.
          weight: typeof l.recommended_weight === "number" ? l.recommended_weight : 1.0,
        },
      ]);
  };

  const setWeight = (path: string, weight: number) =>
    onChange(selected.map((s) => (s.path === path ? { ...s, weight } : s)));
  const setTextWeight = (path: string, weight: number | undefined) =>
    onChange(
      selected.map((selection) => {
        if (selection.path !== path) return selection;
        if (weight === undefined) {
          const linked = { ...selection };
          delete linked.te_weight;
          return linked;
        }
        return { ...selection, te_weight: weight };
      }),
    );

  if (err) {
    return <div className="text-xs text-white/40">LoRAs unavailable — is the backend running?</div>;
  }
  if (!cat) return <div className="text-xs text-white/40">Loading LoRAs…</div>;

  if (!cat.items.length) {
    return (
      <div className="text-xs text-white/40">
        No LoRAs yet — drop <code className="text-white/60">.safetensors</code> files into{" "}
        <code className="text-white/60">models/loras/</code> and reload.
      </div>
    );
  }

  const byPath = new Map(cat.items.map((l) => [l.path, l]));
  // Grouped, not filtered. The backend already reports `compatible: true` when
  // either side is unknown, and a MISSING field is treated the same way — an
  // older backend that does not send it must not collapse the entire library
  // into "not for this model". Absence of evidence is not evidence of mismatch.
  const selectable = (l: LoraInfo) => l.selectable !== false;
  const suits = (l: LoraInfo) => selectable(l) && l.compatible !== false;
  const fits = cat.items.filter(suits);
  const misfits = cat.items.filter((l) => selectable(l) && !suits(l));
  const unavailable = cat.items.filter((l) => !selectable(l));

  return (
    <div className="space-y-2">
      {/* Active adapters, each with its own weight */}
      {selected.map((s) => {
        const info = byPath.get(s.path);
        return (
          <div key={s.path} className="border-l-2 border-accent bg-panel2 p-2.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs truncate" title={s.path}>
                {info?.label ?? s.path}
                {!info && <span className="text-danger ml-1">(missing)</span>}
              </span>
              <button
                className="icon-btn size-7 min-h-0 shrink-0"
                aria-label={`Remove ${info?.label ?? s.path}`}
                onClick={() => onChange(selected.filter((x) => x.path !== s.path))}
              >
                <Icon name="close" size={13} />
              </button>
            </div>
            <div className="flex items-center gap-2 mt-1">
              <input
                type="range"
                min={cat.weight_min}
                max={cat.weight_max}
                step={0.05}
                value={s.weight}
                aria-label={`Weight for ${info?.label ?? s.path}`}
                onChange={(e) => setWeight(s.path, Number(e.target.value))}
              />
              <span className="technical w-10 text-right text-xs text-muted">{s.weight.toFixed(2)}</span>
            </div>
            {(modelVariant === "sdxl" || info?.family === "sdxl") && (
              <details className="mt-2 border-t border-edge/70 pt-1.5 text-xs text-muted">
                <summary className="cursor-pointer select-none py-1">Advanced · text encoder</summary>
                <p className="mb-2 leading-relaxed text-white/40">
                  Lower this when a character or style LoRA overwhelms unrelated prompt words.
                </p>
                <div className="flex items-center gap-2">
                  <input
                    type="range"
                    min={cat.weight_min}
                    max={cat.weight_max}
                    step={0.05}
                    value={s.te_weight ?? s.weight}
                    aria-label={`Text encoder weight for ${info?.label ?? s.path}`}
                    onChange={(event) => setTextWeight(s.path, Number(event.target.value))}
                  />
                  <span className="technical w-10 text-right text-xs text-muted">
                    {(s.te_weight ?? s.weight).toFixed(2)}
                  </span>
                </div>
                {s.te_weight !== undefined && (
                  <button
                    className="mt-1 text-[10px] text-accent hover:underline"
                    onClick={() => setTextWeight(s.path, undefined)}
                  >
                    Link to main weight
                  </button>
                )}
              </details>
            )}
          </div>
        );
      })}

      <button className="btn w-full text-xs" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Icon name={open ? "check" : "plus"} size={14} />{" "}
        {open ? "Done" : `Add LoRA · ${selected.length}/${cat.max_active}`}
      </button>

      {open && (
        <div className="max-h-56 divide-y divide-edge overflow-y-auto border border-edge bg-bg">
          {fits.map((l) => (
            <LoraRow
              key={l.path}
              lora={l}
              on={selected.some((s) => s.path === l.path)}
              disabled={!selected.some((s) => s.path === l.path) && atCap}
              onToggle={() => toggle(l)}
            />
          ))}

          {/* Incompatible adapters are grouped and collapsed, never hidden.
              A file the user downloaded five minutes ago that simply does not
              appear reads as a broken app, not as a helpful filter. */}
          {misfits.length > 0 && (
            <>
              <button
                className="w-full px-2 py-1.5 text-left text-xs text-muted hover:bg-panel2"
                aria-expanded={showIncompatible}
                onClick={() => setShowIncompatible((v) => !v)}
              >
                <Icon name={showIncompatible ? "chevron-down" : "chevron-right"} size={11} /> Not for this
                model ({misfits.length})
              </button>
              {showIncompatible &&
                misfits.map((l) => (
                  <LoraRow
                    key={l.path}
                    lora={l}
                    on={selected.some((s) => s.path === l.path)}
                    disabled
                    onToggle={() => toggle(l)}
                  />
                ))}
            </>
          )}

          {unavailable.length > 0 && (
            <>
              <button
                className="w-full px-2 py-1.5 text-left text-xs text-muted hover:bg-panel2"
                aria-expanded={showUnavailable}
                onClick={() => setShowUnavailable((value) => !value)}
              >
                <Icon name={showUnavailable ? "chevron-down" : "chevron-right"} size={11} /> Not adapters (
                {unavailable.length})
              </button>
              {showUnavailable &&
                unavailable.map((lora) => (
                  <LoraRow key={lora.path} lora={lora} on={false} disabled onToggle={() => {}} />
                ))}
            </>
          )}
        </div>
      )}

      {atCap && (
        <div className="text-xs text-white/40">
          Stack limit reached ({cat.max_active}). Remove one to add another.
        </div>
      )}
    </div>
  );
}

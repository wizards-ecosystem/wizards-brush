import { useState } from "react";
import type { Control, Presets, StyleItem } from "../api/types";
import { Icon } from "./icons";
import { LoraPicker } from "./LoraPicker";
import {
  effectiveControlDefault,
  effectiveControlDescriptor,
  effectiveControlValue,
} from "../lib/generators";

type Vals = Record<string, any>;
type OnChange = (name: string, value: any) => void;

/** Flatten built-in + user styles into id→text and category groups. */
export function styleMap(presets: Presets | null): Record<string, string> {
  const m: Record<string, string> = {};
  presets?.style_profiles?.forEach((p) => p.items.forEach((s) => (m[s.id] = s.text)));
  presets?.user?.filter((u) => u.type === "style").forEach((u) => (m[`u${u.id}`] = u.payload.text || ""));
  return m;
}

function visible(c: Control, values: Vals, controls: Control[], visiting = new Set<string>()): boolean {
  if (!c.show_if) return true;
  const want = c.show_if.equals;
  const got = values[c.show_if.field];
  if (!(Array.isArray(want) ? want.includes(got) : got === want)) return false;
  if (visiting.has(c.name)) return false;
  const parent = controls.find((candidate) => candidate.name === c.show_if?.field);
  if (!parent) return true;
  const next = new Set(visiting);
  next.add(c.name);
  return visible(parent, values, controls, next);
}

/** Controls the Simple form shows. Everything else still submits its default —
 *  the switch changes what is asked, never what is sent. */
export function isBasic(c: Control): boolean {
  return c.tier === "basic";
}

/**
 * Controls hidden by Simple mode that no longer hold their default.
 *
 * Simple mode is only honest if it cannot hide a decision. A user who sets
 * guidance to 6.0 in Full and switches back would otherwise keep generating at
 * 6.0 with nothing on screen saying so, which reads as the app ignoring the
 * Quality tier. The panel surfaces this count and offers to clear it.
 */
export function overriddenHidden(controls: Control[], values: Vals): Control[] {
  if (!controls.some(isBasic)) return []; // untiered: Simple hides nothing
  return controls.filter((c) => {
    if (isBasic(c)) return false;
    const v = effectiveControlValue(c, values);
    if (v === undefined) return false;
    const expected = effectiveControlDefault(c, values);
    if (Array.isArray(v) && Array.isArray(expected)) return v.length !== expected.length;
    return v !== expected;
  });
}

export function DynamicControls({
  controls,
  values,
  onChange,
  presets,
  simple = false,
}: {
  controls: Control[];
  values: Vals;
  onChange: OnChange;
  presets: Presets | null;
  /** Show only the basic tier, and drop the Advanced disclosure entirely. */
  simple?: boolean;
}) {
  const [showAdv, setShowAdv] = useState(false);
  // A generator whose controls declare no tier at all has nothing to simplify —
  // filtering would leave an empty form with a Generate button under it. Show
  // everything instead, so an untiered generator degrades to today's behaviour
  // rather than to a blank panel.
  const tiered = controls.some(isBasic);
  const shown = simple && tiered ? controls.filter(isBasic) : controls;
  const main = shown.filter((c) => c.section !== "Advanced" && visible(c, values, controls));
  const adv = shown.filter((c) => c.section === "Advanced");
  const advVisible = adv.filter((c) => visible(c, values, controls));
  const controlNames = new Set(controls.map((c) => c.name));
  const settings = (presets?.settings || []).filter((p) =>
    (p.requires || []).every((name) => controlNames.has(name)),
  );
  const current = (name: string) =>
    values[name] ?? controls.find((control) => control.name === name)?.default;
  const settingIsActive = (preset: (typeof settings)[number]) =>
    Object.entries(preset.values)
      .filter(([name]) => controlNames.has(name))
      .every(([name, value]) => current(name) === value);
  const applySetting = (preset: (typeof settings)[number]) => {
    Object.entries(preset.values).forEach(([name, value]) => {
      if (controlNames.has(name)) onChange(name, value);
    });
  };

  return (
    <div className="space-y-5">
      {settings.length > 0 && (
        <div>
          <div className="label">Quick settings</div>
          <div className="flex flex-wrap gap-1.5">
            {settings.map((preset) => (
              <button
                key={preset.id}
                type="button"
                className={`chip ${settingIsActive(preset) ? "chip-active" : ""}`}
                title={preset.description}
                aria-pressed={settingIsActive(preset)}
                onClick={() => applySetting(preset)}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <div className="mt-1 text-[10px] text-white/40">
            Model-aware quality, batch, and finishing bundles.
          </div>
        </div>
      )}

      {main.map((c) => (
        <Field key={c.name} c={c} values={values} onChange={onChange} presets={presets} />
      ))}

      {advVisible.length > 0 && (
        <div className="border-t border-edge pt-4">
          <button
            className="flex w-full items-center justify-between text-xs font-medium text-muted hover:text-ink"
            onClick={() => setShowAdv((s) => !s)}
            aria-expanded={showAdv}
          >
            <span>Advanced controls</span>
            <span className="flex items-center gap-2">
              <span className="technical text-[9px]">{advVisible.length}</span>
              <Icon
                name="chevron-right"
                size={14}
                className={`transition-transform ${showAdv ? "rotate-90" : ""}`}
              />
            </span>
          </button>
          {showAdv && (
            <div className="mt-4 border-l-2 border-edge pl-4">
              <div className="space-y-5">
                {advVisible.map((c) => (
                  <Field key={c.name} c={c} values={values} onChange={onChange} presets={presets} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Field(props: { c: Control; values: Vals; onChange: OnChange; presets: Presets | null }) {
  const { c, values } = props;
  const constraint = c.constrained_by;
  const got = constraint && values[constraint.field];
  const constrained =
    !!constraint &&
    (Array.isArray(constraint.equals) ? constraint.equals.includes(got) : got === constraint.equals);
  const shownValues = constrained ? { ...values, [c.name]: constraint.value } : values;
  const descriptor = effectiveControlDescriptor(c, shownValues);
  return (
    <fieldset
      disabled={constrained}
      className={`m-0 min-w-0 border-0 p-0 ${constrained ? "opacity-65" : ""}`}
    >
      <FieldInner {...props} c={descriptor} values={shownValues} />
      {constrained && <div className="mt-1 text-[10px] text-warn">{constraint?.reason}</div>}
    </fieldset>
  );
}

function FieldInner({
  c,
  values,
  onChange,
  presets,
}: {
  c: Control;
  values: Vals;
  onChange: OnChange;
  presets: Presets | null;
}) {
  const v = values[c.name] ?? c.default;
  const dependentHint = c.hint_keys_by?.map[values[c.hint_keys_by.field]];
  const hintKey = dependentHint || c.hint_key;
  const hint = hintKey && presets?.hints[hintKey];
  const optionHint = c.option_hints?.[String(v)];
  const id = `control-${c.name}`;

  if (c.type === "textarea") {
    return <PromptField c={c} v={v} values={values} onChange={onChange} presets={presets} />;
  }

  if (c.type === "segmented") {
    return (
      <div>
        <div className="flex justify-between items-center">
          <label className="label mb-0">{c.label}</label>
        </div>
        <div className="mt-1 flex gap-1 border border-edge bg-bg p-1" role="group" aria-label={c.label}>
          {c.options?.map((o) => (
            <button
              key={o}
              className={`seg ${v === o ? "seg-active" : ""}`}
              onClick={() => onChange(c.name, o)}
              aria-pressed={v === o}
              title={c.option_hints?.[o] || (c.option_labels?.[o] ? `${c.option_labels[o]} (${o})` : o)}
            >
              {c.option_labels?.[o] ?? o}
            </button>
          ))}
        </div>
        {optionHint && <div className="mt-1 text-xs text-ink/65">{optionHint}</div>}
        {hint && <div className="text-[10px] text-white/40 mt-1">{hint}</div>}
      </div>
    );
  }

  if (c.type === "aspect") {
    return (
      <div>
        <label className="label">{c.label}</label>
        <div className="grid grid-cols-4 gap-1.5" role="group" aria-label={c.label}>
          {c.options?.map((o) => (
            <button
              key={o}
              onClick={() => onChange(c.name, o)}
              className={`flex min-h-14 flex-col items-center justify-center gap-1 border text-[10px] transition ${
                v === o
                  ? "border-accent bg-accent/15 text-white"
                  : "border-edge bg-panel2 text-white/60 hover:border-accent/40"
              }`}
              title={o}
              aria-pressed={v === o}
            >
              <AspectGlyph ratio={o} />
              {o}
            </button>
          ))}
        </div>
        {hint && <div className="text-[10px] text-white/40 mt-1">{hint}</div>}
      </div>
    );
  }

  if (c.type === "slider") {
    return (
      <div>
        <div className="flex justify-between">
          <label className="label" htmlFor={id}>
            {c.label}
          </label>
          <output htmlFor={id} className="technical text-xs text-ink">
            {v}
          </output>
        </div>
        <input
          type="range"
          id={id}
          min={c.min}
          max={c.max}
          step={c.step}
          value={v}
          onChange={(e) => onChange(c.name, Number(e.target.value))}
        />
        {hint && <div className="text-[10px] text-white/40 mt-1">{hint}</div>}
      </div>
    );
  }

  if (c.type === "number") {
    return (
      <div>
        <label className="label" htmlFor={id}>
          {c.label}
        </label>
        <input
          id={id}
          type="number"
          className="input"
          value={v}
          // Keep "" while the user is mid-edit (backspace-to-retype) — coercing
          // it to 0 would instantly persist seed/width 0 into the draft. The
          // backend coerces "" back to the param's default at submit.
          onChange={(e) => onChange(c.name, e.target.value === "" ? "" : Number(e.target.value))}
        />
        {hint && <div className="text-[10px] text-white/40 mt-1">{hint}</div>}
      </div>
    );
  }

  if (c.type === "select") {
    return (
      <div>
        <label className="label" htmlFor={id}>
          {c.label}
        </label>
        <select id={id} className="input" value={v} onChange={(e) => onChange(c.name, e.target.value)}>
          {c.options?.map((o) => (
            <option key={o} value={o}>
              {c.option_labels?.[o] ?? o}
            </option>
          ))}
        </select>
        {optionHint && <div className="mt-1 text-xs text-ink/65">{optionHint}</div>}
        {hint && <div className="text-[10px] text-white/40 mt-1">{hint}</div>}
      </div>
    );
  }

  if (c.type === "lora") {
    return (
      <div>
        <div className="flex justify-between items-center">
          <label className="label mb-0">{c.label}</label>
        </div>
        <div className="mt-1">
          <LoraPicker
            value={v ?? []}
            onChange={(next) => onChange(c.name, next)}
            modelVariant={values.model_variant}
          />
        </div>
        {hint && <div className="text-xs text-white/35 mt-1">{hint}</div>}
      </div>
    );
  }

  if (c.type === "toggle") {
    return (
      <label className="flex cursor-pointer items-center justify-between gap-3 border-y border-edge py-2.5 select-none">
        <span className="text-sm text-ink/85">{c.label}</span>
        <input
          id={id}
          type="checkbox"
          className="peer sr-only"
          checked={!!v}
          onChange={(e) => onChange(c.name, e.target.checked)}
        />
        <span
          className={`h-5 w-9 rounded-full p-0.5 transition-colors peer-focus-visible:outline-2 peer-focus-visible:outline-focus ${v ? "bg-accent" : "bg-edge"}`}
        >
          <span
            className={`block size-4 rounded-full bg-bg transition-transform ${v ? "translate-x-4" : ""}`}
          />
        </span>
      </label>
    );
  }
  return null;
}

function AspectGlyph({ ratio }: { ratio: string }) {
  if (ratio === "Custom") return <span className="text-white/50">⋯</span>;
  const [w, h] = ratio.split(":").map(Number);
  const maxDim = 18;
  const bw = w >= h ? maxDim : (maxDim * w) / h;
  const bh = h >= w ? maxDim : (maxDim * h) / w;
  return (
    <span className="flex items-center justify-center" style={{ width: maxDim, height: maxDim }}>
      <span className="border border-current rounded-[2px]" style={{ width: bw, height: bh }} />
    </span>
  );
}

function PromptField({
  c,
  v,
  values,
  onChange,
  presets,
}: {
  c: Control;
  v: any;
  values: Vals;
  onChange: OnChange;
  presets: Presets | null;
}) {
  const [showStyles, setShowStyles] = useState(false);
  const isPrompt = c.styles === true;
  const isNegative = c.preset_group === "negative";
  const selected: string[] = values.style_ids || [];
  const id = `control-${c.name}`;

  const toggleStyle = (id: string) => {
    const next = selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id];
    onChange("style_ids", next);
  };

  const userPrompts = presets?.user?.filter((u) => u.type === "prompt") || [];
  const userStyles = presets?.user?.filter((u) => u.type === "style") || [];
  const promptGroups = Array.from(
    (presets?.prompt || []).reduce((groups, preset) => {
      const category = preset.category || "Starters";
      groups.set(category, [...(groups.get(category) || []), preset]);
      return groups;
    }, new Map<string, NonNullable<Presets>["prompt"]>()),
  );

  return (
    <div>
      <label className="label" htmlFor={id}>
        {c.label}
      </label>
      <textarea
        id={id}
        className="input min-h-[84px] resize-y"
        value={v}
        placeholder={c.label}
        onChange={(e) => onChange(c.name, e.target.value)}
      />

      {isPrompt && presets && (
        <>
          <div className="mt-2 max-h-44 space-y-2 overflow-y-auto pr-1">
            {promptGroups.map(([category, group]) => (
              <div key={category}>
                <div className="mb-1 text-[10px] uppercase tracking-wide text-white/30">{category}</div>
                <div className="flex flex-wrap gap-1.5">
                  {group.map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      className="chip"
                      title={p.text}
                      onClick={() => onChange(c.name, v ? `${v}, ${p.text}` : p.text)}
                    >
                      <Icon name="plus" size={11} /> {p.label}
                    </button>
                  ))}
                </div>
              </div>
            ))}
            {userPrompts.length > 0 && (
              <div>
                <div className="mb-1 text-[10px] uppercase tracking-wide text-white/30">Saved</div>
                <div className="flex flex-wrap gap-1.5">
                  {userPrompts.map((p) => (
                    <button
                      key={`u${p.id}`}
                      type="button"
                      className="chip border-accent/50"
                      onClick={() => onChange(c.name, v ? `${v}, ${p.payload.text}` : p.payload.text)}
                      title="Your saved prompt"
                    >
                      <Icon name="bookmark" size={11} /> {p.name}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>

          <button
            className="mt-3 flex items-center gap-2 text-xs text-muted hover:text-ink"
            onClick={() => setShowStyles((s) => !s)}
            aria-expanded={showStyles}
          >
            <Icon
              name="chevron-right"
              size={13}
              className={`transition-transform ${showStyles ? "rotate-90" : ""}`}
            />
            Styles {selected.length > 0 && <span className="technical text-accent">{selected.length}</span>}
          </button>
          {showStyles && (
            <div className="mt-2 space-y-2 max-h-64 overflow-y-auto pr-1">
              {presets.style_profiles.map((cat) => (
                <div key={cat.category}>
                  <div className="text-[10px] uppercase tracking-wide text-white/30 mb-1">{cat.category}</div>
                  <div className="flex flex-wrap gap-1.5">
                    {cat.items.map((s: StyleItem) => (
                      <button
                        key={s.id}
                        className={`chip ${selected.includes(s.id) ? "chip-active" : ""}`}
                        onClick={() => toggleStyle(s.id)}
                        title={s.text}
                        aria-pressed={selected.includes(s.id)}
                      >
                        {s.label || s.text}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
              {userStyles.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-white/30 mb-1">Saved</div>
                  <div className="flex flex-wrap gap-1.5">
                    {userStyles.map((s) => (
                      <button
                        key={`u${s.id}`}
                        className={`chip ${selected.includes(`u${s.id}`) ? "chip-active" : ""}`}
                        onClick={() => toggleStyle(`u${s.id}`)}
                        title={s.payload.text}
                        aria-pressed={selected.includes(`u${s.id}`)}
                      >
                        <Icon name="bookmark" size={11} /> {s.name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {isNegative && presets && (
        <select
          className="input mt-2 text-xs"
          aria-label={`Insert preset into ${c.label}`}
          value=""
          onChange={(e) => {
            const p = presets.negative.find((n) => n.id === e.target.value);
            if (p) onChange(c.name, p.text);
          }}
        >
          <option value="">Insert negative preset…</option>
          {presets.negative.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

import { useRef, useState } from "react";
import type {
  Asset,
  Control,
  GeneratorSpec,
  Presets,
  VariantCapabilities,
  VariantOperation,
} from "../api/types";
import { applyDependentDefaults } from "../lib/generators";
import {
  addValues,
  availableAxes,
  formula,
  insertAt,
  newAxis,
  parseAxisValues,
  placeholder,
  type AxisDraft,
  type StageDraft,
} from "../lib/variantSets";
import { DynamicControls } from "./DynamicControls";
import { FinishingStepsEditor } from "./FinishingStepsEditor";
import { GalleryPicker } from "./GalleryPicker";
import { Icon } from "./icons";
import { IconButton, SegmentedControl } from "./ui";

/** The controls a set may set for an operation: the registry's, minus the ones
 *  the set supplies itself (prompt, negative, batch, seeds). */
export function operationControls(
  spec: GeneratorSpec | undefined,
  caps: VariantCapabilities | null,
): Control[] {
  const controlled = new Set(caps?.set_controlled ?? []);
  return (spec?.controls ?? []).filter((control) => !controlled.has(control.name));
}

function AxisEditor({
  axis,
  onChange,
  onRemove,
  maxValues,
}: {
  axis: AxisDraft;
  onChange: (axis: AxisDraft) => void;
  onRemove: () => void;
  maxValues: number;
}) {
  const [entry, setEntry] = useState("");
  const commit = (raw: string) => {
    const values = addValues(axis.values, parseAxisValues(raw)).slice(0, maxValues);
    onChange({ ...axis, values });
    setEntry("");
  };
  return (
    <div className="border border-edge bg-panel2/60 p-3" data-testid="axis-editor">
      <div className="flex items-center gap-2">
        <input
          className="input technical flex-1 text-xs"
          aria-label="Axis name"
          placeholder="axis_name"
          value={axis.name}
          onChange={(e) => onChange({ ...axis, name: e.target.value.trim() })}
        />
        <span className="technical shrink-0 text-[10px] text-muted">{axis.values.length} values</span>
        <IconButton icon="close" label={`Remove axis ${axis.name || ""}`.trim()} onClick={onRemove} />
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {axis.values.map((value) => (
          <button
            key={value}
            type="button"
            className="chip"
            aria-label={`Remove value ${value}`}
            onClick={() => {
              const content = { ...axis.content };
              delete content[value];
              onChange({ ...axis, values: axis.values.filter((v) => v !== value), content });
            }}
          >
            {value} <Icon name="close" size={11} />
          </button>
        ))}
        <input
          className="input min-w-32 flex-1 text-xs"
          aria-label={`Add values to ${axis.name || "axis"}`}
          placeholder="type a value, Enter or comma to add"
          value={entry}
          onChange={(e) => {
            const next = e.target.value;
            if (next.includes(",") || next.includes("\n")) commit(next);
            else setEntry(next);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              if (entry.trim()) commit(entry);
            }
          }}
          onBlur={() => entry.trim() && commit(entry)}
          onPaste={(e) => {
            const text = e.clipboardData.getData("text");
            if (text.includes(",") || text.includes("\n")) {
              e.preventDefault();
              commit(`${entry}${text}`);
            }
          }}
        />
      </div>
      {axis.values.length > 0 && (
        <label className="mt-2 flex items-center gap-2 text-[11px] text-muted">
          <input
            type="checkbox"
            checked={axis.describe}
            onChange={(e) => onChange({ ...axis, describe: e.target.checked })}
          />
          Describe each value for the prompt (its {placeholder(axis.name || "axis")} becomes this text)
        </label>
      )}
      {axis.describe && (
        <div className="mt-2 space-y-1.5">
          {axis.values.map((value) => (
            <label key={value} className="flex items-center gap-2 text-xs">
              <span className="technical w-24 shrink-0 truncate text-muted" title={value}>
                {value}
              </span>
              <input
                className="input flex-1 text-xs"
                aria-label={`Description for ${value}`}
                placeholder={`text used for ${value}`}
                value={axis.content[value] || ""}
                onChange={(e) => onChange({ ...axis, content: { ...axis.content, [value]: e.target.value } })}
              />
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

function Template({
  label,
  value,
  onChange,
  axes,
  rows = 3,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  axes: string[];
  rows?: number;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const insert = (name: string) => {
    const el = ref.current;
    const start = el?.selectionStart ?? value.length;
    const end = el?.selectionEnd ?? value.length;
    const next = insertAt(value, start, end, placeholder(name));
    onChange(next.text);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(next.cursor, next.cursor);
    });
  };
  return (
    <div>
      <label className="label">{label}</label>
      <textarea
        ref={ref}
        className="input min-h-[72px] resize-y"
        rows={rows}
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {axes.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1" aria-label={`Insert an axis into ${label}`}>
          {axes.map((name) => (
            <button
              key={name}
              type="button"
              className="chip technical text-[10px]"
              onClick={() => insert(name)}
            >
              <Icon name="plus" size={10} /> {placeholder(name)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  children,
  open = false,
}: {
  title: string;
  children: React.ReactNode;
  open?: boolean;
}) {
  const [shown, setShown] = useState(open);
  return (
    <div className="border-t border-edge pt-3">
      <button
        type="button"
        className="flex w-full items-center justify-between text-xs font-medium text-muted hover:text-ink"
        onClick={() => setShown((s) => !s)}
        aria-expanded={shown}
      >
        {title}
        <Icon name="chevron-right" size={14} className={`transition-transform ${shown ? "rotate-90" : ""}`} />
      </button>
      {shown && <div className="mt-3 space-y-3">{children}</div>}
    </div>
  );
}

const numberOr = (raw: string): number | "" => (raw === "" ? "" : Number(raw));

export function VariantStageEditor({
  stage,
  index,
  stages,
  operations,
  caps,
  spec,
  presets,
  assetById = () => undefined,
  onChange,
  onRemove,
}: {
  stage: StageDraft;
  index: number;
  stages: StageDraft[];
  operations: VariantOperation[];
  caps: VariantCapabilities | null;
  spec: GeneratorSpec | undefined;
  presets: Presets | null;
  /** Thumbnails for reference images that may not be in the recent snapshot. */
  assetById?: (id: number) => Asset | undefined;
  onChange: (stage: StageDraft) => void;
  onRemove?: () => void;
}) {
  const [simple, setSimple] = useState(true);
  const [pickingReference, setPickingReference] = useState(false);
  const controls = operationControls(spec, caps);
  const axes = availableAxes(stages, index);
  const choices = index === 0 ? operations : operations.filter((op) => op.takes_source);
  const op = choices.find((candidate) => candidate.kind === stage.operation);
  // A later stage's own output is always its first input; an operation that
  // takes several images can add up to two fixed references alongside it.
  const referenceSlots = index > 0 && op ? Math.min(2, Math.max(0, op.max_sources - 1)) : 0;
  const perValue = Object.entries(stage.valueParams ?? {}).flatMap(([axis, byValue]) =>
    Object.entries(byValue).map(
      ([value, settings]) => `${axis}=${value}: ${Object.keys(settings).join(", ")}`,
    ),
  );
  const setAxis = (i: number, axis: AxisDraft) =>
    onChange({ ...stage, axes: stage.axes.map((a, j) => (j === i ? axis : a)) });
  const setParam = (name: string, value: any) =>
    onChange({
      ...stage,
      params: {
        ...stage.params,
        [name]: value,
        ...(name === "model_variant" ? { loras: [] } : {}),
        ...applyDependentDefaults(controls, name, stage.params[name], value, stage.params),
      },
    });
  const v = stage.validation;
  const limits = caps?.limits;

  return (
    <section className="space-y-4 border border-edge bg-panel p-4" aria-label={`Stage ${index + 1}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="page-kicker">Stage {index + 1}</div>
          <div className="text-sm text-muted">
            {index === 0 ? "Runs on the source images." : `Runs on every output of stage ${index}.`}
          </div>
        </div>
        {onRemove && <IconButton icon="trash" label={`Remove stage ${index + 1}`} onClick={onRemove} />}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor={`op-${stage.uid}`}>
            Operation
          </label>
          <select
            id={`op-${stage.uid}`}
            className="input"
            value={stage.operation}
            onChange={(e) => onChange({ ...stage, operation: e.target.value, params: {} })}
          >
            {choices.map((op) => (
              <option key={op.kind} value={op.kind}>
                {op.title}
              </option>
            ))}
          </select>
          <div className="mt-1 text-[11px] text-muted">
            {choices.find((op) => op.kind === stage.operation)?.note}
          </div>
        </div>
        <div>
          <label className="label" htmlFor={`name-${stage.uid}`}>
            Stage label (optional)
          </label>
          <input
            id={`name-${stage.uid}`}
            className="input"
            value={stage.name}
            maxLength={80}
            onChange={(e) => onChange({ ...stage, name: e.target.value })}
          />
        </div>
      </div>

      {referenceSlots > 0 && (
        <div>
          <div className="flex items-baseline justify-between">
            <div className="label mb-0">Reference images (optional)</div>
            <span className="technical text-[10px] text-muted">
              {stage.references.length}/{referenceSlots}
            </span>
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            {stage.references.map((id) => {
              const asset = assetById(id);
              return (
                <div key={id} className="media-tile relative size-16">
                  {asset ? (
                    <img src={asset.thumb_url || asset.url} alt="" className="h-full w-full object-cover" />
                  ) : (
                    <div className="technical flex h-full items-center justify-center text-[10px] text-muted">
                      #{id}
                    </div>
                  )}
                  <IconButton
                    icon="close"
                    label={`Remove reference ${id}`}
                    className="absolute right-0 top-0 bg-black/60 text-white"
                    onClick={() =>
                      onChange({ ...stage, references: stage.references.filter((r) => r !== id) })
                    }
                  />
                </div>
              );
            })}
            {stage.references.length < referenceSlots && (
              <button
                type="button"
                className="flex size-16 flex-col items-center justify-center gap-1 border border-dashed border-edge text-[10px] text-muted hover:text-ink"
                onClick={() => setPickingReference(true)}
              >
                <Icon name="plus" size={14} /> Add
              </button>
            )}
          </div>
          <div className="mt-1 text-[11px] text-muted">
            Sent after the previous stage's output, the same images for every variant of this stage.
          </div>
        </div>
      )}

      <div>
        <div className="flex items-baseline justify-between">
          <div className="label mb-0">Axes</div>
          <span className="technical text-[10px] text-accent" aria-label="Stage combination formula">
            {formula(stage.axes)} per {index === 0 ? "set" : "input"}
          </span>
        </div>
        <div className="mt-2 space-y-2">
          {stage.axes.map((axis, i) => (
            <AxisEditor
              key={axis.uid}
              axis={axis}
              maxValues={limits?.values_per_axis ?? 256}
              onChange={(next) => setAxis(i, next)}
              onRemove={() => onChange({ ...stage, axes: stage.axes.filter((_, j) => j !== i) })}
            />
          ))}
        </div>
        {stage.axes.length < (limits?.axes_per_stage ?? 12) && (
          <button
            type="button"
            className="btn mt-2 text-xs"
            onClick={() => onChange({ ...stage, axes: [...stage.axes, newAxis()] })}
          >
            <Icon name="plus" size={13} /> Add axis
          </button>
        )}
      </div>

      <Template
        label="Instruction template"
        value={stage.prompt}
        axes={axes}
        onChange={(prompt) => onChange({ ...stage, prompt })}
      />
      <Section title="Negative template">
        <Template
          label="Negative template"
          value={stage.negative}
          axes={axes}
          rows={2}
          onChange={(negative) => onChange({ ...stage, negative })}
        />
      </Section>

      <Section title="Operation settings" open={index === 0}>
        <SegmentedControl
          className="w-full"
          label="How many settings to show"
          value={simple ? "simple" : "full"}
          onChange={(next) => setSimple(next === "simple")}
          options={[
            { value: "simple", label: "Simple" },
            { value: "full", label: "Full controls" },
          ]}
        />
        {controls.length ? (
          <DynamicControls
            controls={controls}
            values={stage.params}
            onChange={setParam}
            presets={presets}
            simple={simple}
          />
        ) : (
          <div className="text-xs text-muted">This operation has no further settings.</div>
        )}
        {perValue.length > 0 && (
          <div className="border-l-2 border-edge pl-3 text-[11px] text-muted">
            <div className="text-ink/80">Per-value settings from the recipe (kept as they are):</div>
            {perValue.map((line) => (
              <div key={line} className="technical">
                {line}
              </div>
            ))}
            <button
              type="button"
              className="mt-1 text-accent underline"
              onClick={() => onChange({ ...stage, valueParams: {} })}
            >
              Remove them
            </button>
          </div>
        )}
      </Section>

      <Section title="Finishing">
        <FinishingStepsEditor
          steps={stage.finishing}
          processors={caps?.finishing ?? []}
          onChange={(finishing) => onChange({ ...stage, finishing })}
          note="Runs inside each variant's job, in this order, after the operation's own finishing preset."
        />
      </Section>

      <Section title="Validation">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={v.png}
            onChange={(e) => onChange({ ...stage, validation: { ...v, png: e.target.checked } })}
          />
          Must be a PNG
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={v.exactSize}
            onChange={(e) => onChange({ ...stage, validation: { ...v, exactSize: e.target.checked } })}
          />
          Must be exactly
          <input
            className="input w-20 text-xs"
            type="number"
            aria-label="Required width"
            value={v.width}
            disabled={!v.exactSize}
            onChange={(e) => onChange({ ...stage, validation: { ...v, width: Number(e.target.value) } })}
          />
          ×
          <input
            className="input w-20 text-xs"
            type="number"
            aria-label="Required height"
            value={v.height}
            disabled={!v.exactSize}
            onChange={(e) => onChange({ ...stage, validation: { ...v, height: Number(e.target.value) } })}
          />
        </label>
        <div>
          <div className="label">Transparency</div>
          <SegmentedControl
            label="Transparency requirement"
            value={v.alpha}
            onChange={(alpha) => onChange({ ...stage, validation: { ...v, alpha } })}
            options={[
              { value: "any", label: "Any" },
              { value: "required", label: "Alpha required" },
              { value: "forbidden", label: "Opaque only" },
            ]}
          />
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={v.corners}
            onChange={(e) => onChange({ ...stage, validation: { ...v, corners: e.target.checked } })}
          />
          All four corners transparent
        </label>
        <div className="grid grid-cols-3 gap-2">
          <label className="text-[11px] text-muted">
            Min transparent %
            <input
              className="input mt-1 text-xs"
              type="number"
              min={0}
              max={100}
              value={v.minTransparent}
              onChange={(e) =>
                onChange({ ...stage, validation: { ...v, minTransparent: numberOr(e.target.value) } })
              }
            />
          </label>
          <label className="text-[11px] text-muted">
            Max transparent %
            <input
              className="input mt-1 text-xs"
              type="number"
              min={0}
              max={100}
              value={v.maxTransparent}
              onChange={(e) =>
                onChange({ ...stage, validation: { ...v, maxTransparent: numberOr(e.target.value) } })
              }
            />
          </label>
          <label className="text-[11px] text-muted">
            Safe margin px
            <input
              className="input mt-1 text-xs"
              type="number"
              min={0}
              value={v.margin}
              onChange={(e) => onChange({ ...stage, validation: { ...v, margin: numberOr(e.target.value) } })}
            />
          </label>
        </div>
      </Section>

      <Section title="Output names">
        <div>
          <label className="label" htmlFor={`naming-${stage.uid}`}>
            Name template
          </label>
          <input
            id={`naming-${stage.uid}`}
            className="input technical text-xs"
            placeholder={axes.length ? axes.map(placeholder).join("_") : "{{_index}}"}
            value={stage.naming.template}
            onChange={(e) => onChange({ ...stage, naming: { ...stage.naming, template: e.target.value } })}
          />
          <div className="mt-1 text-[11px] text-muted">
            Blank uses every axis. {"{{axis}}"} is the value's identifier, {"{{axis.value}}"} the value as
            written, and / makes a folder. The extension comes from the file.
          </div>
        </div>
        <div>
          <label className="label" htmlFor={`prefix-${stage.uid}`}>
            Prefix
          </label>
          <input
            id={`prefix-${stage.uid}`}
            className="input technical text-xs"
            value={stage.naming.prefix}
            aria-describedby={`prefix-hint-${stage.uid}`}
            onChange={(e) => onChange({ ...stage, naming: { ...stage.naming, prefix: e.target.value } })}
          />
          <div id={`prefix-hint-${stage.uid}`} className="mt-1 text-[11px] text-muted">
            Put in front of every name exactly as written: end it with / for a folder.
          </div>
        </div>
      </Section>
      {pickingReference && (
        <GalleryPicker
          title="Choose a reference image"
          filter={(asset) => asset.generator !== "mask" && !stage.references.includes(asset.id)}
          onClose={() => setPickingReference(false)}
          onPick={(asset) => {
            setPickingReference(false);
            onChange({ ...stage, references: [...stage.references, asset.id].slice(0, referenceSlots) });
          }}
        />
      )}
    </section>
  );
}

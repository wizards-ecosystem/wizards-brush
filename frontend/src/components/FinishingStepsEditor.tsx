import { useState } from "react";
import type { Control, VariantFinishingStep, VariantProcessor } from "../api/types";
import { Icon } from "./icons";
import { IconButton } from "./ui";

/** The backend's cap (finishing.MAX_STEPS). */
export const MAX_FINISHING_STEPS = 6;

/** A step with every option at its declared default. */
export function newStep(processor: VariantProcessor): VariantFinishingStep {
  const step: VariantFinishingStep = { processor: processor.name };
  for (const option of processor.options) step[option.name] = option.default;
  return step;
}

/** A number field that can be cleared while typing: the step keeps its last
 *  valid number, and a field left empty shows that number again on blur. */
function NumberInput({
  option,
  value,
  onChange,
  label,
}: {
  option: Control;
  value: unknown;
  onChange: (value: unknown) => void;
  label: string;
}) {
  const shown = String(value ?? option.default ?? "");
  const [text, setText] = useState(shown);
  const [synced, setSynced] = useState(shown);
  if (shown !== synced) {
    // The step changed from outside (a loaded recipe): follow it.
    setSynced(shown);
    setText(shown);
  }
  return (
    <input
      className="input text-xs"
      type="number"
      aria-label={label}
      min={option.min}
      max={option.max}
      step={option.step}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const next = Number(e.target.value);
        if (e.target.value !== "" && Number.isFinite(next)) {
          setSynced(String(next));
          onChange(next);
        }
      }}
      onBlur={() => setText(shown)}
    />
  );
}

function OptionInput({
  option,
  value,
  onChange,
  label,
}: {
  option: Control;
  value: unknown;
  onChange: (value: unknown) => void;
  label: string;
}) {
  if (option.type === "number" || option.type === "slider") {
    return <NumberInput option={option} value={value} onChange={onChange} label={label} />;
  }
  // select / segmented: the declared options, plus the current value when a
  // recipe carries one the picker does not list (a custom #rrggbb padding).
  const options = (option.options ?? []).map(String);
  const current = value === undefined || value === null ? String(option.default) : String(value);
  if (!options.includes(current)) options.push(current);
  return (
    <select
      className="input text-xs"
      aria-label={label}
      value={current}
      onChange={(e) => onChange(e.target.value)}
    >
      {options.map((o) => (
        <option key={o} value={o}>
          {option.option_labels?.[o] ?? o}
        </option>
      ))}
    </select>
  );
}

/**
 * An ordered list of finishing processors (app/finishing.py): the same chain a
 * Variant Set stage runs and any image generation can run as `finish_steps`.
 * Order matters — resizing before an upscale is not the same image as after —
 * so steps can be moved, not only added and removed.
 */
export function FinishingStepsEditor({
  steps,
  processors,
  onChange,
  note,
}: {
  steps: VariantFinishingStep[];
  processors: VariantProcessor[];
  onChange: (steps: VariantFinishingStep[]) => void;
  note?: string;
}) {
  const byName = new Map(processors.map((p) => [p.name, p]));
  const set = (i: number, step: VariantFinishingStep) => onChange(steps.map((s, j) => (j === i ? step : s)));
  const move = (i: number, by: number) => {
    const next = [...steps];
    const [step] = next.splice(i, 1);
    next.splice(i + by, 0, step);
    onChange(next);
  };
  const addable = processors.filter((p) => p.available);
  const unavailable = processors.filter((p) => !p.available);

  return (
    <div className="space-y-2" role="group" aria-label="Finishing steps">
      {steps.length === 0 && (
        <div className="text-xs text-muted">No finishing steps: the output is kept as generated.</div>
      )}
      <ol className="space-y-2">
        {steps.map((step, i) => {
          const processor = byName.get(step.processor);
          const label = processor?.label ?? step.processor;
          return (
            <li key={`${step.processor}-${i}`} className="border border-edge bg-bg/40 p-2">
              <div className="flex items-center gap-2">
                <span className="technical w-4 text-[10px] text-muted">{i + 1}</span>
                <span className="min-w-0 flex-1 truncate text-sm">{label}</span>
                <IconButton
                  icon="arrow-up"
                  label={`Move ${label} earlier`}
                  disabled={i === 0}
                  onClick={() => move(i, -1)}
                />
                <IconButton
                  icon="arrow-down"
                  label={`Move ${label} later`}
                  disabled={i === steps.length - 1}
                  onClick={() => move(i, 1)}
                />
                <IconButton
                  icon="close"
                  label={`Remove ${label}`}
                  onClick={() => onChange(steps.filter((_, j) => j !== i))}
                />
              </div>
              {processor && !processor.available && (
                <div className="mt-1 text-[11px] text-warn">
                  <Icon name="alert" size={12} className="mr-1 inline" />
                  {processor.unavailable_reason || "Unavailable here"}: this step will be skipped with a
                  warning.
                </div>
              )}
              {!processor && (
                <div className="mt-1 text-[11px] text-warn">Unknown processor on this install.</div>
              )}
              {processor && processor.options.length > 0 && (
                <div className="mt-2 grid grid-cols-2 gap-2 pl-6">
                  {processor.options.map((option) => (
                    <label key={option.name} className="block text-[11px] text-muted">
                      {option.label}
                      <OptionInput
                        option={option}
                        label={`${label}: ${option.label}`}
                        value={step[option.name]}
                        onChange={(value) => set(i, { ...step, [option.name]: value })}
                      />
                    </label>
                  ))}
                </div>
              )}
            </li>
          );
        })}
      </ol>
      {steps.length < MAX_FINISHING_STEPS && addable.length > 0 && (
        <select
          className="input text-xs"
          aria-label="Add a finishing step"
          value=""
          onChange={(e) => {
            const processor = byName.get(e.target.value);
            if (processor) onChange([...steps, newStep(processor)]);
          }}
        >
          <option value="">+ Add a step…</option>
          {addable.map((p) => (
            <option key={p.name} value={p.name}>
              {p.label}
            </option>
          ))}
        </select>
      )}
      {unavailable.length > 0 && (
        <div className="text-[11px] text-muted">
          Not available here:{" "}
          {unavailable.map((p) => `${p.label} (${p.unavailable_reason || "unavailable"})`).join("; ")}
        </div>
      )}
      {note && <div className="text-[11px] text-muted">{note}</div>}
    </div>
  );
}

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { LoraInfo, PlanAdapter } from "../../api/types";
import { useStore } from "../../store/useStore";
import { Icon } from "../icons";

type Entry = { resource: string; strength: number };

/**
 * The ordered adapter stack, by resource identity.
 *
 * Order is meaning, so it is edited explicitly and never sorted. An adapter
 * the server blocks for this model (another family, a remote target that
 * cannot apply it) stays in the list with its reason until it is removed:
 * its identity and strength are never cleared silently. Only adapters the
 * library scan has verified can be added, because a preset names content.
 */
export function AdapterStack({
  planAdapters,
  value,
  onChange,
}: {
  planAdapters: PlanAdapter[];
  /** The override, or null while the preset's own list applies. */
  value: Entry[] | null;
  onChange: (next: Entry[] | null) => void;
}) {
  const controlsRevision = useStore((s) => s.controlsRevision);
  const [catalog, setCatalog] = useState<LoraInfo[] | null>(null);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .loras()
      .then((c) => alive && setCatalog(c.items))
      .catch(() => alive && setCatalog([]));
    return () => {
      alive = false;
    };
  }, [controlsRevision]);

  const entries: Entry[] = value ?? planAdapters.map((a) => ({ resource: a.resource, strength: a.strength }));
  const byResource = new Map(
    (catalog || []).filter((l) => l.resource_id).map((l) => [l.resource_id as string, l]),
  );
  const status = new Map(planAdapters.map((a) => [a.resource, a]));
  const set = (next: Entry[]) => onChange(next);
  const move = (index: number, by: number) => {
    const next = [...entries];
    const [item] = next.splice(index, 1);
    next.splice(index + by, 0, item);
    set(next);
  };
  const addable = (catalog || []).filter(
    (l) => l.resource_id && l.selectable !== false && !entries.some((e) => e.resource === l.resource_id),
  );

  return (
    <div className="space-y-2" data-testid="adapter-stack">
      {entries.length === 0 && <div className="text-xs text-muted">No adapters: the model's own look.</div>}
      {entries.map((entry, index) => {
        const info = byResource.get(entry.resource);
        const planned = status.get(entry.resource);
        const blocked = planned?.status === "blocked";
        return (
          <div
            key={`${entry.resource}-${index}`}
            className={`border-l-2 bg-panel2 p-2 ${blocked ? "border-danger" : "border-accent"}`}
            data-testid="adapter-row"
          >
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="truncate" title={entry.resource}>
                {index + 1}. {info?.label ?? planned?.library_path ?? `${entry.resource.slice(0, 26)}…`}
                {!info && !planned?.library_path && <span className="ml-1 text-danger">(not installed)</span>}
                {blocked && <span className="ml-1 text-danger">(blocked)</span>}
              </span>
              <span className="flex shrink-0 gap-0.5">
                <button
                  type="button"
                  className="icon-btn size-6 min-h-0"
                  aria-label={`Move adapter ${index + 1} up`}
                  disabled={index === 0}
                  onClick={() => move(index, -1)}
                >
                  <Icon name="arrow-up" size={11} />
                </button>
                <button
                  type="button"
                  className="icon-btn size-6 min-h-0"
                  aria-label={`Move adapter ${index + 1} down`}
                  disabled={index === entries.length - 1}
                  onClick={() => move(index, 1)}
                >
                  <Icon name="arrow-down" size={11} />
                </button>
                <button
                  type="button"
                  className="icon-btn size-6 min-h-0"
                  aria-label={`Remove adapter ${index + 1}`}
                  onClick={() => set(entries.filter((_, n) => n !== index))}
                >
                  <Icon name="close" size={11} />
                </button>
              </span>
            </div>
            <div className="mt-1 flex items-center gap-2">
              <input
                type="range"
                min={-2}
                max={2}
                step={0.05}
                value={entry.strength}
                aria-label={`Strength of adapter ${index + 1}`}
                onChange={(event) =>
                  set(
                    entries.map((e, n) => (n === index ? { ...e, strength: Number(event.target.value) } : e)),
                  )
                }
              />
              <span className="technical w-10 text-right text-[11px] text-muted">
                {entry.strength.toFixed(2)}
              </span>
            </div>
          </div>
        );
      })}
      <div className="flex gap-2">
        <button
          type="button"
          className="btn flex-1 text-xs"
          aria-expanded={adding}
          onClick={() => setAdding((a) => !a)}
        >
          <Icon name={adding ? "check" : "plus"} size={13} /> {adding ? "Done" : "Add adapter"}
        </button>
        {value !== null && (
          <button type="button" className="btn text-xs" onClick={() => onChange(null)}>
            Use preset's
          </button>
        )}
      </div>
      {adding && (
        <div
          className="max-h-48 divide-y divide-edge overflow-y-auto border border-edge"
          role="listbox"
          aria-label="Adapters to add"
        >
          {catalog === null && <div className="px-2 py-2 text-xs text-muted">Loading the LoRA library…</div>}
          {catalog !== null && addable.length === 0 && (
            <div className="px-2 py-2 text-xs text-muted">
              No verified adapters to add. Import one in Resources, or wait for the library scan.
            </div>
          )}
          {addable.map((l) => (
            <button
              key={l.path}
              type="button"
              role="option"
              aria-selected={false}
              className="block w-full px-2 py-1.5 text-left text-xs hover:bg-panel2"
              onClick={() => {
                set([...entries, { resource: l.resource_id as string, strength: l.recommended_weight ?? 1 }]);
                setAdding(false);
              }}
            >
              {l.label}
              {l.family && l.family !== "unknown" && (
                <span className="technical ml-2 text-white/40">{l.arch}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

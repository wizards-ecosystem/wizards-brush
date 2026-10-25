import { useMemo, useState } from "react";
import type { PresetSummary } from "../../api/types";
import { Icon } from "../icons";

const VIDEO_OPERATIONS = new Set(["text_to_video", "image_to_video", "long_video"]);

/** Who owns a preset, never how good it is: an AI-saved preset is custom. */
export function OriginBadge({ preset }: { preset: Pick<PresetSummary, "origin" | "client"> }) {
  if (preset.origin === "curated") {
    return (
      <span className="technical border border-accent/60 px-1.5 py-0.5 text-[9px] uppercase text-accent">
        Curated
      </span>
    );
  }
  const by = preset.client && preset.client !== "ui" ? ` · saved by ${preset.client}` : "";
  return (
    <span className="technical border border-edge px-1.5 py-0.5 text-[9px] uppercase text-muted">
      Custom{by}
    </span>
  );
}

/**
 * Choose a preset: search, a few useful tags, and image or video setups.
 * The list shows what each preset is for; whether the selected one is ready
 * here comes from the server's plan, not from this list.
 */
export function PresetPicker({
  presets,
  selected,
  onSelect,
  output,
}: {
  presets: PresetSummary[];
  selected: string | null;
  onSelect: (preset: PresetSummary) => void;
  output: "image" | "video";
}) {
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState<string | null>(null);
  const forOutput = useMemo(
    () => presets.filter((p) => (output === "video") === VIDEO_OPERATIONS.has(String(p.operation))),
    [presets, output],
  );
  const tags = useMemo(() => {
    const counts = new Map<string, number>();
    forOutput.forEach((p) => p.tags.forEach((t) => counts.set(t, (counts.get(t) || 0) + 1)));
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8)
      .map(([t]) => t);
  }, [forOutput]);
  const needle = query.trim().toLowerCase();
  const shown = forOutput.filter(
    (p) =>
      (!tag || p.tags.includes(tag)) &&
      (!needle || `${p.name} ${p.description} ${p.tags.join(" ")}`.toLowerCase().includes(needle)),
  );

  return (
    <div className="space-y-2" data-testid="preset-picker">
      <div className="relative">
        <Icon name="search" size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-muted" />
        <input
          className="input pl-7 text-xs"
          placeholder="Find a preset"
          aria-label="Find a preset"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      {tags.length > 0 && (
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter presets by tag">
          {tags.map((t) => (
            <button
              key={t}
              type="button"
              className={`chip text-[10px] ${tag === t ? "border-accent text-ink" : ""}`}
              aria-pressed={tag === t}
              onClick={() => setTag(tag === t ? null : t)}
            >
              {t}
            </button>
          ))}
        </div>
      )}
      <div
        className="max-h-64 divide-y divide-edge overflow-y-auto border border-edge"
        role="listbox"
        aria-label="Presets"
      >
        {shown.length === 0 && (
          <div className="px-3 py-4 text-xs text-muted">
            {forOutput.length === 0 ? `No ${output} presets yet.` : "No preset matches."}
          </div>
        )}
        {shown.map((p) => (
          <button
            key={p.id}
            type="button"
            role="option"
            aria-selected={p.id === selected}
            className={`block w-full px-3 py-2 text-left ${p.id === selected ? "bg-accent/12" : "hover:bg-panel2"}`}
            onClick={() => onSelect(p)}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-sm">{p.name}</span>
              <OriginBadge preset={p} />
            </div>
            {p.description && (
              <div className="mt-0.5 line-clamp-2 text-[11px] text-muted">{p.description}</div>
            )}
            {p.tags.length > 0 && (
              <div className="technical mt-1 truncate text-[9px] uppercase text-white/35">
                {p.tags.join(" · ")}
              </div>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}

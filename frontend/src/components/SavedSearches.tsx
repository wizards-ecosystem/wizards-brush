import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { AssetQuery, UserPreset } from "../api/types";
import { useStore } from "../store/useStore";
import { Icon } from "./icons";
import { ConfirmDialog, NameDialog } from "./ui";

/** The filter fields worth remembering. Deliberately excludes limit/offset —
 *  paging position is not part of what makes a search yours. */
export type SearchState = Pick<
  AssetQuery,
  "q" | "kind" | "favorite" | "min_rating" | "sort" | "collection_id"
>;

export function SavedSearches({
  current,
  onApply,
}: {
  current: SearchState;
  onApply: (s: SearchState) => void;
}) {
  const toast = useStore((s) => s.toast);
  const [items, setItems] = useState<UserPreset[]>([]);
  const [naming, setNaming] = useState(false);
  const [deleting, setDeleting] = useState<UserPreset | null>(null);

  const load = useCallback(
    () =>
      api
        .userPresets("search")
        .then(setItems)
        .catch(() => setItems([])),
    [],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // Nothing set means nothing worth saving — offering the button would produce
  // a "search" that just shows everything.
  const hasFilters = Object.values(current).some((v) => v !== undefined && v !== "" && v !== 0);

  const save = async (name: string) => {
    setNaming(false);
    try {
      await api.createPreset({ type: "search", name, payload: current });
      await load();
      toast(`Saved “${name}”`);
    } catch {
      toast("Couldn't save the search", "error");
    }
  };

  const remove = async (p: UserPreset) => {
    setDeleting(null);
    try {
      await api.deletePreset(p.id);
      await load();
    } catch {
      toast("Couldn't delete the saved search", "error");
    }
  };

  if (!items.length && !hasFilters) return null;

  return (
    <div className="flex flex-wrap items-center gap-2">
      {items.map((p) => (
        <span key={p.id} className="inline-flex items-center">
          <button
            className="chip"
            title="Apply this search"
            onClick={() => onApply(p.payload as SearchState)}
          >
            <Icon name="search" size={11} /> {p.name}
          </button>
          <button
            className="btn-ghost text-xs px-1"
            aria-label={`Delete saved search ${p.name}`}
            onClick={() => setDeleting(p)}
          >
            <Icon name="close" size={12} />
          </button>
        </span>
      ))}
      {hasFilters && (
        <button className="chip" onClick={() => setNaming(true)}>
          <Icon name="plus" size={11} /> Save this view
        </button>
      )}

      {naming && (
        <NameDialog
          title="Save search"
          placeholder="e.g. 5-star videos"
          onSubmit={save}
          onClose={() => setNaming(false)}
        />
      )}
      {deleting && (
        <ConfirmDialog
          title={`Delete “${deleting.name}”?`}
          message="Removes the saved search only."
          confirmLabel="Delete"
          onConfirm={() => remove(deleting)}
          onClose={() => setDeleting(null)}
        />
      )}
    </div>
  );
}

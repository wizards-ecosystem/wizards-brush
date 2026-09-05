import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { CollectionInfo } from "../api/types";
import { useStore } from "../store/useStore";
import { Icon } from "./icons";
import { ConfirmDialog, NameDialog } from "./ui";

/**
 * Collection chips above the gallery: scope the view, or file the current
 * selection into one.
 *
 * A collection is a view over assets. Deleting one removes the grouping and
 * nothing else — worth being explicit about in the confirm text, because
 * "delete" next to a pile of images reads as something much worse.
 */
export function CollectionBar({
  activeId,
  onSelect,
  selectedAssets,
  onChanged,
}: {
  activeId: number | null;
  onSelect: (id: number | null) => void;
  selectedAssets: number[];
  onChanged?: () => void;
}) {
  const toast = useStore((s) => s.toast);
  const [items, setItems] = useState<CollectionInfo[]>([]);
  const [naming, setNaming] = useState(false);
  const [deleting, setDeleting] = useState<CollectionInfo | null>(null);

  const load = useCallback(
    () =>
      api
        .collections()
        .then(setItems)
        .catch(() => setItems([])),
    [],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const create = async (name: string) => {
    setNaming(false);
    try {
      const c = await api.createCollection(name);
      // Creating one while assets are selected is almost always "put these in it".
      if (selectedAssets.length) {
        const r = await api.addToCollection(c.id, selectedAssets);
        toast(`Created “${c.name}” with ${r.added} item${r.added === 1 ? "" : "s"}`);
      } else {
        toast(`Created “${c.name}”`);
      }
      await load();
      onChanged?.();
    } catch {
      toast("Couldn't create the collection", "error");
    }
  };

  const fileSelection = async (c: CollectionInfo) => {
    try {
      const r = await api.addToCollection(c.id, selectedAssets);
      toast(r.added ? `Added ${r.added} to “${c.name}”` : `Already in “${c.name}”`);
      await load();
      onChanged?.();
    } catch {
      toast("Couldn't add to the collection", "error");
    }
  };

  const remove = async (c: CollectionInfo) => {
    setDeleting(null);
    try {
      await api.deleteCollection(c.id);
      if (activeId === c.id) onSelect(null);
      await load();
      toast(`Deleted “${c.name}” — the images are untouched`);
    } catch {
      toast("Couldn't delete the collection", "error");
    }
  };

  if (!items.length && !selectedAssets.length) {
    return (
      <div className="flex items-center gap-2">
        <button className="chip" onClick={() => setNaming(true)}>
          <Icon name="plus" size={11} /> New collection
        </button>
        {naming && (
          <NameDialog
            title="New collection"
            placeholder="e.g. Lighthouse studies"
            onSubmit={create}
            onClose={() => setNaming(false)}
          />
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        className={`chip ${activeId === null ? "chip-active" : ""}`}
        onClick={() => onSelect(null)}
        aria-pressed={activeId === null}
      >
        All
      </button>
      {items.map((c) => (
        <span key={c.id} className="inline-flex items-center">
          <button
            className={`chip ${activeId === c.id ? "chip-active" : ""}`}
            onClick={() => (selectedAssets.length ? fileSelection(c) : onSelect(c.id))}
            title={
              selectedAssets.length
                ? `Add ${selectedAssets.length} selected to “${c.name}”`
                : `Show only “${c.name}”`
            }
            aria-pressed={!selectedAssets.length && activeId === c.id}
          >
            {c.name} <span className="text-white/40">{c.count}</span>
          </button>
          <button
            className="btn-ghost text-xs px-1"
            aria-label={`Delete collection ${c.name}`}
            onClick={() => setDeleting(c)}
          >
            <Icon name="close" size={12} />
          </button>
        </span>
      ))}
      <button className="chip" onClick={() => setNaming(true)}>
        <Icon name="plus" size={11} />{" "}
        {selectedAssets.length ? `New from ${selectedAssets.length} selected` : "New collection"}
      </button>

      {naming && (
        <NameDialog
          title="New collection"
          placeholder="e.g. Lighthouse studies"
          onSubmit={create}
          onClose={() => setNaming(false)}
        />
      )}
      {deleting && (
        <ConfirmDialog
          title={`Delete “${deleting.name}”?`}
          message="This removes the collection only. Every image in it stays in your gallery."
          confirmLabel="Delete collection"
          onConfirm={() => remove(deleting)}
          onClose={() => setDeleting(null)}
        />
      )}
    </div>
  );
}

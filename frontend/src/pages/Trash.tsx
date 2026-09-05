import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { Asset } from "../api/types";
import { AssetCard } from "../components/AssetCard";
import { Icon } from "../components/icons";
import { Button, ConfirmDialog, EmptyState, PageHeader, Spinner } from "../components/ui";
import { useStore } from "../store/useStore";

/**
 * Deleted assets, recoverable until purged.
 *
 * Deletion used to unlink immediately, so one misclick on a multi-select was
 * unrecoverable. Everything here is reversible except the explicit purge.
 */
export function Trash() {
  const toast = useStore((s) => s.toast);
  const refreshAssets = useStore((s) => s.refreshAssets);
  const refreshStats = useStore((s) => s.refreshStats);
  const [items, setItems] = useState<Asset[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirming, setConfirming] = useState<"selected" | "all" | null>(null);

  // setLoading stays out of the effect body: React 19 flags a synchronous
  // setState there as a cascading render. The initial state already says
  // "loading", so the fetch only ever has to report the result.
  const load = useCallback(
    () =>
      api
        .trash()
        .then((page) => setItems(page.items))
        .catch(() => toast("Couldn't load the trash", "error"))
        .finally(() => setLoading(false)),
    [toast],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = useCallback(
    (id: number) =>
      setSelected((s) => {
        const next = new Set(s);
        if (!next.delete(id)) next.add(id);
        return next;
      }),
    [],
  );

  const after = (msg: string) => {
    setSelected(new Set());
    load();
    refreshAssets();
    refreshStats();
    toast(msg);
  };

  const restore = async (ids: number[]) => {
    try {
      const r = await api.restoreAssets(ids);
      after(`Restored ${r.restored} item${r.restored === 1 ? "" : "s"}`);
    } catch {
      toast("Restore failed", "error");
    }
  };

  const purge = async (ids: number[]) => {
    try {
      const r = await api.purgeTrash(ids);
      after(`Permanently deleted ${r.purged} item${r.purged === 1 ? "" : "s"}`);
    } catch {
      toast("Purge failed", "error");
    }
  };

  if (loading) {
    return (
      <div className="page-pad flex items-center gap-3 text-sm text-muted">
        <Spinner /> Loading Trash…
      </div>
    );
  }

  return (
    <div className="page-shell page-pad mx-auto max-w-[1800px]">
      <PageHeader
        kicker="Recoverable storage"
        title="Trash"
        meta={`${items.length} items`}
        description="Gallery deletions remain recoverable here until you explicitly purge their files."
        actions={
          items.length ? (
            <Button variant="danger" icon="trash" onClick={() => setConfirming("all")}>
              Empty Trash
            </Button>
          ) : undefined
        }
      />

      {items.length > 0 ? (
        <div className="mt-6 grid grid-cols-2 gap-2 opacity-85 sm:grid-cols-3 md:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8">
          {items.map((a) => (
            <AssetCard
              key={a.id}
              asset={a}
              selectable
              selected={selected.has(a.id)}
              onClick={(x) => toggle(x.id)}
              onToggleSelect={(x) => toggle(x.id)}
            />
          ))}
        </div>
      ) : (
        <div className="flex min-h-[480px] items-center justify-center">
          <EmptyState
            icon="trash"
            title="Trash is empty"
            description="There is nothing waiting for recovery or permanent deletion."
          />
        </div>
      )}

      {selected.size > 0 && (
        <div className="action-shelf fixed bottom-[calc(72px+env(safe-area-inset-bottom))] left-1/2 z-30 flex w-[min(560px,calc(100vw-24px))] -translate-x-1/2 items-center gap-2 px-3 py-2 lg:bottom-5">
          <span className="technical mr-1 text-xs">{selected.size} selected</span>
          <Button size="sm" icon="restore" onClick={() => restore([...selected])}>
            Restore
          </Button>
          <Button size="sm" variant="danger" icon="trash" onClick={() => setConfirming("selected")}>
            Delete permanently
          </Button>
          <button
            className="btn-ghost ml-auto"
            aria-label="Clear selection"
            onClick={() => setSelected(new Set())}
          >
            <Icon name="close" size={15} />
          </button>
        </div>
      )}

      {confirming && (
        <ConfirmDialog
          title={confirming === "all" ? "Empty the trash?" : "Delete permanently?"}
          message={
            confirming === "all"
              ? `Permanently delete all ${items.length} item${items.length === 1 ? "" : "s"} and their files. This cannot be undone.`
              : `Permanently delete ${selected.size} item${selected.size === 1 ? "" : "s"} and their files. This cannot be undone.`
          }
          confirmLabel="Delete permanently"
          onConfirm={() => {
            const ids = confirming === "all" ? [] : [...selected];
            setConfirming(null);
            purge(ids);
          }}
          onClose={() => setConfirming(null)}
        />
      )}
    </div>
  );
}

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { Asset, AssetQuery } from "../api/types";
import { AssetCard } from "../components/AssetCard";
import { AssetModal } from "../components/AssetModal";
import { CollectionBar } from "../components/CollectionBar";
import { CompareModal } from "../components/CompareModal";
import { Icon } from "../components/icons";
import { SavedSearches, type SearchState } from "../components/SavedSearches";
import { Button, ConfirmDialog, EmptyState, IconButton, Modal, PageHeader, Skeleton } from "../components/ui";
import { useStore } from "../store/useStore";

const PAGE = 60;

export function Gallery() {
  const refreshAssets = useStore((state) => state.refreshAssets);
  const refreshStats = useStore((state) => state.refreshStats);
  const toast = useStore((state) => state.toast);
  const assetRevision = useStore((state) => state.assetRevision);
  const [items, setItems] = useState<Asset[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Asset | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [kind, setKind] = useState<"" | "image" | "video">("");
  const [search, setSearch] = useState("");
  const [favoriteOnly, setFavoriteOnly] = useState(false);
  const [minimumRating, setMinimumRating] = useState(0);
  const [sort, setSort] = useState("newest");
  const [compare, setCompare] = useState<[Asset, Asset] | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [collectionId, setCollectionId] = useState<number | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);

  const query = useCallback(
    (offset: number): AssetQuery => ({
      limit: PAGE,
      offset,
      sort,
      q: search || undefined,
      kind: kind || undefined,
      favorite: favoriteOnly || undefined,
      min_rating: minimumRating || undefined,
      collection_id: collectionId ?? undefined,
    }),
    [sort, search, kind, favoriteOnly, minimumRating, collectionId],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.assets(query(0));
      setItems(page.items);
      setTotal(page.total);
    } catch (caught) {
      setError(String(caught));
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    const timer = setTimeout(() => {
      load();
      setSelected(new Set());
    }, 200);
    return () => clearTimeout(timer);
  }, [load]);

  // Gallery owns a filtered, paginated query, so the store's recent-assets
  // snapshot cannot be spliced into it safely. The socket increments this
  // invalidation signal for every committed batch item; debounce consecutive
  // cells and re-run the authoritative query without clearing user selection.
  useEffect(() => {
    if (!assetRevision) return;
    const timer = setTimeout(load, 200);
    return () => clearTimeout(timer);
  }, [assetRevision, load]);

  const loadMore = async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.assets(query(items.length));
      setItems((previous) => {
        const seen = new Set(previous.map((asset) => asset.id));
        return [...previous, ...page.items.filter((asset) => !seen.has(asset.id))];
      });
      setTotal(page.total);
    } catch (caught) {
      toast(`Failed to load more: ${caught}`, "error");
    } finally {
      setLoadingMore(false);
    }
  };

  const toggleSelected = useCallback(
    (asset: Asset) =>
      setSelected((current) => {
        const next = new Set(current);
        if (next.has(asset.id)) next.delete(asset.id);
        else next.add(asset.id);
        return next;
      }),
    [],
  );

  const deleteSelection = async () => {
    try {
      await api.bulkDelete([...selected]);
      setSelected(new Set());
      await Promise.all([load(), refreshAssets(), refreshStats()]);
      toast("Moved selection to Trash", "info");
    } catch (caught) {
      toast(`Delete failed: ${caught}`, "error");
    }
  };

  const exportSelection = async () => {
    try {
      await api.exportZip([...selected]);
      toast("Exported zip", "success");
    } catch (caught) {
      toast(`Export failed: ${caught}`, "error");
    }
  };

  const hasFilters = !!(
    search ||
    kind ||
    favoriteOnly ||
    minimumRating ||
    collectionId != null ||
    sort !== "newest"
  );
  const clearFilters = () => {
    setSearch("");
    setKind("");
    setFavoriteOnly(false);
    setMinimumRating(0);
    setSort("newest");
    setCollectionId(null);
  };

  const applySearch = (state: SearchState) => {
    setSearch(state.q ?? "");
    setKind((state.kind as "" | "image" | "video") ?? "");
    setFavoriteOnly(Boolean(state.favorite));
    setMinimumRating(state.min_rating ?? 0);
    setSort(state.sort ?? "newest");
    setCollectionId(state.collection_id ?? null);
  };

  const renderRefinements = (footer?: ReactNode) => (
    <div className="space-y-5">
      <div>
        <div className="label">Media type</div>
        <div className="flex gap-1 border border-edge bg-panel p-1" role="group" aria-label="Media type">
          {(["", "image", "video"] as const).map((value) => (
            <button
              key={value || "all"}
              className={`seg ${kind === value ? "seg-active" : ""}`}
              aria-pressed={kind === value}
              onClick={() => setKind(value)}
            >
              {value || "All"}
            </button>
          ))}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="label" htmlFor="gallery-rating">
            Rating
          </label>
          <select
            id="gallery-rating"
            className="input"
            value={minimumRating}
            onChange={(event) => setMinimumRating(Number(event.target.value))}
          >
            <option value={0}>Any rating</option>
            {[1, 2, 3, 4, 5].map((rating) => (
              <option key={rating} value={rating}>
                {rating}+ stars
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="gallery-sort">
            Order
          </label>
          <select
            id="gallery-sort"
            className="input"
            value={sort}
            onChange={(event) => setSort(event.target.value)}
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="rating">Top rated</option>
            <option value="favorite">Favorites first</option>
            {/* Behaviour rather than intent: what you actually exported,
                downloaded or built on, which is not always what you starred. */}
            <option value="used">Most used</option>
            <option value="recently_used">Recently used</option>
          </select>
        </div>
      </div>
      <button
        className={`flex w-full items-center justify-between border-y border-edge py-3 text-sm ${favoriteOnly ? "text-accent" : "text-muted hover:text-ink"}`}
        onClick={() => setFavoriteOnly((value) => !value)}
        aria-pressed={favoriteOnly}
      >
        <span className="flex items-center gap-2">
          <Icon name="heart" size={16} /> Favorites only
        </span>
        <span
          className={`h-5 w-9 rounded-full p-0.5 transition-colors ${favoriteOnly ? "bg-accent" : "bg-edge"}`}
        >
          <span
            className={`block size-4 rounded-full bg-bg transition-transform ${favoriteOnly ? "translate-x-4" : ""}`}
          />
        </span>
      </button>
      {footer}
    </div>
  );

  return (
    <div className="page-shell page-pad mx-auto max-w-[1800px]">
      <PageHeader
        kicker="Library"
        title="Gallery"
        meta={`${total} items`}
        description="Search the work, build collections, and send promising material back through the workshop."
      />

      <div className="sticky top-0 z-20 -mx-4 mt-5 border-y border-edge bg-bg/96 px-4 py-3 sm:-mx-6 sm:px-6 xl:-mx-8 xl:px-8">
        <div className="flex items-center gap-2">
          <label className="relative min-w-0 flex-1">
            <span className="sr-only">Search gallery</span>
            <Icon
              name="search"
              size={17}
              className="pointer-events-none absolute left-3 top-1/2 z-10 -translate-y-1/2 text-muted"
            />
            <input
              className="input h-11 pl-10 text-sm"
              placeholder="Search prompts, captions, or tags"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <Button icon="filter" className="lg:hidden" onClick={() => setFiltersOpen(true)}>
            Refine {hasFilters && <span className="size-1.5 rounded-full bg-accent" />}
          </Button>
          <div className="hidden items-center gap-2 lg:flex">
            {(["", "image", "video"] as const).map((value) => (
              <button
                key={value || "all"}
                className={`btn text-xs ${kind === value ? "border-accent text-ink" : ""}`}
                onClick={() => setKind(value)}
                aria-pressed={kind === value}
              >
                {value ? <Icon name={value} size={14} /> : <Icon name="gallery" size={14} />}
                {value || "All"}
              </button>
            ))}
            <button
              className={`btn text-xs ${favoriteOnly ? "border-accent text-accent" : ""}`}
              onClick={() => setFavoriteOnly((value) => !value)}
              aria-pressed={favoriteOnly}
            >
              <Icon name="heart" size={14} /> Favorites
            </button>
            <label>
              <span className="sr-only">Minimum rating</span>
              <select
                className="input w-auto text-xs"
                value={minimumRating}
                onChange={(event) => setMinimumRating(Number(event.target.value))}
              >
                <option value={0}>Any rating</option>
                {[1, 2, 3, 4, 5].map((rating) => (
                  <option key={rating} value={rating}>
                    {rating}+ stars
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span className="sr-only">Sort order</span>
              <select
                className="input w-auto text-xs"
                value={sort}
                onChange={(event) => setSort(event.target.value)}
              >
                <option value="newest">Newest</option>
                <option value="oldest">Oldest</option>
                <option value="rating">Top rated</option>
                <option value="favorite">Favorites</option>
                <option value="used">Most used</option>
                <option value="recently_used">Recently used</option>
              </select>
            </label>
          </div>
          <IconButton
            icon="check"
            label="Select all shown assets"
            disabled={!items.length}
            onClick={() => setSelected(new Set(items.map((asset) => asset.id)))}
          />
        </div>
      </div>

      <div className="mt-4 flex min-w-0 gap-5 overflow-x-auto border-b border-edge pb-3">
        <SavedSearches
          current={{
            q: search || undefined,
            kind: kind || undefined,
            favorite: favoriteOnly || undefined,
            min_rating: minimumRating || undefined,
            sort,
            collection_id: collectionId ?? undefined,
          }}
          onApply={applySearch}
        />
        <CollectionBar
          activeId={collectionId}
          onSelect={setCollectionId}
          selectedAssets={[...selected]}
          onChanged={load}
        />
      </div>

      <div className="mt-5">
        {loading ? (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8">
            {Array.from({ length: 16 }).map((_, index) => (
              <Skeleton key={index} className="aspect-square" />
            ))}
          </div>
        ) : error ? (
          <div className="flex min-h-[420px] items-center justify-center">
            <EmptyState
              icon="alert"
              title="The gallery could not be loaded"
              description={error}
              action={
                <Button icon="refresh" onClick={load}>
                  Try again
                </Button>
              }
            />
          </div>
        ) : items.length === 0 ? (
          <div className="flex min-h-[420px] items-center justify-center">
            <EmptyState
              icon={hasFilters ? "search" : "image"}
              title={hasFilters ? "No work matches this view" : "The gallery is waiting"}
              description={
                hasFilters
                  ? "Clear or loosen the current refinements to bring more work into view."
                  : "Generated images and videos will collect here automatically."
              }
              action={hasFilters ? <Button onClick={clearFilters}>Clear refinements</Button> : undefined}
            />
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8">
              {items.map((asset) => (
                <AssetCard
                  key={asset.id}
                  asset={asset}
                  onClick={setOpen}
                  selectable
                  selected={selected.has(asset.id)}
                  onToggleSelect={toggleSelected}
                />
              ))}
            </div>
            {items.length < total && (
              <div className="flex justify-center py-8">
                <Button loading={loadingMore} onClick={loadMore}>
                  {loadingMore ? "Loading…" : `Load ${Math.min(PAGE, total - items.length)} more`}
                </Button>
              </div>
            )}
          </>
        )}
      </div>

      {selected.size > 0 && (
        <div className="action-shelf fixed bottom-[calc(72px+env(safe-area-inset-bottom))] left-1/2 z-30 flex w-[min(720px,calc(100vw-24px))] -translate-x-1/2 flex-wrap items-center gap-2 px-3 py-2 lg:bottom-5">
          <span className="technical mr-1 text-xs text-ink">{selected.size} selected</span>
          <Button size="sm" icon="download" onClick={exportSelection}>
            Export zip
          </Button>
          {selected.size === 2 && (
            <Button
              size="sm"
              icon="compare"
              onClick={() => {
                const pair = items.filter((asset) => selected.has(asset.id));
                if (pair.length === 2) setCompare([pair[0], pair[1]]);
              }}
            >
              Compare
            </Button>
          )}
          <Button size="sm" variant="danger" icon="trash" onClick={() => setConfirmingDelete(true)}>
            Move to Trash
          </Button>
          <Button size="sm" variant="quiet" className="ml-auto" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
        </div>
      )}

      {filtersOpen && (
        <Modal
          size="fullscreen"
          label="Gallery refinements"
          onClose={() => setFiltersOpen(false)}
          className="flex-col justify-end bg-black/55 shadow-none"
        >
          <div className="max-h-[85dvh] overflow-y-auto rounded-t-[14px] border-t border-edge bg-panel p-5 pop-in">
            <div className="mb-5 flex items-center justify-between">
              <div>
                <div className="page-kicker">Gallery</div>
                <h2 className="text-lg font-semibold">Refine this view</h2>
              </div>
              <IconButton icon="close" label="Close refinements" onClick={() => setFiltersOpen(false)} />
            </div>
            {renderRefinements(
              <div className="flex gap-2 border-t border-edge pt-4">
                <Button variant="quiet" className="flex-1" onClick={clearFilters}>
                  Reset
                </Button>
                <Button variant="primary" className="flex-1" onClick={() => setFiltersOpen(false)}>
                  Show results
                </Button>
              </div>,
            )}
          </div>
        </Modal>
      )}

      {open && (
        <AssetModal
          asset={open}
          list={items}
          onNavigate={setOpen}
          onClose={() => {
            setOpen(null);
            load();
          }}
        />
      )}
      {compare && <CompareModal a={compare[0]} b={compare[1]} onClose={() => setCompare(null)} />}
      {confirmingDelete && (
        <ConfirmDialog
          title={`Move ${selected.size} selected item${selected.size === 1 ? "" : "s"} to Trash?`}
          message="The files stay on disk and can be restored from Trash until they are permanently purged."
          confirmLabel="Move to Trash"
          onConfirm={deleteSelection}
          onClose={() => setConfirmingDelete(false)}
        />
      )}
    </div>
  );
}

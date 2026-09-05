import { memo } from "react";
import type { Asset } from "../api/types";
import { Icon } from "./icons";

function AssetCardImpl({
  asset,
  onClick,
  selectable,
  selected,
  onToggleSelect,
}: {
  asset: Asset;
  onClick?: (asset: Asset) => void;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (asset: Asset) => void;
}) {
  const description = asset.meta?.prompt || asset.generator || asset.filename;
  return (
    <article
      className={`media-tile group aspect-square transition-transform duration-150 ${selected ? "media-tile-selected" : "hover:-translate-y-0.5"}`}
      style={{ contentVisibility: "auto", containIntrinsicSize: "180px 180px" }}
    >
      <img src={asset.thumb_url || asset.url} alt="" className="h-full w-full object-cover" loading="lazy" />
      <button
        className="absolute inset-0 z-[1] text-left"
        onClick={() => onClick?.(asset)}
        aria-label={`Open ${asset.kind}: ${description}`}
        title={description}
      />

      <div className="pointer-events-none absolute inset-x-0 bottom-0 z-[2] translate-y-full border-t border-white/10 bg-black/76 px-2 py-1.5 transition-transform duration-150 group-hover:translate-y-0 group-focus-within:translate-y-0">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-[10px] text-white/88">{asset.generator}</span>
          {asset.rating > 0 && (
            <span className="flex shrink-0 items-center gap-0.5 text-[9px] text-warn">
              <Icon name="star" size={11} />
              {asset.rating}
            </span>
          )}
        </div>
      </div>

      <div className="pointer-events-none absolute left-2 top-2 z-[2] flex items-center gap-1.5">
        {asset.favorite && (
          <span className="flex size-6 items-center justify-center rounded-[5px] bg-black/70 text-accent">
            <Icon name="heart" size={14} />
          </span>
        )}
        {asset.kind === "video" && (
          <span className="flex size-6 items-center justify-center rounded-[5px] bg-black/70 text-white">
            <Icon name="video" size={14} />
          </span>
        )}
        {/* Both tiles stay visible. If you deliberately reran something, seeing
            it vanish from the gallery reads as a bug — telling you two results
            came out identical is useful, hiding one of them is not. Each copy is
            still its own file on disk, so deleting one leaves the other whole. */}
        {(asset.copies ?? 1) > 1 && (
          <span
            className="technical flex h-6 items-center gap-1 rounded-[5px] bg-black/70 px-1.5 text-[10px] text-white/80"
            title={`${asset.copies} images in your gallery are pixel-identical to this one`}
          >
            <Icon name="copy" size={11} />
            {asset.copies}
          </span>
        )}
        {asset.is_missing && (
          <span
            className="flex size-6 items-center justify-center rounded-[5px] bg-black/70 text-warn"
            title="The file for this asset is missing from disk"
          >
            <Icon name="alert" size={14} />
          </span>
        )}
      </div>

      {selectable && (
        <button
          className={`absolute right-2 top-2 z-[3] flex size-7 items-center justify-center rounded-[5px] border transition-all ${
            selected
              ? "border-accent bg-accent text-[#17100d]"
              : "border-white/40 bg-black/66 text-transparent opacity-100 hover:border-white hover:text-white md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100"
          }`}
          onClick={(event) => {
            event.stopPropagation();
            onToggleSelect?.(asset);
          }}
          aria-label={selected ? "Deselect asset" : "Select asset"}
          aria-pressed={selected}
        >
          <Icon name="check" size={14} />
        </button>
      )}

      {selected && (
        <>
          <span className="pointer-events-none absolute bottom-0 left-0 z-[3] h-8 w-[3px] bg-accent" />
          <span className="pointer-events-none absolute bottom-0 left-0 z-[3] h-[3px] w-8 bg-accent" />
        </>
      )}
    </article>
  );
}

export const AssetCard = memo(AssetCardImpl);

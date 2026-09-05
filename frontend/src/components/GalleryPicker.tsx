import { useStore } from "../store/useStore";
import type { Asset } from "../api/types";
import { AssetCard } from "./AssetCard";
import { IconButton, Modal, EmptyState } from "./ui";

/** Modal grid of existing image assets; click one to use it as an input. */
export function GalleryPicker({
  onPick,
  onClose,
  title = "Choose an image from your gallery",
}: {
  onPick: (a: Asset) => void;
  onClose: () => void;
  title?: string;
}) {
  const images = useStore((s) => s.assets).filter((a) => a.kind === "image");

  return (
    <Modal onClose={onClose} size="wide" label={title}>
      <div className="flex items-center justify-between border-b border-edge p-4">
        <div>
          <div className="page-kicker">Gallery input</div>
          <div className="font-semibold">{title}</div>
        </div>
        <IconButton icon="close" label="Close gallery picker" onClick={onClose} />
      </div>
      <div className="p-4 overflow-y-auto">
        {images.length === 0 ? (
          <div className="flex min-h-72 items-center justify-center">
            <EmptyState
              icon="image"
              title="No images available"
              description="Generate an image first, then it can be reused as source material here."
            />
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6">
            {images.map((asset) => (
              <AssetCard key={asset.id} asset={asset} onClick={onPick} />
            ))}
          </div>
        )}
      </div>
    </Modal>
  );
}

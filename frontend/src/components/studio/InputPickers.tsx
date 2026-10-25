import { useEffect, useRef, useState } from "react";
import { api, authHeaders } from "../../api/client";
import type { Asset, Operation, PresetDefinition } from "../../api/types";
import { useStore } from "../../store/useStore";
import { GalleryPicker } from "../GalleryPicker";
import { Icon } from "../icons";
import { MaskEditor } from "../MaskEditor";
import { Button } from "../ui";

/** How many source images an operation takes before any reference roles. */
export const SOURCE_IMAGES: Record<Operation, [number, number]> = {
  text_to_image: [0, 0],
  image_to_image: [1, 1],
  inpaint: [1, 1],
  outpaint: [1, 1],
  edit: [0, 3],
  control: [1, 1],
  text_to_video: [0, 0],
  image_to_video: [1, 1],
  long_video: [0, 0],
};

function useAsset(id: number | null): Asset | null {
  const recent = useStore((s) => s.assets);
  const cached = id == null ? null : (recent.find((a) => a.id === id) ?? null);
  const [fetched, setFetched] = useState<Asset | null>(null);
  useEffect(() => {
    if (id == null || cached) return;
    let alive = true;
    api
      .asset(id)
      .then((a) => alive && setFetched(a))
      .catch(() => alive && setFetched(null));
    return () => {
      alive = false;
    };
  }, [id, cached]);
  return cached ?? (fetched?.id === id ? fetched : null);
}

function Thumb({ id, onRemove, label }: { id: number; onRemove: () => void; label: string }) {
  const asset = useAsset(id);
  return (
    <div className="relative size-16 shrink-0 border border-edge bg-black">
      {asset?.thumb_url || asset?.url ? (
        <img src={asset.thumb_url ?? asset.url} alt={label} className="size-full object-contain" />
      ) : (
        <span className="technical flex size-full items-center justify-center text-[10px] text-muted">
          #{id}
        </span>
      )}
      <button
        type="button"
        className="absolute right-0 top-0 bg-black/70 p-0.5"
        aria-label={`Remove ${label}`}
        onClick={onRemove}
      >
        <Icon name="close" size={10} />
      </button>
    </div>
  );
}

/** Pick from the gallery, or upload a file into it; either way an asset id. */
function AddImage({
  label,
  onAdd,
  mask = false,
}: {
  label: string;
  onAdd: (id: number) => void;
  mask?: boolean;
}) {
  const [picking, setPicking] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const toast = useStore((s) => s.toast);
  return (
    <div className="flex gap-1">
      <Button size="sm" type="button" onClick={() => setPicking(true)}>
        {label}
      </Button>
      <Button
        size="sm"
        type="button"
        icon="arrow-up"
        aria-label={`Upload ${label.toLowerCase()}`}
        onClick={() => fileRef.current?.click()}
      >
        Upload
      </Button>
      <input
        ref={fileRef}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (!file) return;
          api
            .importAsset(file, { role: mask ? "mask" : "source" })
            .then((asset) => onAdd(asset.id))
            .catch((error) => toast(`Couldn't import: ${error}`, "error"));
        }}
      />
      {picking && (
        <GalleryPicker
          title={mask ? "Choose a mask" : "Choose an image"}
          filter={(a) => (mask ? a.generator === "mask" : a.generator !== "mask")}
          onClose={() => setPicking(false)}
          onPick={(asset) => {
            onAdd(asset.id);
            setPicking(false);
          }}
        />
      )}
    </div>
  );
}

/** Paint a mask on the chosen source and store it as a mask asset: the same
 *  white-means-change convention an API or MCP client uploads. */
function DrawMask({ sourceId, onMask }: { sourceId: number; onMask: (id: number) => void }) {
  const source = useAsset(sourceId);
  const [file, setFile] = useState<File | null>(null);
  const [painted, setPainted] = useState<File | null>(null);
  const toast = useStore((s) => s.toast);
  useEffect(() => {
    if (!source) return;
    const controller = new AbortController();
    fetch(`/api/assets/${source.id}/file`, { headers: authHeaders(), signal: controller.signal })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(`${r.status}`))))
      .then((blob) => setFile(new File([blob], source.filename, { type: blob.type || "image/png" })))
      .catch(() => {});
    return () => controller.abort();
  }, [source]);
  if (!file) return <div className="text-xs text-muted">Loading the image to paint on…</div>;
  return (
    <div className="space-y-2">
      <MaskEditor image={file} onMask={setPainted} />
      <Button
        size="sm"
        type="button"
        disabled={!painted}
        onClick={() =>
          painted &&
          api
            .importAsset(painted, { role: "mask" })
            .then((asset) => onMask(asset.id))
            .catch((error) => toast(`Couldn't save the mask: ${error}`, "error"))
        }
      >
        Use this mask
      </Button>
    </div>
  );
}

export function InputPickers({
  operation,
  definition,
  images,
  mask,
  references,
  onImages,
  onMask,
  onReferences,
}: {
  operation: Operation;
  definition: PresetDefinition | null;
  images: number[];
  mask: number | null;
  references: Record<string, number[]>;
  onImages: (ids: number[]) => void;
  onMask: (id: number | null) => void;
  onReferences: (refs: Record<string, number[]>) => void;
}) {
  const [, max] = SOURCE_IMAGES[operation];
  const roles = definition?.references ?? [];
  const [drawing, setDrawing] = useState(false);
  if (max === 0 && roles.length === 0) return null;
  return (
    <div className="space-y-3" data-testid="input-pickers">
      {max > 0 && (
        <div>
          <div className="label">{max > 1 ? `Source images (up to ${max})` : "Source image"}</div>
          <div className="flex flex-wrap gap-2">
            {images.map((id, index) => (
              <Thumb
                key={`${id}-${index}`}
                id={id}
                label={`source ${index + 1}`}
                onRemove={() => {
                  onImages(images.filter((_, n) => n !== index));
                  if (index === 0) onMask(null); // a mask belongs to the image it was painted on
                }}
              />
            ))}
          </div>
          {images.length < max && (
            <div className="mt-2">
              <AddImage label="Choose image" onAdd={(id) => onImages([...images, id])} />
            </div>
          )}
        </div>
      )}
      {operation === "inpaint" && images.length > 0 && (
        <div>
          <div className="label">Mask · white marks what changes</div>
          {mask !== null ? (
            <Thumb id={mask} label="mask" onRemove={() => onMask(null)} />
          ) : drawing ? (
            <DrawMask
              sourceId={images[0]}
              onMask={(id) => {
                onMask(id);
                setDrawing(false);
              }}
            />
          ) : (
            <div className="flex flex-wrap gap-1">
              <AddImage label="Choose mask" mask onAdd={(id) => onMask(id)} />
              <Button size="sm" type="button" onClick={() => setDrawing(true)}>
                Draw
              </Button>
            </div>
          )}
        </div>
      )}
      {roles.map((role) => {
        const ids = references[role.role] ?? [];
        const maxRefs = role.max ?? 1;
        return (
          <div key={role.role}>
            <div className="label">
              {role.label || role.role}
              <span className="technical ml-1 text-[10px] text-muted">
                {role.min ?? 0}–{maxRefs}
              </span>
            </div>
            <div className="flex flex-wrap gap-2">
              {ids.map((id, index) => (
                <Thumb
                  key={`${id}-${index}`}
                  id={id}
                  label={`${role.role} ${index + 1}`}
                  onRemove={() =>
                    onReferences({ ...references, [role.role]: ids.filter((_, n) => n !== index) })
                  }
                />
              ))}
            </div>
            {ids.length < maxRefs && (
              <div className="mt-2">
                <AddImage
                  label={`Add ${role.label || role.role}`}
                  onAdd={(id) => onReferences({ ...references, [role.role]: [...ids, id] })}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

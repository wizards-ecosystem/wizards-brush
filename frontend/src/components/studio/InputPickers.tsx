import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, authHeaders } from "../../api/client";
import type {
  Asset,
  Operation,
  PresetDefinition,
  ReferenceSetRead,
  ReferenceSetSummary,
} from "../../api/types";
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

type Role = NonNullable<PresetDefinition["references"]>[number];

/** The chosen set's entries for one role, as the server will bind them. */
function SetPreview({ setId, role, min, max }: { setId: number; role: string; min: number; max: number }) {
  const [found, setFound] = useState<ReferenceSetRead | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    api
      .referenceSet(setId)
      .then((read) => {
        if (!alive) return;
        setFound(read);
        setError("");
      })
      .catch((e) => alive && setError(`${e}`));
    return () => {
      alive = false;
    };
  }, [setId]);
  if (error) return <div className="text-[11px] text-danger">{error}</div>;
  if (!found) return <div className="text-[11px] text-muted">Reading the set…</div>;
  const entries = found.definition.roles.find((r) => r.role === role)?.entries ?? [];
  const fits = entries.length >= min && entries.length <= max;
  return (
    <div className="space-y-1" data-testid={`set-preview-${role}`}>
      <div className="flex flex-wrap gap-2">
        {entries.map((entry, index) => (
          <SetThumb key={`${entry.asset_id}-${index}`} id={entry.asset_id} masked={Boolean(entry.mask_id)} />
        ))}
      </div>
      <div className={`text-[11px] ${fits ? "text-muted" : "text-danger"}`}>
        {found.name} · revision {found.revision} · {entries.length} reference{entries.length === 1 ? "" : "s"}
        {!fits && ` — this role takes ${min === max ? min : `${min}–${max}`}; the set is not cut to fit`}
      </div>
    </div>
  );
}

function SetThumb({ id, masked }: { id: number; masked: boolean }) {
  const asset = useAsset(id);
  return (
    <div className="relative size-12 shrink-0 border border-edge bg-black">
      {asset?.thumb_url || asset?.url ? (
        <img src={asset.thumb_url ?? asset.url} alt="" className="size-full object-contain" />
      ) : (
        <span className="technical flex size-full items-center justify-center text-[9px] text-muted">
          #{id}
        </span>
      )}
      {masked && (
        <span className="technical absolute bottom-0 left-0 bg-black/70 px-0.5 text-[8px] uppercase">
          mask
        </span>
      )}
    </div>
  );
}

/**
 * One reference role: images picked here, or a reference set. A role the
 * preset binds to a set uses it until the person picks images or another
 * set; nothing is mixed, and a set larger than the role is refused by the
 * server rather than trimmed.
 */
function RolePicker({
  role,
  sets,
  ids,
  chosenSet,
  onIds,
  onSet,
}: {
  role: Role;
  sets: ReferenceSetSummary[];
  ids: number[];
  chosenSet: number | null;
  onIds: (ids: number[]) => void;
  onSet: (id: number | null) => void;
}) {
  const min = role.min ?? 0;
  const maxRefs = role.max ?? 1;
  const offered = sets.filter((s) => s.roles.some((r) => r.role === role.role));
  const presetSet = role.reference_set ?? null;
  const activeSet = chosenSet ?? (ids.length === 0 ? presetSet : null);
  const mode = activeSet !== null ? "set" : "images";
  const label = role.label || role.role;
  return (
    <div data-testid={`role-${role.role}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="label mb-0">
          {label}
          <span className="technical ml-1 text-[10px] text-muted">
            {min}–{maxRefs}
          </span>
        </div>
        {(offered.length > 0 || presetSet !== null) && (
          <select
            className="input w-auto py-0.5 text-[11px]"
            aria-label={`${label} source`}
            value={activeSet !== null ? String(activeSet) : ""}
            onChange={(event) => {
              const value = event.target.value;
              if (value === "") {
                onSet(null);
                return;
              }
              onIds([]);
              onSet(Number(value));
            }}
          >
            <option value="">Images I pick</option>
            {presetSet !== null && !offered.some((s) => s.id === presetSet) && (
              <option value={String(presetSet)}>The preset's set (#{presetSet})</option>
            )}
            {offered.map((s) => (
              <option key={s.id} value={String(s.id)}>
                {s.name}
                {s.id === presetSet ? " (preset's)" : ""}
              </option>
            ))}
          </select>
        )}
      </div>
      {mode === "set" && activeSet !== null ? (
        <div className="mt-2">
          <SetPreview setId={activeSet} role={role.role} min={min} max={maxRefs} />
          {chosenSet === null && presetSet !== null && (
            <div className="mt-1 text-[11px] text-muted">
              Bound by this preset. Picking images replaces it.
            </div>
          )}
          {chosenSet === null && presetSet !== null && (
            <div className="mt-2">
              <AddImage
                label={`Use my own ${label.toLowerCase()} instead`}
                onAdd={(id) => {
                  onSet(null);
                  onIds([id]);
                }}
              />
            </div>
          )}
        </div>
      ) : (
        <>
          <div className="mt-2 flex flex-wrap gap-2">
            {ids.map((id, index) => (
              <Thumb
                key={`${id}-${index}`}
                id={id}
                label={`${role.role} ${index + 1}`}
                onRemove={() => onIds(ids.filter((_, n) => n !== index))}
              />
            ))}
          </div>
          {ids.length < maxRefs && (
            <div className="mt-2">
              <AddImage label={`Add ${label}`} onAdd={(id) => onIds([...ids, id])} />
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function InputPickers({
  operation,
  definition,
  images,
  mask,
  references,
  referenceSets = {},
  onImages,
  onMask,
  onReferences,
  onReferenceSets = () => {},
}: {
  operation: Operation;
  definition: PresetDefinition | null;
  images: number[];
  mask: number | null;
  references: Record<string, number[]>;
  referenceSets?: Record<string, number>;
  onImages: (ids: number[]) => void;
  onMask: (id: number | null) => void;
  onReferences: (refs: Record<string, number[]>) => void;
  onReferenceSets?: (sets: Record<string, number>) => void;
}) {
  const [, max] = SOURCE_IMAGES[operation];
  const roles = definition?.references ?? [];
  const [drawing, setDrawing] = useState(false);
  const [sets, setSets] = useState<ReferenceSetSummary[]>([]);
  const hasRoles = roles.length > 0;
  useEffect(() => {
    if (!hasRoles) return;
    let alive = true;
    api
      .referenceSets()
      .then((rows) => alive && setSets(rows))
      .catch(() => alive && setSets([]));
    return () => {
      alive = false;
    };
  }, [hasRoles]);
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
      {roles.map((role) => (
        <RolePicker
          key={role.role}
          role={role}
          sets={sets}
          ids={references[role.role] ?? []}
          chosenSet={referenceSets[role.role] ?? null}
          onIds={(ids) => {
            const next = { ...references };
            if (ids.length) next[role.role] = ids;
            else delete next[role.role];
            onReferences(next);
          }}
          onSet={(id) => {
            const next = { ...referenceSets };
            if (id === null) delete next[role.role];
            else next[role.role] = id;
            onReferenceSets(next);
          }}
        />
      ))}
      {hasRoles && (
        <Link to="/references" className="block text-[11px] text-accent underline hover:text-ink">
          Manage reference sets
        </Link>
      )}
    </div>
  );
}

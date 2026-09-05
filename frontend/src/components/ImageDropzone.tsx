import { useEffect, useMemo, useRef, useState } from "react";
import type { Asset } from "../api/types";
import { GalleryPicker } from "./GalleryPicker";
import { Icon } from "./icons";

export function ImageDropzone({
  label,
  file,
  onFile,
  compact = false,
}: {
  label: string;
  file: File | null;
  onFile: (f: File | null) => void;
  compact?: boolean;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [picking, setPicking] = useState(false);

  // One object URL per file (memoized, not re-created every render), revoked
  // when the file changes or the component unmounts.
  const preview = useMemo(() => (file ? URL.createObjectURL(file) : null), [file]);
  useEffect(
    () => () => {
      if (preview) URL.revokeObjectURL(preview);
    },
    [preview],
  );

  // Pull a gallery asset's bytes into a File so it flows through the same upload path.
  const pickFromGallery = async (a: Asset) => {
    setPicking(false);
    const blob = await fetch(a.url).then((r) => r.blob());
    onFile(new File([blob], a.filename, { type: blob.type || "image/png" }));
  };

  return (
    <div>
      <div className="flex items-center justify-between">
        <label className="label">{label}</label>
        <button className="btn-ghost min-h-0 px-1 text-[11px] text-accent" onClick={() => setPicking(true)}>
          <Icon name="gallery" size={13} /> Gallery
        </button>
      </div>
      <button
        type="button"
        onClick={() => ref.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          const f = e.dataTransfer.files?.[0];
          if (f && f.type.startsWith("image/")) onFile(f);
        }}
        className={`relative flex w-full items-center justify-center overflow-hidden border border-dashed bg-bg transition-colors ${compact ? "h-28" : "h-44"} ${
          drag ? "border-accent bg-accent/6" : "border-edge hover:border-white/30 hover:bg-panel2"
        }`}
        aria-label={`${file ? "Replace" : "Upload"} ${label}`}
      >
        {preview ? (
          <img src={preview} alt="input preview" className="h-full w-full object-contain" />
        ) : (
          <span className="flex flex-col items-center gap-2 px-4 text-xs text-muted">
            <Icon name="image" size={20} className="text-accent" />
            Drop an image or choose a file
          </span>
        )}
      </button>
      {file && (
        <button
          className="btn-ghost mt-1 min-h-0 px-1 text-[11px] hover:text-danger"
          onClick={() => onFile(null)}
        >
          <Icon name="close" size={12} /> Remove
        </button>
      )}
      <input
        ref={ref}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(e) => onFile(e.target.files?.[0] || null)}
      />

      {picking && <GalleryPicker onPick={pickFromGallery} onClose={() => setPicking(false)} />}
    </div>
  );
}

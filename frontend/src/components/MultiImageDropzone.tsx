import type { ImageSlot } from "../api/types";
import { ImageDropzone } from "./ImageDropzone";

/** One dropzone per declared image slot (Qwen-Edit multi-image, FLF2V last frame).
 * Files are keyed by slot name; slot visibility honors show_if against form values. */
export function MultiImageDropzone({
  slots,
  files,
  values,
  onChange,
}: {
  slots: ImageSlot[];
  files: Record<string, File | null>;
  values: Record<string, any>;
  onChange: (name: string, f: File | null) => void;
}) {
  const shown = slots.filter((s) => {
    if (!s.show_if) return true;
    const want = s.show_if.equals;
    const got = values[s.show_if.field];
    return Array.isArray(want) ? want.includes(got) : got === want;
  });
  return (
    <div className="space-y-3">
      {shown.map((s) => (
        <ImageDropzone
          key={s.name}
          label={s.label + (s.required ? "" : " (optional)")}
          file={files[s.name] || null}
          onFile={(f) => onChange(s.name, f)}
          compact={s.name !== "image"}
        />
      ))}
    </div>
  );
}

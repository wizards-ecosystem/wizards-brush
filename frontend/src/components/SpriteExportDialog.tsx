import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ArtifactView, SpritePreview, SpriteProfile } from "../api/types";
import { useStore } from "../store/useStore";
import { Button, Modal, Spinner, fmtBytes } from "./ui";

const DEFAULTS: SpriteProfile = {
  mode: "grid",
  name: "sheet",
  fit: "pad",
  padding: 2,
  extrude: 0,
  trim: false,
  allow_multiple_pages: false,
  max_width: 2048,
  max_height: 2048,
  background: "transparent",
  pixel_art: false,
  partial: false,
};

function NumberField({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number | null | undefined;
  min: number;
  max: number;
  onChange: (value: number | null) => void;
}) {
  return (
    <label className="text-[11px] text-muted">
      {label}
      <input
        className="input mt-1 text-xs"
        type="number"
        min={min}
        max={max}
        value={value ?? ""}
        placeholder="auto"
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    </label>
  );
}

/**
 * Export a set's final stage as a sprite sheet (equal cells) or a packed
 * atlas. The server previews pages, sizes and missing frames before anything
 * is written; the export is validated and published as an artifact, so the
 * download link outlives this dialog.
 */
export function SpriteExportDialog({ setId, onClose }: { setId: number; onClose: () => void }) {
  const toast = useStore((s) => s.toast);
  const [profile, setProfile] = useState<SpriteProfile>(DEFAULTS);
  const [preview, setPreview] = useState<SpritePreview | null>(null);
  const [problem, setProblem] = useState("");
  const [building, setBuilding] = useState(false);
  const [made, setMade] = useState<ArtifactView | null>(null);
  const set = (patch: Partial<SpriteProfile>) => {
    setMade(null);
    setProfile((p) => ({ ...p, ...patch }));
  };

  useEffect(() => {
    let alive = true;
    const timer = window.setTimeout(() => {
      api
        .previewSprites(setId, profile)
        .then((answer) => {
          if (!alive) return;
          setPreview(answer);
          setProblem("");
        })
        .catch((e) => {
          if (!alive) return;
          setPreview(null);
          setProblem(`${e}`);
        });
    }, 250);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [setId, profile]);

  const build = async () => {
    setBuilding(true);
    try {
      const artifact = await api.exportSprites(setId, profile);
      setMade(artifact);
      await api.downloadArtifact(artifact);
    } catch (e) {
      toast(`Export failed: ${e}`, "error");
    } finally {
      setBuilding(false);
    }
  };

  const grid = profile.mode === "grid";
  return (
    <Modal onClose={onClose} label="Sprite sheet or atlas" wide>
      <div className="space-y-4 p-5" data-testid="sprite-export">
        <div>
          <div className="page-kicker">Export</div>
          <h2 className="text-lg font-semibold">Sprite sheet or atlas</h2>
          <p className="text-xs text-muted">
            The final stage's variants, in the set's order. Frames are never stretched; missing ones keep
            their place in the order.
          </p>
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="text-[11px] text-muted">
            Mode
            <select
              className="input mt-1 text-xs"
              value={profile.mode}
              onChange={(e) => set({ mode: e.target.value as SpriteProfile["mode"] })}
            >
              <option value="grid">Grid sheet (equal cells)</option>
              <option value="atlas">Packed atlas</option>
            </select>
          </label>
          <label className="text-[11px] text-muted">
            File name
            <input
              className="input mt-1 text-xs"
              value={profile.name ?? ""}
              onChange={(e) => set({ name: e.target.value || "sheet" })}
            />
          </label>
          <label className="text-[11px] text-muted">
            Background
            <select
              className="input mt-1 text-xs"
              value={profile.background === "transparent" ? "transparent" : "colour"}
              onChange={(e) =>
                set({ background: e.target.value === "transparent" ? "transparent" : "#000000" })
              }
            >
              <option value="transparent">Transparent</option>
              <option value="colour">Solid colour</option>
            </select>
          </label>
          {profile.background !== "transparent" && (
            <label className="text-[11px] text-muted">
              Colour
              <input
                className="input mt-1 h-8 p-0"
                type="color"
                value={profile.background}
                onChange={(e) => set({ background: e.target.value })}
              />
            </label>
          )}
          {grid ? (
            <>
              <NumberField
                label="Columns"
                value={profile.columns}
                min={1}
                max={256}
                onChange={(v) => set({ columns: v })}
              />
              <label className="text-[11px] text-muted">
                Frames of other sizes
                <select
                  className="input mt-1 text-xs"
                  value={profile.fit}
                  onChange={(e) => set({ fit: e.target.value as SpriteProfile["fit"] })}
                >
                  <option value="require_match">Refuse</option>
                  <option value="pad">Centre unscaled in the cell</option>
                  <option value="contain">Shrink to a fixed cell</option>
                </select>
              </label>
              <NumberField
                label="Cell width"
                value={profile.cell_width}
                min={1}
                max={8192}
                onChange={(v) =>
                  set({ cell_width: v, cell_height: v === null ? null : (profile.cell_height ?? v) })
                }
              />
              <NumberField
                label="Cell height"
                value={profile.cell_height}
                min={1}
                max={8192}
                onChange={(v) =>
                  set({ cell_height: v, cell_width: v === null ? null : (profile.cell_width ?? v) })
                }
              />
            </>
          ) : (
            <>
              <NumberField
                label="Max page width"
                value={profile.max_width}
                min={16}
                max={8192}
                onChange={(v) => set({ max_width: v ?? 2048 })}
              />
              <NumberField
                label="Max page height"
                value={profile.max_height}
                min={16}
                max={8192}
                onChange={(v) => set({ max_height: v ?? 2048 })}
              />
            </>
          )}
          <NumberField
            label="Padding px"
            value={profile.padding}
            min={0}
            max={256}
            onChange={(v) => set({ padding: v ?? 0 })}
          />
          <NumberField
            label="Edge extrusion px"
            value={profile.extrude}
            min={0}
            max={64}
            onChange={(v) => set({ extrude: v ?? 0 })}
          />
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-2 text-xs">
          {!grid && (
            <>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={!!profile.trim}
                  onChange={(e) => set({ trim: e.target.checked })}
                />
                Trim transparent borders
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={!!profile.allow_multiple_pages}
                  onChange={(e) => set({ allow_multiple_pages: e.target.checked })}
                />
                Allow several pages
              </label>
            </>
          )}
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={!!profile.pixel_art}
              onChange={(e) => set({ pixel_art: e.target.checked })}
            />
            Pixel art (no smoothing)
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={!!profile.partial}
              onChange={(e) => set({ partial: e.target.checked })}
            />
            Export what exists and list missing frames
          </label>
        </div>

        <div className="border border-edge p-3 text-xs" data-testid="sprite-preview">
          {problem ? (
            <div className="text-danger">{problem}</div>
          ) : !preview ? (
            <div className="flex items-center gap-2 text-muted">
              <Spinner /> Checking the layout…
            </div>
          ) : (
            <div className="space-y-1">
              <div>
                {preview.frames.length} frame{preview.frames.length === 1 ? "" : "s"} on{" "}
                {preview.pages.length} page
                {preview.pages.length === 1 ? "" : "s"}:{" "}
                {preview.pages.map((p) => `${p.width}×${p.height}`).join(", ")}
                {preview.grid &&
                  ` · ${preview.grid.columns} columns of ${preview.grid.cell_width}×${preview.grid.cell_height}`}
              </div>
              {preview.missing.length > 0 && (
                <div className="text-warn">
                  Missing: {preview.missing.map((m) => `#${m.index} ${m.key}`).join(", ")}
                </div>
              )}
            </div>
          )}
        </div>
        {made && (
          <div className="text-[11px] text-muted" data-testid="sprite-artifact">
            {made.filename} · {fmtBytes(made.size_bytes)} · sha256 {made.sha256.slice(0, 16)}… · kept until{" "}
            {made.expires_at ? new Date(made.expires_at).toLocaleDateString() : "deleted"}
            <button
              type="button"
              className="ml-2 text-accent underline"
              onClick={() => void api.downloadArtifact(made)}
            >
              Download again
            </button>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Close</Button>
          <Button
            variant="primary"
            icon="download"
            loading={building}
            disabled={!preview}
            onClick={() => void build()}
          >
            Build and download
          </Button>
        </div>
      </div>
    </Modal>
  );
}

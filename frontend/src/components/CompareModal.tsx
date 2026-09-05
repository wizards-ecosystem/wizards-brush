import { useMemo, useState } from "react";
import type { Asset } from "../api/types";
import { HIDE_KEYS } from "../lib/generators";
import { CompareSlider } from "./CompareSlider";
import { Button, IconButton, Modal } from "./ui";

/** A/B compare of two gallery assets: slider (images) or side-by-side, plus a
 * metadata diff table with differing values highlighted. */
export function CompareModal({ a, b, onClose }: { a: Asset; b: Asset; onClose: () => void }) {
  const [left, setLeft] = useState(a);
  const [right, setRight] = useState(b);
  const bothImages = left.kind === "image" && right.kind === "image";
  const [tab, setTab] = useState<"slider" | "side">(bothImages ? "slider" : "side");

  const rows = useMemo(() => {
    const keys = new Set([...Object.keys(left.meta || {}), ...Object.keys(right.meta || {})]);
    return [...keys]
      .filter((k) => !HIDE_KEYS.has(k) && k !== "auto_tags")
      .sort()
      .map((k) => ({
        key: k,
        a: left.meta?.[k] != null ? String(left.meta[k]) : "—",
        b: right.meta?.[k] != null ? String(right.meta[k]) : "—",
      }));
  }, [left, right]);

  const swap = () => {
    setLeft(right);
    setRight(left);
  };

  const pane = (asset: Asset) =>
    asset.kind === "video" ? (
      <video
        src={asset.url}
        controls
        muted
        loop
        className="max-h-[48vh] w-full object-contain bg-black rounded-lg"
      />
    ) : (
      <img
        src={asset.url}
        alt={asset.filename}
        className="max-h-[48vh] w-full object-contain bg-black rounded-lg"
      />
    );

  return (
    <Modal onClose={onClose} size="wide" label="Compare assets">
      <div className="space-y-5 overflow-y-auto p-4 sm:p-5">
        <div className="flex flex-wrap items-center gap-2 border-b border-edge pb-4">
          <div className="mr-2">
            <div className="page-kicker">A / B study</div>
            <div className="font-semibold">Compare assets</div>
          </div>
          <div className="flex gap-1 border border-edge bg-panel p-1">
            {bothImages && (
              <button
                className={`seg ${tab === "slider" ? "seg-active" : ""}`}
                onClick={() => setTab("slider")}
              >
                Slider
              </button>
            )}
            <button className={`seg ${tab === "side" ? "seg-active" : ""}`} onClick={() => setTab("side")}>
              Side by side
            </button>
          </div>
          <Button size="sm" icon="compare" onClick={swap} title="Swap A and B">
            Swap
          </Button>
          <IconButton icon="close" label="Close comparison" className="ml-auto" onClick={onClose} />
        </div>

        {tab === "slider" && bothImages ? (
          <CompareSlider before={left.url} after={right.url} />
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              {pane(left)}
              <div className="text-[11px] text-white/50 mt-1">
                A · #{left.id} · {left.generator}
              </div>
            </div>
            <div>
              {pane(right)}
              <div className="text-[11px] text-white/50 mt-1">
                B · #{right.id} · {right.generator}
              </div>
            </div>
          </div>
        )}

        <div className="border-t border-edge pt-4">
          <div className="label">Metadata</div>
          <div className="text-xs">
            <div className="grid grid-cols-[1fr_2fr_2fr] gap-x-3 border-b border-edge py-1 text-white/40">
              <span>key</span>
              <span>A</span>
              <span>B</span>
            </div>
            {rows.map((r) => (
              <div
                key={r.key}
                className={`grid grid-cols-[1fr_2fr_2fr] gap-x-3 border-b border-edge/40 py-1.5 ${
                  r.a !== r.b ? "text-accent" : "text-white/70"
                }`}
              >
                <span className="text-white/40">{r.key}</span>
                <span className="break-words">{r.a}</span>
                <span className="break-words">{r.b}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </Modal>
  );
}

import { useCallback, useRef, useState } from "react";

const MIN_ZOOM = 1;
const MAX_ZOOM = 8;
const WHEEL_SENSITIVITY = 0.0015;

/**
 * Pan/zoom image viewer for the lightbox.
 *
 * The gallery previously showed images object-contained with no way to inspect
 * them — which matters most on exactly the outputs people care about, where the
 * question is whether the fine detail actually holds up.
 *
 * Zoom is anchored at the cursor rather than the centre, so pointing at a
 * detail and scrolling brings that detail closer instead of drifting off-screen.
 */
export function ZoomableImage({ src, alt }: { src: string; alt: string }) {
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const boxRef = useRef<HTMLDivElement | null>(null);
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
  // Mirrored in state because the cursor and transition depend on it, and
  // React 19 (correctly) rejects reading a ref during render.
  const [dragging, setDragging] = useState(false);

  // A new image must not inherit the previous one's zoom — arrowing through a
  // gallery while zoomed in would otherwise land on an arbitrary crop.
  //
  // Adjust-during-render rather than an effect: the reset lands in the same
  // pass as the new src, so the next image never paints at the old transform
  // for a frame. Same pattern GeneratorView and AssetModal already use.
  const [prevSrc, setPrevSrc] = useState(src);
  if (prevSrc !== src) {
    setPrevSrc(src);
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  }

  const reset = useCallback(() => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  }, []);

  const zoomAt = useCallback((nextZoom: number, clientX: number, clientY: number) => {
    const box = boxRef.current;
    if (!box) return;
    const r = box.getBoundingClientRect();
    // Cursor position relative to the box centre, in unscaled units.
    const cx = clientX - r.left - r.width / 2;
    const cy = clientY - r.top - r.height / 2;
    setZoom((prev) => {
      const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, nextZoom));
      setOffset((o) => {
        if (z === MIN_ZOOM) return { x: 0, y: 0 };
        const k = z / prev;
        return { x: cx - (cx - o.x) * k, y: cy - (cy - o.y) * k };
      });
      return z;
    });
  }, []);

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    zoomAt(zoom * Math.exp(-e.deltaY * WHEEL_SENSITIVITY), e.clientX, e.clientY);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (zoom === MIN_ZOOM) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y };
    setDragging(true);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    setOffset({ x: d.ox + (e.clientX - d.x), y: d.oy + (e.clientY - d.y) });
  };
  const endDrag = () => {
    drag.current = null;
    setDragging(false);
  };

  return (
    <div
      ref={boxRef}
      className="relative flex h-full w-full items-center justify-center overflow-hidden"
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerLeave={endDrag}
      onDoubleClick={(e) => (zoom === MIN_ZOOM ? zoomAt(2, e.clientX, e.clientY) : reset())}
    >
      <img
        src={src}
        alt={alt}
        draggable={false}
        className="max-h-[88vh] max-w-full object-contain select-none"
        style={{
          transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})`,
          cursor: zoom > MIN_ZOOM ? (dragging ? "grabbing" : "grab") : "zoom-in",
          transition: dragging ? "none" : "transform 80ms linear",
          // Nearest-neighbour past ~3x: at that point the interesting question is
          // what the model actually produced, not a smoothed guess at it.
          imageRendering: zoom >= 3 ? "pixelated" : "auto",
        }}
      />

      {zoom > MIN_ZOOM && (
        <div className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-2 rounded-[6px] border border-white/12 bg-black/75 px-2 py-1 text-xs">
          <button className="btn-ghost" onClick={reset} aria-label="Reset zoom">
            Fit
          </button>
          <span className="technical text-white/70">{zoom.toFixed(1)}×</span>
        </div>
      )}
    </div>
  );
}

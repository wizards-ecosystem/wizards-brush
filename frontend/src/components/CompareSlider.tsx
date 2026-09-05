import { useRef, useState } from "react";

export function CompareSlider({ before, after }: { before: string; after: string }) {
  const [pos, setPos] = useState(50);
  const ref = useRef<HTMLDivElement>(null);
  const drag = (e: React.PointerEvent) => {
    const r = ref.current!.getBoundingClientRect();
    setPos(Math.max(0, Math.min(100, ((e.clientX - r.left) / r.width) * 100)));
  };
  return (
    <div>
      <div
        ref={ref}
        className="media-tile relative cursor-ew-resize overflow-hidden select-none"
        style={{ touchAction: "none" }}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          drag(event);
        }}
        onPointerMove={(event) => event.currentTarget.hasPointerCapture(event.pointerId) && drag(event)}
      >
        <img src={after} alt="after" className="w-full block" />
        {/* clip-path (not a measured-width overlay) so render never reads the ref */}
        <img
          src={before}
          alt="before"
          className="absolute inset-0 w-full h-full"
          style={{ clipPath: `inset(0 ${100 - pos}% 0 0)` }}
        />
        <div className="pointer-events-none absolute inset-y-0 w-px bg-white" style={{ left: `${pos}%` }}>
          <span className="absolute left-1/2 top-1/2 flex h-10 w-5 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-[5px] border border-white/60 bg-black/70">
            <span className="h-4 w-px bg-white/70" />
          </span>
        </div>
        <input
          className="absolute inset-0 h-full w-full cursor-ew-resize opacity-0"
          type="range"
          min={0}
          max={100}
          value={pos}
          aria-label="Comparison split"
          onChange={(event) => setPos(Number(event.target.value))}
        />
      </div>
      <div className="technical mt-1 flex justify-between text-[9px] uppercase text-muted">
        <span>Before</span>
        <span>After</span>
      </div>
    </div>
  );
}

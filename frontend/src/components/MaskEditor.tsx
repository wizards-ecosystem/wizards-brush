import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./icons";

/** Paint a white mask (inpaint region) over the source image. Exports a
 *  black/white PNG at the image's natural size. Supports brush/eraser,
 *  undo/redo, invert and a live brush-size preview. */
export function MaskEditor({ image, onMask }: { image: File; onMask: (f: File | null) => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [dims, setDims] = useState({ w: 512, h: 512 });
  const [brush, setBrush] = useState(48);
  const [erase, setErase] = useState(false);
  const [cursor, setCursor] = useState<{ x: number; y: number; show: boolean }>({ x: 0, y: 0, show: false });
  const drawing = useRef(false);
  const lastPoint = useRef<{ x: number; y: number } | null>(null);
  const history = useRef<ImageData[]>([]);
  const redo = useRef<ImageData[]>([]);
  const [hasHist, setHasHist] = useState(false);
  const [hasRedo, setHasRedo] = useState(false);
  // Displayed width of the editor, tracked by a ResizeObserver so render never
  // measures the DOM ref directly.
  const [wrapW, setWrapW] = useState(0);

  // One object URL per source image (memoized, not re-created every render),
  // revoked when the image changes or the editor unmounts.
  const imgUrl = useMemo(() => URL.createObjectURL(image), [image]);
  useEffect(() => () => URL.revokeObjectURL(imgUrl), [imgUrl]);

  useEffect(() => {
    const im = new Image();
    im.onload = () => {
      setDims({ w: im.naturalWidth, h: im.naturalHeight });
      const c = canvasRef.current!;
      c.width = im.naturalWidth;
      c.height = im.naturalHeight;
      c.getContext("2d")!.clearRect(0, 0, c.width, c.height);
      history.current = [];
      redo.current = [];
      setHasHist(false);
      setHasRedo(false);
      onMask(null);
    };
    im.src = imgUrl;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imgUrl]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => setWrapW(entries[0].contentRect.width));
    ro.observe(el); // fires once on observe, so the initial width arrives async
    return () => ro.disconnect();
  }, []);

  const ctx = () => canvasRef.current!.getContext("2d")!;

  const pos = (e: { clientX: number; clientY: number }) => {
    const c = canvasRef.current!;
    const r = c.getBoundingClientRect();
    return {
      x: ((e.clientX - r.left) / r.width) * c.width,
      y: ((e.clientY - r.top) / r.height) * c.height,
      dx: e.clientX - r.left,
      dy: e.clientY - r.top,
      scale: r.width / c.width,
    };
  };

  const paint = (e: React.PointerEvent) => {
    const p = pos(e);
    setCursor({ x: p.dx, y: p.dy, show: true });
    if (!drawing.current) return;
    const g = ctx();
    g.globalCompositeOperation = erase ? "destination-out" : "source-over";
    g.strokeStyle = erase ? "rgba(0,0,0,1)" : "rgba(255,112,77,0.62)";
    g.fillStyle = g.strokeStyle;
    g.lineWidth = brush;
    g.lineCap = "round";
    g.lineJoin = "round";
    const native = e.nativeEvent;
    const points = typeof native.getCoalescedEvents === "function" ? native.getCoalescedEvents() : [native];
    for (const event of points) {
      const point = pos(event);
      const previous = lastPoint.current;
      g.beginPath();
      if (previous) {
        g.moveTo(previous.x, previous.y);
        g.lineTo(point.x, point.y);
        g.stroke();
      } else {
        g.arc(point.x, point.y, brush / 2, 0, Math.PI * 2);
        g.fill();
      }
      lastPoint.current = { x: point.x, y: point.y };
    }
    g.globalCompositeOperation = "source-over";
  };

  const exportMask = useCallback(() => {
    const src = canvasRef.current!;
    const out = document.createElement("canvas");
    out.width = src.width;
    out.height = src.height;
    const octx = out.getContext("2d")!;
    octx.fillStyle = "black";
    octx.fillRect(0, 0, out.width, out.height);
    const data = src.getContext("2d")!.getImageData(0, 0, src.width, src.height);
    const md = octx.getImageData(0, 0, out.width, out.height);
    let any = false;
    for (let i = 0; i < data.data.length; i += 4) {
      const alpha = data.data[i + 3];
      if (alpha > 1) {
        const value = Math.min(255, Math.round((alpha / 158) * 255));
        md.data[i] = md.data[i + 1] = md.data[i + 2] = value;
        md.data[i + 3] = 255;
        any = true;
      }
    }
    octx.putImageData(md, 0, 0);
    if (!any) {
      onMask(null);
      return;
    }
    out.toBlob((b) => b && onMask(new File([b], "mask.png", { type: "image/png" })), "image/png");
  }, [onMask]);

  const snapshot = () => {
    const c = canvasRef.current!;
    history.current.push(ctx().getImageData(0, 0, c.width, c.height));
    if (history.current.length > 30) history.current.shift();
    redo.current = [];
    setHasHist(true);
    setHasRedo(false);
  };

  const beginStroke = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    snapshot();
    drawing.current = true;
    lastPoint.current = null;
    paint(e);
  };
  const endStroke = () => {
    if (!drawing.current) return;
    drawing.current = false;
    lastPoint.current = null;
    exportMask();
  };

  const undo = () => {
    if (!history.current.length) return;
    const c = canvasRef.current!;
    redo.current.push(ctx().getImageData(0, 0, c.width, c.height));
    ctx().putImageData(history.current.pop()!, 0, 0);
    setHasHist(history.current.length > 0);
    setHasRedo(true);
    exportMask();
  };
  const redoFn = () => {
    if (!redo.current.length) return;
    const c = canvasRef.current!;
    history.current.push(ctx().getImageData(0, 0, c.width, c.height));
    ctx().putImageData(redo.current.pop()!, 0, 0);
    setHasRedo(redo.current.length > 0);
    setHasHist(true);
    exportMask();
  };
  const invert = () => {
    snapshot();
    const c = canvasRef.current!;
    const g = ctx();
    const d = g.getImageData(0, 0, c.width, c.height);
    for (let i = 0; i < d.data.length; i += 4) {
      if (d.data[i + 3] > 10) {
        d.data[i + 3] = 0;
      } else {
        d.data[i] = 255;
        d.data[i + 1] = 112;
        d.data[i + 2] = 77;
        d.data[i + 3] = 153;
      }
    }
    g.putImageData(d, 0, 0);
    exportMask();
  };
  const clear = () => {
    snapshot();
    const c = canvasRef.current!;
    ctx().clearRect(0, 0, c.width, c.height);
    onMask(null);
  };

  const previewScale = (wrapW || dims.w) / dims.w;

  return (
    <div>
      <div className="flex items-center justify-between">
        <label className="label mb-0">Mask — paint the area to regenerate</label>
        <div className="flex gap-1">
          <button
            className={`btn-ghost text-xs ${!erase ? "text-accent" : ""}`}
            onClick={() => setErase(false)}
            aria-pressed={!erase}
          >
            <Icon name="edit" size={14} /> Brush
          </button>
          <button
            className={`btn-ghost text-xs ${erase ? "text-accent" : ""}`}
            onClick={() => setErase(true)}
            aria-pressed={erase}
          >
            Eraser
          </button>
        </div>
      </div>
      <div
        ref={wrapRef}
        className="media-tile relative mt-1 overflow-hidden border border-edge"
        style={{ aspectRatio: `${dims.w}/${dims.h}` }}
      >
        <img src={imgUrl} alt="inpaint source" className="absolute inset-0 w-full h-full object-contain" />
        <canvas
          ref={canvasRef}
          className="absolute inset-0 w-full h-full object-contain cursor-none"
          style={{ touchAction: "none" }}
          onPointerDown={beginStroke}
          onPointerMove={paint}
          onPointerUp={endStroke}
          onPointerCancel={endStroke}
          onPointerEnter={() => setCursor((c) => ({ ...c, show: true }))}
          onPointerLeave={() => {
            setCursor((c) => ({ ...c, show: false }));
            endStroke();
          }}
        />
        {cursor.show && (
          <div
            className="absolute rounded-full border border-white/80 pointer-events-none"
            style={{
              width: brush * previewScale,
              height: brush * previewScale,
              left: cursor.x - (brush * previewScale) / 2,
              top: cursor.y - (brush * previewScale) / 2,
              background: erase ? "rgba(255,255,255,0.1)" : "rgba(255,112,77,0.2)",
            }}
          />
        )}
      </div>
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        <span className="text-xs text-white/50">Brush</span>
        <input
          type="range"
          className="flex-1 min-w-[80px]"
          min={8}
          max={200}
          value={brush}
          onChange={(e) => setBrush(Number(e.target.value))}
        />
        <button className="icon-btn" disabled={!hasHist} onClick={undo} aria-label="Undo mask stroke">
          <Icon name="undo" size={15} />
        </button>
        <button className="icon-btn" disabled={!hasRedo} onClick={redoFn} aria-label="Redo mask stroke">
          <Icon name="redo" size={15} />
        </button>
        <button className="btn text-xs" onClick={invert}>
          Invert
        </button>
        <button className="btn text-xs" onClick={clear}>
          Clear
        </button>
      </div>
    </div>
  );
}

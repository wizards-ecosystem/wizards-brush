import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Asset } from "../api/types";
import { AssetCard } from "../components/AssetCard";
import { CompareSlider } from "../components/CompareSlider";
import { Icon } from "../components/icons";
import { ProgressBar } from "../components/ProgressBar";
import { Button, EmptyState, PageHeader } from "../components/ui";
import { useStore } from "../store/useStore";

export function Tools() {
  const assets = useStore((s) => s.assets);
  const jobs = useStore((s) => s.jobs);
  const toast = useStore((s) => s.toast);
  const [sel, setSel] = useState<Asset | null>(null);
  const [scale, setScale] = useState(4);
  const [factor, setFactor] = useState(2);
  const [lastJob, setLastJob] = useState<number | null>(null);
  const [after, setAfter] = useState<Asset | null>(null);
  const [filter, setFilter] = useState<"" | "image" | "video">("");

  const run = async (kind: "upscale" | "face-restore" | "interpolate" | "detail") => {
    if (!sel) return;
    setAfter(null);
    const body: any = { asset_id: sel.id };
    if (kind === "upscale") body.scale = scale;
    if (kind === "interpolate") body.factor = factor;
    try {
      const { job_id } = await api.tool(kind, body);
      setLastJob(job_id);
      toast(`${kind} queued`, "success");
    } catch (e) {
      toast(`${kind} failed: ${e}`, "error");
    }
  };

  const isImg = sel?.kind === "image";
  const job = lastJob != null ? jobs[lastJob] : null;

  // When the tool job finishes, load its result for the before/after compare.
  const resultId = job?.status === "done" ? job.result?.asset_ids?.[0] : undefined;
  useEffect(() => {
    if (resultId != null) {
      api
        .asset(resultId)
        .then(setAfter)
        .catch(() => {});
    }
  }, [resultId]);

  const shown = filter ? assets.filter((a) => a.kind === filter) : assets;

  return (
    <div className="grid h-full grid-cols-1 bg-bg lg:grid-cols-[minmax(0,1fr)_360px]">
      <div className="overflow-y-auto page-pad">
        <PageHeader
          kicker="Finishing room"
          title="Tools"
          description="Choose material from the gallery, apply a finishing process, and inspect the result against its source."
          actions={
            <div className="flex gap-1 border border-edge bg-panel p-1" role="group" aria-label="Asset type">
              {(["", "image", "video"] as const).map((f) => (
                <button
                  key={f || "all"}
                  className={`seg min-w-16 ${filter === f ? "seg-active" : ""}`}
                  onClick={() => setFilter(f)}
                  aria-pressed={filter === f}
                >
                  {f || "all"}
                </button>
              ))}
            </div>
          }
        />

        {sel && (
          <section className="mt-6 border-y border-edge bg-panel py-4">
            <div className="mb-3 flex items-center justify-between px-4">
              <div>
                <div className="page-kicker">Inspection stage</div>
                <h2 className="text-sm font-semibold">{after ? "Before / after" : "Selected material"}</h2>
              </div>
              <span className="technical text-[10px] text-muted">
                {sel.kind} · {sel.width}×{sel.height}
              </span>
            </div>
            <div className="flex min-h-[320px] items-center justify-center bg-black/60">
              {after && isImg ? (
                <div className="w-full max-w-5xl">
                  <CompareSlider before={sel.url} after={after.url} />
                </div>
              ) : sel.kind === "video" ? (
                <video src={sel.url} className="max-h-[60vh] w-full object-contain" controls muted loop />
              ) : (
                <img
                  src={sel.url}
                  alt={sel.meta?.prompt || sel.filename}
                  className="max-h-[60vh] w-full object-contain"
                />
              )}
            </div>
          </section>
        )}

        <section className="mt-6">
          <div className="mb-3 flex items-center justify-between">
            <div className="label mb-0">Choose source material</div>
            <span className="technical text-[10px] text-muted">{shown.length} available</span>
          </div>
          {shown.length === 0 ? (
            <div className="flex min-h-[360px] items-center justify-center">
              <EmptyState
                icon="gallery"
                title="No material is available"
                description="Generate or restore an asset, then return here to apply finishing tools."
              />
            </div>
          ) : (
            <div className="grid grid-cols-3 gap-2 md:grid-cols-5 xl:grid-cols-7 2xl:grid-cols-9">
              {shown.map((asset) => (
                <AssetCard
                  key={asset.id}
                  asset={asset}
                  selected={sel?.id === asset.id}
                  onClick={() => {
                    setSel(asset);
                    setAfter(null);
                  }}
                />
              ))}
            </div>
          )}
        </section>
      </div>

      <aside
        className="space-y-5 overflow-y-auto border-t border-edge bg-panel p-5 lg:border-t-0 lg:border-l"
        aria-label="Tool controls"
      >
        <div>
          <div className="page-kicker">Process inspector</div>
          <h2 className="text-lg font-semibold">{sel ? "Choose a finish" : "Awaiting material"}</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted">
            {sel
              ? "Every result is saved as a new gallery asset; the source remains untouched."
              : "Select an image or video from the material grid."}
          </p>
        </div>

        {sel && isImg && (
          <div className="space-y-4 border-t border-edge pt-4">
            <div>
              <div className="flex justify-between">
                <label className="label">Upscale factor</label>
                <span className="technical text-xs">×{scale}</span>
              </div>
              <input
                type="range"
                min={2}
                max={4}
                step={2}
                value={scale}
                onChange={(e) => setScale(Number(e.target.value))}
              />
              <p className="mt-1 text-[11px] leading-relaxed text-muted">
                Preserves aspect ratio and caps the finished long side at 4096 px.
              </p>
            </div>
            <Button variant="primary" className="w-full" icon="image" onClick={() => run("upscale")}>
              Upscale · Real-ESRGAN
            </Button>
            <Button className="w-full" onClick={() => run("face-restore")}>
              Restore faces · GFPGAN
            </Button>
            <Button className="w-full" onClick={() => run("detail")}>
              Auto-detail faces
            </Button>
          </div>
        )}

        {sel && !isImg && (
          <div className="space-y-4 border-t border-edge pt-4">
            <div>
              <div className="flex justify-between">
                <label className="label">Interpolation factor</label>
                <span className="technical text-xs">×{factor}</span>
              </div>
              <input
                type="range"
                min={2}
                max={4}
                step={1}
                value={factor}
                onChange={(e) => setFactor(Number(e.target.value))}
              />
            </div>
            <Button variant="primary" className="w-full" icon="video" onClick={() => run("interpolate")}>
              Smooth motion · ×{factor}
            </Button>
          </div>
        )}

        {job && <ProgressBar job={job} />}
        {after && (
          <div className="flex items-center gap-2 border-l-2 border-ok bg-ok/8 p-3 text-xs text-ok">
            <Icon name="check" size={15} /> Result saved to the gallery.
          </div>
        )}
      </aside>
    </div>
  );
}

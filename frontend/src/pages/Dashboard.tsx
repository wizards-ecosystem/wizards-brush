import { Link } from "react-router-dom";
import type { GeneratorSpec } from "../api/types";
import { BackendPanel } from "../components/BackendPanel";
import { Icon } from "../components/icons";
import { ProgressBar } from "../components/ProgressBar";
import { EmptyState, PageHeader, fmtBytes } from "../components/ui";
import { useStore } from "../store/useStore";

/**
 * How the launcher groups processes.
 *
 * The old split was "Image" vs "Motion", which sorted by what comes out. That
 * put "Local Image" and "Remote GPU Image" — the same task on different
 * hardware — beside "Inpaint", which needs a picture and a painted mask before
 * it can do anything. The question the user is actually answering first is what
 * they have to hand, so group on that: a description, an existing image, or a
 * finished file that needs work. Device is a badge on the row, not a category.
 *
 * Derived from spec fields the backend already publishes, so a new generator
 * lands in the right group without touching this file.
 */
const GROUPS: {
  key: string;
  label: string;
  blurb: string;
  icon: "spark" | "image";
  match: (spec: GeneratorSpec) => boolean;
}[] = [
  {
    key: "from-idea",
    label: "From a description",
    blurb: "Start with words alone.",
    icon: "spark",
    match: (spec) => !spec.needs_image,
  },
  {
    key: "from-image",
    label: "From an image you have",
    blurb: "Change, extend, or animate a picture.",
    icon: "image",
    match: (spec) => spec.needs_image,
  },
];

/** Plain-language row title. `spec.title` carries the model and lane, which the
 *  subtitle and badge already say; the row should say what it does. */
const PLAIN_TITLES: Record<string, string> = {
  image_local: "New image",
  image_colab: "New image",
  img2img: "Remix an image",
  inpaint: "Replace part of an image",
  outpaint: "Extend past the edges",
  image_edit: "Edit by instruction",
  control_local: "Copy a pose or composition",
  t2v: "New video",
  i2v: "Animate an image",
  long_video: "Long video",
};

function GeneratorLedger({ specs, remoteConnected }: { specs: GeneratorSpec[]; remoteConnected: boolean }) {
  return (
    <div className="divide-y divide-edge border-y border-edge">
      {specs.map((spec) => {
        const unavailable = !!spec.unavailable_reason || (spec.needs_remote && !remoteConnected);
        return (
          <Link
            key={spec.id}
            to={`/g/${spec.id}`}
            className="group relative grid grid-cols-[20px_minmax(0,1fr)_auto] items-center gap-3 py-3 pr-2 transition-colors hover:bg-panel2/65 focus-visible:bg-panel2/65"
          >
            <Icon
              name={spec.output === "image" ? "image" : "video"}
              size={15}
              className={spec.output === "image" ? "text-accent" : "text-accent2"}
            />
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium text-ink">
                {PLAIN_TITLES[spec.id] || spec.title}
              </span>
              <span className="mt-0.5 block truncate text-xs text-muted">{spec.subtitle}</span>
            </span>
            <span className="flex items-center gap-2">
              <span
                className={`technical text-[9px] uppercase tracking-wide ${
                  unavailable ? "text-warn" : spec.needs_remote ? "text-accent2" : "text-ok"
                }`}
              >
                {unavailable ? "offline" : spec.needs_remote ? "A100" : "local"}
              </span>
              <Icon
                name="arrow-right"
                size={15}
                className="text-white/28 transition-transform group-hover:translate-x-1 group-hover:text-accent"
              />
            </span>
          </Link>
        );
      })}
    </div>
  );
}

export function Dashboard() {
  const system = useStore((state) => state.system);
  const specs = useStore((state) => state.specs);
  const assets = useStore((state) => state.assets);
  const jobs = useStore((state) => state.jobs);
  const previews = useStore((state) => state.previews);
  const stats = useStore((state) => state.stats);
  const activeJobs = Object.values(jobs)
    .filter((job) => job.status === "running" || job.status === "queued")
    .sort((a, b) => b.id - a.id);
  // Grouped by what the user starts from, then image processes before video
  // ones inside each group so the list reads shortest-path-first.
  const grouped = GROUPS.map((group) => ({
    ...group,
    specs: specs
      .filter(group.match)
      .slice()
      .sort((a, b) => Number(a.output === "video") - Number(b.output === "video")),
  })).filter((group) => group.specs.length > 0);

  return (
    <div className="page-shell page-pad mx-auto max-w-[1680px]">
      <PageHeader
        kicker="Local creative workstation"
        title="Studio"
        description="Shape an image, set a scene in motion, or pick up where the workshop left off."
        actions={
          <Link to="/gallery" className="btn">
            <Icon name="gallery" size={16} /> Open gallery
          </Link>
        }
      />

      {stats && (
        <dl className="mt-6 grid grid-cols-2 border-y border-edge sm:grid-cols-5">
          {[
            ["Images", String(stats.images)],
            ["Videos", String(stats.videos)],
            ["Made today", String(stats.today)],
            ["Disk", fmtBytes(stats.disk_bytes)],
            ["In motion", String(activeJobs.length)],
          ].map(([label, value], index) => (
            <div
              key={label}
              className={`px-3 py-3 first:pl-0 sm:px-5 ${index > 0 ? "border-l border-edge" : ""}`}
            >
              <dt className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted">{label}</dt>
              <dd className="technical mt-1 text-lg text-ink">{value}</dd>
            </div>
          ))}
        </dl>
      )}

      {activeJobs.length > 0 && (
        <section className="mt-6 border-l-[3px] border-accent bg-panel px-4 py-3">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <span className="size-2 animate-pulse rounded-full bg-accent" />
              <h2 className="text-sm font-semibold">The workshop is running</h2>
              <span className="technical text-[10px] text-muted">{activeJobs.length} active</span>
            </div>
            <Link to="/queue" className="btn-ghost text-xs">
              Open queue <Icon name="arrow-right" size={14} />
            </Link>
          </div>
          <div className="grid gap-3 lg:grid-cols-2">
            {activeJobs.slice(0, 4).map((job) => (
              <ProgressBar key={job.id} job={job} preview={previews[job.id]} />
            ))}
          </div>
        </section>
      )}

      <div className="mt-8 grid items-start gap-10 xl:grid-cols-[minmax(410px,0.85fr)_minmax(560px,1.3fr)]">
        <section>
          <div className="mb-4 flex items-end justify-between">
            <div>
              <div className="page-kicker">Start a process</div>
              <h2 className="display-title text-[30px] leading-none">What are we making?</h2>
            </div>
            <Link to="/tools" className="btn-ghost text-xs">
              Finishing tools <Icon name="arrow-right" size={14} />
            </Link>
          </div>

          {specs.length === 0 ? (
            <EmptyState
              icon="alert"
              title="Generator catalogue unavailable"
              description="The backend is still loading its capabilities. The launcher will fill in as soon as it responds."
            />
          ) : (
            <div className="space-y-7">
              {grouped.map((group) => (
                <div key={group.key}>
                  <div className="mb-2 flex items-baseline gap-2">
                    <Icon name={group.icon} size={15} className="translate-y-0.5 text-accent" />
                    <span className="text-xs font-semibold text-ink">{group.label}</span>
                    <span className="truncate text-[11px] text-muted">{group.blurb}</span>
                  </div>
                  <GeneratorLedger specs={group.specs} remoteConnected={!!system?.remote_gpu?.connected} />
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="min-w-0">
          <div className="mb-4 flex items-end justify-between">
            <div>
              <div className="page-kicker">Latest material</div>
              <h2 className="display-title text-[30px] leading-none">From the gallery</h2>
            </div>
            <Link to="/gallery" className="btn-ghost text-xs">
              View all <Icon name="arrow-right" size={14} />
            </Link>
          </div>

          {assets.length === 0 ? (
            <div className="flex min-h-[360px] items-end border-y border-edge py-8">
              <EmptyState
                icon="image"
                title="The workbench is clear"
                description="Choose a process and your first result will arrive here, ready to compare, refine, or send somewhere new."
                action={
                  <Link to={specs[0] ? `/g/${specs[0].id}` : "/settings"} className="btn-primary">
                    Begin making <Icon name="arrow-right" size={15} />
                  </Link>
                }
              />
            </div>
          ) : (
            // Masonry columns, each tile at the asset's own aspect ratio.
            //
            // This grid used to be a fixed-height 4x3 with object-cover, which
            // crops to fill: a 9:16 portrait in a square cell lost its top and
            // bottom, and a wide landscape in a tall cell was blown up and
            // cropped to a detail. The preview is meant to be recognisable, so
            // the tile follows the image rather than the image following the
            // tile. Dimensions come from the asset row; anything missing them
            // falls back to square.
            <div className="columns-2 gap-1.5 sm:columns-3">
              {assets.slice(0, 6).map((asset, index) => (
                <Link
                  key={asset.id}
                  to="/gallery"
                  className="media-tile group mb-1.5 block break-inside-avoid"
                  style={{
                    aspectRatio: asset.width && asset.height ? `${asset.width} / ${asset.height}` : "1 / 1",
                    contentVisibility: "auto",
                    containIntrinsicSize: "260px 260px",
                  }}
                >
                  <img
                    src={asset.thumb_url || asset.url}
                    alt={asset.meta?.prompt || asset.filename}
                    className="h-full w-full object-contain transition duration-300 group-hover:scale-[1.02]"
                    loading={index > 1 ? "lazy" : "eager"}
                  />
                  <span className="absolute inset-x-0 bottom-0 flex items-center justify-between bg-black/62 px-2 py-1.5 text-[10px] text-white opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
                    <span className="truncate">{asset.generator}</span>
                    <Icon name={asset.kind} size={13} />
                  </span>
                </Link>
              ))}
            </div>
          )}
        </section>
      </div>

      <section className="mt-10">
        <BackendPanel />
      </section>
    </div>
  );
}

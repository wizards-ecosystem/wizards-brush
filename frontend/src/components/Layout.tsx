import { useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import type { GeneratorSpec } from "../api/types";
import { useStore } from "../store/useStore";
import { BrushMark, Icon, type IconName } from "./icons";
import { IconButton, Modal, StatusIndicator } from "./ui";
import { Toaster } from "./Toaster";

const workspaceLinks: Array<{ to: string; label: string; icon: IconName }> = [
  { to: "/", label: "Studio", icon: "studio" },
  { to: "/gallery", label: "Gallery", icon: "gallery" },
  { to: "/queue", label: "Queue", icon: "queue" },
  { to: "/tools", label: "Tools", icon: "tools" },
  { to: "/trash", label: "Trash", icon: "trash" },
  { to: "/settings", label: "Settings", icon: "settings" },
];

function RailLink({ to, label, icon, badge }: { to: string; label: string; icon: IconName; badge?: number }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      aria-label={label}
      className={({ isActive }) =>
        `group relative flex h-[54px] w-full flex-col items-center justify-center gap-0.5 text-[9px] font-medium tracking-wide transition-colors ${
          isActive ? "text-ink" : "text-muted hover:bg-panel2 hover:text-ink"
        }`
      }
    >
      {({ isActive }) => (
        <>
          {isActive && <span className="absolute left-0 top-3 h-7 w-[3px] bg-accent" aria-hidden="true" />}
          <Icon name={icon} size={18} />
          <span>{label}</span>
          {!!badge && (
            <span
              className="technical absolute right-2 top-1.5 min-w-4 rounded-full bg-accent px-1 text-center text-[9px] font-semibold text-[#17100d]"
              aria-label={`${badge} active job${badge === 1 ? "" : "s"}`}
            >
              {badge > 99 ? "99+" : badge}
            </span>
          )}
        </>
      )}
    </NavLink>
  );
}

function MobileLink({
  to,
  label,
  icon,
  badge,
}: {
  to: string;
  label: string;
  icon: IconName;
  badge?: number;
}) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        `relative flex min-h-14 flex-1 flex-col items-center justify-center gap-0.5 text-[10px] font-medium ${
          isActive ? "text-ink" : "text-muted"
        }`
      }
    >
      {({ isActive }) => (
        <>
          {isActive && <span className="absolute top-0 h-[3px] w-7 -skew-x-12 bg-accent" />}
          <Icon name={icon} size={19} />
          <span>{label}</span>
          {!!badge && (
            <span
              className="technical absolute left-1/2 top-1 ml-2 min-w-4 rounded-full bg-accent px-1 text-center text-[9px] text-[#17100d]"
              aria-label={`${badge} active job${badge === 1 ? "" : "s"}`}
            >
              {badge > 99 ? "99+" : badge}
            </span>
          )}
        </>
      )}
    </NavLink>
  );
}

function LauncherList({
  specs,
  remoteConnected,
  onNavigate,
}: {
  specs: GeneratorSpec[];
  remoteConnected: boolean;
  onNavigate: () => void;
}) {
  const group = (output: "image" | "video") => specs.filter((spec) => spec.output === output);
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-3 pb-5">
      {(["image", "video"] as const).map((output) => (
        <section key={output} className="mt-4 first:mt-1">
          <div className="mb-1 flex items-center gap-2 px-2">
            <Icon name={output} size={14} className="text-muted" />
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
              {output === "image" ? "Image making" : "Motion"}
            </h3>
          </div>
          <div className="divide-y divide-edge/70">
            {group(output).map((spec) => {
              const unavailable = !!spec.unavailable_reason || (spec.needs_remote && !remoteConnected);
              return (
                <Link
                  key={spec.id}
                  to={`/g/${spec.id}`}
                  onClick={onNavigate}
                  className="group relative flex items-start gap-3 px-2 py-3 transition-colors hover:bg-panel2 focus-visible:bg-panel2"
                >
                  <span className="technical mt-0.5 w-6 shrink-0 text-[10px] text-white/28">
                    {String(group(output).indexOf(spec) + 1).padStart(2, "0")}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium text-ink group-hover:text-white">
                      {spec.title}
                    </span>
                    <span className="mt-0.5 block line-clamp-2 text-xs leading-relaxed text-muted">
                      {spec.subtitle}
                    </span>
                  </span>
                  <span
                    className={`technical mt-0.5 shrink-0 text-[9px] uppercase ${
                      unavailable ? "text-warn" : spec.needs_remote ? "text-accent2" : "text-ok"
                    }`}
                  >
                    {unavailable ? "offline" : spec.needs_remote ? "A100" : "local"}
                  </span>
                </Link>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}

function SheetFrame({
  children,
  onClose,
  label,
  side = "left",
}: {
  children: ReactNode;
  onClose: () => void;
  label: string;
  side?: "left" | "bottom";
}) {
  if (side === "bottom") {
    return (
      <Modal
        size="fullscreen"
        onClose={onClose}
        label={label}
        className="flex-col bg-transparent shadow-none"
      >
        <div className="order-2 max-h-[78dvh] overflow-y-auto rounded-t-[14px] border-t border-edge bg-panel pb-[env(safe-area-inset-bottom)] pop-in">
          {children}
        </div>
        <button
          className="order-1 min-h-0 flex-1 cursor-default bg-black/55"
          aria-label={`Close ${label}`}
          onClick={onClose}
        />
      </Modal>
    );
  }
  return (
    <Modal size="fullscreen" onClose={onClose} label={label} className="flex-row bg-transparent shadow-none">
      <div className="hidden w-[72px] shrink-0 lg:block" aria-hidden="true" />
      <aside className="flex h-full w-[min(320px,92vw)] flex-col border-r border-edge bg-panel shadow-float sheet-in">
        {children}
      </aside>
      <button
        className="min-w-0 flex-1 cursor-default bg-black/55"
        aria-label={`Close ${label}`}
        onClick={onClose}
      />
    </Modal>
  );
}

export function Layout() {
  const specs = useStore((state) => state.specs);
  const system = useStore((state) => state.system);
  const active = useStore(
    (state) =>
      Object.values(state.jobs).filter((job) => job.status === "running" || job.status === "queued").length,
  );
  const [launcherOpen, setLauncherOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [systemOpen, setSystemOpen] = useState(false);
  const { pathname } = useLocation();
  const [previousPath, setPreviousPath] = useState(pathname);
  if (previousPath !== pathname) {
    setPreviousPath(pathname);
    if (launcherOpen) setLauncherOpen(false);
    if (moreOpen) setMoreOpen(false);
    if (systemOpen) setSystemOpen(false);
  }

  const gpu = system?.local_gpu;
  const remote = system?.remote_gpu;
  const vram = gpu?.available
    ? `${Math.round((gpu.memory_used_mb || 0) / 1024)}/${Math.round((gpu.memory_total_mb || 0) / 1024)} GB`
    : "offline";

  const systemDetails = (
    <div className="space-y-4">
      <div>
        <div className="page-kicker">Compute</div>
        <h2 className="text-lg font-semibold">Workshop status</h2>
      </div>
      <div className="space-y-3 border-t border-edge pt-4">
        <div>
          <StatusIndicator ok={!!gpu?.available} tone="local" label="Local GPU" detail={vram} />
          <div className="mt-1 pl-4 text-[11px] text-muted">
            {gpu?.name || "No local GPU reported"}
            {gpu?.available &&
              ` · ${gpu.utilization_pct || 0}% load${gpu.temperature_c ? ` · ${gpu.temperature_c}°C` : ""}`}
          </div>
        </div>
        <div>
          <StatusIndicator
            ok={!!remote?.connected}
            tone="remote"
            label="Remote GPU"
            detail={remote?.connected ? remote.gpu || "ready" : "disconnected"}
          />
          <div className="mt-1 pl-4 text-[11px] text-muted">
            {remote?.connected
              ? `${remote.queue_depth || 0} remote jobs waiting`
              : remote?.reason || "Connect from Settings"}
          </div>
        </div>
      </div>
      <div className="grid gap-2">
        <Link
          to="/diagnosis"
          className="btn-primary w-full"
          onClick={() => {
            setSystemOpen(false);
            setMoreOpen(false);
          }}
        >
          <Icon name="info" size={15} /> Open full diagnosis
        </Link>
        <Link
          to="/settings"
          className="btn w-full"
          onClick={() => {
            setSystemOpen(false);
            setMoreOpen(false);
          }}
        >
          <Icon name="settings" size={15} /> Configure connections
        </Link>
      </div>
    </div>
  );

  return (
    <div className="relative flex h-dvh overflow-hidden bg-bg text-ink">
      <aside className="relative z-20 hidden w-[72px] shrink-0 flex-col border-r border-edge bg-panel lg:flex">
        <Link
          to="/"
          className="flex h-[68px] items-center justify-center border-b border-edge text-accent"
          aria-label="The Wizard's Brush home"
        >
          <BrushMark className="size-9" />
        </Link>
        <nav className="flex min-h-0 flex-1 flex-col py-2" aria-label="Primary navigation">
          <RailLink to="/" label="Studio" icon="studio" />
          <button
            className={`relative flex h-[58px] w-full flex-col items-center justify-center gap-0.5 text-[9px] font-medium tracking-wide transition-colors ${
              launcherOpen || pathname.startsWith("/g/")
                ? "text-ink"
                : "text-muted hover:bg-panel2 hover:text-ink"
            }`}
            onClick={() => setLauncherOpen(true)}
            aria-label="Open generator launcher"
            aria-expanded={launcherOpen}
          >
            {(launcherOpen || pathname.startsWith("/g/")) && (
              <span className="absolute left-0 top-3 h-8 w-[3px] bg-accent" />
            )}
            <span className="mb-0.5 flex size-7 items-center justify-center rounded-[6px] bg-accent text-[#17100d]">
              <Icon name="make" size={17} />
            </span>
            <span>Make</span>
          </button>
          <RailLink to="/gallery" label="Gallery" icon="gallery" />
          <RailLink to="/queue" label="Queue" icon="queue" badge={active} />
          <RailLink to="/tools" label="Tools" icon="tools" />
          <div className="mt-auto border-t border-edge pt-2">
            <RailLink to="/trash" label="Trash" icon="trash" />
            <RailLink to="/settings" label="Settings" icon="settings" />
          </div>
        </nav>
        <button
          className="flex h-[54px] flex-col items-center justify-center gap-1 border-t border-edge text-[9px] text-muted hover:bg-panel2 hover:text-ink"
          aria-label="Open workshop status"
          aria-expanded={systemOpen}
          onClick={() => setSystemOpen((open) => !open)}
        >
          <span className="flex items-center gap-2">
            <span className={`size-1.5 rounded-full ${gpu?.available ? "bg-ok" : "bg-danger"}`} />
            <span className={`size-1.5 rounded-full ${remote?.connected ? "bg-accent2" : "bg-danger"}`} />
          </span>
          Compute
        </button>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-edge bg-panel px-4 lg:hidden">
          <Link to="/" className="flex items-center gap-2" aria-label="The Wizard's Brush home">
            <BrushMark className="size-7 text-accent" />
            <span className="display-title text-[21px] leading-none">The Wizard's Brush</span>
          </Link>
          <button
            className="flex min-h-11 items-center gap-2 text-xs text-muted"
            onClick={() => setMoreOpen(true)}
            aria-label="Open navigation"
            aria-expanded={moreOpen}
          >
            <span className="flex items-center gap-1.5">
              <span className={`size-1.5 rounded-full ${gpu?.available ? "bg-ok" : "bg-danger"}`} />
              <span className={`size-1.5 rounded-full ${remote?.connected ? "bg-accent2" : "bg-danger"}`} />
            </span>
            <Icon name="more" />
          </button>
        </header>
        <main className="min-h-0 flex-1 overflow-y-auto pb-[calc(64px+env(safe-area-inset-bottom))] lg:pb-0">
          <Outlet />
        </main>
      </div>

      <nav
        className="fixed inset-x-0 bottom-0 z-30 flex border-t border-edge bg-panel/98 pb-[env(safe-area-inset-bottom)] lg:hidden"
        aria-label="Mobile navigation"
      >
        <MobileLink to="/" label="Studio" icon="studio" />
        <button
          className={`relative flex min-h-14 flex-1 flex-col items-center justify-center gap-0.5 text-[10px] font-medium ${
            pathname.startsWith("/g/") ? "text-ink" : "text-muted"
          }`}
          onClick={() => setLauncherOpen(true)}
          aria-label="Open generator launcher"
          aria-expanded={launcherOpen}
        >
          {pathname.startsWith("/g/") && <span className="absolute top-0 h-[3px] w-7 -skew-x-12 bg-accent" />}
          <span className="flex size-7 items-center justify-center rounded-[6px] bg-accent text-[#17100d]">
            <Icon name="make" size={17} />
          </span>
          <span>Make</span>
        </button>
        <MobileLink to="/gallery" label="Gallery" icon="gallery" />
        <MobileLink to="/queue" label="Queue" icon="queue" badge={active} />
        <button
          className="flex min-h-14 flex-1 flex-col items-center justify-center gap-0.5 text-[10px] font-medium text-muted"
          onClick={() => setMoreOpen(true)}
        >
          <Icon name="more" size={19} />
          <span>More</span>
        </button>
      </nav>

      {launcherOpen && (
        <SheetFrame onClose={() => setLauncherOpen(false)} label="Generator launcher">
          <div className="flex h-[68px] shrink-0 items-center justify-between border-b border-edge px-4">
            <div>
              <div className="page-kicker mb-0">Make</div>
              <h2 className="text-lg font-semibold">Choose a process</h2>
            </div>
            <IconButton
              icon="close"
              label="Close generator launcher"
              onClick={() => setLauncherOpen(false)}
            />
          </div>
          <LauncherList
            specs={specs}
            remoteConnected={!!remote?.connected}
            onNavigate={() => setLauncherOpen(false)}
          />
        </SheetFrame>
      )}

      {moreOpen && (
        <SheetFrame side="bottom" onClose={() => setMoreOpen(false)} label="More navigation">
          <div className="flex items-center justify-between border-b border-edge px-4 py-3">
            <div>
              <div className="page-kicker mb-0">Workspace</div>
              <h2 className="text-lg font-semibold">More tools</h2>
            </div>
            <IconButton icon="close" label="Close navigation" onClick={() => setMoreOpen(false)} />
          </div>
          <nav className="grid grid-cols-3 gap-px bg-edge" aria-label="More destinations">
            {workspaceLinks.slice(3).map((item) => (
              <Link
                key={item.to}
                to={item.to}
                className="flex min-h-24 flex-col items-center justify-center gap-2 bg-panel text-sm text-muted hover:bg-panel2 hover:text-ink"
              >
                <Icon name={item.icon} size={22} />
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="p-5">{systemDetails}</div>
        </SheetFrame>
      )}

      {systemOpen && (
        <>
          <button
            className="fixed inset-0 z-30 bg-black/20"
            aria-label="Close workshop status"
            onClick={() => setSystemOpen(false)}
          />
          <div className="fixed bottom-3 left-[84px] z-40 w-[340px] border border-edge bg-panel3 p-5 shadow-float pop-in">
            {systemDetails}
          </div>
        </>
      )}
      <Toaster />
    </div>
  );
}

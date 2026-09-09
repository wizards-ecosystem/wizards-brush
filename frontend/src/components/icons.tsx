import type { ReactNode, SVGProps } from "react";

export type IconName =
  | "studio"
  | "make"
  | "gallery"
  | "queue"
  | "tools"
  | "trash"
  | "settings"
  | "more"
  | "menu"
  | "close"
  | "history"
  | "bookmark"
  | "braces"
  | "chevron-down"
  | "chevron-right"
  | "chevron-left"
  | "arrow-up"
  | "arrow-down"
  | "arrow-right"
  | "search"
  | "filter"
  | "image"
  | "video"
  | "layers"
  | "sliders"
  | "heart"
  | "star"
  | "check"
  | "alert"
  | "info"
  | "play"
  | "pause"
  | "skip"
  | "undo"
  | "redo"
  | "compare"
  | "refresh"
  | "copy"
  | "download"
  | "edit"
  | "plus"
  | "external"
  | "restore"
  | "spark"
  | "gpu"
  | "cloud";

export function Icon({
  name,
  size = 18,
  strokeWidth = 1.7,
  ...props
}: SVGProps<SVGSVGElement> & {
  name: IconName;
  size?: number;
}) {
  const common = {
    fill: "none",
    stroke: "currentColor",
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    strokeWidth,
  };

  let body: ReactNode;
  switch (name) {
    case "studio":
      body = (
        <>
          <path {...common} d="M4 4h6v6H4zM14 4h6v10h-6zM4 14h6v6H4zM14 18h6v2h-6z" />
        </>
      );
      break;
    case "make":
      body = (
        <>
          <circle {...common} cx="12" cy="12" r="7" />
          <path {...common} d="M12 2v3M12 19v3M2 12h3M19 12h3M7.2 7.2 5 5M19 19l-2.2-2.2" />
          <circle {...common} cx="12" cy="12" r="2" />
        </>
      );
      break;
    case "gallery":
      body = (
        <>
          <rect {...common} x="3" y="5" width="18" height="15" rx="2" />
          <path {...common} d="m3 16 5-5 4 4 3-3 6 6" />
          <circle {...common} cx="16.5" cy="9" r="1.5" />
        </>
      );
      break;
    case "queue":
      body = (
        <>
          <path {...common} d="M8 6h13M8 12h13M8 18h13" />
          <circle {...common} cx="3.5" cy="6" r="1" />
          <circle {...common} cx="3.5" cy="12" r="1" />
          <circle {...common} cx="3.5" cy="18" r="1" />
        </>
      );
      break;
    case "tools":
    case "sliders":
      body = (
        <>
          <path {...common} d="M4 6h7M15 6h5M4 12h3M11 12h9M4 18h10M18 18h2" />
          <circle {...common} cx="13" cy="6" r="2" />
          <circle {...common} cx="9" cy="12" r="2" />
          <circle {...common} cx="16" cy="18" r="2" />
        </>
      );
      break;
    case "trash":
      body = (
        <>
          <path {...common} d="M4 7h16M9 3h6l1 4M7 7l1 14h8l1-14M10 11v6M14 11v6" />
        </>
      );
      break;
    case "settings":
      body = (
        <>
          <circle {...common} cx="12" cy="12" r="3" />
          <path
            {...common}
            d="M19 14.5l1.5 1.2-2 3.4-1.9-.7a8 8 0 0 1-2.1 1.2L14.2 22h-4.4l-.4-2.4a8 8 0 0 1-2-1.2l-2 .7-2-3.4L5 14.5a8 8 0 0 1 0-2.5l-1.6-1.2 2-3.4 2 .7a8 8 0 0 1 2-1.2l.4-2.4h4.4l.4 2.4a8 8 0 0 1 2 1.2l2-.7 2 3.4L19 12a8 8 0 0 1 0 2.5Z"
          />
        </>
      );
      break;
    case "more":
      body = (
        <>
          <circle {...common} cx="5" cy="12" r="1" />
          <circle {...common} cx="12" cy="12" r="1" />
          <circle {...common} cx="19" cy="12" r="1" />
        </>
      );
      break;
    case "menu":
      body = <path {...common} d="M4 7h16M4 12h16M4 17h16" />;
      break;
    case "close":
      body = <path {...common} d="m6 6 12 12M18 6 6 18" />;
      break;
    case "history":
      body = (
        <>
          <path {...common} d="M4 5v5h5M5.5 9a8 8 0 1 1-.5 5" />
          <path {...common} d="M12 8v5l3 2" />
        </>
      );
      break;
    case "bookmark":
      body = <path {...common} d="M6 3h12v18l-6-4-6 4z" />;
      break;
    case "braces":
      body = (
        <path
          {...common}
          d="M9 3H7a2 2 0 0 0-2 2v4a3 3 0 0 1-2 3 3 3 0 0 1 2 3v4a2 2 0 0 0 2 2h2M15 3h2a2 2 0 0 1 2 2v4a3 3 0 0 0 2 3 3 3 0 0 0-2 3v4a2 2 0 0 1-2 2h-2"
        />
      );
      break;
    case "chevron-down":
      body = <path {...common} d="m6 9 6 6 6-6" />;
      break;
    case "chevron-right":
      body = <path {...common} d="m9 6 6 6-6 6" />;
      break;
    case "chevron-left":
      body = <path {...common} d="m15 6-6 6 6 6" />;
      break;
    case "arrow-up":
      body = <path {...common} d="M12 20V4m-6 6 6-6 6 6" />;
      break;
    case "arrow-down":
      body = <path {...common} d="M12 4v16m6-6-6 6-6-6" />;
      break;
    case "arrow-right":
      body = <path {...common} d="M4 12h16m-6-6 6 6-6 6" />;
      break;
    case "search":
      body = (
        <>
          <circle {...common} cx="10.5" cy="10.5" r="6.5" />
          <path {...common} d="m16 16 5 5" />
        </>
      );
      break;
    case "filter":
      body = <path {...common} d="M4 5h16l-6.5 7.5V19l-3 1v-7.5z" />;
      break;
    case "image":
      body = (
        <>
          <rect {...common} x="3" y="4" width="18" height="16" rx="2" />
          <path {...common} d="m4 17 5-5 3 3 2-2 6 6" />
          <circle {...common} cx="16" cy="9" r="1.5" />
        </>
      );
      break;
    case "video":
      body = (
        <>
          <rect {...common} x="3" y="6" width="13" height="12" rx="2" />
          <path {...common} d="m16 10 5-3v10l-5-3" />
        </>
      );
      break;
    case "layers":
      body = (
        <>
          <path {...common} d="m12 3 9 5-9 5-9-5z" />
          <path {...common} d="m3 12 9 5 9-5M3 16l9 5 9-5" />
        </>
      );
      break;
    case "heart":
      body = (
        <path
          {...common}
          d="M20.8 5.8a5 5 0 0 0-7.1 0L12 7.5l-1.7-1.7a5 5 0 0 0-7.1 7.1L12 21l8.8-8.1a5 5 0 0 0 0-7.1Z"
        />
      );
      break;
    case "star":
      body = <path {...common} d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z" />;
      break;
    case "check":
      body = <path {...common} d="m5 12 4 4L19 6" />;
      break;
    case "alert":
      body = (
        <>
          <path {...common} d="M12 3 2.5 20h19z" />
          <path {...common} d="M12 9v5M12 17.5h.01" />
        </>
      );
      break;
    case "info":
      body = (
        <>
          <circle {...common} cx="12" cy="12" r="9" />
          <path {...common} d="M12 11v6M12 7h.01" />
        </>
      );
      break;
    case "play":
      body = <path {...common} d="m8 5 11 7-11 7z" />;
      break;
    case "pause":
      body = <path {...common} d="M8 5v14M16 5v14" />;
      break;
    case "skip":
      body = (
        <>
          <path {...common} d="m5 5 9 7-9 7zM18 5v14" />
        </>
      );
      break;
    case "undo":
      body = <path {...common} d="m9 7-5 5 5 5M5 12h8a6 6 0 0 1 6 6" />;
      break;
    case "redo":
      body = <path {...common} d="m15 7 5 5-5 5m4-5h-8a6 6 0 0 0-6 6" />;
      break;
    case "compare":
      body = (
        <>
          <path {...common} d="M8 7h12m-3-3 3 3-3 3M16 17H4m3-3-3 3 3 3" />
        </>
      );
      break;
    case "refresh":
      body = (
        <>
          <path {...common} d="M20 7v5h-5M4 17v-5h5" />
          <path {...common} d="M6.1 8a7 7 0 0 1 11.7-1L20 12M4 12l2.2 5a7 7 0 0 0 11.7-1" />
        </>
      );
      break;
    case "copy":
      body = (
        <>
          <rect {...common} x="8" y="8" width="12" height="12" rx="2" />
          <path {...common} d="M16 8V4H4v12h4" />
        </>
      );
      break;
    case "download":
      body = (
        <>
          <path {...common} d="M12 3v12m-5-5 5 5 5-5M4 20h16" />
        </>
      );
      break;
    case "edit":
      body = (
        <>
          <path {...common} d="m4 20 4.5-1 10-10-3.5-3.5-10 10zM13.5 7 17 10.5" />
        </>
      );
      break;
    case "plus":
      body = <path {...common} d="M12 5v14M5 12h14" />;
      break;
    case "external":
      body = (
        <>
          <path {...common} d="M14 4h6v6M20 4l-9 9" />
          <path {...common} d="M18 13v7H4V6h7" />
        </>
      );
      break;
    case "restore":
      body = (
        <>
          <path {...common} d="M4 5v5h5M5.2 9.3A8 8 0 1 1 5 15" />
        </>
      );
      break;
    case "spark":
      body = (
        <>
          <path {...common} d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5z" />
          <path {...common} d="m19 16 .6 2.4L22 19l-2.4.6L19 22l-.6-2.4L16 19l2.4-.6z" />
        </>
      );
      break;
    case "gpu":
      body = (
        <>
          <rect {...common} x="4" y="6" width="16" height="12" rx="2" />
          <rect {...common} x="8" y="9" width="8" height="6" rx="1" />
          <path {...common} d="M7 3v3M12 3v3M17 3v3M7 18v3M12 18v3M17 18v3" />
        </>
      );
      break;
    case "cloud":
      body = <path {...common} d="M7 19h11a4 4 0 0 0 .7-7.9A7 7 0 0 0 5.3 9.4 4.8 4.8 0 0 0 7 19Z" />;
      break;
  }

  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden={props["aria-label"] ? undefined : true}
      focusable="false"
      {...props}
    >
      {body}
    </svg>
  );
}

export function BrushMark({ className = "" }: { className?: string }) {
  return (
    <img
      src="/brand/brush-icon-dark.svg"
      className={className}
      width="128"
      height="128"
      alt=""
      aria-hidden="true"
    />
  );
}

import {
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type MutableRefObject,
  type ReactNode,
} from "react";
import { Icon, type IconName } from "./icons";

const cx = (...values: Array<string | false | null | undefined>) => values.filter(Boolean).join(" ");

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={cx(
        "inline-block size-4 rounded-full border-2 border-current/25 border-t-current animate-spin",
        className,
      )}
      aria-hidden="true"
    />
  );
}

export type ButtonVariant = "primary" | "secondary" | "quiet" | "danger";
export function Button({
  variant = "secondary",
  size = "md",
  icon,
  loading = false,
  className,
  children,
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  icon?: IconName;
  loading?: boolean;
}) {
  const variantClass = variant === "primary" ? "btn-primary" : variant === "quiet" ? "btn-ghost" : "btn";
  return (
    <button
      className={cx(
        variantClass,
        size === "sm" && "text-xs",
        variant === "danger" && "text-danger",
        className,
      )}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <Spinner /> : icon ? <Icon name={icon} size={size === "sm" ? 15 : 17} /> : null}
      {children}
    </button>
  );
}

export function Tooltip({
  label,
  children,
  align = "center",
}: {
  label: string;
  children: ReactNode;
  align?: "start" | "center" | "end";
}) {
  return (
    <span className="group/tooltip relative inline-flex">
      {children}
      <span
        role="tooltip"
        className={cx(
          "pointer-events-none absolute z-[120] hidden whitespace-nowrap border border-edge bg-panel3 px-2 py-1 text-[11px] text-ink shadow-float group-hover/tooltip:block group-focus-within/tooltip:block",
          "left-1/2 top-full mt-1.5 -translate-x-1/2 rounded-[4px]",
          align === "start" && "left-0 translate-x-0",
          align === "end" && "right-0 left-auto translate-x-0",
        )}
      >
        {label}
      </span>
    </span>
  );
}

export function IconButton({
  icon,
  label,
  className,
  title,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { icon: IconName; label: string }) {
  return (
    <button className={cx("icon-btn", className)} aria-label={label} title={title ?? label} {...props}>
      <Icon name={icon} />
    </button>
  );
}

export function SegmentedControl<T extends string>({
  value,
  options,
  onChange,
  label,
  className,
}: {
  value: T;
  options: ReadonlyArray<{ value: T; label: string }>;
  onChange: (value: T) => void;
  label: string;
  className?: string;
}) {
  return (
    <div
      className={cx("flex gap-1 rounded-[7px] border border-edge bg-panel p-1", className)}
      role="group"
      aria-label={label}
    >
      {options.map((option) => (
        <button
          key={option.value}
          className={cx("seg", value === option.value && "seg-active")}
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function PageHeader({
  kicker,
  title,
  description,
  meta,
  actions,
  className,
}: {
  kicker?: string;
  title: string;
  description?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cx("flex flex-wrap items-end justify-between gap-4", className)}>
      <div className="min-w-0">
        {kicker && <div className="page-kicker">{kicker}</div>}
        <div className="flex min-w-0 items-baseline gap-3">
          <h1 className="page-title">{title}</h1>
          {meta && <span className="technical text-xs text-muted">{meta}</span>}
        </div>
        {description && <div className="mt-1 max-w-2xl text-sm text-muted">{description}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function EmptyState({
  icon = "spark",
  title,
  description,
  action,
  className,
}: {
  icon?: IconName;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("flex flex-col items-start gap-3 text-left", className)}>
      <div className="empty-mark">
        <Icon name={icon} size={24} />
      </div>
      <div>
        <div className="font-medium text-ink">{title}</div>
        {description && <div className="mt-1 max-w-sm text-sm leading-relaxed text-muted">{description}</div>}
      </div>
      {action}
    </div>
  );
}

export function StatusIndicator({
  ok,
  label,
  detail,
  tone,
  compact = false,
}: {
  ok: boolean;
  label: string;
  detail?: ReactNode;
  tone?: "local" | "remote";
  compact?: boolean;
}) {
  const color = !ok ? "text-danger" : tone === "remote" ? "text-accent2" : "text-ok";
  return (
    <span className={cx("inline-flex min-w-0 items-center gap-2", compact ? "text-[11px]" : "text-xs")}>
      <span className={cx("status-dot", color, ok ? "bg-current" : "border border-current bg-transparent")} />
      <span className="truncate text-ink/78">{label}</span>
      {detail && !compact && <span className="technical truncate text-muted">{detail}</span>}
    </span>
  );
}

let modalDepth = 0;
let savedOverflow = "";

/** Accessible overlay foundation used by dialogs, inspectors and lightboxes. */
export function Modal({
  children,
  onClose,
  wide = false,
  size,
  label = "Dialog",
  description,
  initialFocusRef,
  className,
}: {
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
  size?: "standard" | "wide" | "fullscreen";
  label?: string;
  description?: string;
  initialFocusRef?: MutableRefObject<HTMLElement | null>;
  className?: string;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  const descriptionId = useId();
  const resolvedSize = size ?? (wide ? "wide" : "standard");
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const restoreTo = document.activeElement as HTMLElement | null;
    const level = ++modalDepth;
    if (modalDepth === 1) {
      savedOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }

    const dialog = dialogRef.current;
    const focusables = () =>
      Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter((el) => !el.hasAttribute("hidden"));

    const frame = requestAnimationFrame(() => {
      (initialFocusRef?.current ?? focusables()[0] ?? dialog)?.focus();
    });

    const onKey = (event: KeyboardEvent) => {
      if (level !== modalDepth) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusables();
      if (!items.length) {
        event.preventDefault();
        dialog?.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("keydown", onKey);
      modalDepth -= 1;
      if (modalDepth === 0) document.body.style.overflow = savedOverflow;
      restoreTo?.focus();
    };
  }, [initialFocusRef]);

  return (
    <div
      className={cx(
        "fixed inset-0 z-50 flex bg-black/78 animate-in",
        resolvedSize === "fullscreen"
          ? "items-stretch justify-stretch p-0"
          : "items-center justify-center p-3 sm:p-6",
      )}
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={cx(
          "flex max-h-[94dvh] w-full flex-col overflow-hidden border border-edge bg-panel shadow-float pop-in",
          resolvedSize === "standard" && "max-w-xl rounded-[10px]",
          resolvedSize === "wide" && "max-w-6xl rounded-[10px]",
          resolvedSize === "fullscreen" && "h-dvh max-h-none max-w-none border-0 rounded-none",
          className,
        )}
      >
        {description && (
          <span id={descriptionId} className="sr-only">
            {description}
          </span>
        )}
        {children}
      </div>
    </div>
  );
}

export function NameDialog({
  title,
  placeholder = "",
  initial = "",
  onSubmit,
  onClose,
}: {
  title: string;
  placeholder?: string;
  initial?: string;
  onSubmit: (name: string) => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState(initial);
  const ref = useRef<HTMLInputElement>(null);
  const inputId = useId();
  const submit = () => {
    const next = value.trim();
    if (!next) return;
    onSubmit(next);
    onClose();
  };
  return (
    <Modal onClose={onClose} label={title} initialFocusRef={ref}>
      <form
        className="space-y-5 p-5"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <div>
          <div className="page-kicker">Name</div>
          <h2 className="text-lg font-semibold">{title}</h2>
        </div>
        <div>
          <label className="label" htmlFor={inputId}>
            Name
          </label>
          <input
            id={inputId}
            ref={ref}
            className="input"
            placeholder={placeholder}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </div>
        <div className="flex justify-end gap-2 border-t border-edge pt-4">
          <Button variant="quiet" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" disabled={!value.trim()}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel = "Delete",
  danger = true,
  onConfirm,
  onClose,
}: {
  title: string;
  message?: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Modal onClose={onClose} label={title} description={message}>
      <div className="space-y-5 p-5">
        <div className="flex items-start gap-3">
          <div
            className={cx(
              "mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-[7px]",
              danger ? "bg-danger/12 text-danger" : "bg-accent/12 text-accent",
            )}
          >
            <Icon name={danger ? "alert" : "info"} />
          </div>
          <div>
            <h2 className="text-lg font-semibold">{title}</h2>
            {message && <p className="mt-1 text-sm leading-relaxed text-muted">{message}</p>}
          </div>
        </div>
        <div className="flex justify-end gap-2 border-t border-edge pt-4">
          <Button variant="quiet" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant={danger ? "danger" : "primary"}
            onClick={() => {
              onConfirm();
              onClose();
            }}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export function StarRating({
  value,
  onChange,
  size = "text-base",
}: {
  value: number;
  onChange?: (next: number) => void;
  size?: string;
}) {
  return (
    <div className={cx("flex gap-0.5", size)} role={onChange ? "group" : undefined} aria-label="Rating">
      {[1, 2, 3, 4, 5].map((rating) =>
        onChange ? (
          <button
            key={rating}
            className={cx(
              "rounded-[3px] p-0.5",
              rating <= value ? "text-warn" : "text-white/25",
              "hover:text-warn",
            )}
            onClick={(event) => {
              event.stopPropagation();
              onChange(rating === value ? 0 : rating);
            }}
            aria-label={`${rating} star${rating > 1 ? "s" : ""}`}
            aria-pressed={rating <= value}
          >
            <Icon name="star" size={15} />
          </button>
        ) : (
          <Icon
            key={rating}
            name="star"
            size={14}
            className={rating <= value ? "text-warn" : "text-white/18"}
          />
        ),
      )}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={cx("skeleton", className)} aria-hidden="true" />;
}

export function fmtBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

import { useStore } from "../store/useStore";
import { Icon } from "./icons";

const STYLE: Record<string, string> = {
  info: "border-edge bg-panel3",
  success: "border-ok/45 bg-panel3",
  error: "border-danger/55 bg-panel3",
};

export function Toaster() {
  const toasts = useStore((s) => s.toasts);
  const dismiss = useStore((s) => s.dismissToast);
  return (
    <div
      className="fixed bottom-[calc(76px+env(safe-area-inset-bottom))] right-3 z-[100] flex w-80 max-w-[calc(100vw-24px)] flex-col gap-2 lg:bottom-4 lg:right-4"
      aria-live="polite"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`pop-in flex cursor-pointer items-start gap-3 border-l-[3px] p-3 shadow-float ${STYLE[t.kind]}`}
          onClick={() => dismiss(t.id)}
          // Errors interrupt (assertive); info/success announce politely.
          role={t.kind === "error" ? "alert" : "status"}
        >
          <Icon
            name={t.kind === "success" ? "check" : t.kind === "error" ? "alert" : "info"}
            size={16}
            className={`mt-0.5 shrink-0 ${t.kind === "success" ? "text-ok" : t.kind === "error" ? "text-danger" : "text-muted"}`}
          />
          <span className="flex-1 break-words text-sm text-ink/92">{t.text}</span>
          <button
            className="shrink-0 text-muted hover:text-ink"
            aria-label="Dismiss notification"
            onClick={(event) => {
              event.stopPropagation();
              dismiss(t.id);
            }}
          >
            <Icon name="close" size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}

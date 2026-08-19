import type { JobStatus } from "@/lib/api";
import { humanizeStatus } from "@/lib/format";

export interface StatusChipProps {
  status: JobStatus | string;
  /** Show the leading dot (pulsing for in-flight states). */
  dot?: boolean;
  size?: "sm" | "md";
  className?: string;
}

interface StatusStyle {
  label: string;
  /** Tailwind classes built from semantic tokens — no hardcoded hex. */
  chip: string;
  dot: string;
  /** In-flight states get an animated dot. */
  pulse: boolean;
}

const STATUS_STYLES: Record<string, StatusStyle> = {
  QUEUED: {
    label: "Queued",
    chip: "bg-status-queued-bg text-status-queued",
    dot: "bg-status-queued",
    pulse: false,
  },
  SENDING: {
    label: "Sending",
    chip: "bg-status-sending-bg text-status-sending",
    dot: "bg-status-sending",
    pulse: true,
  },
  AWAITING_CONFIRM: {
    label: "Awaiting confirm",
    chip: "bg-status-awaiting-bg text-status-awaiting",
    dot: "bg-status-awaiting",
    pulse: true,
  },
  SENT: {
    label: "Sent",
    chip: "bg-status-sent-bg text-status-sent",
    dot: "bg-status-sent",
    pulse: false,
  },
  FAILED: {
    label: "Failed",
    chip: "bg-status-failed-bg text-status-failed",
    dot: "bg-status-failed",
    pulse: false,
  },
  CANCELLED: {
    label: "Cancelled",
    chip: "bg-status-cancelled-bg text-status-cancelled",
    dot: "bg-status-cancelled",
    pulse: false,
  },
};

const FALLBACK: StatusStyle = {
  label: "Unknown",
  chip: "bg-surface-2 text-ink-muted",
  dot: "bg-ink-muted",
  pulse: false,
};

const SIZES = {
  sm: "px-2.5 py-0.5 text-[11px] gap-1.5",
  md: "px-3 py-1 text-xs gap-2",
} as const;

/**
 * Pill chip for a job state. AWAITING_CONFIRM and SENDING carry a pulsing dot;
 * the pulse is disabled under prefers-reduced-motion (see globals.css).
 */
export function StatusChip({
  status,
  dot = true,
  size = "md",
  className,
}: StatusChipProps) {
  const key = String(status).toUpperCase();
  const style = STATUS_STYLES[key] ?? {
    ...FALLBACK,
    label: humanizeStatus(String(status)),
  };

  return (
    <span
      className={[
        "inline-flex items-center rounded-full font-semibold uppercase tracking-[0.08em] whitespace-nowrap",
        style.chip,
        SIZES[size],
        className ?? "",
      ].join(" ")}
    >
      {dot ? (
        <span
          aria-hidden="true"
          className={[
            "inline-block h-1.5 w-1.5 shrink-0 rounded-full",
            style.dot,
            style.pulse ? "animate-status-pulse" : "",
          ].join(" ")}
        />
      ) : null}
      {style.label}
    </span>
  );
}

export default StatusChip;

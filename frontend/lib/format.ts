/** Number, date and relative-time formatting helpers. */

const numberFormatter = new Intl.NumberFormat("en-US");

/** 1234 → "1,234". Non-finite input renders as an em dash. */
export function formatNumber(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return numberFormatter.format(value);
}

/** 1234 → "1.2k", 1_500_000 → "1.5M". For tight stat tiles. */
export function formatCompact(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs < 1000) return String(Math.round(value));
  if (abs < 1_000_000) return `${trimZero(value / 1000)}k`;
  return `${trimZero(value / 1_000_000)}M`;
}

function trimZero(n: number): string {
  return n.toFixed(1).replace(/\.0$/, "");
}

/** 0.732 → "73.2%". `value` is a ratio, not an already-scaled percentage. */
export function formatPercent(
  value: number | null | undefined,
  digits = 1,
): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/** Seconds → "4.2s" / "3m 12s" / "1h 04m". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, seconds);
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    return `${m}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  }
  const h = Math.floor(s / 3600);
  return `${h}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

function toDate(input: string | number | Date | null | undefined): Date | null {
  if (input == null) return null;
  const d = input instanceof Date ? input : new Date(input);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "14:03:22" — local wall-clock time, good for a live feed. */
export function formatTime(
  input: string | number | Date | null | undefined,
): string {
  const d = toDate(input);
  if (!d) return "—";
  return d.toLocaleTimeString("en-US", { hour12: false });
}

/** "Aug 19, 14:03" */
export function formatDateTime(
  input: string | number | Date | null | undefined,
): string {
  const d = toDate(input);
  if (!d) return "—";
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31_536_000],
  ["month", 2_592_000],
  ["week", 604_800],
  ["day", 86_400],
  ["hour", 3600],
  ["minute", 60],
  ["second", 1],
];

const relativeFormatter = new Intl.RelativeTimeFormat("en-US", {
  numeric: "auto",
});

/**
 * "just now" / "12s ago" / "3 minutes ago".
 *
 * Note for callers: this reads the wall clock, so rendering it during SSR and
 * then again on the client can produce a hydration mismatch. Render it inside a
 * client component that has mounted (or key it off polled data).
 */
export function formatRelative(
  input: string | number | Date | null | undefined,
  now: number = Date.now(),
): string {
  const d = toDate(input);
  if (!d) return "—";
  const deltaSeconds = (d.getTime() - now) / 1000;
  const abs = Math.abs(deltaSeconds);
  if (abs < 5) return "just now";
  for (const [unit, secondsInUnit] of RELATIVE_UNITS) {
    if (abs >= secondsInUnit || unit === "second") {
      return relativeFormatter.format(
        Math.round(deltaSeconds / secondsInUnit),
        unit,
      );
    }
  }
  return "just now";
}

/** Truncates with an ellipsis, never mid-surrogate-pair. */
export function truncate(value: string, max = 60): string {
  const chars = Array.from(value);
  if (chars.length <= max) return value;
  return `${chars.slice(0, max - 1).join("")}…`;
}

/** "QUEUED" / "AWAITING_CONFIRM" → "Queued" / "Awaiting confirm". */
export function humanizeStatus(status: string): string {
  const lower = status.toLowerCase().replace(/_/g, " ");
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

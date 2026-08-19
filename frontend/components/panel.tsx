import type { ReactNode } from "react";

export interface PanelProps {
  /** Small uppercase letter-spaced blue label above the title. */
  kicker?: string;
  title?: ReactNode;
  /** One honest sentence explaining what this panel actually shows. */
  description?: ReactNode;
  /** Rendered top-right — refresh timestamps, counts, small controls. */
  actions?: ReactNode;
  children: ReactNode;
  /** Remove the body padding when the child draws its own edge-to-edge table. */
  flush?: boolean;
  className?: string;
  id?: string;
}

/**
 * The white rounded-3xl card that every dashboard section sits on. Header and
 * body are separate so a table can run flush to the card edge while the header
 * keeps its padding.
 */
export function Panel({
  kicker,
  title,
  description,
  actions,
  children,
  flush = false,
  className,
  id,
}: PanelProps) {
  const hasHeader = Boolean(kicker || title || description || actions);

  return (
    <section
      id={id}
      className={[
        "rounded-[var(--radius-card)] border border-line bg-surface shadow-[var(--shadow-card)]",
        className ?? "",
      ].join(" ")}
    >
      {hasHeader ? (
        <div className="flex flex-col gap-4 border-b border-line px-5 py-5 sm:flex-row sm:items-start sm:justify-between sm:px-7 sm:py-6">
          <div className="min-w-0">
            {kicker ? (
              <span className="text-xs font-semibold uppercase tracking-[0.2em] text-accent">
                {kicker}
              </span>
            ) : null}
            {title ? (
              <h2 className="mt-2 text-xl font-bold tracking-tight text-ink sm:text-2xl">
                {title}
              </h2>
            ) : null}
            {description ? (
              <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-muted">
                {description}
              </p>
            ) : null}
          </div>
          {actions ? (
            <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
              {actions}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className={flush ? "" : "px-5 py-5 sm:px-7 sm:py-6"}>{children}</div>
    </section>
  );
}

/**
 * Amber "the backend did not answer" banner. Deliberately states what is
 * unknown rather than rendering a zero that looks like real data.
 */
export function ErrorBanner({
  title = "Backend unreachable",
  message,
  hint,
  className,
}: {
  title?: string;
  message?: string;
  hint?: ReactNode;
  className?: string;
}) {
  return (
    <div
      role="status"
      className={[
        "rounded-2xl border border-status-queued/30 bg-status-queued-bg px-4 py-3.5",
        className ?? "",
      ].join(" ")}
    >
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className="mt-1.5 inline-block h-2 w-2 shrink-0 rounded-full bg-status-queued"
        />
        <div className="min-w-0 text-sm">
          <p className="font-semibold text-status-queued">{title}</p>
          {message ? (
            <p className="mt-1 break-words text-status-queued/85">{message}</p>
          ) : null}
          {hint ? (
            <p className="mt-1 text-status-queued/75">{hint}</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/** Neutral "nothing here yet, and here is why" state. Never a bare dash. */
export function EmptyState({
  title,
  detail,
  className,
}: {
  title: string;
  detail?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={[
        "rounded-2xl border border-dashed border-line px-5 py-10 text-center",
        className ?? "",
      ].join(" ")}
    >
      <p className="text-sm font-semibold text-ink">{title}</p>
      {detail ? (
        <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
          {detail}
        </p>
      ) : null}
    </div>
  );
}

/** Grey shimmer block used while the first poll is in flight. */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={[
        "animate-pulse rounded-lg bg-surface-2 motion-reduce:animate-none",
        className ?? "",
      ].join(" ")}
    />
  );
}

export default Panel;

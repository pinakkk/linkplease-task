"use client";

import {
  getExtendedStats,
  type ExtendedStats,
  type MetricKey,
  type RequestOptions,
} from "@/lib/api";
import { usePoll } from "@/lib/use-poll";
import Reveal, { RevealItem } from "./reveal";
import StatCard from "./stat-card";
import { ErrorBanner, Skeleton } from "./panel";

/** Stable module-level fetcher — usePoll requires a stable reference. */
function fetchExtended(options?: RequestOptions) {
  return getExtendedStats(options);
}

export interface LiveStatsProps {
  /** Poll cadence. 2s on the dashboard; slower on the landing page. */
  intervalMs?: number;
  /** Scroll-triggered count-up (landing) vs continuous morph (dashboard). */
  animateOnView?: boolean;
  /** Reveal the cards on scroll — off on the dashboard, which is above the fold. */
  reveal?: boolean;
  /** Called on every successful poll so a parent can share the same data. */
  onData?: (stats: ExtendedStats) => void;
  className?: string;
}

const CARDS: {
  key: MetricKey;
  label: string;
  hint: string;
  tone?: "surface" | "navy";
}[] = [
  {
    key: "sent",
    label: "DMs sent",
    hint: "Confirmed delivered, not merely accepted",
  },
  {
    key: "failed",
    label: "Failed",
    hint: "Retry and resend budgets exhausted",
  },
  {
    key: "queued",
    label: "Queued",
    hint: "Still owed, including unconfirmed 202s",
  },
  {
    key: "duplicates_blocked",
    label: "Duplicates blocked",
    hint: "Second DMs we refused to send",
  },
];

/**
 * The four graded numbers, live from `/api/stats/extended`.
 *
 * When the backend cannot be reached we render an amber banner instead of the
 * grid. Showing four zeros would be indistinguishable from a genuinely idle
 * pipeline, and that is exactly the kind of number this project is about not
 * inventing.
 */
export function LiveStats({
  intervalMs = 5000,
  animateOnView = true,
  reveal = true,
  className,
}: LiveStatsProps) {
  const { data, error, loading } = usePoll(fetchExtended, intervalMs);

  const gridClass = [
    "grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4",
    className ?? "",
  ].join(" ");

  if (loading && !data) {
    return (
      <div className={gridClass}>
        {CARDS.map((card) => (
          <Skeleton key={card.key} className="h-36 w-full rounded-[var(--radius-card)]" />
        ))}
      </div>
    );
  }

  if (!data) {
    return (
      <ErrorBanner
        className={className}
        title="Live numbers unavailable"
        message={error?.message}
        hint="These cards stay blank on purpose. Rendering zeros here would look exactly like an idle pipeline, and we would rather show nothing than show something we cannot verify."
      />
    );
  }

  const cards = CARDS.map((card) => (
    <StatCard
      key={card.key}
      value={data[card.key] ?? 0}
      label={card.label}
      hint={card.hint}
      animateOnView={animateOnView}
      tone={card.tone}
    />
  ));

  return (
    <>
      {error ? (
        <ErrorBanner
          className="mb-4"
          title="Showing the last good reading"
          message={error.message}
          hint="The numbers below are from the most recent successful poll, not from right now."
        />
      ) : null}

      {reveal ? (
        <Reveal stagger className={gridClass}>
          {cards.map((card, i) => (
            <RevealItem key={CARDS[i].key}>{card}</RevealItem>
          ))}
        </Reveal>
      ) : (
        <div className={gridClass}>{cards}</div>
      )}
    </>
  );
}

/** Shared fetcher so the dashboard can poll the same route without duplicating it. */
export { fetchExtended };

export default LiveStats;

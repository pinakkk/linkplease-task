"use client";

import { motion, useReducedMotion } from "motion/react";
import { CountUp } from "./count-up";
import type { RateBudget } from "@/lib/api";

export interface RateMeterProps {
  budget: RateBudget | undefined;
  /** True while the first poll is still in flight. */
  loading?: boolean;
  className?: string;
}

/** PseudoGram's documented ceiling. We never spend the tenth slot. */
const PLATFORM_LIMIT = 10;

/**
 * Visualises the send budget as discrete slots across the rolling window.
 *
 * The point this has to make visually: the tenth slot is greyed out and struck
 * through because *we* refuse to use it, not because the platform took it away.
 * Banking one request of headroom absorbs clock skew between our window and
 * theirs, so a burst can never tip us into a 429.
 */
export function RateMeter({ budget, loading = false, className }: RateMeterProps) {
  const reduced = useReducedMotion();

  const max = budget?.max ?? 9;
  const windowSeconds = budget?.window_seconds ?? 60;
  const used = Math.max(0, Math.min(budget?.used ?? 0, max));
  const known = budget?.used != null;

  // Render every slot the platform allows, so the self-imposed gap is visible.
  const slots = Array.from({ length: Math.max(max, PLATFORM_LIMIT) }, (_, i) => {
    const index = i + 1;
    if (index > max) return "forfeited" as const;
    return index <= used ? ("used" as const) : ("free" as const);
  });

  return (
    <div className={className}>
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div>
          <div className="flex items-baseline gap-1 text-4xl font-bold tracking-tight text-ink sm:text-5xl">
            {known ? (
              <CountUp value={used} animateOnView={false} duration={0.5} />
            ) : (
              <span className="text-ink-muted">—</span>
            )}
            <span className="text-2xl font-bold text-accent sm:text-3xl">
              /{max}
            </span>
          </div>
          <p className="mt-2 text-xs font-semibold uppercase tracking-[0.16em] text-ink-muted">
            Sends in the last {windowSeconds}s
          </p>
        </div>

        <p className="max-w-xs text-sm leading-relaxed text-ink-muted">
          {known
            ? `${max} is our own ceiling, not theirs. PseudoGram allows ${PLATFORM_LIMIT} per rolling ${windowSeconds}s — we spend at most ${max} and bank the rest as headroom for clock skew.`
            : "The backend did not report a rate budget, so this meter is showing nothing rather than guessing."}
        </p>
      </div>

      <div
        className="mt-6 flex items-stretch gap-1.5"
        role="meter"
        aria-valuenow={known ? used : undefined}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-label={`Send budget: ${known ? used : "unknown"} of ${max} used in the last ${windowSeconds} seconds`}
      >
        {slots.map((slot, i) => {
          const base = "h-10 flex-1 rounded-md sm:h-12";
          if (slot === "forfeited") {
            return (
              <div
                key={i}
                title="Deliberately unused — one request of headroom under the platform limit"
                className={`${base} border border-dashed border-line bg-transparent`}
              />
            );
          }
          if (slot === "free" || loading) {
            return <div key={i} className={`${base} bg-surface-2`} />;
          }
          return (
            <motion.div
              key={i}
              className={`${base} bg-accent`}
              initial={reduced ? false : { scaleY: 0.3, opacity: 0.4 }}
              animate={{ scaleY: 1, opacity: 1 }}
              transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
              style={{ originY: 1 }}
            />
          );
        })}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-ink-muted">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="h-2.5 w-2.5 rounded-sm bg-accent" />
          Spent
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="h-2.5 w-2.5 rounded-sm bg-surface-2"
          />
          Available
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="h-2.5 w-2.5 rounded-sm border border-dashed border-line"
          />
          Forfeited on purpose
        </span>
      </div>
    </div>
  );
}

export default RateMeter;

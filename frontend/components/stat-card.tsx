/* Copyright (c) 2026 Pinak Kundu. All rights reserved.
 * Licensed under the Business Source License 1.1 (see LICENSE).
 * No use, copying, or modification without written permission. */
"use client";

import type { ReactNode } from "react";
import { CountUp } from "./count-up";
import { TiltCard, TiltLayer } from "./tilt-card";

export interface StatCardProps {
  /** The number to display. Changes tween via CountUp. */
  value: number;
  /** Small uppercase caption under the number. */
  label: string;
  /** Rendered in accent blue after the number, e.g. "+", "%", "/9". */
  suffix?: string;
  /** Rendered in accent blue before the number. */
  prefix?: string;
  /** Optional line of secondary detail below the caption. */
  hint?: ReactNode;
  /** Decimal places. */
  decimals?: number;
  /**
   * `false` for live-polled numbers so they morph on every update instead of
   * waiting for a scroll trigger. Defaults to true (scroll count-up).
   */
  animateOnView?: boolean;
  /** Deep-navy inverted variant for emphasis among white cards. */
  tone?: "surface" | "navy";
  /** Disable the 3D tilt for this card. */
  tilt?: boolean;
  className?: string;
}

/**
 * Stat tile: big count-up number with a blue suffix, small uppercase caption,
 * on a rounded card with a 3D tilt (auto-disabled on touch / reduced motion).
 */
export function StatCard({
  value,
  label,
  suffix,
  prefix,
  hint,
  decimals = 0,
  animateOnView = true,
  tone = "surface",
  tilt = true,
  className,
}: StatCardProps) {
  const navy = tone === "navy";

  const body = (
    <>
      <TiltLayer depth={28}>
        <div
          className={`text-4xl font-bold tracking-tight sm:text-5xl ${
            navy ? "text-navy-ink" : "text-ink"
          }`}
        >
          <CountUp
            value={value}
            decimals={decimals}
            prefix={prefix}
            suffix={suffix}
            animateOnView={animateOnView}
            affixClassName={navy ? "text-accent-soft" : "text-accent"}
          />
        </div>
      </TiltLayer>

      <TiltLayer depth={16}>
        <div
          className={`mt-3 text-xs font-semibold uppercase tracking-[0.16em] ${
            navy ? "text-navy-ink/70" : "text-ink-muted"
          }`}
        >
          {label}
        </div>
        {hint ? (
          <div
            className={`mt-2 text-sm ${
              navy ? "text-navy-ink/60" : "text-ink-muted"
            }`}
          >
            {hint}
          </div>
        ) : null}
      </TiltLayer>
    </>
  );

  const shell = [
    "rounded-[var(--radius-card)] p-6 sm:p-7 shadow-[var(--shadow-card)]",
    navy ? "bg-navy" : "bg-surface border border-line",
    className ?? "",
  ].join(" ");

  if (!tilt) {
    return <div className={shell}>{body}</div>;
  }

  return (
    <TiltCard className={shell} hoverLift={6}>
      {body}
    </TiltCard>
  );
}

export default StatCard;

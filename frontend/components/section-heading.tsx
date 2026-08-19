/* Copyright (c) 2026 Pinak Kundu. All rights reserved.
 * Licensed under the Business Source License 1.1 (see LICENSE).
 * No use, copying, or modification without written permission. */
import type { ReactNode } from "react";

export interface SectionHeadingProps {
  /** Small uppercase letter-spaced blue label above the headline. */
  kicker?: string;
  /** The headline's leading text, rendered in tight-tracked bold ink. */
  children: ReactNode;
  /** Trailing phrase rendered in royal-blue italic, e.g. "real authority." */
  accent?: string;
  /** Supporting copy under the headline. */
  description?: ReactNode;
  /** Headline scale. `hero` is the landing-page 6xl/7xl treatment. */
  size?: "hero" | "section" | "sub";
  align?: "left" | "center";
  as?: "h1" | "h2" | "h3";
  className?: string;
}

const SIZES = {
  hero: "text-5xl sm:text-6xl lg:text-7xl leading-[0.95] tracking-tight",
  section: "text-4xl sm:text-5xl leading-[1.02] tracking-tight",
  sub: "text-2xl sm:text-3xl leading-tight tracking-tight",
} as const;

/**
 * Kicker + headline with the accent phrase in royal-blue italic — the core
 * typographic unit of the design language (BLUEPRINT §7.1).
 */
export function SectionHeading({
  kicker,
  children,
  accent,
  description,
  size = "section",
  align = "left",
  as: Tag = "h2",
  className,
}: SectionHeadingProps) {
  const alignment = align === "center" ? "text-center items-center" : "";

  return (
    <div className={["flex flex-col", alignment, className ?? ""].join(" ")}>
      {kicker ? (
        <span className="mb-4 text-xs font-semibold uppercase tracking-[0.2em] text-accent">
          {kicker}
        </span>
      ) : null}

      <Tag className={`font-bold text-ink text-balance ${SIZES[size]}`}>
        {children}
        {accent ? (
          <>
            {" "}
            <span className="italic text-accent">{accent}</span>
          </>
        ) : null}
      </Tag>

      {description ? (
        <p
          className={[
            "mt-6 max-w-2xl text-base leading-relaxed text-ink-muted sm:text-lg",
            align === "center" ? "mx-auto" : "",
          ].join(" ")}
        >
          {description}
        </p>
      ) : null}
    </div>
  );
}

export default SectionHeading;

"use client";

import { motion, useReducedMotion } from "motion/react";
import type { ReactNode } from "react";

export interface RevealProps {
  children: ReactNode;
  className?: string;
  /** Extra seconds before this element animates. */
  delay?: number;
  /** Rise distance in px. */
  y?: number;
  /**
   * Stagger direct children instead of animating the wrapper as one block.
   * Children must be <Reveal.Item> (or any motion child using the item variant).
   */
  stagger?: boolean;
  /** Seconds between staggered children. */
  staggerDelay?: number;
  as?: "div" | "section" | "ul" | "li" | "article" | "header" | "footer";
}

const VIEWPORT = { once: true, margin: "-80px" } as const;

/**
 * Scroll-reveal wrapper: fade + 24px rise, once only, triggered 80px before the
 * element enters the viewport. Under prefers-reduced-motion the content renders
 * immediately with no transform.
 */
export function Reveal({
  children,
  className,
  delay = 0,
  y = 24,
  stagger = false,
  staggerDelay = 0.08,
  as = "div",
}: RevealProps) {
  const reduced = useReducedMotion();
  const Component = motion[as];

  if (reduced) {
    return <Component className={className}>{children}</Component>;
  }

  if (stagger) {
    return (
      <Component
        className={className}
        initial="hidden"
        whileInView="visible"
        viewport={VIEWPORT}
        variants={{
          hidden: {},
          visible: {
            transition: { staggerChildren: staggerDelay, delayChildren: delay },
          },
        }}
      >
        {children}
      </Component>
    );
  }

  return (
    <Component
      className={className}
      initial={{ opacity: 0, y }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={VIEWPORT}
      transition={{ duration: 0.6, delay, ease: [0.22, 1, 0.36, 1] }}
    >
      {children}
    </Component>
  );
}

export interface RevealItemProps {
  children: ReactNode;
  className?: string;
  y?: number;
  as?: "div" | "li" | "article" | "section";
}

/** A child of <Reveal stagger>. Picks up the parent's stagger timing. */
export function RevealItem({
  children,
  className,
  y = 24,
  as = "div",
}: RevealItemProps) {
  const reduced = useReducedMotion();
  const Component = motion[as];

  if (reduced) {
    return <Component className={className}>{children}</Component>;
  }

  return (
    <Component
      className={className}
      variants={{
        hidden: { opacity: 0, y },
        visible: {
          opacity: 1,
          y: 0,
          transition: { duration: 0.6, ease: [0.22, 1, 0.36, 1] },
        },
      }}
    >
      {children}
    </Component>
  );
}

Reveal.Item = RevealItem;

export default Reveal;

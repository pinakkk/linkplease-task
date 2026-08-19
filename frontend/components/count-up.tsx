"use client";

import {
  animate,
  useInView,
  useIsomorphicLayoutEffect,
  useReducedMotion,
} from "motion/react";
import { useRef, useState } from "react";
import { formatNumber } from "@/lib/format";

export interface CountUpProps {
  /** The target number. Changing it tweens from the previously shown value. */
  value: number;
  /** Tween duration in seconds. */
  duration?: number;
  /** Decimal places to render. */
  decimals?: number;
  /** Rendered before/after the number (e.g. "+", "%", "/9"). */
  prefix?: string;
  suffix?: string;
  /**
   * Wait until the element scrolls into view before the first count-up.
   * Live-polled numbers should set this to false.
   */
  animateOnView?: boolean;
  /** Custom formatter; defaults to a thousands-separated integer/decimal. */
  format?: (n: number) => string;
  className?: string;
  /** Class applied to the prefix/suffix spans (e.g. "text-accent"). */
  affixClassName?: string;
}

/**
 * Animates a number from its previously rendered value to the new one — used
 * both for scroll-into-view count-up and for live-polled value morphing, so a
 * 2s poll never produces a jarring swap.
 *
 * Under prefers-reduced-motion the value jumps straight to its target.
 */
export function CountUp({
  value,
  duration = 1.1,
  decimals = 0,
  prefix,
  suffix,
  animateOnView = true,
  format,
  className,
  affixClassName,
}: CountUpProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: "-40px" });
  const reduced = useReducedMotion();

  // Server and first client paint render the same string → no hydration warning.
  const [display, setDisplay] = useState(value);
  const currentRef = useRef(value);
  const startedRef = useRef(false);

  useIsomorphicLayoutEffect(() => {
    if (animateOnView && !inView && !startedRef.current) return;

    if (reduced) {
      currentRef.current = value;
      setDisplay(value);
      return;
    }

    const from = startedRef.current ? currentRef.current : 0;
    startedRef.current = true;

    if (from === value) {
      setDisplay(value);
      return;
    }

    const controls = animate(from, value, {
      duration,
      ease: [0.22, 1, 0.36, 1],
      onUpdate: (latest) => {
        currentRef.current = latest;
        setDisplay(latest);
      },
      onComplete: () => {
        currentRef.current = value;
        setDisplay(value);
      },
    });

    return () => controls.stop();
  }, [value, inView, animateOnView, reduced, duration]);

  const rounded =
    decimals > 0
      ? Number(display.toFixed(decimals))
      : Math.round(display as number);

  const text = format
    ? format(rounded)
    : decimals > 0
      ? rounded.toFixed(decimals)
      : formatNumber(rounded);

  const settled = format ? format(value) : formatNumber(value);

  return (
    <span ref={ref} className={className}>
      {/* The visible number changes every animation frame; announcing it would
          spam screen readers, so the whole visual group is aria-hidden and the
          settled value is exposed once in an sr-only span. */}
      <span aria-hidden="true">
        {prefix ? <span className={affixClassName}>{prefix}</span> : null}
        <span suppressHydrationWarning>{text}</span>
        {suffix ? <span className={affixClassName}>{suffix}</span> : null}
      </span>
      <span className="sr-only">{`${prefix ?? ""}${settled}${suffix ?? ""}`}</span>
    </span>
  );
}

export default CountUp;

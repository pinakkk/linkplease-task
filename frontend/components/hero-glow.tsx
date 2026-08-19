"use client";

import {
  motion,
  useReducedMotion,
  useScroll,
  useTransform,
} from "motion/react";
import { useRef, type ReactNode } from "react";

/**
 * Hero shell: a slow-drifting radial glow behind the headline that parallaxes
 * against the scroll, with the content layer moving at a different rate.
 *
 * The glow is painted with the accent token at low alpha via colour-mix, so it
 * re-tints itself when the theme flips — no hardcoded colour anywhere.
 */
export function HeroGlow({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();

  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ["start start", "end start"],
  });

  // The blob drifts down and fades as the hero scrolls away; the content rises
  // slightly faster, which reads as depth.
  const glowY = useTransform(scrollYProgress, [0, 1], ["0%", "38%"]);
  const glowOpacity = useTransform(scrollYProgress, [0, 1], [1, 0.15]);
  const glowScale = useTransform(scrollYProgress, [0, 1], [1, 1.25]);
  const contentY = useTransform(scrollYProgress, [0, 1], ["0px", "-56px"]);

  return (
    <div ref={ref} className="relative isolate">
      <motion.div
        aria-hidden="true"
        className="pointer-events-none absolute left-1/2 top-[-12rem] -z-10 h-[42rem] w-[min(80rem,140vw)] -translate-x-1/2"
        style={
          reduced ? undefined : { y: glowY, opacity: glowOpacity, scale: glowScale }
        }
      >
        <div
          className="h-full w-full"
          style={{
            background:
              "radial-gradient(closest-side, color-mix(in oklab, var(--accent) 28%, transparent), transparent 72%)",
          }}
        />
      </motion.div>

      {/* A second, smaller and offset blob gives the glow a non-circular
          silhouette without needing an image asset. */}
      <motion.div
        aria-hidden="true"
        className="pointer-events-none absolute right-[8%] top-[2rem] -z-10 h-[26rem] w-[26rem]"
        style={reduced ? undefined : { y: glowY, opacity: glowOpacity }}
      >
        <div
          className="h-full w-full"
          style={{
            background:
              "radial-gradient(closest-side, color-mix(in oklab, var(--accent) 16%, transparent), transparent 70%)",
          }}
        />
      </motion.div>

      <motion.div style={reduced ? undefined : { y: contentY }}>
        {children}
      </motion.div>
    </div>
  );
}

export default HeroGlow;

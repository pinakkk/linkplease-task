/* Copyright (c) 2026 Pinak Kundu. All rights reserved.
 * Licensed under the Business Source License 1.1 (see LICENSE).
 * No use, copying, or modification without written permission. */
"use client";

import {
  motion,
  useMotionTemplate,
  useMotionValue,
  useReducedMotion,
  useSpring,
} from "motion/react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";

const TiltEnabledContext = createContext(false);

export interface TiltCardProps {
  children: ReactNode;
  className?: string;
  /** Maximum rotation in degrees on each axis. */
  maxTilt?: number;
  /** Perspective distance in px. */
  perspective?: number;
  /** Lift the whole card toward the viewer on hover, in px. */
  hoverLift?: number;
  style?: CSSProperties;
  as?: "div" | "article" | "section" | "li";
}

const SPRING = { stiffness: 220, damping: 22, mass: 0.6 } as const;

/**
 * 3D tilt parallax card. Follows the pointer with spring-damped
 * rotateX/rotateY (±maxTilt, default 6°) and resets on leave.
 *
 * Disabled entirely on touch/coarse-pointer devices and under
 * prefers-reduced-motion — in those cases it renders a plain element with no
 * transform and no pointer listeners.
 *
 * Inner elements can be lifted on the Z axis with <TiltLayer depth={n}>.
 */
export function TiltCard({
  children,
  className,
  maxTilt = 6,
  perspective = 1000,
  hoverLift = 0,
  style,
  as = "div",
}: TiltCardProps) {
  const reduced = useReducedMotion();
  const [coarsePointer, setCoarsePointer] = useState(true);

  // Assume "no tilt" until we've confirmed a fine pointer on the client, so the
  // server render and first client paint agree.
  useEffect(() => {
    const mq = window.matchMedia("(hover: hover) and (pointer: fine)");
    const update = () => setCoarsePointer(!mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  const enabled = !reduced && !coarsePointer;

  const rotateX = useSpring(useMotionValue(0), SPRING);
  const rotateY = useSpring(useMotionValue(0), SPRING);
  const lift = useSpring(useMotionValue(0), SPRING);

  const transform = useMotionTemplate`perspective(${perspective}px) rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateZ(${lift}px)`;

  const handlePointerMove = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      if (!enabled) return;
      const rect = event.currentTarget.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      // -0.5 .. 0.5 relative to the card centre.
      const px = (event.clientX - rect.left) / rect.width - 0.5;
      const py = (event.clientY - rect.top) / rect.height - 0.5;
      // Pointer below centre tips the top of the card away → negative rotateX.
      rotateX.set(-py * maxTilt * 2);
      rotateY.set(px * maxTilt * 2);
      lift.set(hoverLift);
    },
    [enabled, maxTilt, hoverLift, rotateX, rotateY, lift],
  );

  const handlePointerLeave = useCallback(() => {
    rotateX.set(0);
    rotateY.set(0);
    lift.set(0);
  }, [rotateX, rotateY, lift]);

  const Component = motion[as];

  if (!enabled) {
    const Plain = as;
    return (
      <TiltEnabledContext.Provider value={false}>
        <Plain className={className} style={style}>
          {children}
        </Plain>
      </TiltEnabledContext.Provider>
    );
  }

  return (
    <TiltEnabledContext.Provider value={true}>
      <Component
        className={className}
        style={{ ...style, transform, transformStyle: "preserve-3d" }}
        onPointerMove={handlePointerMove}
        onPointerLeave={handlePointerLeave}
      >
        {children}
      </Component>
    </TiltEnabledContext.Provider>
  );
}

export interface TiltLayerProps {
  children: ReactNode;
  /** translateZ in px — 20–40 reads well for text/icons over a card. */
  depth?: number;
  className?: string;
  style?: CSSProperties;
  as?: "div" | "span" | "p" | "h2" | "h3";
}

/**
 * Lifts its children toward the viewer inside a <TiltCard>, producing parallax
 * as the card rotates. A no-op (plain element) when tilt is disabled.
 */
export function TiltLayer({
  children,
  depth = 24,
  className,
  style,
  as = "div",
}: TiltLayerProps) {
  const enabled = useContext(TiltEnabledContext);
  const Component = as;

  return (
    <Component
      className={className}
      style={
        enabled
          ? {
              ...style,
              transform: `translateZ(${depth}px)`,
              transformStyle: "preserve-3d",
            }
          : style
      }
    >
      {children}
    </Component>
  );
}

/**
 * NOTE: no `TiltCard.Layer` static-property shorthand — see reveal.tsx. Import
 * { TiltLayer } explicitly.
 */

export default TiltCard;

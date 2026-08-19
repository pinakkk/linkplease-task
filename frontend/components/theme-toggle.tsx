/* Copyright (c) 2026 Pinak Kundu. All rights reserved.
 * Licensed under the Business Source License 1.1 (see LICENSE).
 * No use, copying, or modification without written permission. */
"use client";

import { useCallback, useLayoutEffect, useSyncExternalStore } from "react";
import {
  getCurrentTheme,
  resolveTheme,
  applyTheme,
  subscribeToTheme,
  toggleThemeWithReveal,
  type Theme,
} from "@/lib/theme";

export interface ThemeToggleProps {
  className?: string;
}

// Snapshot getters must be stable identities across renders.
const alwaysTrue = () => true;
const alwaysFalse = () => false;

/**
 * Sun/moon theme toggle. The active theme is read from <html> after mount
 * (the pre-hydration script already set it), so the server and first client
 * render agree and no hydration warning is produced.
 */
export function ThemeToggle({ className }: ThemeToggleProps) {
  // The active theme lives on <html>, outside React — useSyncExternalStore is
  // the right primitive to read it. The server snapshot is "light", matching the
  // data-theme the layout renders, so hydration is clean; the client snapshot
  // then reports whatever the pre-hydration script actually applied.
  const theme = useSyncExternalStore<Theme>(
    subscribeToTheme,
    getCurrentTheme,
    () => "light",
  );
  const mounted = useSyncExternalStore(
    subscribeToTheme,
    alwaysTrue,
    alwaysFalse,
  );

  // React's dev-mode Strict Mode remount resets <html> to the attributes it
  // manages from JSX, wiping the one the pre-hydration script set. Re-apply it
  // before paint. No-op in production.
  useLayoutEffect(() => {
    applyTheme(resolveTheme());
  }, []);

  const next: Theme = theme === "dark" ? "light" : "dark";

  const handleClick = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    // Keyboard activation reports clientX/Y of 0 — fall back to the button centre.
    const fromKeyboard = event.clientX === 0 && event.clientY === 0;
    const origin = fromKeyboard
      ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
      : { x: event.clientX, y: event.clientY };

    // toggleThemeWithReveal writes the attribute, which notifies the store and
    // re-renders this component with the new theme.
    toggleThemeWithReveal(
      document.documentElement.getAttribute("data-theme") === "dark"
        ? "light"
        : "dark",
      origin,
    );
  }, []);

  return (
    <button
      type="button"
      onClick={handleClick}
      className={[
        "inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full",
        "border border-line bg-surface text-ink-muted",
        "transition-colors duration-200 hover:text-ink hover:bg-surface-2",
        "active:scale-95 motion-reduce:active:scale-100",
        className ?? "",
      ].join(" ")}
      aria-label={
        mounted
          ? `Switch to ${next} theme`
          : "Toggle between light and dark theme"
      }
      title={mounted ? `Switch to ${next} theme` : undefined}
    >
      {/* Both icons render; the `dark:` variant (keyed on data-theme) picks
          one, so the button is correct before hydration too. */}
      <SunIcon className="h-5 w-5 dark:hidden" />
      <MoonIcon className="hidden h-5 w-5 dark:block" />
    </button>
  );
}

function SunIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
    </svg>
  );
}

function MoonIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

export default ThemeToggle;

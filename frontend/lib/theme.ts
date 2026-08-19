/**
 * Theme helpers. The pre-hydration inline script and the runtime toggle both
 * live here so the storage key / attribute name can never drift between them.
 */

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "linkplease-theme";
export const THEME_ATTRIBUTE = "data-theme";

/**
 * Blocking inline script injected into <head>. Runs before first paint so the
 * correct theme attribute is on <html> before any styled pixel is drawn — no
 * flash of the wrong theme. The layout renders <html data-theme="light"
 * suppressHydrationWarning>, so React accepts whatever this script wrote.
 *
 * Kept as a single string constant so it cannot drift from the helpers below.
 */
export const THEME_INIT_SCRIPT = `(function(){try{var k=${JSON.stringify(
  THEME_STORAGE_KEY,
)};var s=localStorage.getItem(k);var t=(s==="light"||s==="dark")?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.setAttribute(${JSON.stringify(
  THEME_ATTRIBUTE,
)},t);document.documentElement.style.colorScheme=t;}catch(e){document.documentElement.setAttribute(${JSON.stringify(
  THEME_ATTRIBUTE,
)},"light");}})();`;

/**
 * Subscribes to changes of the data-theme attribute on <html> — the store
 * backing useSyncExternalStore in theme-toggle.tsx. The attribute is the single
 * source of truth, so any writer (the toggle, the pre-hydration script, a
 * dev-mode remount) notifies every subscriber automatically.
 */
export function subscribeToTheme(onChange: () => void): () => void {
  if (typeof document === "undefined") return () => {};
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: [THEME_ATTRIBUTE],
  });
  return () => observer.disconnect();
}

/** Reads the theme currently applied to <html>. Safe on the server (→ light). */
export function getCurrentTheme(): Theme {
  if (typeof document === "undefined") return "light";
  return document.documentElement.getAttribute(THEME_ATTRIBUTE) === "dark"
    ? "dark"
    : "light";
}

/** The user's explicitly stored choice, or null when they've never chosen. */
export function getStoredTheme(): Theme | null {
  if (typeof window === "undefined") return null;
  try {
    const v = window.localStorage.getItem(THEME_STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

/** OS preference — used ONLY as the initial default when nothing is stored. */
export function getSystemTheme(): Theme {
  if (typeof window === "undefined" || !window.matchMedia) return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

/** The theme that should be active right now, ignoring the DOM. */
export function resolveTheme(): Theme {
  return getStoredTheme() ?? getSystemTheme();
}

/**
 * Writes the theme to <html>.
 *
 * `persist` defaults to false so that merely applying the resolved theme (e.g.
 * the Strict Mode re-apply) never records a choice the user did not make — a
 * visitor with no stored preference keeps following prefers-color-scheme. Only
 * the toggle passes `persist: true`.
 */
export function applyTheme(theme: Theme, persist = false): void {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute(THEME_ATTRIBUTE, theme);
  document.documentElement.style.colorScheme = theme;
  if (!persist) return;
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    /* private mode / storage disabled — the theme still applies for this page */
  }
}

export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

const TRANSITION_CLASS = "theme-transition";
const TRANSITION_MS = 450;
let transitionTimer: ReturnType<typeof setTimeout> | undefined;

/**
 * Fallback transition for browsers without the View Transitions API: enable a
 * global colour transition for ~450ms, then strip it so it doesn't tax every
 * subsequent interaction.
 */
export function runFallbackTransition(apply: () => void): void {
  const root = document.documentElement;
  if (prefersReducedMotion()) {
    apply();
    return;
  }
  root.classList.add(TRANSITION_CLASS);
  apply();
  if (transitionTimer) clearTimeout(transitionTimer);
  transitionTimer = setTimeout(() => {
    root.classList.remove(TRANSITION_CLASS);
  }, TRANSITION_MS);
}

type ViewTransitionDocument = Document & {
  startViewTransition?: (cb: () => void) => { finished: Promise<void> };
};

/**
 * Flips the theme with a circular reveal expanding from (originX, originY) —
 * normally the centre of the toggle button. Falls back to the timed colour
 * transition when View Transitions are unavailable or motion is reduced.
 */
export function toggleThemeWithReveal(
  next: Theme,
  origin?: { x: number; y: number },
): void {
  const doc = document as ViewTransitionDocument;
  const apply = () => applyTheme(next, true);

  if (typeof doc.startViewTransition !== "function" || prefersReducedMotion()) {
    runFallbackTransition(apply);
    return;
  }

  const root = document.documentElement;
  const x = origin?.x ?? window.innerWidth / 2;
  const y = origin?.y ?? window.innerHeight / 2;
  // Radius must reach the furthest corner of the viewport.
  const radius = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  );

  root.style.setProperty("--vt-x", `${x}px`);
  root.style.setProperty("--vt-y", `${y}px`);
  root.style.setProperty("--vt-r", `${radius}px`);
  root.classList.add("theme-reveal");

  const transition = doc.startViewTransition(apply);
  transition.finished
    .catch(() => undefined)
    .finally(() => {
      root.classList.remove("theme-reveal");
    });
}

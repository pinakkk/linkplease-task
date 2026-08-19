"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { ThemeToggle } from "./theme-toggle";

interface NavLink {
  href: string;
  label: string;
}

const LINKS: NavLink[] = [
  { href: "/", label: "Overview" },
  { href: "/dashboard", label: "Dashboard" },
  { href: "/#pipeline", label: "Pipeline" },
  { href: "/#guarantee", label: "Guarantee" },
];

const CTA: NavLink = { href: "/dashboard", label: "Open dashboard" };

export interface NavProps {
  className?: string;
}

/**
 * Floating pill navbar — sticky, backdrop-blurred capsule with the logo left,
 * links centre, and theme toggle + CTA right. On small screens the centre links
 * collapse into a disclosure panel below the pill.
 */
export function Nav({ className }: NavProps) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  // Close the mobile panel on navigation by deriving it during render rather
  // than in an effect (no cascading render, no flash of the open panel).
  const [lastPath, setLastPath] = useState(pathname);
  if (lastPath !== pathname) {
    setLastPath(pathname);
    setOpen(false);
  }

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname === href.split("#")[0];

  return (
    <header
      className={[
        "sticky top-0 z-50 px-4 pt-4 sm:px-6 sm:pt-6",
        className ?? "",
      ].join(" ")}
    >
      <a
        href="#main"
        className="sr-only rounded-full bg-accent px-4 py-2 text-sm font-semibold text-white focus:not-sr-only focus:absolute focus:left-6 focus:top-6 focus:z-50"
      >
        Skip to content
      </a>

      <nav
        aria-label="Primary"
        className="mx-auto flex w-full max-w-6xl items-center gap-3 rounded-full border border-line bg-surface/80 px-3 py-2 shadow-[var(--shadow-card)] backdrop-blur-xl sm:px-4"
      >
        {/* Logo */}
        <Link
          href="/"
          className="flex shrink-0 items-center gap-2 rounded-full px-2 py-1 text-base font-bold tracking-tight text-ink"
        >
          <span
            aria-hidden="true"
            className="grid h-7 w-7 place-items-center rounded-lg bg-accent text-[13px] font-black text-white"
          >
            L
          </span>
          <span>
            LinkPlease
          </span>
        </Link>

        {/* Centre links (desktop) */}
        <ul className="mx-auto hidden items-center gap-1 md:flex">
          {LINKS.map((link) => (
            <li key={link.href}>
              <Link
                href={link.href}
                aria-current={isActive(link.href) ? "page" : undefined}
                className={[
                  "rounded-full px-3.5 py-2 text-sm font-medium transition-colors",
                  isActive(link.href)
                    ? "bg-surface-2 text-ink"
                    : "text-ink-muted hover:bg-surface-2 hover:text-ink",
                ].join(" ")}
              >
                {link.label}
              </Link>
            </li>
          ))}
        </ul>

        {/* Right cluster */}
        <div className="ml-auto flex shrink-0 items-center gap-2 md:ml-0">
          <ThemeToggle />

          <Link
            href={CTA.href}
            className="hidden rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-accent/20 transition-all duration-200 hover:shadow-xl hover:shadow-accent/30 active:scale-[0.98] motion-reduce:transition-none motion-reduce:active:scale-100 sm:inline-flex"
          >
            {CTA.label}
          </Link>

          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-controls="mobile-nav"
            aria-label={open ? "Close menu" : "Open menu"}
            className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-line bg-surface text-ink-muted transition-colors hover:text-ink md:hidden"
          >
            <svg
              viewBox="0 0 24 24"
              className="h-5 w-5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              aria-hidden="true"
            >
              {open ? (
                <path d="M6 6l12 12M18 6L6 18" />
              ) : (
                <path d="M4 7h16M4 12h16M4 17h16" />
              )}
            </svg>
          </button>
        </div>
      </nav>

      {/* Mobile panel */}
      {open ? (
        <div
          id="mobile-nav"
          className="mx-auto mt-2 w-full max-w-6xl rounded-[var(--radius-card)] border border-line bg-surface p-2 shadow-[var(--shadow-card)] md:hidden"
        >
          <ul className="flex flex-col">
            {LINKS.map((link) => (
              <li key={link.href}>
                <Link
                  href={link.href}
                  aria-current={isActive(link.href) ? "page" : undefined}
                  className={[
                    "block rounded-2xl px-4 py-3 text-sm font-medium transition-colors",
                    isActive(link.href)
                      ? "bg-surface-2 text-ink"
                      : "text-ink-muted hover:bg-surface-2 hover:text-ink",
                  ].join(" ")}
                >
                  {link.label}
                </Link>
              </li>
            ))}
          </ul>
          <Link
            href={CTA.href}
            className="mt-1 block rounded-2xl bg-accent px-4 py-3 text-center text-sm font-semibold text-white sm:hidden"
          >
            {CTA.label}
          </Link>
        </div>
      ) : null}
    </header>
  );
}

export default Nav;

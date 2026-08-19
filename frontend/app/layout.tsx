/* Copyright (c) 2026 Pinak Kundu. All rights reserved.
 * Licensed under the Business Source License 1.1 (see LICENSE).
 * No use, copying, or modification without written permission. */
import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import Nav from "@/components/nav";
import { THEME_INIT_SCRIPT } from "@/lib/theme";
import "./globals.css";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "LinkPlease — comment-to-DM pipeline",
  description:
    "Live operations dashboard for the LinkPlease comment-to-DM pipeline: no duplicate DMs, no silent loss.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#F4F4FA" },
    { media: "(prefers-color-scheme: dark)", color: "#0B0D1A" },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // suppressHydrationWarning: the inline script below writes data-theme and
    // style.colorScheme onto <html> before React hydrates, which React would
    // otherwise report as a mismatch.
    <html
      lang="en"
      data-theme="light"
      className={`${inter.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        {/* Blocking, synchronous — runs during HTML parsing so the correct
            theme is on <html> before the first paint. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="flex min-h-full flex-col bg-bg text-ink">
        <Nav />
        <main id="main" className="flex-1">
          {children}
        </main>
      </body>
    </html>
  );
}

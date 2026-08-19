/* Copyright (c) 2026 Pinak Kundu. All rights reserved.
 * Licensed under the Business Source License 1.1 (see LICENSE).
 * No use, copying, or modification without written permission. */
/**
 * PLACEHOLDER — Agent F builds the real dashboard here in Phase 2 on top of
 * the primitives in components/ and the typed client in lib/api.ts.
 */
import SectionHeading from "@/components/section-heading";

export default function DashboardPage() {
  return (
    <div className="mx-auto max-w-6xl px-4 pb-24 pt-16 sm:px-6 sm:pt-24">
      <SectionHeading
        as="h1"
        size="section"
        kicker="Live operations"
        accent="in flight."
        description="Stat cards, rules manager, activity feed, rate-budget meter and the simulation truth-diff panel land here in Phase 2."
      >
        Pipeline
      </SectionHeading>
    </div>
  );
}

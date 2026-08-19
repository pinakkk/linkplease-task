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

/**
 * PLACEHOLDER — Agent F replaces this in Phase 2 with the full landing page.
 *
 * It exists only to exercise every design-system primitive so the tokens,
 * theme flip, motion and typography can be verified visually. Nothing else
 * imports from this file.
 */
import Reveal, { RevealItem } from "@/components/reveal";
import SectionHeading from "@/components/section-heading";
import StatCard from "@/components/stat-card";
import StatusChip from "@/components/status-chip";
import { JOB_STATUSES } from "@/lib/api";

export default function Home() {
  return (
    <div className="mx-auto max-w-6xl px-4 pb-24 pt-16 sm:px-6 sm:pt-24">
      <Reveal>
        <SectionHeading
          as="h1"
          size="hero"
          kicker="Comment to DM, reliably"
          accent="never twice."
          description="Every qualifying comment gets exactly one DM — delivered, confirmed, and accounted for. Ingest in under five seconds, rate-budgeted sending, and a reconciler that proves a 202 actually landed."
        >
          Send it once.
        </SectionHeading>
      </Reveal>

      <Reveal
        stagger
        className="mt-16 grid grid-cols-2 gap-4 lg:grid-cols-4"
        delay={0.1}
      >
        {[
          { value: 1284, label: "DMs sent", suffix: "+" },
          { value: 3, label: "Failed" },
          { value: 12, label: "Queued" },
          { value: 96, label: "Duplicates blocked" },
        ].map((stat) => (
          <RevealItem key={stat.label}>
            <StatCard
              value={stat.value}
              label={stat.label}
              suffix={stat.suffix}
            />
          </RevealItem>
        ))}
      </Reveal>

      <Reveal className="mt-20">
        <SectionHeading kicker="Job states" accent="honestly.">
          Every row, tracked
        </SectionHeading>
        <div className="mt-8 flex flex-wrap gap-3">
          {JOB_STATUSES.map((status) => (
            <StatusChip key={status} status={status} />
          ))}
        </div>
      </Reveal>

      <Reveal className="mt-20">
        <div className="rounded-[var(--radius-card)] bg-navy p-8 shadow-[var(--shadow-card)] sm:p-12">
          <span className="text-xs font-semibold uppercase tracking-[0.2em] text-accent-soft">
            Our guarantee
          </span>
          <p className="mt-4 max-w-2xl text-3xl font-bold leading-tight tracking-tight text-navy-ink sm:text-4xl">
            No duplicate DMs, no silent loss —{" "}
            <span className="italic text-accent-soft">provably.</span>
          </p>
        </div>
      </Reveal>
    </div>
  );
}

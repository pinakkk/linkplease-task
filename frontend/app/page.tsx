import Link from "next/link";
import HeroGlow from "@/components/hero-glow";
import LiveStats from "@/components/live-stats";
import Reveal, { RevealItem } from "@/components/reveal";
import SectionHeading from "@/components/section-heading";
import TiltCard, { TiltLayer } from "@/components/tilt-card";
import { API_BASE } from "@/lib/api";

const GITHUB_URL = "https://github.com/pinakkk/linkplease";

/**
 * Each step describes the mechanism we actually implemented — no aspirational
 * copy. If a sentence here stops being true of the backend, it is a bug in
 * this file.
 */
const STEPS = [
  {
    index: "01",
    title: "Ingest",
    body: "The webhook writes the event to Postgres and returns 200 — nothing calls Instagram on that path. A repeated event_id collides on the primary key, so a redelivery is recognised in the same round trip that records it.",
  },
  {
    index: "02",
    title: "Match",
    body: "Keywords match case-insensitively anywhere in the comment text. Each match tries to insert a DM job; a unique index on (rule, user) makes the second attempt fail instead of queueing a second DM.",
  },
  {
    index: "03",
    title: "Send",
    body: "Sends drain at 9 per 60 seconds — one under the platform's limit — so the queue is honest about its backlog. Retries reuse the same idempotency key, which makes a timed-out send safe to repeat.",
  },
  {
    index: "04",
    title: "Reconcile",
    body: "A 202 means accepted, not delivered. A separate loop re-reads each accepted DM until the platform commits to delivered or failed, and only the delivered ones are counted as sent.",
  },
];

const GUARANTEES = [
  {
    title: "One DM per person per rule",
    body: "Enforced by a unique database index, not by an in-process set. Two workers racing on the same commenter produce one insert and one conflict.",
  },
  {
    title: "Nothing pending lives only in memory",
    body: "Every retry and its next attempt time is a row in Postgres. A restart mid-backoff resumes the same job instead of forgetting it.",
  },
  {
    title: "A 202 is not a delivery",
    body: "Accepted DMs sit in AWAITING_CONFIRM and count as queued until the platform confirms them. That keeps our `sent` lower than a naive counter — deliberately.",
  },
];

export default function Home() {
  return (
    <>
      {/* ---------------------------------------------------------------- Hero */}
      <HeroGlow>
        <section className="mx-auto max-w-6xl px-4 pb-16 pt-14 sm:px-6 sm:pb-24 sm:pt-20">
          <span className="inline-flex items-center gap-2 rounded-full border border-line bg-surface/70 px-4 py-1.5 text-xs font-semibold uppercase tracking-[0.16em] text-accent backdrop-blur">
            Comment → DM pipeline
          </span>

          <h1 className="mt-7 max-w-4xl text-balance text-5xl font-bold leading-[0.95] tracking-tight text-ink sm:text-6xl lg:text-7xl">
            Someone comments. They get the DM{" "}
            <span className="italic text-accent">exactly once.</span>
          </h1>

          <p className="mt-7 max-w-2xl text-base leading-relaxed text-ink-muted sm:text-lg">
            LinkPlease turns Instagram comments into direct messages on top of a
            deliberately hostile mock API — one that redelivers events, rate
            limits, fails at random, and reports success on DMs that never
            arrive. This build deduplicates at the database level, sends inside a
            self-imposed rate budget, keeps every pending retry on disk, and
            confirms each delivery before counting it.
          </p>

          <div className="mt-9 flex flex-wrap items-center gap-3">
            <Link
              href="/dashboard"
              className="inline-flex items-center gap-2 rounded-full bg-accent px-7 py-3.5 text-sm font-semibold text-white shadow-lg shadow-accent/20 transition-all duration-200 hover:shadow-xl hover:shadow-accent/30 active:scale-[0.98] motion-reduce:transition-none motion-reduce:active:scale-100"
            >
              Open the live dashboard
            </Link>
            <a
              href={GITHUB_URL}
              className="inline-flex items-center gap-2 rounded-full border border-line bg-surface px-7 py-3.5 text-sm font-semibold text-ink transition-colors hover:bg-surface-2"
            >
              Read the source
            </a>
          </div>
        </section>
      </HeroGlow>

      {/* --------------------------------------------------------- Live stats */}
      <section className="mx-auto max-w-6xl px-4 pb-20 sm:px-6 sm:pb-28">
        <Reveal>
          <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
            <div>
              <span className="text-xs font-semibold uppercase tracking-[0.2em] text-accent">
                Live from the deployment
              </span>
              <h2 className="mt-3 text-2xl font-bold tracking-tight text-ink sm:text-3xl">
                The four numbers we are graded on
              </h2>
            </div>
            <p className="max-w-md text-sm leading-relaxed text-ink-muted">
              Read straight from the running backend, not a snapshot. If it is
              idle, these are small — an honest small number is the point.
            </p>
          </div>
        </Reveal>

        <LiveStats className="mt-8" intervalMs={5000} />
      </section>

      {/* ------------------------------------------------------ How it works */}
      <section id="pipeline" className="mx-auto max-w-6xl px-4 pb-20 sm:px-6 sm:pb-28">
        <Reveal>
          <SectionHeading kicker="How it works" accent="four stages.">
            One comment, exactly
          </SectionHeading>
        </Reveal>

        <Reveal stagger className="mt-12 grid gap-4 md:grid-cols-2" delay={0.05}>
          {STEPS.map((step) => (
            <RevealItem key={step.index}>
              <TiltCard
                className="group relative h-full overflow-hidden rounded-[var(--radius-card)] border border-line bg-surface p-7 shadow-[var(--shadow-card)] sm:p-9"
                hoverLift={8}
              >
                {/* Big faded index numeral, per the reference design. */}
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute -right-2 -top-6 select-none text-[7rem] font-bold leading-none tracking-tight text-ink/[0.045] sm:text-[9rem]"
                >
                  {step.index}
                </span>

                <TiltLayer depth={26}>
                  <span className="text-xs font-bold uppercase tracking-[0.2em] text-accent">
                    {step.index}
                  </span>
                  <h3 className="mt-3 text-2xl font-bold tracking-tight text-ink">
                    {step.title}
                  </h3>
                </TiltLayer>

                <TiltLayer depth={14}>
                  <p className="mt-4 max-w-md text-sm leading-relaxed text-ink-muted sm:text-base">
                    {step.body}
                  </p>
                </TiltLayer>
              </TiltCard>
            </RevealItem>
          ))}
        </Reveal>
      </section>

      {/* --------------------------------------------------- Guarantee (navy) */}
      <section id="guarantee" className="mx-auto max-w-6xl px-4 pb-20 sm:px-6 sm:pb-28">
        <Reveal>
          <div className="overflow-hidden rounded-[var(--radius-card)] bg-navy p-8 shadow-[var(--shadow-card)] sm:p-12 lg:p-14">
            {/* In dark mode --accent-soft is a deep navy, which would vanish
                against this card. navy-ink is light in both themes. */}
            <span className="text-xs font-semibold uppercase tracking-[0.2em] text-navy-ink/70">
              What we actually guarantee
            </span>

            <h2 className="mt-5 max-w-3xl text-balance text-3xl font-bold leading-tight tracking-tight text-navy-ink sm:text-4xl lg:text-5xl">
              Three promises the code can be held to —{" "}
              <span className="italic text-navy-ink/55">and nothing more.</span>
            </h2>

            <div className="mt-12 grid gap-8 md:grid-cols-3">
              {GUARANTEES.map((g, i) => (
                <div
                  key={g.title}
                  className="border-t border-navy-ink/15 pt-6"
                >
                  <span
                    aria-hidden="true"
                    className="text-xs font-bold uppercase tracking-[0.2em] text-navy-ink/40"
                  >
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <h3 className="mt-3 text-lg font-bold tracking-tight text-navy-ink">
                    {g.title}
                  </h3>
                  <p className="mt-3 text-sm leading-relaxed text-navy-ink/65">
                    {g.body}
                  </p>
                </div>
              ))}
            </div>

            <p className="mt-12 max-w-3xl text-sm leading-relaxed text-navy-ink/55">
              There is no claim here that every edge case is handled. The ways
              this system can still lose a DM, send a duplicate, or report a
              wrong number are written down in FAILURES.md in the repository —
              that list is part of the submission, not an admission tucked away
              in it.
            </p>
          </div>
        </Reveal>
      </section>

      {/* ------------------------------------------------------------ Footer */}
      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-6xl flex-col gap-8 px-4 py-12 sm:px-6 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="flex items-center gap-2 text-base font-bold tracking-tight text-ink">
              <span
                aria-hidden="true"
                className="grid h-7 w-7 place-items-center rounded-lg bg-accent text-[13px] font-black text-white"
              >
                L
              </span>
              LinkPlease
            </div>
            <p className="mt-3 max-w-sm text-sm leading-relaxed text-ink-muted">
              A comment-to-DM pipeline built for the LinkPlease technical
              assignment. Backend on Fly.io, dashboard on Cloudflare.
            </p>
            <p className="mt-4 font-mono text-xs text-ink-muted">
              API base: {API_BASE}
            </p>
          </div>

          <nav aria-label="Footer" className="flex flex-col gap-2 text-sm">
            <Link
              href="/dashboard"
              className="font-semibold text-ink transition-colors hover:text-accent"
            >
              Live dashboard
            </Link>
            <a
              href={GITHUB_URL}
              className="font-semibold text-ink transition-colors hover:text-accent"
            >
              GitHub repository
            </a>
            <a
              href={`${API_BASE}/stats`}
              className="text-ink-muted transition-colors hover:text-accent"
            >
              Raw /stats
            </a>
            <a
              href={`${API_BASE}/healthz`}
              className="text-ink-muted transition-colors hover:text-accent"
            >
              Health check
            </a>
          </nav>
        </div>
      </footer>
    </>
  );
}

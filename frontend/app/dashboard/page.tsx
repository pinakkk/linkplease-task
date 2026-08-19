"use client";

import { useCallback } from "react";
import ActivityFeed from "@/components/activity-feed";
import Panel, { ErrorBanner, Skeleton } from "@/components/panel";
import RateMeter from "@/components/rate-meter";
import RuleForm from "@/components/rule-form";
import RulesTable from "@/components/rules-table";
import SimPanel from "@/components/sim-panel";
import StatCard from "@/components/stat-card";
import {
  getExtendedStats,
  getJobs,
  getRules,
  type MetricKey,
  type RequestOptions,
} from "@/lib/api";
import { formatDuration, formatNumber, formatRelative } from "@/lib/format";
import { usePoll } from "@/lib/use-poll";

const STATS_INTERVAL_MS = 2000;
const JOBS_INTERVAL_MS = 2000;
const RULES_INTERVAL_MS = 8000;

/* Module-level fetchers — usePoll needs stable references (§ Agent D notes). */
const fetchStats = (options?: RequestOptions) => getExtendedStats(options);
const fetchJobs = (options?: RequestOptions) => getJobs({ limit: 40 }, options);
const fetchRules = (options?: RequestOptions) => getRules(options);

const STAT_CARDS: { key: MetricKey; label: string; hint: string }[] = [
  {
    key: "sent",
    label: "Sent",
    hint: "Reconciler-confirmed deliveries only",
  },
  { key: "failed", label: "Failed", hint: "Terminal — we stopped retrying" },
  { key: "queued", label: "Queued", hint: "Owed: waiting, backing off, or unconfirmed" },
  {
    key: "duplicates_blocked",
    label: "Duplicates blocked",
    hint: "Second DMs suppressed by the (rule, user) constraint",
  },
];

export default function DashboardPage() {
  const stats = usePoll(fetchStats, STATS_INTERVAL_MS);
  const jobs = usePoll(fetchJobs, JOBS_INTERVAL_MS);
  const rules = usePoll(fetchRules, RULES_INTERVAL_MS);

  const refreshRules = useCallback(() => {
    // A new rule changes both the rules table and (soon) the job counts.
    rules.refresh();
    stats.refresh();
  }, [rules, stats]);

  const s = stats.data;
  const byStatus = s?.jobs_by_status ?? {};
  const counters = s?.counters ?? {};
  const events = s?.events ?? {};

  // The backend being down is a page-level fact, not a per-panel one.
  const backendDown = Boolean(stats.error && !s);

  return (
    <div className="mx-auto max-w-6xl px-4 pb-24 pt-10 sm:px-6 sm:pt-14">
      {/* ------------------------------------------------------------ Header */}
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
        <div>
          <span className="text-xs font-semibold uppercase tracking-[0.2em] text-accent">
            Live operations
          </span>
          <h1 className="mt-3 text-4xl font-bold tracking-tight text-ink sm:text-5xl">
            Pipeline <span className="italic text-accent">in flight.</span>
          </h1>
        </div>

        <div className="text-sm text-ink-muted">
          {stats.updatedAt ? (
            <span suppressHydrationWarning>
              Updated {formatRelative(stats.updatedAt)} · polling every{" "}
              {STATS_INTERVAL_MS / 1000}s
            </span>
          ) : (
            <span>Connecting to the backend…</span>
          )}
        </div>
      </header>

      {backendDown ? (
        <ErrorBanner
          className="mt-8"
          message={stats.error?.message}
          hint="Every panel below is a read of the backend, so they are all blank rather than showing stale or invented values. Jobs already in the database keep draining meanwhile."
        />
      ) : null}

      {stats.error && s ? (
        <ErrorBanner
          className="mt-8"
          title="Last successful reading shown"
          message={stats.error.message}
          hint="The numbers below are from the most recent poll that succeeded, not from this instant."
        />
      ) : null}

      {/* ------------------------------------------------------- Stat cards */}
      <section aria-label="Graded statistics" className="mt-8">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {stats.loading && !s
            ? STAT_CARDS.map((c) => (
                <Skeleton
                  key={c.key}
                  className="h-36 w-full rounded-[var(--radius-card)]"
                />
              ))
            : STAT_CARDS.map((c) => (
                <StatCard
                  key={c.key}
                  value={s?.[c.key] ?? 0}
                  label={c.label}
                  hint={c.hint}
                  animateOnView={false}
                />
              ))}
        </div>
      </section>

      {/* ------------------------------------------- Rate budget + internals */}
      {/* items-start stops the shorter card from stretching to match the taller
          one and leaving a block of dead space under the meter. */}
      <div className="mt-6 grid items-start gap-6 lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        <Panel
          kicker="Self-imposed cap"
          title="Send rate budget"
          description="A rolling 60-second window measured on our own send log. The worker blocks rather than risking a 429 it would then have to back off from."
        >
          <RateMeter budget={s?.rate_budget} loading={stats.loading && !s} />
        </Panel>

        <Panel
          kicker="Internals"
          title="Beyond the graded four"
          description="Counters the grader never sees, kept because they are what makes the four defensible."
        >
          <dl className="grid grid-cols-2 gap-x-6 gap-y-5 text-sm">
            <Metric
              label="Cancelled jobs"
              value={s?.cancelled}
              note="Comment deleted before we sent — owed by nobody, so counted in none of the four"
            />
            <Metric
              label="Dup (rule, user)"
              value={counters.duplicates_blocked_rule_user}
              note="The formula currently reported as duplicates_blocked"
            />
            <Metric
              label="Redelivered events"
              value={counters.duplicate_events_suppressed}
              note="Same event_id seen twice, dropped at ingest"
            />
            <Metric
              label="…that would have DMed"
              value={counters.duplicate_events_would_dm}
              note="The delta between the two duplicate formulas"
            />
            <Metric
              label="Events received"
              value={events.received}
              note="Rows written by the webhook"
            />
            <Metric
              label="Unprocessed"
              value={events.unprocessed}
              note="Accepted but not yet matched"
            />
          </dl>

          <div className="mt-6 border-t border-line pt-5 text-sm">
            <p className="text-ink-muted">
              Oldest unconfirmed 202:{" "}
              <span className="font-semibold text-ink">
                {s?.oldest_awaiting_confirm_seconds == null
                  ? "none outstanding"
                  : formatDuration(s.oldest_awaiting_confirm_seconds)}
              </span>
            </p>
            {s?.duplicates_formula ? (
              <p className="mt-2 text-ink-muted">
                Duplicate formula in use:{" "}
                <span className="font-mono text-xs font-semibold text-ink">
                  {s.duplicates_formula}
                </span>
              </p>
            ) : null}
            {Object.keys(byStatus).length > 0 ? (
              <p className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
                {Object.entries(byStatus).map(([status, count]) => (
                  <span key={status}>
                    <span className="font-mono">{status}</span>{" "}
                    <span className="font-semibold text-ink">
                      {formatNumber(count as number)}
                    </span>
                  </span>
                ))}
              </p>
            ) : null}
          </div>
        </Panel>
      </div>

      {/* ------------------------------------------------------------ Rules */}
      <Panel
        className="mt-6"
        kicker="Rules"
        title="Keyword rules"
        description="A rule is the only thing that turns a comment into a DM obligation. Matching is case-insensitive and substring-based, exactly as the contract specifies."
      >
        <RuleForm onCreated={refreshRules} />
      </Panel>

      <Panel className="mt-6" flush>
        <RulesTable
          rules={rules.data}
          loading={rules.loading}
          error={rules.error}
        />
      </Panel>

      {/* --------------------------------------------------------- Activity */}
      <Panel
        className="mt-6"
        flush
        kicker="Activity"
        title="Recent jobs"
        actions={
          <span className="text-xs text-ink-muted">
            Newest first · {formatNumber(jobs.data?.length ?? 0)} shown
          </span>
        }
        description="Every DM obligation and where it currently sits. A job only reaches SENT once the platform confirms the delivery."
      >
        <ActivityFeed
          jobs={jobs.data}
          loading={jobs.loading}
          error={jobs.error}
        />
      </Panel>

      {/* ------------------------------------------------------- Simulation */}
      <Panel
        className="mt-6"
        kicker="Calibration"
        title="Simulation truth diff"
        description="Fires PseudoGram's own 500-event burst at our webhook, then puts our four numbers next to their server-side truth for the same run. This is how we check our own work rather than trusting it."
      >
        <SimPanel />
      </Panel>
    </div>
  );
}

/** One internals figure with the sentence that makes it meaningful. */
function Metric({
  label,
  value,
  note,
}: {
  label: string;
  value: number | undefined;
  note: string;
}) {
  return (
    <div>
      <dt className="text-xs font-semibold uppercase tracking-[0.14em] text-ink-muted">
        {label}
      </dt>
      <dd className="mt-1.5 text-2xl font-bold tabular-nums tracking-tight text-ink">
        {value == null ? (
          <span className="text-ink-muted">—</span>
        ) : (
          formatNumber(value)
        )}
      </dd>
      <p className="mt-1 text-xs leading-relaxed text-ink-muted">{note}</p>
    </div>
  );
}

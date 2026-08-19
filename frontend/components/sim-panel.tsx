"use client";

import { motion, useReducedMotion } from "motion/react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getSimulationReport,
  startSimulation,
  type ApiError,
  type MetricKey,
  type SimulationReport,
} from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { EmptyState, ErrorBanner } from "./panel";

const SIM_COUNT = 500;
const SIM_DURATION_SECONDS = 10;

/** Poll cadence and ceiling for the report — the run itself takes ~10s, and
 *  the queue then drains at 9 sends/60s, so we keep watching for a while. */
const POLL_INTERVAL_MS = 3000;
const MAX_POLL_MS = 6 * 60 * 1000;

const METRICS: { key: MetricKey; label: string; meaning: string }[] = [
  {
    key: "sent",
    label: "sent",
    meaning: "Reconciler confirmed delivered. A 202 alone never counts here.",
  },
  {
    key: "failed",
    label: "failed",
    meaning: "We gave up: a 400, or the retry and resend budgets ran out.",
  },
  {
    key: "queued",
    label: "queued",
    meaning: "Still owed — waiting to send, backing off, or 202-unconfirmed.",
  },
  {
    key: "duplicates_blocked",
    label: "duplicates_blocked",
    meaning: "DM obligations we deliberately suppressed as already-satisfied.",
  },
];

type RunPhase = "idle" | "starting" | "running" | "done" | "error";

export interface SimPanelProps {
  className?: string;
}

/**
 * Runs PseudoGram's own simulator against our webhook, then diffs our four
 * numbers against their server-side truth for the same run.
 *
 * The reason this panel exists: `/stats` being *self-consistent* proves
 * nothing. The only check that means anything is comparing our count to the
 * count of the party that generated the events. A mismatch shown here is a
 * finding, not a failure of the page.
 */
export function SimPanel({ className }: SimPanelProps) {
  const reduced = useReducedMotion();

  const [phase, setPhase] = useState<RunPhase>("idle");
  const [runId, setRunId] = useState<string>();
  const [report, setReport] = useState<SimulationReport>();
  const [error, setError] = useState<ApiError>();
  /** Seconds since the run started, ticked by an effect so render stays pure. */
  const [elapsed, setElapsed] = useState(0);

  // Lets the polling effect stop itself without becoming a dependency loop.
  const stopRef = useRef(false);

  // Elapsed-time ticker. Kept separate from the report poll so the "started Ns
  // ago" line advances every second without issuing a request every second.
  useEffect(() => {
    if (phase !== "running") return;
    const id = setInterval(() => setElapsed((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [phase]);

  useEffect(() => {
    if (phase !== "running" || !runId) return;

    stopRef.current = false;
    const began = Date.now();
    let timer: ReturnType<typeof setTimeout>;

    const tick = async () => {
      if (stopRef.current) return;
      const result = await getSimulationReport(runId);

      if (stopRef.current) return;

      if (result.ok) {
        setReport(result.data);
        // Anything other than an explicitly pending status is a final answer.
        const status = String(result.data.status ?? "complete").toLowerCase();
        if (status !== "pending" && status !== "running") {
          setPhase("done");
          return;
        }
      } else if (result.error.status === 404) {
        // The run exists on our side but the report is not materialised yet.
        // Keep waiting rather than reporting a failure.
      } else {
        setError(result.error);
        setPhase("error");
        return;
      }

      if (Date.now() - began > MAX_POLL_MS) {
        setPhase("done");
        return;
      }
      timer = setTimeout(tick, POLL_INTERVAL_MS);
    };

    timer = setTimeout(tick, POLL_INTERVAL_MS);

    return () => {
      stopRef.current = true;
      clearTimeout(timer);
    };
  }, [phase, runId]);

  const handleRun = useCallback(async () => {
    setPhase("starting");
    setError(undefined);
    setReport(undefined);
    setRunId(undefined);

    const result = await startSimulation({
      count: SIM_COUNT,
      duration_seconds: SIM_DURATION_SECONDS,
    });

    if (!result.ok) {
      setError(result.error);
      setPhase("error");
      return;
    }

    setRunId(result.data.run_id);
    setElapsed(0);
    setPhase("running");
  }, []);

  const busy = phase === "starting" || phase === "running";

  return (
    <div className={className}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <button
          type="button"
          onClick={handleRun}
          disabled={busy}
          className="inline-flex items-center gap-2 rounded-full bg-accent px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-accent/20 transition-all duration-200 hover:shadow-xl hover:shadow-accent/30 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60 disabled:active:scale-100 motion-reduce:transition-none motion-reduce:active:scale-100"
        >
          {busy ? (
            <>
              <span
                aria-hidden="true"
                className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent motion-reduce:animate-none"
              />
              {phase === "starting" ? "Starting run…" : "Watching the run…"}
            </>
          ) : (
            `Run ${SIM_COUNT} events over ${SIM_DURATION_SECONDS}s`
          )}
        </button>

        {runId ? (
          <span className="font-mono text-xs text-ink-muted">
            run_id {runId}
          </span>
        ) : null}
      </div>

      {phase === "running" ? (
        <p className="mt-4 text-sm leading-relaxed text-ink-muted">
          {SIM_COUNT} events land over {SIM_DURATION_SECONDS}s, but the send
          queue drains at 9 per 60s by design — so `queued` will stay high for
          minutes and that is the correct behaviour, not a backlog bug. Polling
          for the truth diff every {POLL_INTERVAL_MS / 1000}s.
          {elapsed > 0 ? ` Started ${elapsed}s ago.` : null}
        </p>
      ) : null}

      {phase === "error" && error ? (
        <ErrorBanner
          className="mt-5"
          title="Simulation could not start"
          message={error.message}
          hint={
            error.status === 401 || error.status === 403
              ? "PseudoGram rejected our API key. The key is still a placeholder in this deployment, so this is the expected response until a real one is set — the pipeline itself is unaffected."
              : "Nothing was written to the pipeline, so no jobs or counters were touched by this attempt."
          }
        />
      ) : null}

      {report ? (
        <ReportTable report={report} reduced={Boolean(reduced)} />
      ) : phase === "idle" ? (
        <EmptyState
          className="mt-6"
          title="No run yet"
          detail="Kick off a run to fire 500 comment events at our own webhook, then compare our four numbers against PseudoGram's server-side truth for exactly those events."
        />
      ) : null}
    </div>
  );
}

function verdict(ours: number | undefined, truth: number | undefined) {
  if (truth == null) return "unreported" as const;
  if (ours == null) return "unreported" as const;
  return ours === truth ? ("match" as const) : ("mismatch" as const);
}

function ReportTable({
  report,
  reduced,
}: {
  report: SimulationReport;
  reduced: boolean;
}) {
  const discrepancies = report.discrepancies ?? [];
  const rows = METRICS.map((metric) => {
    const ours = report.ours?.[metric.key];
    const truth = report.truth?.[metric.key];
    return { ...metric, ours, truth, state: verdict(ours, truth) };
  });

  const mismatches = rows.filter((r) => r.state === "mismatch").length;
  const unreported = rows.filter((r) => r.state === "unreported").length;
  const pending = String(report.status ?? "").toLowerCase() === "pending";

  return (
    <motion.div
      className="mt-6"
      initial={reduced ? false : { opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
    >
      <div className="flex flex-wrap items-center gap-3">
        <span
          className={[
            "inline-flex items-center gap-2 rounded-full px-3.5 py-1.5 text-xs font-bold uppercase tracking-[0.1em]",
            mismatches > 0
              ? "bg-status-failed-bg text-status-failed"
              : unreported === rows.length
                ? "bg-surface-2 text-ink-muted"
                : "bg-status-sent-bg text-status-sent",
          ].join(" ")}
        >
          {mismatches > 0
            ? `${mismatches} of ${rows.length} disagree`
            : unreported === rows.length
              ? "Truth not reported"
              : `All ${rows.length - unreported} reported numbers agree`}
        </span>
        {pending ? (
          <span className="text-xs text-ink-muted">
            Run still settling — the queue has not finished draining.
          </span>
        ) : null}
      </div>

      <div className="mt-4 overflow-x-auto rounded-2xl border border-line">
        <table className="w-full min-w-[34rem] border-collapse text-left text-sm">
          <caption className="sr-only">
            Our reported statistics compared against PseudoGram&rsquo;s
            server-side truth for this run.
          </caption>
          <thead>
            <tr className="border-b border-line bg-surface-2 text-xs uppercase tracking-[0.14em] text-ink-muted">
              <th scope="col" className="px-4 py-3 font-semibold">
                Metric
              </th>
              <th scope="col" className="px-4 py-3 text-right font-semibold">
                Ours
              </th>
              <th scope="col" className="px-4 py-3 text-right font-semibold">
                Theirs
              </th>
              <th scope="col" className="px-4 py-3 font-semibold">
                Verdict
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key} className="border-b border-line last:border-b-0">
                <th scope="row" className="px-4 py-4 font-normal align-top">
                  <span className="font-mono text-xs font-semibold text-ink">
                    {row.label}
                  </span>
                  <span className="mt-1 block max-w-xs text-xs leading-relaxed text-ink-muted">
                    {row.meaning}
                  </span>
                </th>
                <td className="px-4 py-4 text-right align-top font-semibold tabular-nums text-ink">
                  {row.ours == null ? "—" : formatNumber(row.ours)}
                </td>
                <td className="px-4 py-4 text-right align-top font-semibold tabular-nums text-ink">
                  {row.truth == null ? "—" : formatNumber(row.truth)}
                </td>
                <td className="px-4 py-4 align-top">
                  {row.state === "match" ? (
                    <span className="inline-flex rounded-full bg-status-sent-bg px-2.5 py-1 text-[11px] font-bold uppercase tracking-[0.08em] text-status-sent">
                      Match
                    </span>
                  ) : row.state === "mismatch" ? (
                    <span className="inline-flex rounded-full bg-status-failed-bg px-2.5 py-1 text-[11px] font-bold uppercase tracking-[0.08em] text-status-failed">
                      {row.ours != null && row.truth != null
                        ? `Off by ${formatNumber(Math.abs(row.ours - row.truth))}`
                        : "Mismatch"}
                    </span>
                  ) : (
                    <span className="inline-flex rounded-full bg-surface-2 px-2.5 py-1 text-[11px] font-bold uppercase tracking-[0.08em] text-ink-muted">
                      Not reported
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3 className="mt-8 text-sm font-bold uppercase tracking-[0.14em] text-ink">
        Discrepancies
      </h3>
      {discrepancies.length === 0 ? (
        <p className="mt-2 text-sm leading-relaxed text-ink-muted">
          The backend reported no row-level differences for this run. That means
          every event they sent has a matching record on our side — it does not
          prove the DMs themselves landed, only that our accounting of them
          agrees.
        </p>
      ) : (
        <ul className="mt-3 space-y-2">
          {discrepancies.slice(0, 50).map((d, i) => (
            <li
              key={`${d.kind ?? "diff"}-${d.event_id ?? d.comment_id ?? i}`}
              className="rounded-2xl border border-line bg-bg px-4 py-3 text-sm"
            >
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="rounded-full bg-status-failed-bg px-2.5 py-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-status-failed">
                  {d.kind ?? "difference"}
                </span>
                {d.user_id ? (
                  <span className="font-mono text-xs text-ink-muted">
                    {d.user_id}
                  </span>
                ) : null}
                {d.comment_id ? (
                  <span className="font-mono text-xs text-ink-muted">
                    {d.comment_id}
                  </span>
                ) : null}
                {d.event_id ? (
                  <span className="font-mono text-xs text-ink-muted">
                    {d.event_id}
                  </span>
                ) : null}
              </div>
              {d.detail ? (
                <p className="mt-1.5 text-ink-muted">{d.detail}</p>
              ) : null}
              {d.expected != null || d.actual != null ? (
                <p className="mt-1.5 font-mono text-xs text-ink-muted">
                  expected {JSON.stringify(d.expected) ?? "—"} · got{" "}
                  {JSON.stringify(d.actual) ?? "—"}
                </p>
              ) : null}
            </li>
          ))}
          {discrepancies.length > 50 ? (
            <li className="px-1 text-xs text-ink-muted">
              …and {formatNumber(discrepancies.length - 50)} more not shown.
            </li>
          ) : null}
        </ul>
      )}
    </motion.div>
  );
}

export default SimPanel;

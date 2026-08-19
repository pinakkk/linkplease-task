"use client";

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useState } from "react";
import type { ApiError, Job } from "@/lib/api";
import { formatDateTime, truncate } from "@/lib/format";
import { EmptyState, ErrorBanner, Skeleton } from "./panel";
import { StatusChip } from "./status-chip";

export interface ActivityFeedProps {
  jobs: Job[] | undefined;
  loading: boolean;
  error: ApiError | undefined;
  className?: string;
}

const ERROR_PREVIEW = 90;

/** One job row. Expandable only when there is a `last_error` worth reading. */
function JobRow({ job }: { job: Job }) {
  const [expanded, setExpanded] = useState(false);
  const error = job.last_error ?? "";
  const truncatable = error.length > ERROR_PREVIEW;

  return (
    <div className="border-b border-line px-5 py-4 last:border-b-0 sm:px-7">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
            <span className="font-semibold text-ink">
              {job.username ? `@${job.username}` : "unknown username"}
            </span>
            <span className="font-mono text-[11px] text-ink-muted">
              {job.user_id}
            </span>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
            <span className="font-mono">{job.comment_id}</span>
            <span aria-hidden="true">·</span>
            {/* attempt = retries inside the current send cycle; cycle bumps
                only when the reconciler orders a fresh resend (§4.4). */}
            <span>
              attempt {job.attempt} · cycle {job.cycle}
            </span>
            {job.dm_id ? (
              <>
                <span aria-hidden="true">·</span>
                <span className="font-mono">{job.dm_id}</span>
              </>
            ) : null}
          </div>
        </div>

        <div className="flex shrink-0 flex-col items-start gap-1.5 sm:items-end">
          <StatusChip status={job.status} size="sm" />
          <span className="whitespace-nowrap text-[11px] text-ink-muted">
            {formatDateTime(job.updated_at)}
          </span>
        </div>
      </div>

      {error ? (
        <div className="mt-3 rounded-xl bg-status-failed-bg px-3.5 py-2.5">
          <p className="break-words font-mono text-[11px] leading-relaxed text-status-failed">
            {expanded || !truncatable ? error : truncate(error, ERROR_PREVIEW)}
          </p>
          {truncatable ? (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              className="mt-1.5 text-[11px] font-semibold uppercase tracking-[0.1em] text-status-failed underline underline-offset-2"
            >
              {expanded ? "Show less" : "Show full error"}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Live job feed, newest first. New rows slide in; the list is keyed by
 * `job_id` so a poll that returns the same rows never re-animates them.
 */
export function ActivityFeed({
  jobs,
  loading,
  error,
  className,
}: ActivityFeedProps) {
  const reduced = useReducedMotion();

  if (loading && !jobs) {
    return (
      <div className={`space-y-3 px-5 py-5 sm:px-7 ${className ?? ""}`}>
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-16 w-full" />
        ))}
      </div>
    );
  }

  if (error && !jobs) {
    return (
      <div className={`px-5 py-5 sm:px-7 ${className ?? ""}`}>
        <ErrorBanner
          message={error.message}
          hint="Jobs already queued in the database keep draining regardless of whether this page can read them."
        />
      </div>
    );
  }

  if (!jobs || jobs.length === 0) {
    return (
      <div className={`px-5 py-5 sm:px-7 ${className ?? ""}`}>
        <EmptyState
          title="No jobs yet"
          detail="A job appears the moment a comment matches a rule. Create a rule above and send a comment containing its keyword — or run the simulation below."
        />
      </div>
    );
  }

  return (
    <div className={className}>
      <AnimatePresence initial={false}>
        {jobs.map((job) => (
          <motion.div
            key={job.job_id}
            layout={reduced ? false : "position"}
            initial={reduced ? false : { opacity: 0, x: -16 }}
            animate={{ opacity: 1, x: 0 }}
            exit={reduced ? undefined : { opacity: 0 }}
            transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
          >
            <JobRow job={job} />
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

export default ActivityFeed;

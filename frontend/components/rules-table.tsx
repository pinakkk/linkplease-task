"use client";

import type { ApiError, Rule } from "@/lib/api";
import { formatNumber, formatDateTime, truncate } from "@/lib/format";
import { EmptyState, ErrorBanner, Skeleton } from "./panel";

export interface RulesTableProps {
  rules: Rule[] | undefined;
  loading: boolean;
  error: ApiError | undefined;
  className?: string;
}

/**
 * Rules with their per-rule job counts. `job_count` is DM obligations created
 * by that rule — not DMs delivered — so it is labelled as such rather than
 * "sent", which would overstate what we know.
 */
export function RulesTable({
  rules,
  loading,
  error,
  className,
}: RulesTableProps) {
  if (loading && !rules) {
    return (
      <div className={`space-y-3 px-5 py-5 sm:px-7 ${className ?? ""}`}>
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-14 w-full" />
        ))}
      </div>
    );
  }

  if (error && !rules) {
    return (
      <div className={`px-5 py-5 sm:px-7 ${className ?? ""}`}>
        <ErrorBanner
          message={error.message}
          hint="Rules already stored on the backend are unaffected — this is a read failure, not a data loss."
        />
      </div>
    );
  }

  if (!rules || rules.length === 0) {
    return (
      <div className={`px-5 py-5 sm:px-7 ${className ?? ""}`}>
        <EmptyState
          title="No rules yet"
          detail="Create one above. Until a rule exists, incoming comments are recorded but match nothing, so no DM is ever owed."
        />
      </div>
    );
  }

  return (
    <div className={`overflow-x-auto ${className ?? ""}`}>
      <table className="w-full min-w-[38rem] border-collapse text-left text-sm">
        <caption className="sr-only">
          Rules currently configured, with the number of DM jobs each has
          created.
        </caption>
        <thead>
          <tr className="border-b border-line text-xs uppercase tracking-[0.14em] text-ink-muted">
            <th scope="col" className="px-5 py-3 font-semibold sm:px-7">
              Keyword
            </th>
            <th scope="col" className="px-5 py-3 font-semibold">
              DM message
            </th>
            <th scope="col" className="px-5 py-3 text-right font-semibold">
              Jobs created
            </th>
            <th scope="col" className="px-5 py-3 font-semibold sm:px-7">
              Created
            </th>
          </tr>
        </thead>
        <tbody>
          {rules.map((rule) => (
            <tr
              key={rule.rule_id}
              className="border-b border-line last:border-b-0 align-top"
            >
              <th scope="row" className="px-5 py-4 font-normal sm:px-7">
                <span className="inline-flex rounded-full bg-accent-soft px-3 py-1 text-xs font-bold uppercase tracking-[0.08em] text-accent">
                  {rule.keyword}
                </span>
                <span className="mt-2 block font-mono text-[11px] text-ink-muted">
                  {rule.rule_id}
                </span>
              </th>
              <td className="max-w-sm px-5 py-4 text-ink-muted">
                {truncate(rule.dm_message, 110)}
              </td>
              <td className="px-5 py-4 text-right font-semibold tabular-nums text-ink">
                {formatNumber(rule.job_count ?? 0)}
              </td>
              <td className="whitespace-nowrap px-5 py-4 text-ink-muted sm:px-7">
                {rule.created_at ? formatDateTime(rule.created_at) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default RulesTable;

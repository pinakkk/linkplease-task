/**
 * Typed client for the LinkPlease backend (BLUEPRINT §6).
 *
 * Design rules:
 *  - Nothing in here ever throws into a render. Every call resolves to an
 *    ApiResult discriminated union.
 *  - Every request is bounded by an AbortController timeout, so a hung Fly
 *    machine can never wedge the polling loop.
 *  - Client-side only by intent: the dashboard polls from the browser, so no
 *    Node-only APIs are used and nothing here needs a Node runtime on Cloudflare.
 */

export const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000"
).replace(/\/+$/, "");

/* -------------------------------------------------------------------------- */
/* Contract types                                                              */
/* -------------------------------------------------------------------------- */

/** The four graded numbers — exactly these keys, never wrapped, never renamed. */
export interface Stats {
  sent: number;
  failed: number;
  queued: number;
  duplicates_blocked: number;
}

export interface Rule {
  rule_id: string;
  keyword: string;
  dm_message: string;
  created_at?: string;
  job_count?: number;
}

export const JOB_STATUSES = [
  "QUEUED",
  "SENDING",
  "AWAITING_CONFIRM",
  "SENT",
  "FAILED",
  "CANCELLED",
] as const;

export type JobStatus = (typeof JOB_STATUSES)[number];

export interface Job {
  job_id: number;
  rule_id: string;
  user_id: string;
  username: string | null;
  comment_id: string;
  status: JobStatus;
  attempt: number;
  cycle: number;
  dm_id: string | null;
  last_error: string | null;
  updated_at: string;
}

/**
 * The four graded numbers plus pipeline internals. Modelled permissively —
 * Agent A is finalising the exact shape, so every field beyond the graded four
 * is optional and consumers must guard before rendering.
 */
export interface ExtendedStats extends Stats {
  /** Dedup counters — §4.3 calibration. */
  duplicates_blocked_rule_user?: number;
  duplicate_events_suppressed?: number;

  cancelled?: number;
  sending?: number;
  awaiting_confirm?: number;

  /** Per-status counts, keyed by JobStatus. */
  by_status?: Partial<Record<JobStatus, number>>;

  /** Rate budget, e.g. 7 of 9 sends used in the current window. */
  rate_budget_used?: number;
  rate_budget_max?: number;
  rate_window_seconds?: number;

  /** How far behind the reconciler is, in seconds. */
  reconciler_lag_seconds?: number;
  reconciler_last_run_at?: string | null;
  worker_last_run_at?: string | null;

  /** Event ingest audit. */
  events_received?: number;
  events_redelivered?: number;
  events_deduplicated?: number;

  [key: string]: unknown;
}

export interface EventRecord {
  event_id?: string;
  type?: string;
  comment_id?: string;
  user_id?: string;
  username?: string | null;
  text?: string;
  received_at?: string;
  duplicate?: boolean;
  [key: string]: unknown;
}

export interface Health {
  status?: string;
  db?: boolean | string;
  worker_heartbeat?: string | null;
  reconciler_heartbeat?: string | null;
  [key: string]: unknown;
}

export interface CreateRuleInput {
  keyword: string;
  dm_message: string;
}

export interface SimulationStart {
  run_id: string;
  [key: string]: unknown;
}

export interface SimulationDiscrepancy {
  kind?: string;
  user_id?: string;
  rule_id?: string;
  expected?: unknown;
  actual?: unknown;
  detail?: string;
  [key: string]: unknown;
}

export interface SimulationReport {
  run_id?: string;
  status?: string;
  truth?: Record<string, unknown>;
  ours?: Record<string, unknown>;
  discrepancies?: SimulationDiscrepancy[];
  matched?: boolean;
  [key: string]: unknown;
}

export interface JobsQuery {
  status?: JobStatus;
  limit?: number;
}

/* -------------------------------------------------------------------------- */
/* Result type                                                                 */
/* -------------------------------------------------------------------------- */

export type ApiErrorKind = "network" | "timeout" | "http" | "parse";

export interface ApiError {
  kind: ApiErrorKind;
  message: string;
  status?: number;
}

export type ApiResult<T> =
  | { ok: true; data: T; error?: undefined }
  | { ok: false; data?: undefined; error: ApiError };

export const DEFAULT_TIMEOUT_MS = 6000;

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function fail<T>(error: ApiError): ApiResult<T> {
  return { ok: false, error };
}

export interface RequestOptions {
  timeoutMs?: number;
  /** Caller-owned signal (e.g. from a React effect cleanup). */
  signal?: AbortSignal;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  options: RequestOptions = {},
): Promise<ApiResult<T>> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  const onExternalAbort = () => controller.abort();
  if (options.signal) {
    if (options.signal.aborted) controller.abort();
    else options.signal.addEventListener("abort", onExternalAbort);
  }

  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
      cache: "no-store",
    });

    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.text();
        if (body) detail = body.slice(0, 400);
      } catch {
        /* ignore — the status alone is enough */
      }
      return fail<T>({
        kind: "http",
        status: res.status,
        message: `HTTP ${res.status}: ${detail}`,
      });
    }

    if (res.status === 204) return ok(undefined as T);

    try {
      return ok((await res.json()) as T);
    } catch {
      return fail<T>({
        kind: "parse",
        message: "Response was not valid JSON",
      });
    }
  } catch (err) {
    const aborted =
      (err instanceof DOMException && err.name === "AbortError") ||
      (err as { name?: string } | null)?.name === "AbortError";
    if (aborted) {
      return fail<T>({
        kind: options.signal?.aborted ? "network" : "timeout",
        message: options.signal?.aborted
          ? "Request cancelled"
          : `Request timed out after ${timeoutMs}ms`,
      });
    }
    return fail<T>({
      kind: "network",
      message:
        err instanceof Error ? err.message : "Could not reach the backend",
    });
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onExternalAbort);
  }
}

/* -------------------------------------------------------------------------- */
/* Endpoints                                                                   */
/* -------------------------------------------------------------------------- */

/** `GET /stats` — the four graded integers. */
export function getStats(options?: RequestOptions): Promise<ApiResult<Stats>> {
  return request<Stats>("/stats", {}, options);
}

/** `GET /api/stats/extended` — graded four plus pipeline internals. */
export function getExtendedStats(
  options?: RequestOptions,
): Promise<ApiResult<ExtendedStats>> {
  return request<ExtendedStats>("/api/stats/extended", {}, options);
}

/** `GET /api/rules` — rules with per-rule job counts. */
export function getRules(options?: RequestOptions): Promise<ApiResult<Rule[]>> {
  return request<Rule[] | { rules: Rule[] }>("/api/rules", {}, options).then(
    (r) => {
      if (!r.ok) return r as ApiResult<Rule[]>;
      const list = Array.isArray(r.data) ? r.data : (r.data?.rules ?? []);
      return ok(list);
    },
  );
}

/** `POST /rules` — graded route; returns `{rule_id, keyword, dm_message}`. */
export function createRule(
  input: CreateRuleInput,
  options?: RequestOptions,
): Promise<ApiResult<Rule>> {
  return request<Rule>(
    "/rules",
    { method: "POST", body: JSON.stringify(input) },
    options,
  );
}

/** `GET /api/jobs?status=&limit=` — activity feed. */
export function getJobs(
  query: JobsQuery = {},
  options?: RequestOptions,
): Promise<ApiResult<Job[]>> {
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  if (query.limit != null) params.set("limit", String(query.limit));
  const qs = params.toString();
  return request<Job[] | { jobs: Job[] }>(
    `/api/jobs${qs ? `?${qs}` : ""}`,
    {},
    options,
  ).then((r) => {
    if (!r.ok) return r as ApiResult<Job[]>;
    const list = Array.isArray(r.data) ? r.data : (r.data?.jobs ?? []);
    return ok(list);
  });
}

/** `GET /api/events?limit=` — recent raw events (audit trail). */
export function getEvents(
  limit = 50,
  options?: RequestOptions,
): Promise<ApiResult<EventRecord[]>> {
  return request<EventRecord[] | { events: EventRecord[] }>(
    `/api/events?limit=${encodeURIComponent(limit)}`,
    {},
    options,
  ).then((r) => {
    if (!r.ok) return r as ApiResult<EventRecord[]>;
    const list = Array.isArray(r.data) ? r.data : (r.data?.events ?? []);
    return ok(list);
  });
}

/** `POST /api/simulate` — kicks off a simulation run, returns `{run_id}`. */
export function startSimulation(
  body: Record<string, unknown> = {},
  options?: RequestOptions,
): Promise<ApiResult<SimulationStart>> {
  return request<SimulationStart>(
    "/api/simulate",
    { method: "POST", body: JSON.stringify(body) },
    // Kicking off 500 events can take longer than a plain read.
    { timeoutMs: 20000, ...options },
  );
}

/** `GET /api/simulate/{run_id}/report` — truth-diff report. */
export function getSimulationReport(
  runId: string,
  options?: RequestOptions,
): Promise<ApiResult<SimulationReport>> {
  return request<SimulationReport>(
    `/api/simulate/${encodeURIComponent(runId)}/report`,
    {},
    { timeoutMs: 15000, ...options },
  );
}

/** `GET /healthz` — DB ping + worker/reconciler heartbeats. */
export function getHealth(
  options?: RequestOptions,
): Promise<ApiResult<Health>> {
  return request<Health>("/healthz", {}, { timeoutMs: 4000, ...options });
}

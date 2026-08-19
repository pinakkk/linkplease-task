"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiError, ApiResult, RequestOptions } from "./api";

export interface PollState<T> {
  data: T | undefined;
  error: ApiError | undefined;
  loading: boolean;
  /** Timestamp (ms) of the last successful fetch, for "updated Xs ago". */
  updatedAt: number | undefined;
  /** Fetch immediately, outside the interval (e.g. after a mutation). */
  refresh: () => void;
}

/**
 * Polls `fetcher` every `intervalMs`, pausing while the tab is hidden so we
 * don't hammer the backend in a background tab. `data` is kept from the last
 * success, so a transient error shows a banner instead of blanking the UI.
 *
 * `fetcher` should be a stable reference (module-level function or useCallback).
 */
export function usePoll<T>(
  fetcher: (options?: RequestOptions) => Promise<ApiResult<T>>,
  intervalMs = 2000,
  enabled = true,
): PollState<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<ApiError>();
  const [loading, setLoading] = useState(enabled);
  const [updatedAt, setUpdatedAt] = useState<number>();
  const [tick, setTick] = useState(0);

  // Keeping the fetcher in a ref lets the polling effect call the latest one
  // without re-subscribing (and restarting the interval) on every render.
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  }, [fetcher]);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const controller = new AbortController();

    const run = async () => {
      if (document.visibilityState === "hidden") return;
      const result = await fetcherRef.current({ signal: controller.signal });
      if (cancelled) return;
      if (result.ok) {
        setData(result.data);
        setError(undefined);
        setUpdatedAt(Date.now());
      } else if (result.error.message !== "Request cancelled") {
        setError(result.error);
      }
      setLoading(false);
    };

    void run();
    const id = setInterval(run, intervalMs);
    // Catch up immediately when the tab comes back to the foreground.
    document.addEventListener("visibilitychange", run);

    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(id);
      document.removeEventListener("visibilitychange", run);
    };
  }, [intervalMs, enabled, tick]);

  return { data, error, loading, updatedAt, refresh };
}

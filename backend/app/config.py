# Copyright (c) 2026 Pinak Kundu. All rights reserved.
# Licensed under the Business Source License 1.1 (see LICENSE).
# No use, copying, or modification without written permission.
"""Runtime configuration. Everything comes from the environment; the defaults
are the ones BLUEPRINT settles on, so a bare `uvicorn app.main:app` runs the
same policy as production."""
import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


# --- Credentials / endpoints -------------------------------------------------
# The API key is also the HMAC secret for webhook signatures (ASSIGNMENT).
PSEUDOGRAM_API_KEY = os.getenv("PSEUDOGRAM_API_KEY", "")
PSEUDOGRAM_BASE_URL = os.getenv(
    "PSEUDOGRAM_BASE_URL", "https://pseudogram-api.onrender.com"
).rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Our own public base URL, used when asking the simulator to fire at us.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")

# --- Rate limiting -----------------------------------------------------------
# Their limit is 10 per rolling 60s. We spend 9 and bank one for clock skew
# between our clock and theirs (BLUEPRINT §4.4).
RATE_LIMIT_MAX = _int("RATE_LIMIT_MAX", 9)
RATE_LIMIT_WINDOW_SECONDS = _int("RATE_LIMIT_WINDOW_SECONDS", 60)

# --- Retry policy ------------------------------------------------------------
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 5)      # send attempts within one cycle
MAX_CYCLES = _int("MAX_CYCLES", 3)          # reconciler-ordered resends
BACKOFF_CAP_SECONDS = _float("BACKOFF_CAP_SECONDS", 60.0)
HTTP_TIMEOUT_SECONDS = _float("HTTP_TIMEOUT_SECONDS", 10.0)

# --- Loop cadence ------------------------------------------------------------
WORKER_IDLE_SLEEP = _float("WORKER_IDLE_SLEEP", 0.5)
RECONCILER_INTERVAL = _float("RECONCILER_INTERVAL", 3.0)
MATCHER_SWEEP_INTERVAL = _float("MATCHER_SWEEP_INTERVAL", 5.0)
# A job stuck in SENDING longer than this was orphaned by a crash (§5 row 7).
SENDING_STALE_SECONDS = _float("SENDING_STALE_SECONDS", 60.0)
# Reconciler poll schedule per job, in seconds after the 202 (BLUEPRINT §4.5).
CONFIRM_SCHEDULE = (2, 5, 10, 30)
CONFIRM_INTERVAL_AFTER = _float("CONFIRM_INTERVAL_AFTER", 60.0)

# --- Database pool -----------------------------------------------------------
# Sized >= 20 so a 500-event burst cannot exhaust it (BLUEPRINT §5 row 11).
DB_POOL_MIN = _int("DB_POOL_MIN", 4)
DB_POOL_MAX = _int("DB_POOL_MAX", 20)

# --- Behaviour flags ---------------------------------------------------------
# Signature verification is Part B. Only ever disabled for local unit tests.
VERIFY_SIGNATURES = os.getenv("VERIFY_SIGNATURES", "1") not in ("0", "false", "False")
# Allow the dashboard origin plus local dev.
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
]

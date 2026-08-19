# BLUEPRINT.md — LinkPlease Tech Intern Assignment

> **Binding spec for implementation.** If anything here conflicts with `ASSIGNMENT.md`, `ASSIGNMENT.md` wins — it is the external contract the grader scripts against. Re-read the "Non-negotiable: your API contract" section of `ASSIGNMENT.md` before every deploy and before submitting.

**Target: Parts A + B + C.** But the build order guarantees that at every milestone we have a submittable, honest system — a clean Part A beats a broken Part C.

---

## 0. What actually gets graded (design around this)

1. An automated script fires **500 events at our deployed URL** and compares `/stats` against their server-side truth. → Correctness of counters under load is the #1 priority. **Inflated numbers are worse than honest low numbers** — every counter must be conservative and defensible.
2. They read `FAILURES.md`. → We write it **only from observed behavior in real simulation runs**, never speculation dressed as testing. Long and honest beats short and clean.
3. They watch a 3-minute Loom (recorded by the human, not generated). → The design must have one *articulable* central tradeoff (ours: **DB-as-queue over a broker — simplicity and crash-safety over horizontal scale**).
4. They call. → Every line must be explainable. No magic dependencies.

**Grading reality checks baked into this design:**
- `working_url` must stay live 7 days past deadline → Fly machine must **never auto-stop** (`auto_stop_machines = false`, `min_machines_running = 1`). A sleeping machine scores zero.
- `/webhook` must return 200 within 5s → ingestion path is a signature check + one idempotent INSERT, nothing else.
- The stack is explicitly not graded; the UI is explicitly not graded. The dashboard exists to *stand out at stage 4 (the call)* and to make our own testing/calibration visible — it must never jeopardize the backend contract.

---

## 1. System architecture

```
                 ┌──────────────────────────────────────────────────────────┐
                 │                    Fly.io (always-on)                     │
 PseudoGram ───▶ │  FastAPI (uvicorn, 1 process)                            │
  webhooks       │  ┌────────────┐   ┌─────────────────────────────────┐    │
                 │  │ HTTP layer │   │        asyncio background        │   │
 Grader ───────▶ │  │ /webhook   │   │  ┌──────────┐  ┌─────────────┐  │   │
 script          │  │ /rules     │──▶│  │ Send     │  │ Reconciler  │  │   │
                 │  │ /stats     │   │  │ worker   │  │ (dm status  │  │   │
 Dashboard ────▶ │  │ /api/*     │   │  │ (1 loop) │  │  poller)    │  │   │
 (CORS)          │  └────────────┘   │  └────┬─────┘  └──────┬──────┘  │   │
                 │                   └───────┼───────────────┼─────────┘   │
                 │                           ▼               ▼             │
                 │                ┌──────────────────────────────┐         │
                 │                │   Postgres (Fly Postgres)    │         │
                 │                │   single source of truth:    │         │
                 │                │   events, rules, dm_jobs,    │         │
                 │                │   send_log, tombstones       │         │
                 │                └──────────────────────────────┘         │
                 └──────────────────────────┬───────────────────────────────┘
                                            │ POST /v1/dm/send (rate-budgeted)
                                            │ GET  /v1/dm/{id} (free reads)
                                            ▼
                                   PseudoGram mock API

 ┌───────────────────────────────────────────────┐
 │  Cloudflare Workers (OpenNext)                │
 │  Next.js 15 + Tailwind + TS dashboard         │
 │  landing page · rules manager · live stats ·  │
 │  activity feed · simulation runner            │
 └───────────────────────────────────────────────┘
```

**The one central tradeoff (for the Loom):** Postgres is the queue, the dedup ledger, the rate-limit log, and the stats source — no Redis, no Celery, no broker. What we give up: horizontal scale (a single send-worker loop) and low-latency dispatch. What we get: one source of truth, crash-safe retries (nothing pending lives only in memory), trivially consistent `/stats`, and a system a single person can fully explain. At 9 sends/minute allowed by the platform rate limit, a broker would be pure ceremony.

**Concurrency model:** exactly **one** uvicorn process, **one** send-worker coroutine, **one** reconciler coroutine (started in FastAPI lifespan). This makes the rate limiter and dedup race-free by construction on the happy path. The residual races (multi-request webhook inserts, crash mid-send) are handled by DB constraints and idempotency keys, and anything left goes in `FAILURES.md`.

---

## 2. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI + uvicorn, Python 3.12 | Assignment's own stack; async fits the polling loops |
| DB | Postgres (Fly Postgres, attached) | Constraints do the dedup; `FOR UPDATE SKIP LOCKED` does the queue; survives restarts |
| DB access | `asyncpg` + hand-written SQL (or SQLAlchemy Core) | Every query explainable line-by-line; no ORM magic |
| HTTP client | `httpx.AsyncClient` | Timeouts, connection reuse |
| Backend deploy | Fly.io, `fly deploy --remote-only` | No local Docker available; machine pinned always-on |
| Frontend | Next.js 15 (App Router) + TypeScript + Tailwind CSS v4 | Required stack |
| Animation | Framer Motion (`motion`) + CSS transforms | Scroll reveals, 3D tilt parallax, theme transition |
| Frontend deploy | `@opennextjs/cloudflare` on Cloudflare Workers | Required deploy target |
| Fonts | Inter (variable) via `next/font` | Matches the reference aesthetic |

Fallback if Fly Postgres provisioning fights us: SQLite (WAL mode) on a Fly volume with the same schema — acceptable because there is exactly one process. Postgres is Plan A; do not switch casually.

---

## 3. Data model (Postgres)

```sql
-- Every webhook delivery ever received. The event-level dedup ledger.
CREATE TABLE events (
    event_id      TEXT PRIMARY KEY,          -- their id; UNIQUE = dedup
    event_type    TEXT NOT NULL,             -- comment.created | comment.deleted
    payload       JSONB NOT NULL,            -- raw body, for audit/debug
    sent_at       TIMESTAMPTZ,               -- their timestamp (unordered!)
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    redeliveries  INT NOT NULL DEFAULT 0     -- bumped when same event_id re-arrives
);

CREATE TABLE rules (
    rule_id     TEXT PRIMARY KEY,            -- e.g. "rule_" + nanoid
    keyword     TEXT NOT NULL,               -- stored as-given; matched case-insensitively
    dm_message  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (rule, user) DM obligation. THE core table.
CREATE TABLE dm_jobs (
    job_id           BIGSERIAL PRIMARY KEY,
    rule_id          TEXT NOT NULL REFERENCES rules(rule_id),
    user_id          TEXT NOT NULL,           -- identity is user_id, never username
    username         TEXT,                    -- display only
    comment_id       TEXT NOT NULL,           -- triggering comment (latest if revived)
    post_id          TEXT,
    status           TEXT NOT NULL DEFAULT 'QUEUED',
      -- QUEUED | SENDING | AWAITING_CONFIRM | SENT | FAILED | CANCELLED
    attempt          INT NOT NULL DEFAULT 0,  -- send attempts within current cycle
    cycle            INT NOT NULL DEFAULT 0,  -- bumped on reconciler resend (new Idempotency-Key)
    dm_id            TEXT,                    -- from 202 response
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- "Same user never DMed twice for the same rule": one *live* job per (rule,user).
-- Partial unique: a CANCELLED job (deleted comment before send) may be revived
-- by a NEW comment — the user never received the DM, so a fresh obligation is valid.
CREATE UNIQUE INDEX uq_live_job ON dm_jobs (rule_id, user_id)
    WHERE status <> 'CANCELLED';
CREATE INDEX idx_jobs_due ON dm_jobs (next_attempt_at) WHERE status = 'QUEUED';
CREATE INDEX idx_jobs_confirming ON dm_jobs (updated_at) WHERE status = 'AWAITING_CONFIRM';

-- Rolling-window rate ledger for POST /v1/dm/send (reads are free).
CREATE TABLE send_log (
    id       BIGSERIAL PRIMARY KEY,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_send_log_at ON send_log (sent_at);

-- comment.deleted that arrived before (or without) its comment.created.
CREATE TABLE deleted_comments (
    comment_id  TEXT PRIMARY KEY,
    deleted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Monotonic counters that aren't derivable from job rows.
CREATE TABLE counters (
    name  TEXT PRIMARY KEY,   -- 'duplicates_blocked_rule_user', 'duplicate_events_suppressed'
    value BIGINT NOT NULL DEFAULT 0
);
```

### Job state machine

```
                       comment.deleted (before send)
              ┌──────────────────────────────────────────┐
              ▼                                          │
         CANCELLED ◀─────────────┐                       │
              │ new comment,     │                       │
              │ same (rule,user) │                       │
              ▼                  │                       │
  match ─▶ QUEUED ─▶ SENDING ─▶ 202 ─▶ AWAITING_CONFIRM ─▶ delivered ─▶ SENT
              ▲          │                    │
              │          │ 429/500,           │ reconciler sees "failed"
              │          │ attempt < MAX      │ and cycle < MAX_CYCLES:
              └──────────┘ (backoff)          └─▶ back to QUEUED, cycle+1, attempt=0
                         │
                         │ 400, or attempt ≥ MAX, or cycle ≥ MAX_CYCLES
                         ▼
                       FAILED (terminal)
```

Revival rule (settled): **CANCELLED is revivable by a new qualifying comment** (flip the same row back to QUEUED with the new `comment_id`, `cycle+1`). **FAILED is terminal** — we already burned real send attempts for that user; retrying a FAILED job on a new comment risks a double DM if one of the "failed" sends actually landed.

---

## 4. Pipeline design

### 4.1 `POST /webhook` — the 5-second contract

Fast path only; all real work is async:

1. Read the **raw body bytes** first (needed for HMAC — never re-serialize parsed JSON).
2. Verify `X-PseudoGram-Signature: sha256=<hex>` = HMAC-SHA256(raw body, key=API key), via `hmac.compare_digest`. Invalid/missing → `401`, event ignored. (Part B.)
3. Parse JSON. Malformed → `400`.
4. `INSERT INTO events ... ON CONFLICT (event_id) DO UPDATE SET redeliveries = events.redeliveries + 1` — one round trip tells us if this is a redelivery.
5. If it was a redelivery → return `200` immediately. **Event-level dedup done.**
6. If new: dispatch to the in-process handler (`asyncio.create_task` or a small asyncio.Queue nudge — the DB is the real queue; this just wakes the matcher promptly) and return `200`.

Nothing here calls PseudoGram. Worst case latency = one DB upsert. If the DB is down we return `500` and the event is lost — recorded in `FAILURES.md` (their side may or may not redeliver; we don't control it).

### 4.2 Matching & DM-level dedup

For a new `comment.created`:

1. If `comment_id ∈ deleted_comments` → tombstoned before we saw it; create no jobs (or create-as-CANCELLED for audit). No counter.
2. Load all rules; match **case-insensitive substring** of `keyword` in `text` (`keyword.lower() in text.lower()` — exactly the contract, no word boundaries, no regex cleverness).
3. For each matched rule: `INSERT INTO dm_jobs ... ON CONFLICT` against `uq_live_job` `DO NOTHING`, check rowcount.
   - Inserted → new obligation, worker will pick it up.
   - Conflict → this user already has a live job for this rule → **`duplicates_blocked += 1`**.
   - Existing job is CANCELLED (no conflict on partial index) → revive: `status='QUEUED', comment_id=new, cycle=cycle+1, attempt=0`.

For `comment.deleted` (only `comment_id` populated):
- Upsert into `deleted_comments`.
- Cancel any job with that `comment_id` still in `QUEUED` (atomic `UPDATE ... WHERE status='QUEUED'`). If it's already `SENDING`/`AWAITING_CONFIRM`/`SENT`, the DM is in flight or delivered — nothing sensible to do; leave it.

### 4.3 `duplicates_blocked` — the calibration trap

Two candidate semantics, and we must match *their* server-side number:

- **(a)** suppressed (rule,user) DM obligations from *distinct* comments — our `duplicates_blocked_rule_user` counter.
- **(b)** (a) **plus** redelivered events that would have triggered a DM — needs event-dup tracking too.

We keep **both counters internally** and decide the mapping empirically: run the simulator, `GET /v1/simulate/{run_id}/truth`, and compare which formula reproduces their "duplicates" accounting. **Do not submit before this calibration passes on at least two runs.** Default (pre-calibration): semantics (a) — redelivered events are the *same* event, not a DM we "chose not to send".

### 4.4 Send worker — one loop, rate-budgeted

```
loop forever:
  job = SELECT ... FROM dm_jobs WHERE status='QUEUED' AND next_attempt_at <= now()
        ORDER BY next_attempt_at LIMIT 1 FOR UPDATE SKIP LOCKED
  if none: sleep 0.5s; continue
  wait_for_rate_budget()            # see below
  mark SENDING
  POST /v1/dm/send  with Idempotency-Key: "job:{job_id}:c{cycle}"
    202  → record dm_id, status = AWAITING_CONFIRM, append send_log
    429  → status = QUEUED, next_attempt_at = now() + Retry-After (+ jitter); do NOT count as attempt
    500  → attempt += 1; if attempt ≥ MAX_ATTEMPTS(5) → FAILED
            else QUEUED, next_attempt_at = now() + min(2^attempt, 60)s + jitter(0–1s)
    400  → FAILED immediately (payload bug — retrying can't help), log full body loudly
    timeout/connection error → treat as 500-class BUT the send may have gone through;
            the Idempotency-Key makes the retry safe (same key ⇒ original dm_id back)
```

**Rate limiter (the budget is 9, not 10):** before each send, `SELECT count(*) FROM send_log WHERE sent_at > now() - interval '60 seconds'`; if ≥ 9, sleep until the oldest of those 9 exits the window, then re-check. Rolling window, server-side clock, one consumer ⇒ no races. We bank one request of headroom for clock skew between our clock and theirs and for the odd 429 anyway. `GET /v1/dm/{id}` reads are explicitly free — the reconciler never touches this budget.

**Idempotency-Key discipline (subtle, load-bearing):**
- Key = `job:{job_id}:c{cycle}` — stable across *retries within a cycle* (so a timeout-retry can't double-send),
- but **changes when the reconciler orders a resend** (`cycle+1`) — if we reused the old key, the API would just return the original *failed* `dm_id` forever and the resend would be a no-op loop.

### 4.5 Reconciler — because `202 ≠ delivered` (Part C)

~15% of accepted DMs quietly fail. Loop every ~3s:

1. Pick `AWAITING_CONFIRM` jobs due for a check (poll schedule per job: 2s, 5s, 10s, 30s, then every 60s).
2. `GET /v1/dm/{dm_id}` (free):
   - `delivered` → `SENT`. **This is the only path that increments "sent".**
   - `failed` → if `cycle < MAX_CYCLES(3)`: back to `QUEUED`, `cycle+1`, `attempt=0` (full retry policy again). Else `FAILED`.
   - `queued` → keep polling. If stuck non-terminal > 15 min, keep polling at 60s but it keeps counting as `queued` in stats — honest, not inflated.

### 4.6 `/stats` — one consistent snapshot

```sql
SELECT
  count(*) FILTER (WHERE status = 'SENT')                                   AS sent,
  count(*) FILTER (WHERE status = 'FAILED')                                 AS failed,
  count(*) FILTER (WHERE status IN ('QUEUED','SENDING','AWAITING_CONFIRM')) AS queued
FROM dm_jobs;
-- + duplicates_blocked from counters
```

Single query ⇒ internally consistent snapshot even mid-burst. Semantics, stated so we can defend them on the call:
- `sent` — **only reconciler-confirmed `delivered`**. A 202 is not a sent.
- `failed` — gave up: 400, or retry budget exhausted, or resend cycles exhausted.
- `queued` — anything we still owe: waiting to send, backing off, or 202-unconfirmed.
- `duplicates_blocked` — DMs we deliberately did not send (per §4.3 calibration).
- CANCELLED appears in none of the four buckets (it was never owed once deleted). Verify against truth data; if their accounting disagrees, calibrate.

---

## 5. Failure analysis — worst case / best case per scenario

| # | Scenario | Worst case if naive | Our behavior | Residual risk (→ FAILURES.md if observed) |
|---|---|---|---|---|
| 1 | Same `event_id` twice (8% redelivery) | Double DM | `events` PK upsert dedups at ingest | Two redeliveries in-flight in the same millisecond race the upsert — constraint still guarantees one winner; only counter skew possible |
| 2 | Same user comments 5× on same rule | 5 DMs | `uq_live_job` partial unique index; conflicts increment `duplicates_blocked` | None known — constraint-enforced |
| 3 | `POST /v1/dm/send` returns 500 (~20%) | DM lost | Backoff retry ≤5 attempts, job persisted in DB | ≥5 consecutive 500s (~0.03%) → honest `failed` |
| 4 | 429 rate limit | Ban/thrash or stuck queue | Budget 9/60s pre-check + honor `Retry-After`; 429 doesn't consume an attempt | Clock skew vs their window could still yield rare 429s — handled path, no loss |
| 5 | 202 accepted, later fails (~15%) | Counted "sent", actually lost | Reconciler confirms; failed → resend with **new** idempotency cycle | DM stuck in `queued` on their side forever → we report `queued` forever (honest) |
| 6 | Timeout after send actually landed | Retry double-sends | Same-cycle Idempotency-Key ⇒ API returns original `dm_id` | Relies on their idempotency working as documented |
| 7 | Process crash mid-`SENDING` | In-memory retry lost / double send | All state in Postgres; on boot, `SENDING` older than 60s → back to `QUEUED`, same cycle ⇒ same key ⇒ safe | Crash *after* 202 but *before* recording `dm_id`: retry gets original `dm_id` via idempotency, but if key handling on their side is imperfect → possible dup. Test it; write it up |
| 8 | Machine restarts (deploy, OOM, Fly reschedule) | Everything in memory gone | DB-as-queue: zero pending state in memory by design | Events arriving during the restart window get non-200 → lost unless they redeliver |
| 9 | `comment.deleted` before DM sent | DM sent for deleted comment | Atomic cancel of QUEUED job | Delete arriving mid-`SENDING` (sub-second window) → DM goes out anyway; document |
| 10 | `comment.deleted` before `comment.created` (out of order) | Ghost DM | Tombstone table checked at match time | None known |
| 11 | 500 events / 10s burst | Dropped webhooks (5s deadline) | Ingest = HMAC + 1 upsert (ms); queue drains at 9/min honestly reported as `queued` | Postgres connection-pool exhaustion under burst — size pool ≥ 20, verify in simulation |
| 12 | Forged webhook | Poisoned data / fake DMs | HMAC on raw bytes, constant-time compare, reject → 401 | None known |
| 13 | DB down | Everything | `/webhook` 500s (lost events), `/stats` 500s | Single point of failure — the accepted tradeoff; say so plainly |
| 14 | Rule created mid-burst | Comments before rule creation unmatched | By design: rules match comments arriving *after* creation (no retro-matching) | Confirm against truth data that they don't expect retro-matching |
| 15 | Two uvicorn workers by misconfig | Rate limiter and dedup race | Pin `--workers 1`; assert single-instance at startup | Human error guard: fly.toml comment + CI grep |

**Best case** (everything cooperates): 500 events → ~460 unique after redelivery, matched jobs enqueue in seconds, sends drain at 9/min with zero 429s, reconciler confirms each within ~10s, `/stats` matches truth exactly on all four numbers.

**Worst case we still survive:** burst + 20% 500s + a mid-burst deploy + several deleted comments: nothing is lost because every obligation is a Postgres row before any send is attempted; the only casualties are events that arrive during the seconds the process is actually down, and those are enumerated honestly in `FAILURES.md`.

---

## 6. Backend API surface

**Graded contract (exact paths, exact shapes — never wrap, never rename):**
- `POST /webhook` → `200` fast (§4.1)
- `POST /rules` → `201` `{"rule_id","keyword","dm_message"}`
- `GET /stats` → `{"sent","failed","queued","duplicates_blocked"}` (integers, exactly these four keys)

**Dashboard/internal (additive only — extra routes can't break the grader):**
- `GET /api/rules` — list rules with per-rule job counts
- `GET /api/jobs?status=&limit=` — activity feed (job rows + state)
- `GET /api/stats/extended` — the four graded numbers **plus** internals: both duplicate counters, cancelled count, rate-budget usage, reconciler lag
- `GET /api/events?limit=` — recent raw events (audit)
- `POST /api/simulate` — proxy to `POST /v1/simulate/start` pointing at our own `/webhook`; returns `run_id`
- `GET /api/simulate/{run_id}/report` — fetch truth, diff against our DB, return a discrepancy report (this is the calibration tool, §4.3)
- `GET /healthz` — DB ping + worker/reconciler heartbeat timestamps

CORS: allow the Cloudflare frontend origin (and localhost:3000) on `/api/*` and the three contract routes for the dashboard's read paths.

---

## 7. Frontend — Next.js dashboard (the standout layer)

The graders ignore UI, but stage 4 is a call — a polished, *honest* dashboard that visualizes the pipeline (live stats, job states, a "run 500-event simulation" button with a truth-diff report) demonstrates command of the system. It must never be load-bearing for grading.

### 7.1 Design language (from the AcdyOn reference)

- **Canvas:** near-white lavender-tinted background; content on white rounded-3xl cards with soft, large-radius shadows. Generous whitespace; max-w-6xl centered.
- **Nav:** floating pill navbar (full-width rounded capsule, white, subtle shadow) with logo left, links center, theme toggle + primary CTA button (pill, royal blue) right. Sticky with backdrop blur.
- **Typography:** Inter. Huge tight-tracked bold black headlines (`text-6xl/7xl`, `tracking-tight`); the final word/phrase in **royal-blue italic** ("advancement.", "real authority."). Small uppercase letter-spaced blue kicker labels above sections (`text-xs tracking-[0.2em] uppercase`).
- **Accents:** big faded index numerals (01–04) on feature cards; one inverted deep-navy (#0D1130) card among white ones for emphasis; pill-shaped tags/chips.
- **Stat row:** 4-up grid of white cards — big number with the suffix (`+`, `%`) in blue, small uppercase gray caption. Numbers animate with a count-up on scroll into view.

### 7.2 Design tokens (CSS variables, both themes)

```css
:root {
  --bg: #F4F4FA;        --surface: #FFFFFF;   --surface-2: #EDEDF6;
  --ink: #0A0A14;       --ink-muted: #5B5B70; --line: #E4E4EF;
  --accent: #2E45E6;    --accent-soft: #E6E9FD;
  --navy: #0D1130;      --navy-ink: #EDEEFF;
  --radius-card: 1.5rem; --shadow-card: 0 20px 50px -24px rgb(13 17 48 / 0.18);
}
[data-theme="dark"] {
  --bg: #0B0D1A;        --surface: #12152A;   --surface-2: #1A1E38;
  --ink: #F0F1FA;       --ink-muted: #9A9DBB; --line: #23264A;
  --accent: #5B6EF5;    --accent-soft: #1B2050;
  --navy: #060814;      --navy-ink: #EDEEFF;
  --shadow-card: 0 20px 50px -24px rgb(0 0 0 / 0.6);
}
```

**Theme system:** default **light**; toggle sets `data-theme="dark"` on `<html>`, persisted to `localStorage`, pre-hydration inline script prevents flash. Transition: View Transitions API **circular reveal from the toggle button** where supported, falling back to a global `transition: background-color .45s ease, color .45s ease, border-color .45s ease` (scoped via a temporary `.theme-transition` class so it doesn't tax every interaction). All colors go through tokens — zero hardcoded hex in components. Respect `prefers-color-scheme` only as the *initial* default if no stored choice.

### 7.3 Motion system

- **Scroll reveals:** Framer Motion `whileInView` — fade + 24px rise, staggered children (0.08s), once-only, `viewport={{ margin: "-80px" }}`.
- **3D tilt parallax cards:** on `mousemove`, cards get `perspective(1000px) rotateX/rotateY` (max ±6°) with inner elements translated on Z (`translateZ(20–40px)`) for depth — stat cards, institution cards, hero visual. Spring-damped (Framer `useSpring`), reset on leave.
- **Hero parallax:** layered elements move at different rates on scroll (`useScroll` + `useTransform`), including a slow-drifting radial glow blob behind the headline.
- **Micro-interactions:** buttons scale 0.98 on press; nav CTA has a subtle shadow-grow on hover; count-up numbers; animated status dots (pulse) on live feed rows.
- **Live data:** stats poll every 2s with number *morph* (tween between values), feed rows enter with slide-in.
- **Accessibility:** every effect gated behind `prefers-reduced-motion: no-preference`; tilt disabled on touch devices.

### 7.4 Pages & components

```
app/
  layout.tsx            # fonts, theme script, ThemeProvider, Nav
  page.tsx              # Landing: hero (huge headline, blue italic accent, parallax),
                        #   stat row (live from /api/stats/extended), "how it works"
                        #   numbered cards (01 ingest → 02 match → 03 send → 04 reconcile),
                        #   navy "our guarantee" card (no dup DMs / no silent loss), footer
  dashboard/page.tsx    # 4 live stat cards (sent/failed/queued/duplicates_blocked),
                        #   rules manager (create form + table), activity feed (live jobs
                        #   w/ state chips: QUEUED amber, AWAITING_CONFIRM blue pulse,
                        #   SENT green, FAILED red, CANCELLED gray), rate-budget meter (x/9),
                        #   simulation panel: run 500/10s → poll → truth-diff report table
components/
  nav.tsx  theme-toggle.tsx  tilt-card.tsx  stat-card.tsx  count-up.tsx
  section-heading.tsx (kicker + headline w/ accent word)  reveal.tsx
  rules-table.tsx  rule-form.tsx  activity-feed.tsx  sim-panel.tsx  status-chip.tsx
lib/
  api.ts (typed client, NEXT_PUBLIC_API_BASE)  theme.ts  format.ts
```

Data fetching: plain client-side polling (SWR or a 10-line hook) — this is a live ops dashboard, no need for server components fetching a Fly backend on the edge. Empty/error states designed (backend unreachable → amber banner, never a crash).

---

## 8. Deployment

### 8.1 Backend — Fly.io

```toml
# fly.toml (essentials)
app = "linkplease-backend"          # or whatever's free
primary_region = "bom"
[build]                              # Dockerfile: python:3.12-slim, uv/pip install, uvicorn
[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = false         # NON-NEGOTIABLE: sleeping machine = zero
  auto_start_machines = true
  min_machines_running = 1
[checks.health]
  port = 8000; type = "http"; path = "/healthz"; interval = "30s"
```

- Deploy: `fly deploy --remote-only` (no local Docker on this machine).
- DB: `fly postgres create` + `fly postgres attach` → `DATABASE_URL` secret. Migrations run at startup (idempotent `CREATE TABLE IF NOT EXISTS` script — no Alembic ceremony for 5 tables).
- Secrets: `fly secrets set PSEUDOGRAM_API_KEY=...` (user runs keygen themselves and provides it).
- Exactly **1 machine, 1 uvicorn worker** (`--workers 1`) — the concurrency model depends on it.
- `working_url` for submission = the Fly URL. Must stay live 7 days past deadline → double-check `auto_stop_machines=false` *after* deploy (`fly config show`), and check the app a few times during the week.

### 8.2 Frontend — Cloudflare via OpenNext

- `@opennextjs/cloudflare` + `wrangler deploy`; `NEXT_PUBLIC_API_BASE` → Fly URL.
- Frontend URL goes in the README and the Loom — it is *not* the `working_url`.

### 8.3 Environments

Local dev: `uvicorn --reload` + local Postgres (docker not available → use Fly Postgres proxy `fly proxy 5432` or a local SQLite dev mode), `next dev`. A `cloudflared`/`ngrok` tunnel or the deployed Fly URL is needed to receive real simulator webhooks — simplest reliable loop: **deploy early, test against the real Fly deployment.**

---

## 9. Testing & calibration plan

1. **Unit (pytest, no network):** matcher (case-insensitivity, substring, emoji text, multiple rules), HMAC verify (valid/invalid/missing/mutated raw body), state transitions (table-driven: every edge in §3's machine), rate-window math, stats SQL semantics.
2. **Integration vs a local fake PseudoGram** (tiny FastAPI stub reproducing 429/500/202-then-failed/idempotency): full pipeline drills, including crash-recovery (kill worker mid-send, restart, assert no dup / no loss).
3. **Live simulator runs (the real gate):**
   - `POST /api/simulate` 500 events / 10s at the deployed URL.
   - Wait for queue drain (`queued` monotonically → stable; at 9/min a 500-event burst can legitimately take a long time — verify the grader tolerance by reading truth data timing, and note that most of the 500 events won't match a rule or will be duplicates).
   - `GET /api/simulate/{run_id}/report` — diff our four numbers vs truth. Iterate until **exact match on two consecutive runs**, calibrating `duplicates_blocked` semantics (§4.3) and CANCELLED accounting (§4.6).
4. **Restart drill on Fly:** `fly machine restart` mid-burst; assert zero loss vs truth afterward.
5. Only observations from steps 2–4 go into `FAILURES.md`, with the run conditions attached ("saw it twice during a 500-event run" style).

---

## 10. Delivery plan (each milestone independently submittable)

| Milestone | Contents | Gate |
|---|---|---|
| M1 — Part A live | Schema, webhook ingest + dedup, rules CRUD, matcher, send worker + rate budget + retries, `/stats`, deployed on Fly | Simulator run: no dup DMs, no silent loss |
| M2 — Part B | HMAC verification, `/stats` consistency under load | Forged-request tests; stats correct mid-burst |
| M3 — Part C | Reconciler + resend cycles, `comment.deleted` + tombstones + revival, 500/10s burst clean | Truth-diff exact match ×2 runs |
| M4 — Frontend | Landing + dashboard, themed, animated, deployed on Cloudflare | Visual QA both themes, reduced-motion, mobile |
| M5 — Ship | `FAILURES.md` from observed runs, README (architecture + run instructions), Loom (human), submit | Contract re-read; `auto_stop_machines=false` verified; submission POST |

Human-only tasks (never automated): PseudoGram apply/keygen (real personal data), Loom recording, final `/v1/submit` (can be re-sent to overwrite, so an early draft submission after M1 is smart insurance).

---

## 11. Open items to resolve during implementation

1. `duplicates_blocked` exact semantics — empirically calibrate (§4.3). **Blocking for submission.**
2. Whether CANCELLED jobs appear anywhere in their truth accounting (§4.6). Calibrate.
3. Whether grader waits for queue drain or scores mid-burst — if mid-burst, honest `queued` is exactly what they want to see; no action, but confirm timing from truth data.
4. Fly Postgres vs SQLite-on-volume — Postgres Plan A; decide within first deploy hour.
5. Retro-matching (rule created after comments) — assumed **no**; verify against truth data.

---

## Amendments

Changes made during implementation, with the reason. The spec above is left as
written; these entries override it where they conflict.

### A1. `events` gains `processed_at`; `dm_jobs` gains `check_after` and `checks`
**§3.** Three columns the schema needed and did not have.
- `events.processed_at` — the fast path returns 200 and matches in a background
  task. If that task dies (crash, deploy, unhandled error), the event is
  ingested but never matched, and nothing on disk records that. `processed_at
  IS NULL` is that record, and it makes the matcher sweep loop and the boot
  replay possible. Without it, "no DM is silently lost" is not true across a
  restart in the window between insert and match.
- `dm_jobs.check_after` / `checks` — §4.5 specifies a per-job reconciler poll
  schedule (2s, 5s, 10s, 30s, then 60s). That schedule has to live somewhere
  per job; these two columns are it.

### A2. CANCELLED revival is a fresh row, not a resurrected one
**§3 (revival rule), §4.2 step 3.** The spec says a new qualifying comment
flips the CANCELLED row back to QUEUED with `cycle+1`. The implementation
instead lets the INSERT succeed: `uq_live_job` excludes CANCELLED rows, so the
insert simply does not conflict and a brand-new job row is created.

Why the change: the specified UPDATE can itself violate `uq_live_job` when a
live row and a cancelled row coexist for the same (rule, user) — verified in
testing. The fresh insert cannot. It also gets a naturally fresh
Idempotency-Key (`job:{new_id}:c0`) rather than depending on a cycle bump to
produce one, and it preserves the cancelled row as audit history.

Externally identical: exactly one live obligation and exactly one DM per
(rule, user). The tests assert that invariant rather than the representation.

### A3. Rate-ledger entries are written for every issued request, not just
accepted ones
**§4.4.** `send_log` gets a row before the request goes out, and the row stands
whether the answer was 202, 429, 500, or a timeout. Their limiter counts
requests, not successes, so counting only successes would let a run of 500s
push us over the real limit. Recording before rather than after means a crash
mid-request over-counts by one — the conservative direction.

### A4. Region is `sin`, not `bom`
**§8.1.** Mumbai had no volume capacity for a Postgres cluster at provisioning
time (`app is already using all available zones in region bom`). The app and
the database must share a region, so both moved to Singapore.

### A5. Fly creates a second machine unless stopped
**§8.1.** `fly deploy` with `min_machines_running = 1` silently provisions a
second machine "for high availability". Two machines means two send workers and
two reconcilers against a 10-per-60s limit — the budget of 9 becomes 18 and
every send starts getting 429s. Machine count must be checked after every
deploy. Documented in `fly.toml`.

### A6. Frontend is Next 16, not Next 15
**§2, §7.** `create-next-app@latest` installs Next 16.3.1 / React 19.2.8.
`@opennextjs/cloudflare@1.20.2` declares `next: ">=15.5.21 <16 || >=16.2.11"`,
so 16.3.1 is supported and 16.0–16.2.10 is an excluded range — meaning
downgrading is the risky move, not staying. Kept 16.

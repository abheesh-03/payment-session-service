# Interview notes

Short answers to common design questions, each tied to the code and tests that
back it. Payments in this project are **simulated**: the provider is an in-process
mock with no external side effects.

## Why PostgreSQL and Redis?

- **PostgreSQL is the source of truth.** Session state, successful-payment
  receipts, and all locking live there, because correctness needs transactions,
  row locks (`SELECT … FOR UPDATE`), advisory locks, unique constraints, and
  `CHECK` constraints (`backend/alembic/versions/`).
- **Redis is only an accelerator** for replaying committed receipts
  (`backend/app/cache/receipt_cache.py`). It is written after the PostgreSQL commit,
  and every miss, malformed entry, or Redis error falls back to PostgreSQL. Keys can
  expire or vanish without affecting correctness, which is why idempotency does not
  rely on Redis. The client fails fast (no retries, 1 s timeouts;
  `backend/app/cache/redis_client.py`).
- Tests: `backend/tests/test_concurrency.py` (deleted cache key, malformed entries,
  simulated Redis errors) and `backend/tests/test_dependency_outages.py` (Redis on a
  closed port; one connection attempt per command).

## How are states modeled?

- `CREATED → PAID` and `CREATED → EXPIRED`; both targets are terminal
  (`backend/app/services/payment_service.py`). Status is a string column with a
  database `CHECK`; clients never send a status or timestamps.
- Money is `Decimal` end to end, stored as `NUMERIC(12,2)`, and serialized as an
  exact string; invalid precision is rejected, never rounded.
- Tests: `backend/tests/test_state_transitions.py`, `backend/tests/test_sessions.py`.

## How are duplicates and retries prevented?

- Pay requires an `Idempotency-Key`. A successful payment stores a receipt with the
  key's SHA-256 digest (unique), a request fingerprint (operation + session ID), and
  the exact response body (`backend/app/models/payment_receipt.py`).
- Same key + same session → the stored `200` is replayed; same key + another session
  → `409 IDEMPOTENCY_CONFLICT`; a new key on a paid session →
  `409 SESSION_ALREADY_PAID`. Failed attempts never write a receipt, so they do not
  consume the key.
- In the browser, a synchronous in-flight guard blocks double clicks, and one key
  per attempt is kept in `sessionStorage` (`frontend/lib/paymentKeyStore.ts`).
- Tests: `backend/tests/test_state_transitions.py`, `backend/tests/test_provider.py`,
  `frontend/e2e/checkout.spec.ts`.

## What happens during concurrent payment attempts?

- One transaction, fixed lock order: advisory lock on the key digest → receipt
  lookup → session row lock → decisions using `clock_timestamp()` read under the
  lock (`backend/app/services/payment_service.py`,
  `backend/app/repositories/`).
- Same key: requests serialize on the advisory lock; one pays, the rest replay the
  identical body. Different keys: requests serialize on the row lock; the second sees
  `PAID` and gets `409`. One key across two sessions: one succeeds, the other gets
  `IDEMPOTENCY_CONFLICT` and stays `CREATED`.
- Tests: `backend/tests/test_concurrency.py` uses independent connections and
  committed rows; a holder connection takes the lock first and the test waits until
  PostgreSQL reports both workers blocked (`pg_blocking_pids`) before releasing it.
  Provider invocation counts confirm exactly one charge attempt.

## How are expiration and stale state handled?

- **Lazy expiration:** a `GET` or pay on an overdue `CREATED` session commits
  `EXPIRED` first. A pay commits the expiration *before* returning
  `409 SESSION_EXPIRED`, so the error does not roll it back. The clock is re-read
  after the provider returns, so expiry reached during the provider call is caught
  too. Untouched rows can stay `CREATED` until read (a documented limitation).
- **Stale browser state:** the checkout re-fetches on `online`, `focus`, tab
  visibility, and Refresh; older responses are cancelled or ignored and can never
  replace a terminal state; recovery work is cancelled on navigation
  (`frontend/components/Checkout.tsx`). The countdown only triggers a server check;
  the browser never decides `EXPIRED`.
- Tests: `backend/tests/test_state_transitions.py`, `backend/tests/test_provider.py`
  (expiry during provider call), `frontend/e2e/recovery.spec.ts`,
  `frontend/e2e/lifecycle.spec.ts`.

## What happens if the response is lost after success?

- The backend has already committed `PAID` and the receipt. The browser sees a
  network error, which it treats as **ambiguous**: it re-fetches the session and
  shows `PAID`. If the session were still `CREATED`, an explicit retry would reuse
  the same key; if the status could not be fetched, Pay stays disabled until it can.
- Retrying a committed attempt with its key returns the original `200`, so a retry
  cannot pay twice.
- Test: `frontend/e2e/recovery.spec.ts` forwards the pay request to the real backend,
  confirms `PAID` and one receipt independently, then resets the connection before
  the browser receives the response.

## How are provider failures tested?

- The provider is injected (`backend/app/providers/base.py`, `mock.py`). Tests swap
  in a thread-safe counting provider that can succeed, report an outage, or raise
  (`backend/tests/fakes.py`).
- Known outage → `503 PAYMENT_PROVIDER_UNAVAILABLE`, session unchanged, no receipt or
  cache entry, same key succeeds later. Unexpected error → rollback and sanitized
  `500`; tests plant secret-looking text in the exception and assert it never reaches
  responses or logs.
- Counting asserts the provider is not called for replays, conflicts, or
  unknown/paid/expired sessions, and is called once under concurrency.
- Tests: `backend/tests/test_provider.py`, `backend/tests/test_concurrency.py`; in the
  browser, `frontend/e2e/checkout.spec.ts` injects `X-Mock-Provider-Outcome: fail`
  against a backend started with `ENABLE_MOCK_FAILURES=true`.

## How do the frontend, backend, and database layers interact?

- The browser calls FastAPI directly (`frontend/lib/api.ts`); the Next.js server only
  serves pages. CORS allows `http://localhost:3020`.
- Routes (`backend/app/api/`) → service (business rules and transactions) →
  repositories (SQL only, never commit) → PostgreSQL; the service writes Redis after
  commit. Errors share one JSON shape with a request ID.
- Tests span each layer: rollback-isolated API tests, committed concurrency tests,
  real outage tests, and Playwright tests against servers the test runner starts and
  stops itself (`frontend/scripts/run-e2e.mjs`).

## What would change for a real provider or production scale?

- **Real provider:** a charge cannot share a PostgreSQL transaction, so the current
  "call the provider while holding row locks, then commit" design would change: record
  a pending attempt first, send the idempotency key to the provider, commit the
  outcome afterwards, and reconcile provider records against receipts. This project
  does not claim exactly-once external charging.
- **Production concerns not addressed here:** authentication and per-merchant key
  scoping, HTTPS and a public base URL for checkout links, a background expiry sweep,
  receipt retention, observability (metrics and alerts on provider and cache
  failures), and load testing. No performance figures are claimed.

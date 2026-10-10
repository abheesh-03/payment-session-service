# Payment Session API

Base URL (local): `http://127.0.0.1:8020`

## Conventions

- **Money** is sent and returned as a **decimal string** with exactly two
  decimal places (`"49.99"`, `"10.00"`). Requests may send a JSON number or a
  string; strings are recommended. Values are never rounded: anything with more
  than 2 decimal places, more than 10 integer digits (NUMERIC(12,2)), zero,
  negative, NaN or Infinity is rejected.
- **Timestamps** are ISO 8601 in UTC with a `Z` suffix,
  e.g. `2026-10-10T15:44:44.726816Z`.
- **Request IDs**: every response carries `X-Request-ID`. A caller-supplied
  `X-Request-ID` matching `[A-Za-z0-9._-]{1,64}` is echoed back; otherwise
  the server generates one.
- **Unknown request fields are rejected** (422).

## State machine

```
CREATED ──pay──────▶ PAID     (terminal)
   │
   └──expire───────▶ EXPIRED  (terminal)
```

- Only `CREATED → PAID` and `CREATED → EXPIRED` exist. `PAID` and `EXPIRED`
  never change again, and nothing returns to `CREATED`.
- Clients never supply a status or timestamps; the server decides both.
- A session is overdue when the database clock is **at or after** `expires_at`.
- `paid_at` is set once, on `CREATED → PAID`, and equals that `updated_at`.
  It stays `null` for `EXPIRED` sessions.

### Lazy expiration

There is no background job. An overdue `CREATED` session is moved to
`EXPIRED` (and `updated_at` set) **only when it is next read or paid**:

- `GET /api/sessions/{id}` persists the expiration, then returns it.
- `POST /api/sessions/{id}/pay` persists the expiration, then returns `409 SESSION_EXPIRED`.

Consequently, **sessions nobody touches remain `CREATED` in the database after
`expires_at`**. Anything reading the table directly must treat
`status = 'CREATED' AND expires_at <= now()` as expired.

## POST /api/sessions

Creates a payment session in status `CREATED`. The row is committed to
PostgreSQL before `201` is returned.

Request:

| Field                | Type            | Rules                                                     |
|----------------------|-----------------|-----------------------------------------------------------|
| `merchant_reference` | string          | required; surrounding whitespace trimmed; 1–100 chars; not unique |
| `amount`             | string / number | required; > 0; ≤ 2 decimal places; fits NUMERIC(12,2)     |
| `currency`           | string          | required; `USD`, `EUR`, `GBP`, `INR` (case-insensitive, returned uppercase) |
| `description`        | string / null   | optional; ≤ 500 chars                                     |
| `expires_in_minutes` | integer         | optional; 1–1440; default 15                              |

```json
{
  "merchant_reference": "ORDER-1001",
  "amount": "49.99",
  "currency": "USD",
  "description": "Demo purchase",
  "expires_in_minutes": 15
}
```

Response `201 Created`:

```json
{
  "id": "9ec5ce18-2122-46ee-b5b7-5148be6196bd",
  "merchant_reference": "ORDER-1001",
  "amount": "49.99",
  "currency": "USD",
  "description": "Demo purchase",
  "status": "CREATED",
  "created_at": "2026-10-10T15:44:44.726816Z",
  "expires_at": "2026-10-10T15:59:44.726816Z",
  "paid_at": null,
  "updated_at": "2026-10-10T15:44:44.726816Z",
  "checkout_url": "http://localhost:3020/pay/9ec5ce18-2122-46ee-b5b7-5148be6196bd"
}
```

`checkout_url` is `<FRONTEND_BASE_URL>/pay/<id>` (default `http://localhost:3020`).

Errors: `422 INVALID_REQUEST`.

## GET /api/sessions/{id}

Returns the session from PostgreSQL in the same shape as above.

| Case                     | Status | Code                |
|--------------------------|--------|---------------------|
| Found                    | 200    | —                   |
| Valid but unknown UUID   | 404    | `SESSION_NOT_FOUND` |
| Malformed UUID           | 422    | `INVALID_REQUEST`   |

If the session is `CREATED` and overdue, it is marked `EXPIRED` and committed
before the response is returned. `PAID` and `EXPIRED` sessions are returned
unchanged (repeated fetches do not modify `updated_at`).

## POST /api/sessions/{id}/pay

**Simulated** payment completion through a side-effect-free mock provider (no
network, no money). Takes no request body (any body is ignored).

### Idempotency-Key (required)

| Rule      | Value                                                        |
|-----------|--------------------------------------------------------------|
| Header    | `Idempotency-Key`                                            |
| Length    | 1–128 characters                                             |
| Charset   | `A–Z a–z 0–9 . _ ~ : -` (regex `^[A-Za-z0-9._~:-]{1,128}$`)  |
| Missing / invalid | `422 INVALID_REQUEST` (nothing is changed)           |
| Scope     | **Global** across this service's pay operation: one key = one payment of one session |

Generate a fresh random key (e.g. a UUID) per payment attempt and reuse it
only when retrying that same attempt. Raw keys are never stored or logged;
only their SHA-256 digest is.

### Outcomes

| Case                                          | Status | Code / body                         | Row change                        |
|-----------------------------------------------|--------|-------------------------------------|-----------------------------------|
| `CREATED`, not overdue, new key               | 200    | session (`status: "PAID"`)          | → `PAID`; receipt stored          |
| Retry with the key that paid this session     | 200    | **same body** + `Idempotent-Replayed: true` | none (original `paid_at` kept) |
| Key already paid a **different** session     | 409    | `IDEMPOTENCY_CONFLICT`              | none (neither session touched)    |
| Already `PAID`, different key                 | 409    | `SESSION_ALREADY_PAID`              | none                              |
| `CREATED`, overdue                            | 409    | `SESSION_EXPIRED`                   | → `EXPIRED` (committed)           |
| Already `EXPIRED`                             | 409    | `SESSION_EXPIRED`                   | none                              |
| Valid but unknown UUID                        | 404    | `SESSION_NOT_FOUND`                 | —                                 |
| Malformed UUID or bad/missing key             | 422    | `INVALID_REQUEST`                   | —                                 |
| Provider unavailable (known failure)          | 503    | `PAYMENT_PROVIDER_UNAVAILABLE`      | none (stays `CREATED`)            |
| Unexpected provider error                     | 500    | `INTERNAL_ERROR`                    | none (rolled back)                |
| Session expires while the provider is working | 409    | `SESSION_EXPIRED`                   | → `EXPIRED` (committed)           |

Only **successful** payments create receipts. A request that fails (404, 409
expired/already paid, 422, 500, 503) does not consume its key, so the same key
can be retried and later succeed.

### Mock provider and failure simulation

| Header                    | Values              | Default   |
|---------------------------|---------------------|-----------|
| `X-Mock-Provider-Outcome` | `success` \| `fail` | `success` |

- `fail` makes the mock provider report an outage → `503 PAYMENT_PROVIDER_UNAVAILABLE`.
  It only works when `ENABLE_MOCK_FAILURES=true` (default `false`); otherwise
  `fail` returns `422 INVALID_REQUEST`. Any other value is `422 INVALID_REQUEST`.
- The header is a simulation control, **not** part of the idempotency
  fingerprint: a same-key replay of a successful payment returns the original
  `200` even if it carries `fail`, because replays never call the provider.
- The provider is called only for a new, eligible payment — never for replays,
  key conflicts, or unknown, `PAID` or `EXPIRED` sessions.
- The mock has no network calls, randomness, sleeps or internal retries.

### Retry behavior

- Lost the response to a pay? Retry with the **same** key: you get the original
  `200` body (byte-identical, including the original `paid_at`) and the header
  `Idempotent-Replayed: true`. No second transition happens.
- Retrying with a **new** key after success returns `409 SESSION_ALREADY_PAID`.
- Concurrent requests with the same key are serialized; all of them return the
  same `200` body and exactly one payment happens.

### Client guidance: ambiguous outcomes and key lifetime

A transport failure (connection reset, timeout, offline) or an unexpected 5xx
on `POST /pay` is **ambiguous**: the server may have committed `PAID` before the
response was lost. Clients should:

1. Persist the attempt key **before** sending the first pay request, scoped to
   the session (the reference checkout uses `sessionStorage` key
   `payment-session-service:v1:pay-key:<session id>`).
2. After any failed pay, `GET /api/sessions/{id}`. `PAID` or `EXPIRED` is final;
   `CREATED` means the attempt did not complete and may be retried **with the same
   key**. A retry of an attempt that did commit returns the original `200`
   (`Idempotent-Replayed: true`), so retries never double-charge.
3. If the status cannot be fetched either, do not offer payment until a status
   read succeeds.
4. Discard the key once the session is `PAID` or `EXPIRED`.

Only definite rejections (`503 PAYMENT_PROVIDER_UNAVAILABLE`, `4xx`) mean no
payment was made.

### Durable receipts (PostgreSQL)

Table `payment_receipts`: `key_digest` (SHA-256 hex, **unique**),
`request_fingerprint` (SHA-256 of operation + canonical session UUID),
`session_id` (unique: a session is paid at most once), `response_status`,
`response_body` (JSON snapshot of the 200 response), `created_at` (UTC).
The receipt is inserted in the **same transaction** as the `PAID` update, so
either both commit or neither does. Receipts are never expired.

### Lock order

Every pay runs in one PostgreSQL transaction and always acquires locks in this order:

1. `pg_advisory_xact_lock(<first 64 bits of key digest>)` — serializes requests
   using the same key. A collision between two different keys only makes them
   wait for each other; identity is enforced by the full digest's unique constraint.
2. Receipt lookup by full key digest → replay (matching fingerprint) or
   `IDEMPOTENCY_CONFLICT` (different fingerprint), without touching any session.
3. `SELECT … FOR UPDATE` on the session row, then read `clock_timestamp()` and
   check status/expiry (persisting `EXPIRED` if due).
4. Only for a new, eligible payment: call the provider, read
   `clock_timestamp()` **again** after it returns, re-check expiry, then set
   `PAID` (`paid_at` = `updated_at` = that second reading) and insert the receipt.

Errors are raised only after the transaction ends, so an expiration found in
step 3 is committed before `409 SESSION_EXPIRED` is returned.

### Provider limitation

The `PAID` update and its receipt commit atomically in PostgreSQL. That is a
complete guarantee only because the mock provider has **no external side
effects**. A real charge cannot be part of a PostgreSQL transaction: if the
provider charges and the commit then fails (or the reverse), the two disagree.
A real integration would need provider-side idempotency keys, reconciliation
of provider records against receipts, and a different transaction design
(e.g. recording a pending attempt before calling out, rather than holding row
locks across a network call). None of that is built yet.

### Redis receipt cache

Redis is only a short-lived cache of **committed** successful receipts:

- Key: `<REDIS_KEY_PREFIX>:pay-receipt:<key digest>`; TTL
  `RECEIPT_CACHE_TTL_SECONDS` (default 600).
- Written only **after** the PostgreSQL commit (and refilled after a replay
  served from PostgreSQL). Failed or uncommitted results are never cached.
- Each entry stores the request fingerprint; it is replayed only when the
  fingerprint matches the current request. Otherwise PostgreSQL decides.
- Malformed entries, cache misses, expiry, and Redis errors all fall back to
  PostgreSQL. A failed Redis write cannot undo a committed payment.
- Redis is never used for locking. `/health` still reports `503` if Redis is
  down, but payments remain correct through PostgreSQL. The Redis client makes
  a single attempt per command (no retries, 1 s connect/socket timeouts), so an
  unreachable Redis falls back to PostgreSQL quickly; a refused connection fails
  immediately, while an unresponsive host costs at most about 1 s per cache call.
  Tested in `backend/tests/test_dependency_outages.py`.

## Errors

All errors use one shape; `request_id` equals the `X-Request-ID` header.

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "amount: Decimal input should have no more than 2 decimal places",
    "request_id": "0de3b05439bd404a90a491ce118463b9"
  }
}
```

| Code                  | Status | Meaning                                      |
|-----------------------|--------|----------------------------------------------|
| `INVALID_REQUEST`     | 422    | Body, path or JSON validation failed         |
| `SESSION_NOT_FOUND`   | 404    | No session with that ID                      |
| `SESSION_ALREADY_PAID`| 409    | Pay attempted on a `PAID` session            |
| `SESSION_EXPIRED`     | 409    | Pay attempted on an expired session          |
| `IDEMPOTENCY_CONFLICT`| 409    | Idempotency-Key already paid another session |
| `PAYMENT_PROVIDER_UNAVAILABLE` | 503 | Provider outage; nothing changed, retry with the same key |
| `NOT_FOUND`           | 404    | Unknown route                                |
| `METHOD_NOT_ALLOWED`  | 405    | Wrong HTTP method for the route              |
| `SERVICE_UNAVAILABLE` | 503    | Database unreachable                         |
| `INTERNAL_ERROR`      | 500    | Unexpected error, incl. provider errors; payment work rolled back (details never returned) |

Database errors, driver messages and connection details are never returned.

## GET /health

`200 {"status":"ok","checks":{"postgres":"ok","redis":"ok"}}`,
or `503` with `"status":"degraded"` and the failing dependency marked `"unavailable"`.

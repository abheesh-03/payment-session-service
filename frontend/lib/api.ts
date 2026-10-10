import {
  CURRENCIES,
  type ApiErrorCode,
  type CreateSessionRequest,
  type PaymentSession,
} from "./types";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8020";

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL || DEFAULT_API_BASE_URL
).replace(/\/+$/, "");

/** The server answered with a structured error (or a response we could not use). */
export class ApiError extends Error {
  readonly kind = "api";
  constructor(
    readonly status: number,
    readonly code: ApiErrorCode | string,
    message: string,
    readonly requestId: string | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** The request never produced an HTTP response (offline, DNS, CORS, server down). */
export class NetworkError extends Error {
  readonly kind = "network";
  constructor() {
    super("Could not reach the payment service. Check your connection and try again.");
    this.name = "NetworkError";
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

interface RequestOptions {
  signal?: AbortSignal;
}

async function request(path: string, init: RequestInit): Promise<PaymentSession> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, init);
  } catch (error) {
    if (isAbortError(error)) throw error; // the caller cancelled; not a failure
    throw new NetworkError();
  }

  // Never trust the body shape: it may be HTML from a proxy, empty, or truncated.
  let body: unknown = null;
  try {
    body = await response.json();
  } catch (error) {
    if (isAbortError(error)) throw error;
  }

  if (!response.ok) {
    throw toApiError(response.status, body);
  }
  if (!isPaymentSession(body)) {
    throw new ApiError(
      response.status,
      "UNEXPECTED_RESPONSE",
      "The payment service returned an unexpected response.",
      response.headers.get("X-Request-ID"),
    );
  }
  return body;
}

function toApiError(status: number, body: unknown): ApiError {
  const error = (body as { error?: unknown } | null)?.error as
    | { code?: unknown; message?: unknown; request_id?: unknown }
    | undefined;
  if (error && typeof error.code === "string" && typeof error.message === "string") {
    const requestId = typeof error.request_id === "string" ? error.request_id : null;
    // Messages in the documented error shape are written for clients and sanitized.
    return new ApiError(status, error.code, error.message, requestId);
  }
  // Anything else (HTML error page, empty body) is replaced with a generic message.
  return new ApiError(
    status,
    "UNEXPECTED_RESPONSE",
    status >= 500
      ? "The payment service is having trouble. Please try again."
      : "The request could not be completed.",
    null,
  );
}

function isPaymentSession(value: unknown): value is PaymentSession {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.id === "string" &&
    typeof v.merchant_reference === "string" &&
    typeof v.amount === "string" &&
    typeof v.currency === "string" &&
    (CURRENCIES as readonly string[]).includes(v.currency) &&
    (v.description === null || typeof v.description === "string") &&
    (v.status === "CREATED" || v.status === "PAID" || v.status === "EXPIRED") &&
    typeof v.created_at === "string" &&
    typeof v.expires_at === "string" &&
    (v.paid_at === null || typeof v.paid_at === "string") &&
    typeof v.updated_at === "string" &&
    typeof v.checkout_url === "string"
  );
}

export function createSession(
  payload: CreateSessionRequest,
  { signal }: RequestOptions = {},
): Promise<PaymentSession> {
  return request("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
}

export function fetchSession(
  sessionId: string,
  { signal }: RequestOptions = {},
): Promise<PaymentSession> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: "GET",
    cache: "no-store",
    signal,
  });
}

/**
 * Simulated payment. The caller supplies the Idempotency-Key and must reuse
 * it when retrying the same attempt. Never retried automatically here.
 */
export function paySession(
  sessionId: string,
  idempotencyKey: string,
  { signal }: RequestOptions = {},
): Promise<PaymentSession> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/pay`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    cache: "no-store",
    signal,
  });
}

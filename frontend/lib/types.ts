// Mirrors the FastAPI contract in docs/api.md.

export const CURRENCIES = ["USD", "EUR", "GBP", "INR"] as const;
export type Currency = (typeof CURRENCIES)[number];

export type SessionStatus = "CREATED" | "PAID" | "EXPIRED";

export interface PaymentSession {
  id: string;
  merchant_reference: string;
  /** Exact decimal string with two places, e.g. "49.90". Never a JS number. */
  amount: string;
  currency: Currency;
  description: string | null;
  status: SessionStatus;
  created_at: string;
  expires_at: string;
  paid_at: string | null;
  updated_at: string;
  checkout_url: string;
}

export interface CreateSessionRequest {
  merchant_reference: string;
  /** Decimal string; sent as a JSON string so no float conversion happens. */
  amount: string;
  currency: Currency;
  description?: string;
  /** JSON integer, 1–1440. */
  expires_in_minutes: number;
}

export type ApiErrorCode =
  | "INVALID_REQUEST"
  | "SESSION_NOT_FOUND"
  | "SESSION_ALREADY_PAID"
  | "SESSION_EXPIRED"
  | "IDEMPOTENCY_CONFLICT"
  | "PAYMENT_PROVIDER_UNAVAILABLE"
  | "SERVICE_UNAVAILABLE"
  | "INTERNAL_ERROR"
  | "NOT_FOUND"
  | "METHOD_NOT_ALLOWED"
  // Client-side codes for responses that did not follow the contract.
  | "UNEXPECTED_RESPONSE";

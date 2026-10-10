// Payment-attempt Idempotency-Keys, one per payment session, kept in
// sessionStorage so a reload in the same tab retries with the same key.
//
// - Scoped by session ID under a project-specific, versioned prefix.
// - Every storage access is guarded: storage may be unavailable (privacy
//   modes, sandboxed frames), full (QuotaExceededError) or hold junk.
// - A module-level map keeps a stable in-memory key for the page lifetime,
//   so a storage failure never changes the key between retries.
// - Keys are never logged.

import { newIdempotencyKey } from "./idempotency";

const PREFIX = "payment-session-service:v1:pay-key:";
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const memoryKeys = new Map<string, string>();

function storageKey(sessionId: string): string {
  return `${PREFIX}${sessionId}`;
}

function getStorage(): Storage | null {
  if (typeof window === "undefined") return null; // SSR
  try {
    return window.sessionStorage; // the getter itself can throw
  } catch {
    return null;
  }
}

function readStored(sessionId: string): string | null {
  try {
    const value = getStorage()?.getItem(storageKey(sessionId)) ?? null;
    return value !== null && UUID_V4.test(value) ? value : null;
  } catch {
    return null;
  }
}

function writeStored(sessionId: string, key: string): void {
  try {
    getStorage()?.setItem(storageKey(sessionId), key);
  } catch {
    // Quota or security error: the in-memory key still keeps retries stable.
  }
}

/**
 * The key for this session's payment attempt. Created and saved on first use,
 * which callers must do before sending the first payment request.
 */
export function getOrCreatePaymentKey(sessionId: string): string {
  const key =
    memoryKeys.get(sessionId) ?? readStored(sessionId) ?? newIdempotencyKey();
  memoryKeys.set(sessionId, key);
  if (readStored(sessionId) !== key) writeStored(sessionId, key); // also replaces junk
  return key;
}

/** Forget this session's key (after the backend confirms PAID or EXPIRED). */
export function clearPaymentKey(sessionId: string): void {
  memoryKeys.delete(sessionId);
  try {
    getStorage()?.removeItem(storageKey(sessionId));
  } catch {
    // Nothing else to clean up.
  }
}

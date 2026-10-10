"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, NetworkError, fetchSession, isAbortError, paySession } from "@/lib/api";
import { formatCountdown, formatTimestamp } from "@/lib/format";
import { clearPaymentKey, getOrCreatePaymentKey } from "@/lib/paymentKeyStore";
import type { PaymentSession } from "@/lib/types";
import { useNow } from "@/lib/useNow";

import { Amount } from "./Amount";
import { StatusBadge } from "./StatusBadge";

type LoadError = { kind: "not-found" } | { kind: "failed"; message: string };

/**
 * What the last payment attempt told us.
 * - declined: the server said no payment was made (e.g. provider outage).
 * - ambiguous: no trustworthy answer (network loss, 5xx); it may have succeeded.
 */
type PayOutcome = { kind: "declined"; message: string } | { kind: "ambiguous" };

const isTerminal = (s: PaymentSession) => s.status === "PAID" || s.status === "EXPIRED";

/** Server states only move forward; never let an older CREATED snapshot win. */
function newerOf(prev: PaymentSession | null, next: PaymentSession): PaymentSession {
  return prev && prev.id === next.id && isTerminal(prev) && next.status === "CREATED" ? prev : next;
}

function describeFailure(error: unknown): string {
  if (error instanceof NetworkError || error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}

/** Failures where the server definitely did not take the payment. */
function definiteDecline(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  switch (error.code) {
    case "PAYMENT_PROVIDER_UNAVAILABLE":
      return "The payment provider is temporarily unavailable, so no payment was made.";
    case "INVALID_REQUEST":
    case "IDEMPOTENCY_CONFLICT":
    case "SESSION_NOT_FOUND":
      return "The payment request was rejected, so no payment was made.";
    default:
      return null; // network loss, 500, unexpected responses: outcome unknown
  }
}

export function Checkout({ sessionId }: { sessionId: string }) {
  const [session, setSession] = useState<PaymentSession | null>(null);
  const [loadError, setLoadError] = useState<LoadError | null>(null);
  const [loading, setLoading] = useState(true);
  const [paying, setPaying] = useState(false);
  const [payOutcome, setPayOutcome] = useState<PayOutcome | null>(null);
  // After an ambiguous attempt, Pay stays disabled until a status fetch succeeds.
  const [needsConfirmation, setNeedsConfirmation] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [verifiedDeadline, setVerifiedDeadline] = useState<string | null>(null);

  // Bumped by every fetch/pay (and on unmount); only the latest may apply.
  const requestSeq = useRef(0);
  const fetchAbort = useRef<AbortController | null>(null);
  // Synchronous guard: two clicks in one tick both see `paying === false`.
  const payInFlight = useRef(false);
  // Recovery requested while a payment was in flight; run it afterwards.
  const refreshAfterPay = useRef(false);
  // Pending coalesced recovery; cancelled on unmount (and so on session change,
  // since Checkout is keyed by session ID).
  const recoveryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Fetches canonical state. Callers set `loading` first (it starts true);
  // all state updates happen in the promise callbacks.
  const load = useCallback((): Promise<void> => {
    fetchAbort.current?.abort();
    const controller = new AbortController();
    fetchAbort.current = controller;
    const seq = ++requestSeq.current;
    const isLatest = () => seq === requestSeq.current;
    return fetchSession(sessionId, { signal: controller.signal })
      .then((fresh) => {
        if (!isLatest()) return;
        setSession((prev) => newerOf(prev, fresh));
        setLoadError(null);
        setNeedsConfirmation(false);
      })
      .catch((error: unknown) => {
        if (isAbortError(error) || !isLatest()) return;
        const notFound =
          error instanceof ApiError &&
          (error.code === "SESSION_NOT_FOUND" || error.code === "INVALID_REQUEST");
        setLoadError(
          notFound ? { kind: "not-found" } : { kind: "failed", message: describeFailure(error) },
        );
      })
      .finally(() => {
        if (isLatest()) setLoading(false);
      });
  }, [sessionId]);

  const refresh = useCallback(() => {
    setLoading(true);
    void load();
  }, [load]);

  useEffect(() => {
    void load();
    return () => {
      fetchAbort.current?.abort();
      requestSeq.current += 1; // late callbacks from this mount are ignored
    };
  }, [load]);

  // Recovery: re-read canonical state when the tab regains focus/visibility
  // or the browser comes back online. Events in the same tick are coalesced;
  // during a payment the refresh waits until the payment settles.
  const requestRecovery = useCallback(() => {
    if (payInFlight.current) {
      refreshAfterPay.current = true;
      return;
    }
    if (recoveryTimer.current !== null) return;
    recoveryTimer.current = setTimeout(() => {
      recoveryTimer.current = null;
      if (payInFlight.current) {
        refreshAfterPay.current = true;
        return;
      }
      refresh();
    }, 0);
  }, [refresh]);

  useEffect(
    () => () => {
      if (recoveryTimer.current !== null) clearTimeout(recoveryTimer.current);
      recoveryTimer.current = null;
      refreshAfterPay.current = false;
    },
    [],
  );

  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === "visible") requestRecovery();
    };
    window.addEventListener("online", requestRecovery);
    window.addEventListener("focus", requestRecovery);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("online", requestRecovery);
      window.removeEventListener("focus", requestRecovery);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [requestRecovery]);

  // The attempt key is only needed while the session can still be paid.
  const terminalStatus = session && isTerminal(session) ? session.status : null;
  useEffect(() => {
    if (terminalStatus) clearPaymentKey(sessionId);
  }, [terminalStatus, sessionId]);

  // Informational countdown. When it reaches zero, ask the server once;
  // the browser never decides that a session is EXPIRED.
  const now = useNow();
  const deadlineMs = session ? Date.parse(session.expires_at) : Number.NaN;
  const remainingMs = session?.status === "CREATED" && now !== null ? deadlineMs - now : null;
  const deadlineReached = remainingMs !== null && remainingMs <= 0;
  const verifyingDeadline =
    deadlineReached && (verifiedDeadline !== session?.expires_at || loading);

  const pendingDeadline =
    session?.status === "CREATED" && verifiedDeadline !== session.expires_at
      ? session.expires_at
      : null;
  useEffect(() => {
    // Suspended while paying, so a status fetch never supersedes a payment.
    if (pendingDeadline === null || paying) return;
    const delay = Math.max(0, Date.parse(pendingDeadline) - Date.now());
    const timer = setTimeout(() => {
      setVerifiedDeadline(pendingDeadline);
      requestRecovery(); // waits for an in-flight payment, like other recovery
    }, delay);
    return () => clearTimeout(timer);
  }, [pendingDeadline, paying, requestRecovery]);

  async function handlePay() {
    if (!session || session.status !== "CREATED" || verifyingDeadline || needsConfirmation) return;
    if (payInFlight.current) return;
    payInFlight.current = true;
    // Saved (sessionStorage, or memory fallback) before the request is sent,
    // so a retry after a lost response or reload reuses the same key.
    const key = getOrCreatePaymentKey(sessionId);

    // A pending status fetch must not overwrite the payment result.
    fetchAbort.current?.abort();
    const seq = ++requestSeq.current;
    setLoading(false);
    setPaying(true);
    setPayOutcome(null);
    setNotice(null);
    let refetchAfter = false;
    try {
      const paid = await paySession(sessionId, key);
      if (seq !== requestSeq.current) return;
      setSession((prev) => newerOf(prev, paid));
      setLoadError(null);
      refetchAfter = refreshAfterPay.current;
    } catch (error) {
      if (seq !== requestSeq.current) return;
      // Any failure: re-read canonical state. The server may have committed.
      refetchAfter = true;
      if (error instanceof ApiError && error.code === "SESSION_ALREADY_PAID") {
        setNotice("This session has already been paid.");
      } else if (error instanceof ApiError && error.code === "SESSION_EXPIRED") {
        setNotice("This session expired before the payment could be made.");
      } else {
        const declined = definiteDecline(error);
        if (declined) {
          setPayOutcome({ kind: "declined", message: declined });
        } else {
          setPayOutcome({ kind: "ambiguous" });
          setNeedsConfirmation(true);
        }
      }
    } finally {
      payInFlight.current = false;
      refreshAfterPay.current = false;
      setPaying(false);
      if (refetchAfter && seq === requestSeq.current) refresh();
    }
  }

  if (!session) {
    if (loadError?.kind === "not-found") {
      return (
        <section className="card stack" aria-labelledby="checkout-heading">
          <h1 id="checkout-heading">Payment session not found</h1>
          <p className="muted">
            This checkout link is invalid or the session does not exist. Check the link with the
            merchant.
          </p>
          <div>
            <Link className="button button-secondary" href="/">
              Create a new session
            </Link>
          </div>
        </section>
      );
    }
    if (loadError?.kind === "failed") {
      return (
        <section className="card stack" aria-labelledby="checkout-heading">
          <h1 id="checkout-heading">Couldn&apos;t load this checkout</h1>
          <p className="alert alert-error" role="alert">
            {loadError.message}
          </p>
          <div>
            <button type="button" className="button button-primary" onClick={refresh} disabled={loading}>
              {loading ? "Retrying…" : "Try again"}
            </button>
          </div>
        </section>
      );
    }
    return (
      <section className="card" aria-busy="true">
        <p className="muted" role="status">
          Loading checkout…
        </p>
      </section>
    );
  }

  const canPay =
    session.status === "CREATED" && !paying && !verifyingDeadline && !needsConfirmation;

  return (
    <section className="card stack" aria-labelledby="checkout-heading">
      <div className="header-row">
        <h1 id="checkout-heading">Checkout</h1>
        <StatusBadge status={session.status} />
      </div>

      <Amount amount={session.amount} currency={session.currency} />

      <dl className="details">
        <dt>Merchant reference</dt>
        <dd>{session.merchant_reference}</dd>
        {session.description && (
          <>
            <dt>Description</dt>
            <dd>{session.description}</dd>
          </>
        )}
        <dt>Expires</dt>
        <dd>
          <time dateTime={session.expires_at}>{formatTimestamp(session.expires_at)}</time>
          {remainingMs !== null && remainingMs > 0 && (
            <>
              {" "}
              <span className="countdown muted">({formatCountdown(remainingMs)} left)</span>
            </>
          )}
        </dd>
        {session.paid_at && (
          <>
            <dt>Paid</dt>
            <dd>
              <time dateTime={session.paid_at}>{formatTimestamp(session.paid_at)}</time>
            </dd>
          </>
        )}
      </dl>

      <div aria-live="polite" className="stack">
        {session.status === "PAID" && (
          <p className="alert alert-success">
            <strong>Payment complete</strong>
            This simulated payment was confirmed. No real money was transferred.
          </p>
        )}
        {session.status === "EXPIRED" && (
          <p className="alert alert-warning">
            <strong>This session has expired</strong>
            It can no longer be paid. Ask the merchant for a new checkout link.
          </p>
        )}
        {session.status === "CREATED" && verifyingDeadline && (
          <p className="alert alert-info" role="status">
            The expiry time has passed. Checking the session status with the server…
          </p>
        )}
        {notice && <p className="alert alert-info">{notice}</p>}
        {session.status === "CREATED" && payOutcome?.kind === "declined" && (
          <p className="alert alert-error" role="alert">
            <strong>Payment not completed</strong>
            {payOutcome.message} You can try again; retrying will not create a duplicate payment.
          </p>
        )}
        {session.status === "CREATED" && payOutcome?.kind === "ambiguous" && needsConfirmation && (
          <p className="alert alert-warning" role="alert">
            <strong>Payment status not confirmed yet</strong>
            {loading
              ? "The connection was interrupted, so we're checking whether your payment went through…"
              : "We couldn't confirm whether your payment went through. Payment is paused until the status is confirmed. Use Refresh status; this page also rechecks when your connection returns."}
          </p>
        )}
        {session.status === "CREATED" && payOutcome?.kind === "ambiguous" && !needsConfirmation && (
          <p className="alert alert-info" role="status">
            <strong>Payment not completed</strong>
            We confirmed with the server that this session is still awaiting payment. You can try
            again; retrying will not create a duplicate payment.
          </p>
        )}
        {loadError?.kind === "failed" && !needsConfirmation && (
          <p className="alert alert-error" role="alert">
            Couldn&apos;t refresh the status: {loadError.message}
          </p>
        )}
      </div>

      {session.status === "CREATED" && (
        <div className="stack">
          <button
            type="button"
            className="button button-primary button-block"
            onClick={handlePay}
            disabled={!canPay}
            aria-describedby="simulated-note"
          >
            {paying ? "Processing simulated payment…" : `Pay ${session.amount} ${session.currency} (simulated)`}
          </button>
          <p id="simulated-note" className="muted" style={{ margin: 0, fontSize: "0.9rem" }}>
            This is a simulated payment. No card details are collected and no money is transferred.
          </p>
        </div>
      )}

      <div className="actions">
        <button
          type="button"
          className="button button-secondary"
          onClick={refresh}
          disabled={loading || paying}
        >
          {loading ? "Refreshing…" : "Refresh status"}
        </button>
      </div>
    </section>
  );
}

"use client";

import { useRef, useState } from "react";

import { ApiError, createSession, isAbortError } from "@/lib/api";
import { validateAmount } from "@/lib/money";
import { CURRENCIES, type Currency, type PaymentSession } from "@/lib/types";

interface FormValues {
  merchantReference: string;
  amount: string;
  currency: Currency;
  description: string;
  expiresInMinutes: string;
}

type FieldErrors = Partial<Record<keyof FormValues, string>>;

const INITIAL_VALUES: FormValues = {
  merchantReference: "",
  amount: "",
  currency: "USD",
  description: "",
  expiresInMinutes: "15",
};

function validate(values: FormValues): FieldErrors {
  const errors: FieldErrors = {};
  const reference = values.merchantReference.trim();
  if (!reference) errors.merchantReference = "Enter a merchant reference.";
  else if (reference.length > 100) errors.merchantReference = "Use at most 100 characters.";

  const amountError = validateAmount(values.amount);
  if (amountError) errors.amount = amountError;

  if (values.description.length > 500) errors.description = "Use at most 500 characters.";

  const minutes = values.expiresInMinutes.trim();
  if (!/^\d+$/.test(minutes) || Number(minutes) < 1 || Number(minutes) > 1440) {
    errors.expiresInMinutes = "Enter a whole number of minutes from 1 to 1440.";
  }
  return errors;
}

export function CreateSessionForm({ onCreated }: { onCreated: (session: PaymentSession) => void }) {
  const [values, setValues] = useState<FormValues>(INITIAL_VALUES);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  // Creating is not idempotent: block a second submit in the same tick.
  const submitInFlight = useRef(false);

  function update<K extends keyof FormValues>(key: K, value: FormValues[K]) {
    setValues((prev) => ({ ...prev, [key]: value }));
    setErrors((prev) => ({ ...prev, [key]: undefined }));
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitInFlight.current) return;
    const fieldErrors = validate(values);
    setErrors(fieldErrors);
    setSubmitError(null);
    if (Object.values(fieldErrors).some(Boolean)) return;

    submitInFlight.current = true;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setSubmitting(true);
    try {
      const description = values.description.trim();
      const session = await createSession(
        {
          merchant_reference: values.merchantReference.trim(),
          amount: values.amount.trim(), // decimal string, never Number()
          currency: values.currency,
          ...(description ? { description } : {}),
          expires_in_minutes: Number.parseInt(values.expiresInMinutes.trim(), 10),
        },
        { signal: controller.signal },
      );
      onCreated(session);
    } catch (error) {
      if (isAbortError(error)) return;
      setSubmitError(
        error instanceof ApiError && error.code === "INVALID_REQUEST"
          ? `The server rejected the request: ${error.message}`
          : error instanceof Error
            ? error.message
            : "Something went wrong. Please try again.",
      );
    } finally {
      submitInFlight.current = false;
      if (abortRef.current === controller) setSubmitting(false);
    }
  }

  const describedBy = (key: keyof FormValues, hint?: string) =>
    [hint, errors[key] ? `${key}-error` : null].filter(Boolean).join(" ") || undefined;

  return (
    <form className="card form" onSubmit={handleSubmit} noValidate aria-busy={submitting}>
      <h2>New payment session</h2>

      <div className="field">
        <label htmlFor="merchantReference">Merchant reference</label>
        <input
          id="merchantReference"
          name="merchantReference"
          value={values.merchantReference}
          onChange={(e) => update("merchantReference", e.target.value)}
          maxLength={100}
          autoComplete="off"
          placeholder="ORDER-1001"
          aria-invalid={Boolean(errors.merchantReference)}
          aria-describedby={describedBy("merchantReference")}
          required
        />
        {errors.merchantReference && (
          <span id="merchantReference-error" className="field-error">
            {errors.merchantReference}
          </span>
        )}
      </div>

      <div className="form-row">
        <div className="field">
          <label htmlFor="amount">Amount</label>
          <div className="amount-input">
            <input
              id="amount"
              name="amount"
              type="text"
              inputMode="decimal"
              value={values.amount}
              onChange={(e) => update("amount", e.target.value)}
              placeholder="49.99"
              autoComplete="off"
              aria-invalid={Boolean(errors.amount)}
              aria-describedby={describedBy("amount", "amount-hint")}
              required
            />
            <select
              id="currency"
              name="currency"
              aria-label="Currency"
              value={values.currency}
              onChange={(e) => update("currency", e.target.value as Currency)}
            >
              {CURRENCIES.map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
          </div>
          <span id="amount-hint" className="hint">
            Greater than zero, up to 2 decimal places.
          </span>
          {errors.amount && (
            <span id="amount-error" className="field-error">
              {errors.amount}
            </span>
          )}
        </div>

        <div className="field">
          <label htmlFor="expiresInMinutes">Expires in (minutes)</label>
          <input
            id="expiresInMinutes"
            name="expiresInMinutes"
            type="number"
            inputMode="numeric"
            min={1}
            max={1440}
            step={1}
            value={values.expiresInMinutes}
            onChange={(e) => update("expiresInMinutes", e.target.value)}
            aria-invalid={Boolean(errors.expiresInMinutes)}
            aria-describedby={describedBy("expiresInMinutes", "expires-hint")}
            required
          />
          <span id="expires-hint" className="hint">
            1 to 1440 minutes (24 hours).
          </span>
          {errors.expiresInMinutes && (
            <span id="expiresInMinutes-error" className="field-error">
              {errors.expiresInMinutes}
            </span>
          )}
        </div>
      </div>

      <div className="field">
        <label htmlFor="description">
          Description <span className="muted">(optional)</span>
        </label>
        <textarea
          id="description"
          name="description"
          value={values.description}
          onChange={(e) => update("description", e.target.value)}
          maxLength={500}
          placeholder="Demo purchase"
          aria-invalid={Boolean(errors.description)}
          aria-describedby={describedBy("description")}
        />
        {errors.description && (
          <span id="description-error" className="field-error">
            {errors.description}
          </span>
        )}
      </div>

      {submitError && (
        <p className="alert alert-error" role="alert">
          {submitError}
        </p>
      )}

      <div>
        <button type="submit" className="button button-primary" disabled={submitting}>
          {submitting ? "Creating session…" : "Create payment session"}
        </button>
      </div>
    </form>
  );
}

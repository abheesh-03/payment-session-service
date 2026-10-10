// Amounts stay decimal strings end to end; nothing here uses Number().

// Up to 10 integer digits (NUMERIC(12,2)) and at most 2 decimal places.
const AMOUNT_PATTERN = /^(0|[1-9]\d{0,9})(\.\d{1,2})?$/;

/** Returns an error message, or null when the amount is valid. */
export function validateAmount(raw: string): string | null {
  const value = raw.trim();
  if (value === "") return "Enter an amount.";
  if (!/^[\d.]+$/.test(value)) return "Use digits and an optional decimal point only.";
  const [, fraction = ""] = value.split(".");
  if (value.includes(".") && fraction.length > 2) return "Use at most 2 decimal places.";
  if (!AMOUNT_PATTERN.test(value)) return "Enter a valid amount up to 9999999999.99.";
  if (!/[1-9]/.test(value)) return "Amount must be greater than zero.";
  return null;
}

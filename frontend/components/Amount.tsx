import type { Currency } from "@/lib/types";

/** Shows the backend's exact decimal string; never reformatted through Number. */
export function Amount({ amount, currency }: { amount: string; currency: Currency }) {
  return (
    <p className="amount" aria-label={`${amount} ${currency}`}>
      <span className="amount__value">{amount}</span>
      <span className="amount__currency">{currency}</span>
    </p>
  );
}

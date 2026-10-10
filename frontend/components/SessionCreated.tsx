"use client";

import { QRCodeSVG } from "qrcode.react";

import { formatTimestamp } from "@/lib/format";
import type { PaymentSession } from "@/lib/types";

import { Amount } from "./Amount";
import { CopyLinkButton } from "./CopyLinkButton";
import { StatusBadge } from "./StatusBadge";

export function SessionCreated({
  session,
  onCreateAnother,
}: {
  session: PaymentSession;
  onCreateAnother: () => void;
}) {
  return (
    <section className="card stack" aria-labelledby="created-heading">
      <div className="header-row">
        <h2 id="created-heading">Payment session created</h2>
        <StatusBadge status={session.status} />
      </div>

      <div className="result-grid">
        <div className="stack">
          <Amount amount={session.amount} currency={session.currency} />
          <dl className="details">
            <dt>Reference</dt>
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
            </dd>
            <dt>Session ID</dt>
            <dd className="mono">{session.id}</dd>
          </dl>

          <div className="field">
            <label htmlFor="checkout-url">Hosted checkout link</label>
            <div className="link-box">
              <input id="checkout-url" value={session.checkout_url} readOnly onFocus={(e) => e.target.select()} />
              <CopyLinkButton text={session.checkout_url} />
              <a className="button button-primary" href={session.checkout_url} target="_blank" rel="noopener noreferrer">
                Open checkout
              </a>
            </div>
          </div>
        </div>

        <figure className="qr" style={{ margin: 0 }}>
          {/* Rendered locally; the URL is never sent to a QR service. */}
          <QRCodeSVG value={session.checkout_url} size={176} level="M" marginSize={2} title="Checkout link QR code" />
          <figcaption className="muted" style={{ fontSize: "0.85rem" }}>
            Scan to open checkout
          </figcaption>
        </figure>
      </div>

      <div className="actions">
        <button type="button" className="button button-secondary" onClick={onCreateAnother}>
          Create another session
        </button>
      </div>
    </section>
  );
}

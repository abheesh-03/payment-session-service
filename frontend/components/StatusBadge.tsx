import type { SessionStatus } from "@/lib/types";

const LABELS: Record<SessionStatus, string> = {
  CREATED: "Awaiting payment",
  PAID: "Paid",
  EXPIRED: "Expired",
};

export function StatusBadge({ status }: { status: SessionStatus }) {
  return (
    <span className={`badge badge-${status.toLowerCase()}`}>
      <span className="visually-hidden">Status: </span>
      {LABELS[status]}
    </span>
  );
}

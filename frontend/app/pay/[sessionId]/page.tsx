import type { Metadata } from "next";

import { Checkout } from "@/components/Checkout";

export const metadata: Metadata = {
  title: "Checkout · Payment Session Demo",
  robots: { index: false },
};

export default async function CheckoutPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = await params;
  // key: a different session in the URL gets fresh state (and a fresh payment key).
  return <Checkout key={sessionId} sessionId={sessionId} />;
}

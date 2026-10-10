"use client";

import { useState } from "react";

import type { PaymentSession } from "@/lib/types";

import { CreateSessionForm } from "./CreateSessionForm";
import { SessionCreated } from "./SessionCreated";

export function MerchantDemo() {
  const [created, setCreated] = useState<PaymentSession | null>(null);

  return created ? (
    <SessionCreated session={created} onCreateAnother={() => setCreated(null)} />
  ) : (
    <CreateSessionForm onCreated={setCreated} />
  );
}

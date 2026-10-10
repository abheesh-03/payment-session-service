"use client";

import { useEffect, useRef, useState } from "react";

type CopyState = "idle" | "copied" | "failed";

export function CopyLinkButton({ text }: { text: string }) {
  const [state, setState] = useState<CopyState>("idle");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  async function copy() {
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setState("idle"), 2500);
  }

  return (
    <>
      <button type="button" className="button button-secondary" onClick={copy}>
        {state === "copied" ? "Copied" : "Copy link"}
      </button>
      <span role="status" aria-live="polite" className={state === "failed" ? "field-error" : "visually-hidden"}>
        {state === "copied" && "Checkout link copied to clipboard."}
        {state === "failed" && "Copy failed. Select the link and copy it manually."}
      </span>
    </>
  );
}

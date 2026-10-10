"use client";

import { useSyncExternalStore } from "react";

function subscribe(onTick: () => void): () => void {
  const id = setInterval(onTick, 1000);
  return () => clearInterval(id);
}

// Whole seconds, so the snapshot is stable between ticks.
const getSnapshot = () => Math.floor(Date.now() / 1000) * 1000;
// No clock on the server: callers render a placeholder, so hydration matches.
const getServerSnapshot = () => null;

/** Browser time in ms, updated each second; null during SSR and hydration. */
export function useNow(): number | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

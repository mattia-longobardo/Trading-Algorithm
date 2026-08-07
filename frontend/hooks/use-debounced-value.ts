"use client";

import * as React from "react";

/**
 * Valore che si aggiorna solo dopo una pausa nella digitazione.
 *
 * Serve alle ricerche che finiscono nella query key di TanStack Query: senza,
 * ogni carattere è una chiave nuova, cioè una fetch per tasto premuto.
 */
export function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = React.useState(value);

  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}

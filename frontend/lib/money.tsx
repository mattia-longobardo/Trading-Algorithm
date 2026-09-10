"use client";

/**
 * Valuta e fuso di visualizzazione, in un unico posto.
 *
 * eToro ragiona in dollari: equity, cash, PnL e prezzi arrivano dall'API in
 * USD e in USD restano nel journal. La valuta scelta nelle Impostazioni è
 * quindi solo un modo di *leggere* gli stessi numeri, applicato al momento del
 * rendering con i tassi BCE serviti da /fx/rates. Nessun dato viene riscritto,
 * così cambiare valuta non altera lo storico.
 *
 * Stesso ragionamento per il fuso: l'esecuzione è sempre in UTC, la timezone
 * delle Impostazioni serve solo a mostrare gli orari all'utente.
 */

import * as React from "react";
import { usePathname } from "next/navigation";

import {
  fmtDate,
  fmtDateShort,
  fmtDateTime,
  fmtMoney,
  fmtMoneyCompact,
  fmtMoneySigned,
  tzAbbrev,
} from "@/lib/format";
import { useFxRates, useSettings } from "@/lib/queries";

export interface Display {
  /** Sigla ISO della valuta di visualizzazione (USD se non configurata). */
  currency: string;
  /** Timezone IANA di sola presentazione. */
  timeZone: string;
  /** Sigla del fuso (CEST, JST…) per etichettare gli orari. */
  tzLabel: string;

  /** Importo USD formattato nella valuta di visualizzazione. */
  money(usd: number | null | undefined): string;
  moneyCompact(usd: number | null | undefined): string;
  moneySigned(usd: number | null | undefined): string;

  /** Data/ora nel fuso di visualizzazione. */
  dateTime(iso: string | null | undefined): string;
  date(iso: string | null | undefined): string;
  dateShort(iso: string): string;
}

const FALLBACK: Display = buildDisplay("USD", 1, "UTC");

function buildDisplay(
  currency: string,
  rate: number,
  timeZone: string,
): Display {
  const convert = (usd: number | null | undefined) =>
    usd == null ? null : usd * rate;
  return {
    currency,
    timeZone,
    tzLabel: tzAbbrev(timeZone),
    money: (usd) => fmtMoney(convert(usd), currency),
    moneyCompact: (usd) => fmtMoneyCompact(convert(usd), currency),
    moneySigned: (usd) => fmtMoneySigned(convert(usd), currency),
    dateTime: (iso) => fmtDateTime(iso, timeZone),
    date: (iso) => fmtDate(iso, timeZone),
    dateShort: (iso) => fmtDateShort(iso, timeZone),
  };
}

const DisplayContext = React.createContext<Display>(FALLBACK);

/** Fuso del browser: default sensato finché le impostazioni non arrivano. */
function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function DisplayProvider({ children }: { children: React.ReactNode }) {
  // Sulla pagina di login non c'è sessione: /settings e /fx/rates
  // risponderebbero solo 401. Si resta sul fallback USD/UTC.
  const authenticated = usePathname() !== "/login";
  const settings = useSettings(authenticated);
  const fx = useFxRates(authenticated);

  const currency = settings.data?.currency?.toUpperCase() || "USD";
  const timeZone = settings.data?.timezone || browserTimeZone();
  const rate = currency === "USD" ? 1 : fx.data?.rates?.[currency];

  const value = React.useMemo(
    // Tasso mancante ⇒ si mostrano dollari, invece di stampare numeri
    // convertiti con un cambio inventato.
    () =>
      rate
        ? buildDisplay(currency, rate, timeZone)
        : buildDisplay("USD", 1, timeZone),
    [currency, rate, timeZone],
  );

  return (
    <DisplayContext.Provider value={value}>{children}</DisplayContext.Provider>
  );
}

/** Formattatori di importi e date già allineati alle impostazioni. */
export function useDisplay(): Display {
  return React.useContext(DisplayContext);
}

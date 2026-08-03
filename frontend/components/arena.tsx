"use client";

import { SkullIcon, TrophyIcon } from "lucide-react";

import { Stamp } from "@/components/stamp";
import type { ArenaAgent, TradeDirection } from "@/lib/types";

/** Valore mancante: il campione è troppo piccolo per calcolarlo. */
export const DASH = "—";

/** Applica il formattatore solo se il valore c'è, altrimenti trattino. */
export function orDash(
  value: number | null | undefined,
  fmt: (v: number) => string,
): string {
  return value == null ? DASH : fmt(value);
}

/** Verso della posizione: lo short è l'eccezione, quindi è l'unico evidenziato. */
export function DirectionStamp({ direction }: { direction: TradeDirection }) {
  return direction === "short" ? (
    <Stamp tone="accent">Short</Stamp>
  ) : (
    <Stamp tone="neutral">Long</Stamp>
  );
}

/** Timbro di stato di un agente: campione, morto, evoluto o ancora in gara. */
export function AgentStatusStamp({ agent }: { agent: ArenaAgent }) {
  if (agent.is_champion)
    return (
      <Stamp tone="approved">
        <TrophyIcon /> Campione
      </Stamp>
    );
  if (agent.status === "dead")
    return (
      <Stamp tone="rejected">
        <SkullIcon /> Morto
      </Stamp>
    );
  if (agent.status === "evolved") return <Stamp tone="accent">Evoluto</Stamp>;
  return <Stamp tone="accent">In allenamento</Stamp>;
}

/** Griglia dei parametri di DNA: il profilo di rischio in forma leggibile. */
export function DnaGrid({ agent }: { agent: ArenaAgent }) {
  const dna = agent.dna;
  const holding = dna.max_holding_days ?? 1;
  const rows: [string, string][] = [
    ["Profilo", String(dna.risk_profile)],
    ["Orizzonte", holding <= 1 ? "day trading" : `swing ${holding}g`],
    ["Stop loss", `${dna.stop_loss_pct}%`],
    ["Take profit", `${dna.take_profit_pct}%`],
    ["Max posizioni", String(dna.max_positions)],
    ["Max per posizione", `${dna.max_position_pct}%`],
    ["Aperture per ciclo", String(dna.max_orders_per_cycle)],
    ["Riserva cash", `${dna.min_cash_pct}%`],
    ["Convinzione", `×${dna.conviction_scale}`],
  ];
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
      {rows.map(([label, value]) => (
        <div key={label}>
          <p className="text-muted-foreground font-mono text-[10px] tracking-[0.1em] uppercase">
            {label}
          </p>
          <p className="font-mono text-[13px] font-medium tabular-nums">{value}</p>
        </div>
      ))}
    </div>
  );
}

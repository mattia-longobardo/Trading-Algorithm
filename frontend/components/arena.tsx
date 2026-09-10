"use client";

import Link from "next/link";
import { SkullIcon, TrophyIcon } from "lucide-react";

import { Stamp } from "@/components/stamp";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fmtNum, fmtPctSigned, pnlClass } from "@/lib/format";
import { useDisplay } from "@/lib/money";
import type { AgentDetail, ArenaAgent, TradeDirection } from "@/lib/types";
import { cn } from "@/lib/utils";

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

/** Coppie etichetta/valore in colonne monospaziate: DNA e indicatori. */
export function LabelValueGrid({
  items,
  className,
}: {
  items: { label: string; value: string; tone?: string }[];
  className?: string;
}) {
  return (
    <div className={cn("grid grid-cols-2 gap-x-4 sm:grid-cols-4", className)}>
      {items.map((item) => (
        <div key={item.label}>
          <p className="text-muted-foreground font-mono text-[10px] tracking-[0.1em] uppercase">
            {item.label}
          </p>
          <p className={`font-mono text-[13px] font-medium tabular-nums ${item.tone ?? ""}`}>
            {item.value}
          </p>
        </div>
      ))}
    </div>
  );
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
    <LabelValueGrid
      className="gap-y-1.5"
      items={rows.map(([label, value]) => ({ label, value }))}
    />
  );
}

/** Riga di trade simulato, con l'agente accanto quando la tabella ne mescola due. */
export type SimTradeRow = AgentDetail["trades"][number] & {
  agentId?: string;
  agentName?: string;
};

/**
 * Tabella dei trade simulati chiusi. La usano il dettaglio agente (con i
 * prezzi di ingresso e uscita) e il confronto fra agenti (con la colonna
 * dell'agente al posto dei prezzi): stessa tabella, due viste.
 */
export function SimTradesTable({
  trades,
  showAgentColumn = false,
  showPrices = true,
}: {
  trades: SimTradeRow[];
  showAgentColumn?: boolean;
  showPrices?: boolean;
}) {
  const d = useDisplay();
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {showAgentColumn && <TableHead>Agente</TableHead>}
          <TableHead>Simbolo</TableHead>
          <TableHead>Direzione</TableHead>
          <TableHead className="text-right">Importo</TableHead>
          {showPrices && <TableHead className="text-right">Ingresso → Uscita</TableHead>}
          <TableHead className="text-right">PnL</TableHead>
          <TableHead className="text-right">Durata</TableHead>
          <TableHead>Chiusura</TableHead>
          <TableHead>Motivo chiusura</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {trades.map((t) => (
          <TableRow key={t.agentId ? `${t.agentId}-${t.id}` : t.id}>
            {showAgentColumn && (
              <TableCell>
                <Link
                  href={`/training/${t.agentId}`}
                  className="font-mono text-xs hover:underline"
                >
                  {t.agentName}
                </Link>
              </TableCell>
            )}
            {/* la motivazione di apertura sta nel tooltip: la riga è già larga */}
            <TableCell className="font-mono font-medium" title={t.open_reason}>
              {t.symbol}
            </TableCell>
            <TableCell>
              <DirectionStamp direction={t.direction} />
            </TableCell>
            <TableCell className="text-right font-mono tabular-nums">
              {d.money(t.amount_usd)}
            </TableCell>
            {showPrices && (
              <TableCell className="text-right font-mono text-xs whitespace-nowrap tabular-nums">
                {d.money(t.entry_price)} → {d.money(t.close_price)}
              </TableCell>
            )}
            <TableCell className={`text-right font-mono tabular-nums ${pnlClass(t.pnl_usd)}`}>
              {d.moneySigned(t.pnl_usd)}
              {t.return_pct != null && (
                <span className="ml-1.5 text-xs">({fmtPctSigned(t.return_pct)})</span>
              )}
            </TableCell>
            <TableCell className="text-right font-mono tabular-nums">
              {fmtNum(t.holding_hours, 1)} h
            </TableCell>
            <TableCell className="font-mono text-xs whitespace-nowrap">
              {d.dateTime(t.closed_at)}
            </TableCell>
            <TableCell
              className="text-muted-foreground max-w-64 truncate text-xs"
              title={t.close_reason}
            >
              {t.close_reason}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

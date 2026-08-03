"use client";

import Link from "next/link";
import { ArrowLeftIcon, DnaIcon } from "lucide-react";

import { DuelChart } from "@/components/charts/duel-chart";
import { AgentStatusStamp } from "@/components/arena";
import { PageHeader } from "@/components/page-header";
import { CardSkeleton, ErrorState, TableSkeleton } from "@/components/query-states";
import { Stamp } from "@/components/stamp";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fmtNum, fmtPct, fmtPctSigned, pnlClass } from "@/lib/format";
import { useDisplay, type Display } from "@/lib/money";
import { useArena, useArenaAgent } from "@/lib/queries";
import type {
  AgentDetail,
  AgentMetrics,
  AgentSimTrade,
  TradeDirection,
} from "@/lib/types";

/** Valore mancante: il campione è troppo piccolo per calcolarlo. */
const DASH = "—";

/** Applica il formattatore solo se il valore c'è, altrimenti trattino. */
function orDash(value: number | null | undefined, fmt: (v: number) => string): string {
  return value == null ? DASH : fmt(value);
}

/** Verso della posizione: lo short è l'eccezione, quindi è l'unico evidenziato. */
function DirectionStamp({ direction }: { direction: TradeDirection }) {
  return direction === "short" ? (
    <Stamp tone="accent">Short</Stamp>
  ) : (
    <Stamp tone="neutral">Long</Stamp>
  );
}

/** Verso del confronto: più alto vince, oppure più vicino a zero vince. */
type Better = "higher" | "zero";

interface CompareRow {
  label: string;
  /** Testo mostrato in colonna. */
  render: (m: AgentMetrics) => string;
  /** Valore numerico confrontabile: assente = riga neutra, nessun vincitore. */
  pick?: (m: AgentMetrics) => number | null;
  better?: Better;
  tone?: (m: AgentMetrics) => string;
}

/**
 * Colonna col valore migliore: 0 o 1. Null quando il confronto non ha senso
 * (un valore manca) o finisce in pareggio — meglio nessun grassetto che uno
 * arbitrario.
 */
function betterColumn(
  a: number | null,
  b: number | null,
  better: Better,
): 0 | 1 | null {
  if (a == null || b == null) return null;
  const ka = better === "zero" ? -Math.abs(a) : a;
  const kb = better === "zero" ? -Math.abs(b) : b;
  if (ka === kb) return null;
  return ka > kb ? 0 : 1;
}

function buildRows(d: Display): CompareRow[] {
  return [
    {
      label: "PnL del mese",
      render: (m) => d.moneySigned(m.pnl_usd),
      pick: (m) => m.pnl_usd,
      better: "higher",
      tone: (m) => pnlClass(m.pnl_usd),
    },
    {
      label: "Rendimento",
      render: (m) => orDash(m.return_pct, (v) => fmtPctSigned(v)),
      pick: (m) => m.return_pct,
      better: "higher",
      tone: (m) => pnlClass(m.return_pct),
    },
    { label: "Equity", render: (m) => d.money(m.equity_usd) },
    { label: "Trade chiusi", render: (m) => String(m.n_trades) },
    {
      label: "Win rate",
      render: (m) => orDash(m.win_rate_pct, (v) => fmtPct(v, 1)),
      pick: (m) => m.win_rate_pct,
      better: "higher",
    },
    {
      label: "Profit factor",
      render: (m) => orDash(m.profit_factor, (v) => fmtNum(v)),
      pick: (m) => m.profit_factor,
      better: "higher",
    },
    {
      label: "Sharpe",
      render: (m) => orDash(m.sharpe, (v) => fmtNum(v)),
      pick: (m) => m.sharpe,
      better: "higher",
    },
    {
      label: "Max drawdown",
      render: (m) => orDash(m.max_drawdown_pct, (v) => fmtPct(v, 1)),
      pick: (m) => m.max_drawdown_pct,
      better: "zero",
      tone: (m) => pnlClass(m.max_drawdown_pct),
    },
    {
      label: "Volatilità ann.",
      render: (m) => orDash(m.volatility_pct, (v) => fmtPct(v, 1)),
      pick: (m) => m.volatility_pct,
      better: "zero",
    },
    {
      label: "Expectancy",
      render: (m) => orDash(m.expectancy_usd, (v) => d.moneySigned(v)),
      pick: (m) => m.expectancy_usd,
      better: "higher",
      tone: (m) => pnlClass(m.expectancy_usd),
    },
    {
      label: "Holding medio",
      render: (m) => orDash(m.avg_holding_hours, (v) => `${fmtNum(v, 1)} h`),
    },
    {
      label: "Trade/giorno",
      render: (m) => orDash(m.trades_per_day, (v) => fmtNum(v, 1)),
    },
    { label: "Esposizione", render: (m) => fmtPct(m.exposure_pct, 1) },
    {
      label: "Long (n · PnL)",
      render: (m) => `${m.long.n} · ${d.moneySigned(m.long.pnl_usd)}`,
    },
    {
      label: "Short (n · PnL)",
      render: (m) => `${m.short.n} · ${d.moneySigned(m.short.pnl_usd)}`,
    },
  ];
}

function CompareTable({ left, right }: { left: AgentDetail; right: AgentDetail }) {
  const d = useDisplay();
  const rows = buildRows(d);
  const details = [left, right];
  const insufficient = details.filter((x) => x.metrics.insufficient_sample);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Indicatori a confronto</CardTitle>
        <CardDescription>
          Stessi calcoli della scheda singola, affiancati: in grassetto il valore
          migliore, dove il confronto ha senso
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Indicatore</TableHead>
              {details.map((x) => (
                <TableHead key={x.agent.id} className="text-right">
                  <span className="flex items-center justify-end gap-1.5">
                    <Link
                      href={`/training/${x.agent.id}`}
                      className="font-mono font-medium hover:underline"
                    >
                      {x.agent.name}
                    </Link>
                    <AgentStatusStamp agent={x.agent} />
                  </span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => {
              const winner = row.pick
                ? betterColumn(
                    row.pick(left.metrics),
                    row.pick(right.metrics),
                    row.better ?? "higher",
                  )
                : null;
              return (
                <TableRow key={row.label}>
                  <TableCell className="text-muted-foreground">{row.label}</TableCell>
                  {details.map((x, i) => (
                    <TableCell
                      key={x.agent.id}
                      className={`text-right font-mono tabular-nums ${row.tone?.(x.metrics) ?? ""} ${winner === i ? "font-semibold" : ""}`}
                    >
                      {row.render(x.metrics)}
                    </TableCell>
                  ))}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        {insufficient.length > 0 && (
          <p className="text-muted-foreground text-xs">
            Campione ridotto per{" "}
            {insufficient.map((x) => x.agent.name).join(" e ")}: indicatori poco
            significativi (meno di 5 trade o di 3 giorni di storia).
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function StrategyCard({ detail }: { detail: AgentDetail }) {
  const { agent } = detail;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <DnaIcon className="text-primary size-4" /> Strategia · {agent.name}
        </CardTitle>
        <CardDescription>
          Profilo di rischio: {String(agent.dna.risk_profile)}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm leading-5">{String(agent.dna.strategy)}</p>
      </CardContent>
    </Card>
  );
}

/** Trade con l'etichetta dell'agente che l'ha fatto: la tabella è unica. */
interface MergedTrade extends AgentSimTrade {
  agentId: string;
  agentName: string;
}

function mergeTrades(details: AgentDetail[]): MergedTrade[] {
  return details
    .flatMap((x) =>
      x.trades.map((t) => ({
        ...t,
        agentId: x.agent.id,
        agentName: x.agent.name,
      })),
    )
    .sort((a, b) => new Date(b.closed_at).getTime() - new Date(a.closed_at).getTime())
    .slice(0, 20);
}

function TradesCard({ left, right }: { left: AgentDetail; right: AgentDetail }) {
  const d = useDisplay();
  const trades = mergeTrades([left, right]);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Ultimi trade</CardTitle>
        <CardDescription>
          Gli ultimi 20 trade chiusi dai due agenti, dal più recente
        </CardDescription>
      </CardHeader>
      <CardContent>
        {trades.length === 0 ? (
          <p className="text-muted-foreground py-6 text-center text-sm">
            Ancora nessun trade chiuso
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Agente</TableHead>
                <TableHead>Simbolo</TableHead>
                <TableHead>Direzione</TableHead>
                <TableHead className="text-right">Importo</TableHead>
                <TableHead className="text-right">PnL</TableHead>
                <TableHead className="text-right">Durata</TableHead>
                <TableHead>Chiusura</TableHead>
                <TableHead>Motivo chiusura</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {trades.map((t) => (
                <TableRow key={`${t.agentId}-${t.id}`}>
                  <TableCell>
                    <Link
                      href={`/training/${t.agentId}`}
                      className="font-mono text-xs hover:underline"
                    >
                      {t.agentName}
                    </Link>
                  </TableCell>
                  <TableCell className="font-mono font-medium" title={t.open_reason}>
                    {t.symbol}
                  </TableCell>
                  <TableCell>
                    <DirectionStamp direction={t.direction} />
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {d.money(t.amount_usd)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-mono tabular-nums ${pnlClass(t.pnl_usd)}`}
                  >
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
        )}
      </CardContent>
    </Card>
  );
}

export default function ConfrontoPage() {
  const overview = useArena();
  const agents = overview.data?.agents ?? [];
  // Gli hook stanno fuori da ogni ramo: senza agente passano null e restano
  // disabilitati da soli.
  const left = useArenaAgent(agents[0]?.id ?? null);
  const right = useArenaAgent(agents[1]?.id ?? null);

  const header = (
    <PageHeader
      eyebrow="Arena evolutiva"
      title="Testa a testa"
      description="I due agenti vivi a confronto: stesso capitale, stesso mercato, sopravvive uno solo."
      actions={
        <Button variant="outline" size="sm" asChild>
          <Link href="/training">
            <ArrowLeftIcon /> Allenamento
          </Link>
        </Button>
      }
    />
  );

  const loadingBody = (
    <>
      <CardSkeleton className="h-80 w-full" />
      <Card>
        <CardContent className="p-4">
          <TableSkeleton rows={8} />
        </CardContent>
      </Card>
    </>
  );

  if (overview.isLoading) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        {loadingBody}
      </div>
    );
  }

  if (overview.error || !overview.data) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <ErrorState error={overview.error} title="Arena non disponibile" />
      </div>
    );
  }

  if (agents.length < 2) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <Card>
          <CardContent className="text-muted-foreground py-16 text-center text-sm">
            Servono due agenti vivi per il confronto: la prossima generazione nasce
            al prossimo ciclo di allenamento.
          </CardContent>
        </Card>
      </div>
    );
  }

  if (left.isLoading || right.isLoading) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        {loadingBody}
      </div>
    );
  }

  if (left.error || right.error || !left.data || !right.data) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <ErrorState
          error={left.error ?? right.error}
          title="Schede degli agenti non disponibili"
        />
      </div>
    );
  }

  const leftDetail = left.data;
  const rightDetail = right.data;
  const series = [leftDetail, rightDetail].map((x) => ({
    name: x.agent.name,
    points: x.equity,
  }));
  const drawable = series.some((s) => s.points.length >= 2);

  return (
    <div className="flex flex-col gap-6">
      {header}

      <Card>
        <CardHeader>
          <CardTitle>Curve equity sovrapposte</CardTitle>
          <CardDescription>
            Il conto simulato dei due agenti, rilevato a ogni ciclo di allenamento
          </CardDescription>
        </CardHeader>
        <CardContent>
          {drawable ? (
            <DuelChart series={series} />
          ) : (
            <p className="text-muted-foreground py-16 text-center text-sm">
              Servono almeno due rilevazioni per disegnare le curve: torna dopo il
              prossimo ciclo.
            </p>
          )}
        </CardContent>
      </Card>

      <CompareTable left={leftDetail} right={rightDetail} />

      <div className="grid gap-4 xl:grid-cols-2">
        <StrategyCard detail={leftDetail} />
        <StrategyCard detail={rightDetail} />
      </div>

      <TradesCard left={leftDetail} right={rightDetail} />
    </div>
  );
}

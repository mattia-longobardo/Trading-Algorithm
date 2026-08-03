"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeftIcon, DnaIcon, SwordsIcon } from "lucide-react";

import { AgentEquityChart } from "@/components/charts/agent-equity-chart";
import {
  AgentStatusStamp,
  DirectionStamp,
  DnaGrid,
  orDash,
} from "@/components/arena";
import { PageHeader } from "@/components/page-header";
import { CardSkeleton, ErrorState, TableSkeleton } from "@/components/query-states";
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
import { useDisplay } from "@/lib/money";
import { useArena, useArenaAgent } from "@/lib/queries";
import type { AgentDetail, AgentMetrics, TradeDirection } from "@/lib/types";

function SummaryTile({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint: string;
  tone?: string;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <p className="text-muted-foreground font-mono text-[10px] tracking-[0.14em] uppercase">
          {label}
        </p>
        <p
          className={`mt-2 font-mono text-xl font-semibold tabular-nums sm:text-2xl ${tone ?? ""}`}
        >
          {value}
        </p>
        <p className="text-muted-foreground mt-1 text-[11px]">{hint}</p>
      </CardContent>
    </Card>
  );
}

function SummaryStrip({ metrics }: { metrics: AgentMetrics }) {
  const d = useDisplay();
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      <SummaryTile
        label="PnL del mese"
        value={d.moneySigned(metrics.pnl_usd)}
        hint={
          metrics.return_pct == null
            ? "sul capitale iniziale"
            : `${fmtPctSigned(metrics.return_pct)} sul capitale iniziale`
        }
        tone={pnlClass(metrics.pnl_usd)}
      />
      <SummaryTile
        label="Equity"
        value={d.money(metrics.equity_usd)}
        hint={`${fmtPct(metrics.cash_pct, 1)} cash · ${fmtPct(metrics.exposure_pct, 1)} investito`}
      />
      <SummaryTile
        label="Trade chiusi"
        value={String(metrics.n_trades)}
        hint={`${metrics.n_wins} vinti · ${metrics.n_losses} persi`}
      />
      <SummaryTile
        label="Win rate"
        value={orDash(metrics.win_rate_pct, (v) => fmtPct(v, 1))}
        hint={`profit factor ${orDash(metrics.profit_factor, (v) => fmtNum(v))}`}
      />
    </div>
  );
}

/** Coppie etichetta/valore in stile DNA, per gli indicatori di performance. */
function StatGrid({ items }: { items: { label: string; value: string; tone?: string }[] }) {
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
      {items.map((item) => (
        <div key={item.label}>
          <p className="text-muted-foreground font-mono text-[10px] tracking-[0.1em] uppercase">
            {item.label}
          </p>
          <p
            className={`font-mono text-[13px] font-medium tabular-nums ${item.tone ?? ""}`}
          >
            {item.value}
          </p>
        </div>
      ))}
    </div>
  );
}

function PerformanceCard({ metrics }: { metrics: AgentMetrics }) {
  const d = useDisplay();
  const items = [
    { label: "Sharpe", value: orDash(metrics.sharpe, (v) => fmtNum(v)) },
    {
      label: "Volatilità ann.",
      value: orDash(metrics.volatility_pct, (v) => fmtPct(v, 1)),
    },
    {
      label: "Max drawdown",
      value: orDash(metrics.max_drawdown_pct, (v) => fmtPct(v, 1)),
      tone: pnlClass(metrics.max_drawdown_pct),
    },
    { label: "Profit factor", value: orDash(metrics.profit_factor, (v) => fmtNum(v)) },
    {
      label: "Expectancy",
      value: orDash(metrics.expectancy_usd, (v) => d.moneySigned(v)),
      tone: pnlClass(metrics.expectancy_usd),
    },
    {
      label: "Miglior trade",
      value: orDash(metrics.best_trade_usd, (v) => d.moneySigned(v)),
      tone: pnlClass(metrics.best_trade_usd),
    },
    {
      label: "Peggior trade",
      value: orDash(metrics.worst_trade_usd, (v) => d.moneySigned(v)),
      tone: pnlClass(metrics.worst_trade_usd),
    },
    {
      label: "Media vincite",
      value: orDash(metrics.avg_win_usd, (v) => d.moneySigned(v)),
      tone: pnlClass(metrics.avg_win_usd),
    },
    {
      label: "Media perdite",
      value: orDash(metrics.avg_loss_usd, (v) => d.moneySigned(v)),
      tone: pnlClass(metrics.avg_loss_usd),
    },
    {
      label: "Holding medio",
      value: orDash(metrics.avg_holding_hours, (v) => `${fmtNum(v, 1)} h`),
    },
    {
      label: "Trade/giorno",
      value: orDash(metrics.trades_per_day, (v) => fmtNum(v, 1)),
    },
    { label: "Esposizione", value: fmtPct(metrics.exposure_pct, 1) },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Indicatori di performance</CardTitle>
        <CardDescription>
          Calcolati sul conto simulato dell&apos;agente, dalla nascita a oggi
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <StatGrid items={items} />
        {metrics.insufficient_sample && (
          <p className="text-muted-foreground text-xs">
            Campione ridotto: indicatori poco significativi (meno di 5 trade o di 3
            giorni di storia).
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function DirectionCard({ metrics }: { metrics: AgentMetrics }) {
  const d = useDisplay();
  const columns: [TradeDirection, AgentMetrics["long"]][] = [
    ["long", metrics.long],
    ["short", metrics.short],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Long vs Short</CardTitle>
        <CardDescription>
          Da che parte del mercato l&apos;agente guadagna davvero
        </CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-4">
        {columns.map(([direction, split]) => (
          <div key={direction} className="space-y-2">
            <DirectionStamp direction={direction} />
            <div className="space-y-1.5">
              <div>
                <p className="text-muted-foreground font-mono text-[10px] tracking-[0.1em] uppercase">
                  Trade
                </p>
                <p className="font-mono text-[13px] font-medium tabular-nums">
                  {split.n}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground font-mono text-[10px] tracking-[0.1em] uppercase">
                  PnL
                </p>
                <p
                  className={`font-mono text-[13px] font-medium tabular-nums ${pnlClass(split.pnl_usd)}`}
                >
                  {d.moneySigned(split.pnl_usd)}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground font-mono text-[10px] tracking-[0.1em] uppercase">
                  Win rate
                </p>
                <p className="font-mono text-[13px] font-medium tabular-nums">
                  {orDash(split.win_rate_pct, (v) => fmtPct(v, 1))}
                </p>
              </div>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function PositionsCard({ detail }: { detail: AgentDetail }) {
  const d = useDisplay();
  const positions = detail.agent.open_positions;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Posizioni aperte</CardTitle>
        <CardDescription>
          {positions.length} aperte · {d.money(detail.agent.invested_usd ?? 0)} investiti
        </CardDescription>
      </CardHeader>
      <CardContent>
        {positions.length === 0 ? (
          <p className="text-muted-foreground py-6 text-center text-sm">
            Nessuna posizione aperta
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Simbolo</TableHead>
                <TableHead>Direzione</TableHead>
                <TableHead className="text-right">Importo</TableHead>
                <TableHead className="text-right">Prezzo di ingresso</TableHead>
                <TableHead>Aperta il</TableHead>
                <TableHead>Motivazione</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {positions.map((p) => (
                <TableRow key={p.id}>
                  <TableCell className="font-mono font-medium">{p.symbol}</TableCell>
                  <TableCell>
                    <DirectionStamp direction={p.direction} />
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {d.money(p.amount_usd)}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {d.money(p.entry_price)}
                  </TableCell>
                  <TableCell className="font-mono text-xs whitespace-nowrap">
                    {d.dateTime(p.opened_at)}
                  </TableCell>
                  <TableCell
                    className="text-muted-foreground max-w-64 truncate text-xs"
                    title={p.open_reason}
                  >
                    {p.open_reason}
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

function TradesCard({ detail }: { detail: AgentDetail }) {
  const d = useDisplay();
  const trades = detail.trades;
  // Il registro tiene tutto: qui arriva solo la coda più recente, quindi il
  // conteggio va detto per intero quando la lista è troncata.
  const total = detail.metrics.n_trades;
  const shown = trades.length < total ? `${trades.length} di ${total}` : String(total);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Tutti i trade simulati</CardTitle>
        <CardDescription>{shown} trade chiusi, dal più recente</CardDescription>
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
                <TableHead>Simbolo</TableHead>
                <TableHead>Direzione</TableHead>
                <TableHead className="text-right">Importo</TableHead>
                <TableHead className="text-right">Ingresso → Uscita</TableHead>
                <TableHead className="text-right">PnL</TableHead>
                <TableHead className="text-right">Durata</TableHead>
                <TableHead>Chiusura</TableHead>
                <TableHead>Motivo chiusura</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {trades.map((t) => (
                <TableRow key={t.id}>
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
                  <TableCell className="text-right font-mono text-xs whitespace-nowrap tabular-nums">
                    {d.money(t.entry_price)} → {d.money(t.close_price)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-mono tabular-nums ${pnlClass(t.pnl_usd)}`}
                  >
                    {d.moneySigned(t.pnl_usd)}
                    {t.return_pct != null && (
                      <span className="ml-1.5 text-xs">
                        ({fmtPctSigned(t.return_pct)})
                      </span>
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

export default function AgentDetailPage() {
  const params = useParams<{ agentId: string }>();
  const agentId = params?.agentId ?? "";
  const { data, isLoading, error } = useArenaAgent(agentId || null);
  const { data: overview } = useArena();
  const d = useDisplay();

  // Il rivale è l'altro agente vivo dell'arena: se c'è, il confronto ha senso.
  const rival = (overview?.agents ?? []).find((a) => a.id !== agentId);

  const backButton = (
    <Button variant="outline" size="sm" asChild>
      <Link href="/training">
        <ArrowLeftIcon /> Allenamento
      </Link>
    </Button>
  );

  if (isLoading) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader
          eyebrow="Arena evolutiva"
          title="Agente"
          description="Caricamento della scheda in corso…"
          actions={backButton}
        />
        <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <CardSkeleton key={i} className="h-28 w-full" />
          ))}
        </div>
        <CardSkeleton className="h-80 w-full" />
        <Card>
          <CardContent className="p-4">
            <TableSkeleton rows={6} />
          </CardContent>
        </Card>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader
          eyebrow="Arena evolutiva"
          title="Agente"
          description="La scheda richiesta non è disponibile."
          actions={backButton}
        />
        <ErrorState error={error} title="Agente non trovato" />
      </div>
    );
  }

  const { agent, equity, metrics } = data;

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        eyebrow="Arena evolutiva"
        title={agent.name}
        description={
          <>
            Generazione {agent.generation} · mese {agent.month}
            {agent.born_at ? ` · nato il ${d.date(agent.born_at)}` : ""}
            <span className="mt-2 flex flex-wrap items-center gap-1.5">
              <AgentStatusStamp agent={agent} />
              {agent.death_reason && (
                <span className="text-muted-foreground text-xs">
                  {agent.death_reason}
                </span>
              )}
            </span>
          </>
        }
        actions={
          <div className="flex items-center gap-2">
            {backButton}
            {rival && (
              <Button size="sm" asChild>
                <Link href="/training/confronto">
                  <SwordsIcon /> Confronta
                </Link>
              </Button>
            )}
          </div>
        }
      />

      <SummaryStrip metrics={metrics} />

      <Card>
        <CardHeader>
          <CardTitle>Curva equity</CardTitle>
          <CardDescription>
            Valore del conto simulato rilevato a ogni ciclo di allenamento
          </CardDescription>
        </CardHeader>
        <CardContent>
          {equity.length >= 2 ? (
            <AgentEquityChart points={equity} />
          ) : (
            <p className="text-muted-foreground py-16 text-center text-sm">
              Servono almeno due rilevazioni per disegnare la curva: torna dopo il
              prossimo ciclo.
            </p>
          )}
        </CardContent>
      </Card>

      <PerformanceCard metrics={metrics} />

      <div className="grid gap-4 xl:grid-cols-2">
        <DirectionCard metrics={metrics} />
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <DnaIcon className="text-primary size-4" /> Strategia e DNA
            </CardTitle>
            <CardDescription>
              I parametri con cui l&apos;agente è nato, e quello che ha imparato
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm leading-5">{String(agent.dna.strategy)}</p>
            <DnaGrid agent={agent} />
            {agent.memory && (
              <details className="group">
                <summary className="text-muted-foreground hover:text-foreground cursor-pointer font-mono text-[11px] tracking-[0.08em] uppercase">
                  Memoria dell&apos;agente
                </summary>
                <pre className="text-muted-foreground mt-2 max-h-64 overflow-auto text-xs whitespace-pre-wrap">
                  {agent.memory}
                </pre>
              </details>
            )}
          </CardContent>
        </Card>
      </div>

      <PositionsCard detail={data} />
      <TradesCard detail={data} />
    </div>
  );
}

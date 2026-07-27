"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRightIcon, OctagonXIcon, ZapIcon, ZapOffIcon } from "lucide-react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
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
import { EquityChart } from "@/components/charts/equity-chart";
import {
  MobileField,
  MobileFields,
  MobileItem,
  MobileItemHeader,
  MobileList,
} from "@/components/mobile-list";
import { SectorDonut } from "@/components/charts/sector-donut";
import { ExecutionStatusBadge, SideBadge } from "@/components/status-badges";
import { LiveBadge } from "@/components/site-header";
import { PageHeader } from "@/components/page-header";
import { Stamp } from "@/components/stamp";
import { DateRangeFilter, lastDaysRange } from "@/components/date-range-filter";
import { CardSkeleton, ErrorState, TableSkeleton } from "@/components/query-states";
import {
  useEquityCurve,
  useBacktestSummary,
  useExecutions,
  useKillSwitch,
  useLiveToggle,
  useNews,
  usePortfolio,
  useStatus,
} from "@/lib/queries";
import { pnlClass } from "@/lib/format";
import { useDisplay } from "@/lib/money";
import type { DateRangeValue } from "@/lib/types";

function StatusCard() {
  const { data: status, isLoading, error } = useStatus();
  const d = useDisplay();

  if (isLoading) return <CardSkeleton className="h-48 w-full" />;
  if (error || !status)
    return <ErrorState error={error} title="Stato non disponibile" />;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Stato del sistema</CardTitle>
        <CardDescription>Modalità, campione e freni d&apos;emergenza</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="flex flex-wrap items-center gap-1.5">
          <LiveBadge liveEnabled={status.arena.live_enabled} />
          {status.market_open ? (
            <Stamp tone="accent">Mercato aperto</Stamp>
          ) : (
            <Stamp tone="neutral">Mercato chiuso</Stamp>
          )}
          {status.arena.paused && <Stamp tone="caution">Training in pausa</Stamp>}
        </div>
        <div className="grid gap-0">
          <div className="border-border/60 flex items-center justify-between border-b py-2">
            <span className="text-muted-foreground">Campione live</span>
            {status.champion ? (
              <Link
                href="/training"
                className="hover:text-primary font-mono text-[13px] font-medium transition-colors"
              >
                {status.champion.name} · gen {status.champion.generation}
              </Link>
            ) : (
              <span className="text-muted-foreground font-mono text-[13px]">
                nessuno: serve vincere un mese di allenamento
              </span>
            )}
          </div>
          <div className="border-border/60 flex items-center justify-between border-b py-2">
            <span className="text-muted-foreground">Kill switch</span>
            {status.kill_switch_active ? (
              <Stamp tone="solid-danger">Attivo</Stamp>
            ) : (
              <Stamp tone="neutral">Inattivo</Stamp>
            )}
          </div>
          <div className="border-border/60 flex items-center justify-between border-b py-2">
            <span className="text-muted-foreground">Circuit breaker</span>
            {status.circuit_breaker.tripped ? (
              <Stamp tone="solid-caution">Scattato</Stamp>
            ) : (
              <Stamp tone="approved">OK</Stamp>
            )}
          </div>
          {status.circuit_breaker.tripped && (
            <p className="text-muted-foreground py-2 text-xs">
              {status.circuit_breaker.reason ?? "Motivo non specificato"}
              {status.circuit_breaker.until
                ? ` — fino a ${d.dateTime(status.circuit_breaker.until)} ${d.tzLabel}`
                : ""}
            </p>
          )}
          <div className="flex items-center justify-between py-2">
            <span className="text-muted-foreground">Prossimo ciclo</span>
            <span className="font-mono text-[13px] tabular-nums">
              {status.next_cycle_at
                ? `${d.dateTime(status.next_cycle_at)} ${d.tzLabel}`
                : "non schedulato"}
            </span>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function PortfolioOverviewCard() {
  const { data, isLoading, error } = usePortfolio();
  const d = useDisplay();
  if (isLoading) return <CardSkeleton className="h-80 w-full" />;
  if (error || !data) return <ErrorState error={error} title="Portafoglio non disponibile" />;

  const unrealized = data.positions.reduce((sum, position) => sum + (position.unrealized_pnl_usd ?? 0), 0);
  return (
    <Card>
      <CardHeader><CardTitle>Conto eToro (live)</CardTitle><CardDescription>{data.positions.length} posizioni · {d.money(data.exposure_usd)} esposti</CardDescription></CardHeader>
      <CardContent className="grid gap-6 sm:grid-cols-[minmax(0,1fr)_180px] sm:items-center">
        <div className="grid grid-cols-2 gap-5">
          <div><p className="font-mono text-[10px] tracking-[0.14em] text-muted-foreground uppercase">Disponibile eToro</p><p className="mt-2 font-mono text-xl font-semibold tabular-nums">{d.money(data.cash_usd)}</p></div>
          <div><p className="font-mono text-[10px] tracking-[0.14em] text-muted-foreground uppercase">PnL aperto</p><p className={`mt-2 font-mono text-xl font-semibold tabular-nums ${pnlClass(unrealized)}`}>{d.money(unrealized)}</p></div>
          <div><p className="font-mono text-[10px] tracking-[0.14em] text-muted-foreground uppercase">Equity</p><p className="mt-2 font-mono text-xl font-semibold tabular-nums">{d.money(data.equity_usd)}</p></div>
          <div><p className="font-mono text-[10px] tracking-[0.14em] text-muted-foreground uppercase">Max per trade</p><p className="mt-2 font-mono text-xl font-semibold tabular-nums">{d.money(data.max_trade_amount_usd)}</p></div>
        </div>
        <SectorDonut positions={data.positions} />
      </CardContent>
    </Card>
  );
}

function NewsCard() {
  const { data, isLoading, error } = useNews();
  const d = useDisplay();
  return (
    <Card>
      <CardHeader><CardTitle>Aggiornamenti di mercato</CardTitle><CardDescription>{data?.updated_at ? `Aggiornato ${d.dateTime(data.updated_at)}` : "Dai feed RSS personali"}</CardDescription></CardHeader>
      <CardContent>
        {isLoading ? <TableSkeleton rows={5} /> : error ? <ErrorState error={error} /> : (data?.items.length ?? 0) === 0 ? (
          <p className="text-muted-foreground py-10 text-center text-sm">Nessuna notizia disponibile — aggiorna i feed dalla Knowledge Base.</p>
        ) : <div className="flex flex-col divide-y">{data!.items.slice(0, 6).map((item, index) => <article key={`${item.source}-${index}`} className="py-3 first:pt-0"><div className="flex items-center justify-between gap-3"><span className="font-mono text-[10px] tracking-[0.1em] text-muted-foreground uppercase">{item.source}</span><span className="font-mono text-[10px] text-muted-foreground">{item.published_at || "—"}</span></div><p className="mt-1 line-clamp-2 text-sm leading-5">{item.text}</p></article>)}</div>}
      </CardContent>
    </Card>
  );
}

function MetricCard({ label, value, hint, tone }: { label: string; value: string; hint: string; tone?: string }) {
  return <Card><CardContent className="p-4"><p className="font-mono text-[10px] tracking-[0.14em] text-muted-foreground uppercase">{label}</p><p className={`mt-2 font-mono text-xl font-semibold tabular-nums sm:text-2xl ${tone ?? ""}`}>{value}</p><p className="text-muted-foreground mt-1 text-[11px]">{hint}</p></CardContent></Card>;
}

function MetricsStrip({ range }: { range: DateRangeValue }) {
  const { data, isLoading, error } = useBacktestSummary(range);
  const d = useDisplay();
  if (isLoading) return <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">{Array.from({ length: 8 }, (_, i) => <CardSkeleton key={i} className="h-28 w-full" />)}</div>;
  if (error || !data) return <ErrorState error={error} title="Indicatori non disponibili" />;
  const m = data.metrics;
  const n = (v: number | null, digits = 2) => v == null ? "n/d" : v.toFixed(digits);
  return <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
    <MetricCard label="Sharpe ratio" value={n(m.sharpe)} hint={`${data.n_days} giorni nel campione`} />
    <MetricCard label="Max drawdown" value={m.max_drawdown_pct == null ? "n/d" : `${m.max_drawdown_pct.toFixed(2)}%`} hint="Perdita dal picco massimo" tone="text-negative" />
    <MetricCard label="Win rate" value={m.win_rate_pct == null ? "n/d" : `${m.win_rate_pct.toFixed(1)}%`} hint={`${data.n_closed_trades} trade chiusi`} />
    <MetricCard label="Profit factor" value={n(m.profit_factor)} hint="Profitti lordi / perdite lorde" />
    <MetricCard label="Miglior trade" value={d.moneySigned(m.max_win_usd)} hint="Massimo guadagno realizzato" tone={pnlClass(m.max_win_usd)} />
    <MetricCard label="Peggior trade" value={d.moneySigned(m.max_loss_usd)} hint="Massima perdita realizzata" tone={pnlClass(m.max_loss_usd)} />
    <MetricCard label="Std guadagni" value={d.money(m.std_win_usd)} hint="Dispersione dei trade positivi" />
    <MetricCard label="Std perdite" value={d.money(m.std_loss_usd)} hint="Dispersione dei trade negativi" />
  </div>;
}

function EquityCard({ range, onRangeChange }: { range: DateRangeValue; onRangeChange: (range: DateRangeValue) => void }) {
  const { data, isLoading, error } = useEquityCurve("spy", range);
  return <Card><CardHeader><CardTitle>Andamento equity live</CardTitle><CardDescription>Il grafico segue il periodo pagina, poi può essere regolato indipendentemente.</CardDescription><CardAction><DateRangeFilter value={range} onChange={onRangeChange} label="Grafico" /></CardAction></CardHeader><CardContent>
    {isLoading ? <CardSkeleton className="h-80 w-full" /> : error ? <ErrorState error={error} /> : (data?.points.length ?? 0) < 2 ? <p className="text-muted-foreground py-28 text-center text-sm">La serie equity si costruisce a ogni giornata live: dati insufficienti nel periodo selezionato.</p> : <EquityChart points={data!.points} showBenchmarks={false} />}
  </CardContent></Card>;
}

function ActionsCard() {
  const { data: status } = useStatus();
  const liveToggle = useLiveToggle();
  const killSwitch = useKillSwitch();
  const killActive = status?.kill_switch_active ?? false;
  const liveEnabled = status?.arena.live_enabled ?? false;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Controlli live</CardTitle>
        <CardDescription>
          Attivazione del denaro reale e arresto d&apos;emergenza
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {liveEnabled ? (
          <Button
            variant="outline"
            className="justify-start"
            disabled={liveToggle.isPending}
            onClick={() => liveToggle.mutate(false)}
          >
            <ZapOffIcon className="size-4" /> Spegni trading live
          </Button>
        ) : (
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button
                disabled={liveToggle.isPending}
                className="justify-start"
              >
                <ZapIcon className="size-4" /> Vai live col campione
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Attivare il trading live?</AlertDialogTitle>
                <AlertDialogDescription>
                  {status?.champion
                    ? `${status.champion.name} (generazione ${status.champion.generation}), vincitore dell'ultimo mese di allenamento, inizierà a fare day trading con denaro REALE sul conto eToro. L'allenamento continua in parallelo sui conti simulati.`
                    : "Non c'è ancora un campione: il live si attiva solo dopo che un agente ha vinto almeno un mese di allenamento."}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Annulla</AlertDialogCancel>
                <AlertDialogAction
                  className="bg-negative text-white hover:bg-negative/90 dark:text-[#141517]"
                  onClick={() => liveToggle.mutate(true)}
                >
                  Vai live
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        )}

        {killActive ? (
          <Button
            variant="outline"
            className="justify-start"
            disabled={killSwitch.isPending}
            onClick={() => killSwitch.mutate(false)}
          >
            <OctagonXIcon className="size-4" /> Disattiva kill switch
          </Button>
        ) : (
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button
                variant="destructive"
                className="justify-start"
                disabled={killSwitch.isPending}
              >
                <OctagonXIcon className="size-4" /> KILL SWITCH
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Attivare il kill switch?</AlertDialogTitle>
                <AlertDialogDescription>
                  Nessun ordine verrà più inviato a eToro finché il kill switch
                  resta attivo: viene controllato prima di ogni singolo ordine.
                  L&apos;azione è reversibile da questa pagina.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Annulla</AlertDialogCancel>
                <AlertDialogAction
                  className="bg-negative text-white hover:bg-negative/90 dark:text-[#141517]"
                  onClick={() => killSwitch.mutate(true)}
                >
                  Attiva kill switch
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        )}
        <p className="text-muted-foreground pt-1 text-xs">
          L&apos;allenamento degli agenti si gestisce dalla pagina{" "}
          <Link href="/training" className="text-primary hover:underline">
            Allenamento <ArrowRightIcon className="inline size-3" />
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}

function RecentExecutionsCard() {
  const { data, isLoading, error } = useExecutions(8);
  const d = useDisplay();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Ultime esecuzioni live</CardTitle>
        <CardDescription>Ordini reali più recenti del campione</CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={5} />
        ) : error ? (
          <ErrorState error={error} />
        ) : (data?.executions.length ?? 0) === 0 ? (
          <p className="text-muted-foreground py-6 text-center text-sm">
            Nessuna esecuzione registrata — il registro si popola quando il live è attivo
          </p>
        ) : (
          <>
            <div className="max-md:hidden">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Data</TableHead>
                    <TableHead>Simbolo</TableHead>
                    <TableHead>Lato</TableHead>
                    <TableHead className="text-right">Importo</TableHead>
                    <TableHead>Esito</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data!.executions.map((ex) => (
                    <TableRow key={ex.id}>
                      <TableCell className="font-mono text-[13px] tabular-nums">
                        {d.dateTime(ex.created_at)}
                      </TableCell>
                      <TableCell className="font-mono font-medium">
                        {ex.symbol}
                      </TableCell>
                      <TableCell>
                        <SideBadge side={ex.side} />
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {d.money(ex.amount_usd)}
                      </TableCell>
                      <TableCell>
                        <ExecutionStatusBadge status={ex.status} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            <MobileList>
              {data!.executions.map((ex) => (
                <MobileItem key={ex.id}>
                  <MobileItemHeader>
                    <span className="font-mono text-sm font-medium">
                      {ex.symbol}
                    </span>
                    <span className="flex items-center gap-1.5">
                      <SideBadge side={ex.side} />
                      <ExecutionStatusBadge status={ex.status} />
                    </span>
                  </MobileItemHeader>
                  <MobileFields>
                    <MobileField label="Importo">
                      <span className="font-mono tabular-nums">
                        {d.money(ex.amount_usd)}
                      </span>
                    </MobileField>
                    <MobileField label="Data">
                      <span className="font-mono text-xs tabular-nums">
                        {d.dateTime(ex.created_at)}
                      </span>
                    </MobileField>
                  </MobileFields>
                </MobileItem>
              ))}
            </MobileList>
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default function DashboardPage() {
  const [pageRange, setPageRange] = React.useState(() => lastDaysRange(90));
  const [chartRange, setChartRange] = React.useState(() => lastDaysRange(90));
  const updatePageRange = (range: DateRangeValue) => {
    setPageRange(range);
    setChartRange(range);
  };
  return (
    <div className="flex flex-col gap-6">
      <PageHeader eyebrow="Conto reale" title="Dashboard live" description="Il campione dell'arena opera qui: equity, posizioni reali, esecuzioni e controlli." actions={<DateRangeFilter value={pageRange} onChange={updatePageRange} label="Pagina" />} />
      <MetricsStrip range={pageRange} />
      <EquityCard range={chartRange} onRangeChange={setChartRange} />
      <div className="grid gap-4 lg:grid-cols-2">
        <StatusCard />
        <ActionsCard />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <PortfolioOverviewCard />
        <NewsCard />
      </div>
      <RecentExecutionsCard />
    </div>
  );
}

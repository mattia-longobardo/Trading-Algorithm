"use client";

import * as React from "react";
import Link from "next/link";
import {
  DnaIcon,
  PauseIcon,
  PlayIcon,
  SwordsIcon,
  TrophyIcon,
  ZapIcon,
} from "lucide-react";

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
import { AgentStatusStamp, DnaGrid } from "@/components/arena";
import { EquitySparkline } from "@/components/charts/sparkline";
import { PageHeader } from "@/components/page-header";
import { Stamp } from "@/components/stamp";
import { CardSkeleton, ErrorState, TableSkeleton } from "@/components/query-states";
import {
  useArena,
  useArenaAgent,
  useArenaEvents,
  useArenaPause,
  useTriggerCycle,
} from "@/lib/queries";
import { pnlClass } from "@/lib/format";
import { useDisplay } from "@/lib/money";
import type { ArenaAgent, EquityPoint } from "@/lib/types";

function AgentCard({ agent }: { agent: ArenaAgent }) {
  const d = useDisplay();
  const { data: detail } = useArenaAgent(agent.id);
  const equityPoints: EquityPoint[] = (detail?.equity ?? []).map((p) => ({
    date: p.ts,
    equity_usd: p.equity_usd,
    spy_lump_sum_usd: null,
    spy_cash_flow_matched_usd: null,
  }));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <DnaIcon className="text-primary size-4" />
          <Link href={`/training/${agent.id}`} className="hover:underline">
            {agent.name}
          </Link>
          <AgentStatusStamp agent={agent} />
        </CardTitle>
        <CardDescription>
          Generazione {agent.generation} · mese {agent.month} · budget{" "}
          {d.money(agent.starting_capital_usd)}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-3 gap-4">
          <div>
            <p className="text-muted-foreground font-mono text-[10px] tracking-[0.14em] uppercase">
              PnL del mese
            </p>
            <p
              className={`mt-1 font-mono text-xl font-semibold tabular-nums ${pnlClass(agent.pnl_month_usd)}`}
            >
              {d.moneySigned(agent.pnl_month_usd)}
            </p>
          </div>
          <div>
            <p className="text-muted-foreground font-mono text-[10px] tracking-[0.14em] uppercase">
              Equity
            </p>
            <p className="mt-1 font-mono text-xl font-semibold tabular-nums">
              {d.money(agent.equity_usd)}
            </p>
          </div>
          <div>
            <p className="text-muted-foreground font-mono text-[10px] tracking-[0.14em] uppercase">
              Posizioni
            </p>
            <p className="mt-1 font-mono text-xl font-semibold tabular-nums">
              {agent.open_positions.length}
            </p>
          </div>
        </div>

        {equityPoints.length >= 2 && <EquitySparkline points={equityPoints} />}

        <DnaGrid agent={agent} />

        <div>
          <p className="text-muted-foreground font-mono text-[10px] tracking-[0.14em] uppercase">
            Strategia
          </p>
          <p className="mt-1 text-sm leading-5">{String(agent.dna.strategy)}</p>
        </div>

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

        {agent.open_positions.length > 0 && (
          <div>
            <p className="text-muted-foreground mb-1 font-mono text-[10px] tracking-[0.14em] uppercase">
              Posizioni aperte
            </p>
            <div className="flex flex-col divide-y text-sm">
              {agent.open_positions.map((p) => (
                <div key={p.id} className="flex items-center justify-between py-1.5">
                  <span className="font-mono font-medium">{p.symbol}</span>
                  <span className="text-muted-foreground line-clamp-1 flex-1 px-3 text-xs">
                    {p.open_reason}
                  </span>
                  <span className="font-mono tabular-nums">{d.money(p.amount_usd)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {(detail?.trades.length ?? 0) > 0 && (
          <div>
            <p className="text-muted-foreground mb-1 font-mono text-[10px] tracking-[0.14em] uppercase">
              Ultimi trade simulati
            </p>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Simbolo</TableHead>
                  <TableHead className="text-right">Importo</TableHead>
                  <TableHead className="text-right">PnL</TableHead>
                  <TableHead>Chiusura</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {detail!.trades.slice(0, 6).map((t) => (
                  <TableRow key={t.id}>
                    <TableCell className="font-mono font-medium">{t.symbol}</TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {d.money(t.amount_usd)}
                    </TableCell>
                    <TableCell
                      className={`text-right font-mono tabular-nums ${pnlClass(t.pnl_usd)}`}
                    >
                      {d.moneySigned(t.pnl_usd)}
                    </TableCell>
                    <TableCell className="text-muted-foreground max-w-48 truncate text-xs">
                      {t.close_reason}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}

        <div className="flex justify-end">
          <Button variant="ghost" size="sm" asChild>
            <Link href={`/training/${agent.id}`}>Dettagli →</Link>
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function OverviewStrip() {
  const { data, isLoading, error } = useArena();
  const d = useDisplay();
  if (isLoading)
    return (
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <CardSkeleton key={i} className="h-24 w-full" />
        ))}
      </div>
    );
  if (error || !data) return <ErrorState error={error} title="Arena non disponibile" />;
  const cells: [string, string, string][] = [
    ["Generazione", String(data.generation || "—"), `mese ${data.state.month ?? "—"}`],
    [
      "Giorni alla valutazione",
      String(data.days_to_evaluation),
      "a fine mese chi perde muore",
    ],
    [
      "Mercati",
      data.sessions?.length
        ? data.sessions
            .map(
              (s) =>
                `${s.name === "europe" ? "EU" : s.name === "usa" ? "USA" : s.name} ${s.open_now ? "aperto" : "chiuso"}`,
            )
            .join(" · ")
        : data.market_open
          ? "Aperto"
          : "Chiuso",
      data.next_cycle_at
        ? `prossimo ciclo ${d.dateTime(data.next_cycle_at)}`
        : "nessun ciclo schedulato",
    ],
    [
      "Stato allenamento",
      data.state.paused ? "In pausa" : "Attivo",
      data.state.live_enabled ? "live acceso in parallelo" : "solo simulazione",
    ],
  ];
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      {cells.map(([label, value, hint]) => (
        <Card key={label}>
          <CardContent className="p-4">
            <p className="text-muted-foreground font-mono text-[10px] tracking-[0.14em] uppercase">
              {label}
            </p>
            <p className="mt-2 font-mono text-xl font-semibold tabular-nums">{value}</p>
            <p className="text-muted-foreground mt-1 text-[11px]">{hint}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function LineageCard() {
  const { data, isLoading, error } = useArena();
  const d = useDisplay();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Albero genealogico</CardTitle>
        <CardDescription>
          Ogni mese: il migliore sopravvive (solo se in profitto), viene clonato e
          mutato; l&apos;altro muore
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={4} />
        ) : error || !data ? (
          <ErrorState error={error} />
        ) : data.lineage.length === 0 ? (
          <p className="text-muted-foreground py-6 text-center text-sm">
            La prima generazione nasce al primo ciclo di allenamento
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Agente</TableHead>
                <TableHead>Gen</TableHead>
                <TableHead>Mese</TableHead>
                <TableHead className="text-right">PnL</TableHead>
                <TableHead>Esito</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.lineage.map((agent) => (
                <TableRow key={agent.id}>
                  <TableCell className="font-mono font-medium">
                    <Link href={`/training/${agent.id}`} className="hover:underline">
                      {agent.name}
                    </Link>
                  </TableCell>
                  <TableCell className="font-mono tabular-nums">
                    {agent.generation}
                  </TableCell>
                  <TableCell className="font-mono tabular-nums">{agent.month}</TableCell>
                  <TableCell
                    className={`text-right font-mono tabular-nums ${pnlClass(agent.pnl_month_usd)}`}
                  >
                    {d.moneySigned(agent.pnl_month_usd)}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1.5">
                      <AgentStatusStamp agent={agent} />
                      {agent.death_reason && (
                        <span className="text-muted-foreground max-w-56 truncate text-xs">
                          {agent.death_reason}
                        </span>
                      )}
                    </div>
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

const EVENT_LABELS: Record<string, string> = {
  birth: "Nascita",
  death: "Morte",
  evolution: "Evoluzione",
  pause: "Pausa",
  resume: "Ripresa",
  live_on: "Live acceso",
  live_off: "Live spento",
  error: "Errore",
};

function EventsCard() {
  const { data, isLoading, error } = useArenaEvents(40);
  const d = useDisplay();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Cronologia dell&apos;arena</CardTitle>
        <CardDescription>Nascite, morti, evoluzioni e interventi manuali</CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={5} />
        ) : error || !data ? (
          <ErrorState error={error} />
        ) : data.events.length === 0 ? (
          <p className="text-muted-foreground py-6 text-center text-sm">
            Ancora nessun evento registrato
          </p>
        ) : (
          <div className="flex flex-col divide-y">
            {data.events.map((event) => (
              <div key={event.id} className="flex items-start gap-3 py-2.5 first:pt-0">
                <Stamp
                  tone={
                    event.event === "error"
                      ? "rejected"
                      : event.event === "evolution"
                        ? "approved"
                        : "neutral"
                  }
                >
                  {EVENT_LABELS[event.event] ?? event.event}
                </Stamp>
                <div className="min-w-0 flex-1">
                  <p className="text-muted-foreground truncate text-xs">
                    {JSON.stringify(event.payload)}
                  </p>
                </div>
                <span className="text-muted-foreground shrink-0 font-mono text-[11px] tabular-nums">
                  {d.dateTime(event.ts)}
                </span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function TrainingPage() {
  const { data } = useArena();
  const pause = useArenaPause();
  const cycle = useTriggerCycle();
  const paused = data?.state.paused ?? false;

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        eyebrow="Arena evolutiva"
        title="Allenamento"
        description="Due versioni dello stesso agente si sfidano in day trading simulato con dati eToro reali: la loro vita dipende dal profitto."
        actions={
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" asChild>
              <Link href="/training/confronto">
                <SwordsIcon className="size-4" /> Testa a testa
              </Link>
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={pause.isPending}
              onClick={() => pause.mutate(!paused)}
            >
              {paused ? (
                <>
                  <PlayIcon className="size-4" /> Riprendi
                </>
              ) : (
                <>
                  <PauseIcon className="size-4" /> Pausa
                </>
              )}
            </Button>
            <Button
              size="sm"
              disabled={cycle.isPending || paused}
              onClick={() => cycle.mutate()}
            >
              <ZapIcon className="size-4" /> Ciclo ora
            </Button>
          </div>
        }
      />
      <OverviewStrip />
      <div className="grid gap-4 xl:grid-cols-2">
        {(data?.agents ?? []).map((agent) => (
          <AgentCard key={agent.id} agent={agent} />
        ))}
        {data && data.agents.length === 0 && (
          <Card className="xl:col-span-2">
            <CardContent className="text-muted-foreground py-16 text-center text-sm">
              Nessun agente vivo: la prima generazione nasce automaticamente al
              prossimo ciclo (o premi «Ciclo ora»).
            </CardContent>
          </Card>
        )}
      </div>
      {data?.champion && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <TrophyIcon className="text-positive size-4" /> Campione live:{" "}
              {data.champion.name}
            </CardTitle>
            <CardDescription>
              Vincitore del mese {data.champion.month} (generazione{" "}
              {data.champion.generation}): il suo DNA guida il trading live finché un
              nuovo campione non lo sostituisce.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DnaGrid agent={data.champion} />
          </CardContent>
        </Card>
      )}
      <div className="grid gap-4 xl:grid-cols-2">
        <LineageCard />
        <EventsCard />
      </div>
    </div>
  );
}

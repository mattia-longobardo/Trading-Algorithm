"use client";

import { TriangleAlertIcon } from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { DirectionStamp } from "@/components/arena";
import { SectorDonut } from "@/components/charts/sector-donut";
import { ResponsiveTable, type ResponsiveColumn } from "@/components/responsive-table";
import { PageHeader } from "@/components/page-header";
import { CardSkeleton, ErrorState, TableSkeleton } from "@/components/query-states";
import { usePortfolio } from "@/lib/queries";
import { fmtPctSigned, pnlClass } from "@/lib/format";
import { useDisplay } from "@/lib/money";
import type { Position } from "@/lib/types";

function SummaryTiles({
  cash,
  equity,
  exposure,
}: {
  cash: number;
  equity: number;
  exposure: number;
}) {
  const d = useDisplay();
  const tiles = [
    { label: "Equity bot", value: d.money(equity) },
    { label: "Liquidità", value: d.money(cash) },
    { label: "Esposizione", value: d.money(exposure) },
    {
      label: "Esposizione / equity",
      value: equity > 0 ? `${((exposure / equity) * 100).toFixed(1)}%` : "n/d",
    },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
      {tiles.map((t) => (
        <Card key={t.label} size="sm">
          <CardContent className="space-y-1">
            <p className="text-muted-foreground font-mono text-[10px] tracking-[0.14em] uppercase">
              {t.label}
            </p>
            <p className="font-mono text-xl font-semibold tracking-tight tabular-nums sm:text-2xl">
              {t.value}
            </p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export default function PortfolioPage() {
  const { data, isLoading, error } = usePortfolio();
  const d = useDisplay();

  const columns: ResponsiveColumn<Position>[] = [
    {
      key: "symbol",
      header: "Simbolo",
      className: "font-mono font-medium",
      cell: (p) => p.symbol,
    },
    {
      key: "direction",
      header: "Direzione",
      cell: (p) => <DirectionStamp direction={p.direction} />,
    },
    {
      key: "sector",
      header: "Settore",
      className: "text-muted-foreground",
      cell: (p) => p.sector ?? "n/d",
    },
    {
      key: "amount",
      header: "Importo",
      headerClassName: "text-right",
      className: "text-right font-mono tabular-nums",
      cell: (p) => d.money(p.amount_usd),
      mobile: {
        label: "Importo",
        order: 1,
        render: (p) => <span className="font-mono tabular-nums">{d.money(p.amount_usd)}</span>,
      },
    },
    {
      key: "entry",
      header: "Entry",
      headerClassName: "text-right",
      className: "text-right font-mono tabular-nums",
      cell: (p) => d.money(p.entry_price),
      mobile: {
        label: "Entry",
        order: 3,
        render: (p) => <span className="font-mono tabular-nums">{d.money(p.entry_price)}</span>,
      },
    },
    {
      key: "current",
      header: "Attuale",
      headerClassName: "text-right",
      className: "text-right font-mono tabular-nums",
      cell: (p) => d.money(p.current_price),
      mobile: {
        label: "Attuale",
        order: 4,
        render: (p) => <span className="font-mono tabular-nums">{d.money(p.current_price)}</span>,
      },
    },
    {
      key: "pnl",
      header: "PnL",
      headerClassName: "text-right",
      className: "text-right font-mono tabular-nums",
      cell: (p) => (
        <span className={pnlClass(p.unrealized_pnl_usd)}>{d.moneySigned(p.unrealized_pnl_usd)}</span>
      ),
    },
    {
      key: "pnl_pct",
      header: "PnL %",
      headerClassName: "text-right",
      className: "text-right font-mono tabular-nums",
      cell: (p) => (
        <span className={pnlClass(p.unrealized_pnl_pct)}>{fmtPctSigned(p.unrealized_pnl_pct)}</span>
      ),
    },
    {
      key: "opened",
      header: "Apertura",
      className: "font-mono text-[13px] whitespace-nowrap tabular-nums",
      cell: (p) => d.date(p.opened_at),
      mobile: {
        label: "Apertura",
        order: 2,
        render: (p) => <span className="font-mono text-xs tabular-nums">{d.date(p.opened_at)}</span>,
      },
    },
  ];

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Registry bot"
        title="Portafoglio bot"
        description="Solo le posizioni aperte dal bot: i trade manuali sul conto eToro sono esclusi da tutte le metriche."
      />

      {isLoading ? (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <CardSkeleton key={i} className="h-24 w-full" />
            ))}
          </div>
          <TableSkeleton rows={6} />
        </>
      ) : error || !data ? (
        <ErrorState error={error} />
      ) : (
        <>
          <SummaryTiles
            cash={data.cash_usd}
            equity={data.equity_usd}
            exposure={data.exposure_usd}
          />

          {data.anomalies.length > 0 && (
            <Card className="border-caution/50">
              <CardHeader>
                <CardTitle className="text-caution flex items-center gap-2">
                  <TriangleAlertIcon className="size-4" />
                  Anomalie di riconciliazione
                </CardTitle>
                <CardDescription>
                  Posizioni del registry bot non più coerenti con il conto eToro
                </CardDescription>
              </CardHeader>
              <CardContent>
                <ul className="space-y-2 text-sm">
                  {data.anomalies.map((a, i) => (
                    <li key={i} className="flex flex-wrap items-baseline gap-2">
                      <span className="font-mono font-medium">{a.symbol}</span>
                      <span className="text-muted-foreground">{a.detail}</span>
                      <span className="text-muted-foreground ml-auto font-mono text-xs tabular-nums">
                        {d.dateTime(a.detected_at)}
                      </span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}

          <div className="grid gap-4 xl:grid-cols-3">
            <Card className="xl:col-span-2">
              <CardHeader>
                <CardTitle>Posizioni aperte</CardTitle>
                <CardDescription>
                  {data.positions.length} posizioni nel registry bot
                </CardDescription>
              </CardHeader>
              <CardContent>
                {data.positions.length === 0 ? (
                  <p className="text-muted-foreground py-10 text-center text-sm">
                    Nessuna posizione aperta dal bot — le aperture arrivano con
                    le run live
                  </p>
                ) : (
                  <ResponsiveTable
                    rows={data.positions}
                    rowKey={(p) => String(p.etoro_position_id)}
                    columns={columns}
                    mobileHeader={(p) => (
                      <>
                        <span className="flex min-w-0 items-baseline gap-2">
                          <span className="font-mono text-sm font-medium">{p.symbol}</span>
                          <DirectionStamp direction={p.direction} />
                          <span className="text-muted-foreground truncate text-xs">{p.sector ?? "n/d"}</span>
                        </span>
                        <span className={`font-mono text-sm font-medium tabular-nums ${pnlClass(p.unrealized_pnl_usd)}`}>
                          {d.moneySigned(p.unrealized_pnl_usd)}
                          <span className="ml-1.5 text-xs">{fmtPctSigned(p.unrealized_pnl_pct)}</span>
                        </span>
                      </>
                    )}
                  />
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Allocazione per settore</CardTitle>
                <CardDescription>
                  Valore corrente delle posizioni bot
                </CardDescription>
              </CardHeader>
              <CardContent>
                <SectorDonut positions={data.positions} />
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

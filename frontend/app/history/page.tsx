"use client";

import * as React from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/page-header";
import { ErrorState, TableSkeleton } from "@/components/query-states";
import { Stamp } from "@/components/stamp";
import { DateRangeFilter, lastDaysRange } from "@/components/date-range-filter";
import { MultiStatusFilter } from "@/components/multi-status-filter";
import { ResponsiveTable, type ResponsiveColumn } from "@/components/responsive-table";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { useTradeHistory } from "@/lib/queries";
import { pnlClass } from "@/lib/format";
import { useDisplay } from "@/lib/money";

const statusTone = (status: string) =>
  status === "closed" || status === "open"
    ? ("approved" as const)
    : status === "failed" || status === "rejected"
      ? ("rejected" as const)
      : ("neutral" as const);

export default function HistoryPage() {
  const [range, setRange] = React.useState(() => lastDaysRange(90));
  const [statuses, setStatuses] = React.useState<string[]>([]);
  const [search, setSearch] = React.useState("");
  // la ricerca entra nella query key: senza pausa sarebbe una fetch per tasto
  const history = useTradeHistory(statuses, range, useDebouncedValue(search));
  const rows = history.data?.items ?? [];
  const d = useDisplay();

  const columns: ResponsiveColumn<(typeof rows)[number]>[] = [
    {
      key: "symbol",
      header: "Simbolo",
      className: "font-mono font-medium",
      cell: (item) => item.symbol,
    },
    {
      key: "status",
      header: "Stato",
      cell: (item) => <Stamp tone={statusTone(item.status)}>{item.status}</Stamp>,
    },
    {
      key: "side",
      header: "Lato",
      className: "font-mono text-xs uppercase",
      cell: (item) => item.side,
    },
    {
      key: "amount",
      header: "Importo",
      headerClassName: "text-right",
      className: "text-right font-mono tabular-nums",
      cell: (item) => d.money(item.amount_usd),
      mobile: {
        label: "Importo",
        render: (item) => <span className="font-mono tabular-nums">{d.money(item.amount_usd)}</span>,
      },
    },
    {
      key: "price",
      header: "Prezzo",
      headerClassName: "text-right",
      className: "text-right font-mono tabular-nums",
      cell: (item) => d.money(item.price),
      mobile: {
        label: "Prezzo",
        render: (item) => <span className="font-mono tabular-nums">{d.money(item.price)}</span>,
      },
    },
    {
      key: "pnl",
      header: "PnL",
      headerClassName: "text-right",
      className: "text-right font-mono tabular-nums",
      cell: (item) => (
        <span className={pnlClass(item.pnl_usd)}>{d.moneySigned(item.pnl_usd)}</span>
      ),
    },
    {
      key: "opened",
      header: "Apertura",
      className: "font-mono text-xs whitespace-nowrap",
      cell: (item) => d.dateTime(item.opened_at),
      mobile: {
        label: "Apertura",
        render: (item) => (
          <span className="font-mono text-xs tabular-nums">{d.dateTime(item.opened_at)}</span>
        ),
      },
    },
    {
      key: "closed",
      header: "Chiusura",
      className: "font-mono text-xs whitespace-nowrap",
      cell: (item) => (item.closed_at ? d.dateTime(item.closed_at) : "—"),
      mobile: {
        label: "Chiusura",
        render: (item) => (
          <span className="font-mono text-xs tabular-nums">
            {item.closed_at ? d.dateTime(item.closed_at) : "—"}
          </span>
        ),
      },
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <PageHeader eyebrow="Archivio" title="Storico" description="Tutti i trade aperti, chiusi, annullati, falliti e respinti in ordine cronologico." actions={<DateRangeFilter value={range} onChange={setRange} />} />
      <Card>
        <CardHeader><CardTitle>Storico trade</CardTitle><CardDescription>{rows.length} risultati</CardDescription></CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_220px]">
            <Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Cerca simbolo" aria-label="Cerca nello storico" />
            <MultiStatusFilter value={statuses} onChange={setStatuses} options={[
              { value: "open", label: "Aperti" }, { value: "closed", label: "Chiusi" },
              { value: "cancelled", label: "Annullati" }, { value: "failed", label: "Falliti" },
              { value: "rejected", label: "Respinti" }, { value: "filled", label: "Eseguiti" },
            ]} />
          </div>
          {history.isLoading ? <TableSkeleton rows={10} /> : history.error ? <ErrorState error={history.error} /> : (
            <ResponsiveTable
              rows={rows}
              rowKey={(item) => item.id}
              columns={columns}
              mobileHeader={(item) => (
                <>
                  <span className="flex items-center gap-2">
                    <span className="font-mono text-sm font-medium">{item.symbol}</span>
                    <span className="text-muted-foreground font-mono text-xs uppercase">{item.side}</span>
                  </span>
                  <span className="flex items-center gap-2">
                    <span className={`font-mono text-sm font-medium tabular-nums ${pnlClass(item.pnl_usd)}`}>{d.moneySigned(item.pnl_usd)}</span>
                    <Stamp tone={statusTone(item.status)}>{item.status}</Stamp>
                  </span>
                </>
              )}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

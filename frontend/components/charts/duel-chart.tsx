"use client";

import * as React from "react";
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import type { AgentEquityPoint } from "@/lib/types";
import { useDisplay } from "@/lib/money";

export interface DuelSeries {
  name: string;
  points: AgentEquityPoint[];
}

/** Punto del grafico: un timestamp, l'equity dei due agenti dove esiste. */
interface DuelPoint {
  ts: string;
  a?: number;
  b?: number;
}

/**
 * Le due serie sono campionate a ogni ciclo, ma non necessariamente negli
 * stessi istanti: si uniscono per timestamp e i buchi restano vuoti, cuciti
 * poi da `connectNulls` invece di essere inventati con uno zero.
 */
function mergeSeries(series: DuelSeries[]): DuelPoint[] {
  const byTs = new Map<string, DuelPoint>();
  const keys: ("a" | "b")[] = ["a", "b"];
  series.slice(0, 2).forEach((s, i) => {
    const key = keys[i];
    for (const point of s.points) {
      const row = byTs.get(point.ts) ?? { ts: point.ts };
      row[key] = point.equity_usd;
      byTs.set(point.ts, row);
    }
  });
  return [...byTs.values()].sort(
    (x, y) => new Date(x.ts).getTime() - new Date(y.ts).getTime(),
  );
}

/**
 * Le curve equity dei due agenti sovrapposte: stesso capitale di partenza,
 * stesso mercato, quindi la distanza fra le due linee è tutto il confronto.
 */
export function DuelChart({ series }: { series: DuelSeries[] }) {
  const d = useDisplay();
  const data = React.useMemo(() => mergeSeries(series), [series]);
  const chartConfig = {
    a: { label: series[0]?.name ?? "Agente A", color: "var(--chart-1)" },
    b: { label: series[1]?.name ?? "Agente B", color: "var(--chart-2)" },
  } satisfies ChartConfig;

  return (
    <ChartContainer
      config={chartConfig}
      className="aspect-auto h-[240px] w-full sm:h-[320px] [&_.recharts-cartesian-axis-tick_text]:font-mono [&_.recharts-cartesian-axis-tick_text]:text-[11px]"
    >
      <LineChart data={data} margin={{ left: 4, right: 12, top: 8 }}>
        <CartesianGrid vertical={false} stroke="var(--border)" strokeWidth={1} />
        <XAxis
          dataKey="ts"
          tickLine={false}
          axisLine={false}
          minTickGap={48}
          tickFormatter={(v: string) => d.dateShort(v)}
        />
        <YAxis
          tickLine={false}
          axisLine={false}
          width={64}
          domain={["auto", "auto"]}
          tickFormatter={(v: number) => d.moneyCompact(v)}
        />
        <ChartTooltip
          content={
            <ChartTooltipContent
              labelFormatter={(_, payload) =>
                d.dateTime(payload?.[0]?.payload?.ts as string | undefined)
              }
              formatter={(value, name, item) => (
                <div className="flex w-full items-center gap-2">
                  <span
                    className="size-2 shrink-0 rounded-[2px]"
                    style={{ background: item.color }}
                  />
                  <span className="text-muted-foreground">
                    {chartConfig[name as keyof typeof chartConfig]?.label ?? name}
                  </span>
                  <span className="text-foreground ml-auto font-mono font-medium tabular-nums">
                    {d.money(typeof value === "number" ? value : null)}
                  </span>
                </div>
              )}
            />
          }
        />
        <Line
          dataKey="a"
          type="monotone"
          stroke="var(--color-a)"
          strokeWidth={1.5}
          dot={false}
          connectNulls
        />
        <Line
          dataKey="b"
          type="monotone"
          stroke="var(--color-b)"
          strokeWidth={1.5}
          dot={false}
          connectNulls
        />
        <ChartLegend content={<ChartLegendContent />} />
      </LineChart>
    </ChartContainer>
  );
}

"use client";

import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import type { AgentEquityPoint } from "@/lib/types";
import { useDisplay } from "@/lib/money";

const chartConfig = {
  equity_usd: { label: "Equity", color: "var(--chart-1)" },
} satisfies ChartConfig;

/**
 * Curva equity del conto simulato di un agente. I punti sono intraday: le
 * etichette dell'asse restano al giorno (altrimenti si accavallano) e l'ora
 * compare nel tooltip.
 */
export function AgentEquityChart({ points }: { points: AgentEquityPoint[] }) {
  const d = useDisplay();
  return (
    <ChartContainer
      config={chartConfig}
      className="aspect-auto h-[240px] w-full sm:h-[320px] [&_.recharts-cartesian-axis-tick_text]:font-mono [&_.recharts-cartesian-axis-tick_text]:text-[11px]"
    >
      <LineChart data={points} margin={{ left: 4, right: 12, top: 8 }}>
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
              formatter={(value) => (
                <span className="text-foreground ml-auto font-mono font-medium tabular-nums">
                  {d.money(typeof value === "number" ? value : null)}
                </span>
              )}
            />
          }
        />
        <Line
          dataKey="equity_usd"
          type="monotone"
          stroke="var(--color-equity_usd)"
          strokeWidth={1.5}
          dot={false}
        />
      </LineChart>
    </ChartContainer>
  );
}

# Pagine di dettaglio agenti arena (Allenamento) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** aggiungere alla sezione Allenamento pagine di dettaglio per i due agenti dell'arena (trade completi, strategia, profitti, indicatori di benchmark) e una pagina di confronto testa-a-testa, poi fare deploy dello stack.

**Architecture:** il backend FastAPI arricchisce `GET /arena/agents/{id}` con un blocco `metrics` calcolato da un nuovo modulo puro `etoro_bot/arena/metrics.py` (nessun accesso DB: riceve trade, equity e posizioni) e con la direzione long/short ricavata dal marcatore `[SHORT]` in `open_reason`. Il frontend Next.js (App Router, pagine client con TanStack Query) aggiunge la rotta dinamica `/training/[agentId]` e la rotta `/training/confronto`, riusando i componenti esistenti (Card, Table, Stamp, ChartContainer, PageHeader).

**Tech Stack:** FastAPI + SQLAlchemy 2 (backend, Python 3.12, uv), Next.js 15 App Router + TypeScript + TanStack Query + Recharts/shadcn (frontend), Docker Compose per il deploy.

## Global Constraints

- Lingua UI e commenti: **italiano**, stesso tono asciutto delle pagine esistenti.
- Lavorare SOLO in `/home/mattia/docker/projects/Trading` (checkout su `dev-3.0`). NON creare worktree, NON toccare altri progetti, NON committare (il commit lo fa l'orchestratore a fine lavoro).
- Backend: nessuna nuova dipendenza; niente `numpy` (usare `statistics` della stdlib). Stile: type hints moderni (`float | None`), docstring brevi in italiano.
- Frontend: nessuna nuova dipendenza npm. Pagine client (`"use client"`) che usano gli hook di `lib/queries.ts`; formattazione denaro/date SEMPRE via `useDisplay()` di `lib/money.tsx`; classi PnL via `pnlClass` di `lib/format.ts`.
- Tutti i valori monetari restano in USD lato API; la conversione è solo di presentazione (già gestita da `useDisplay`).
- I test backend girano con `cd backend && uv run pytest`. Verifica frontend: `cd frontend && npm run lint && npx tsc --noEmit`.
- Compatibilità: il payload API esistente non deve perdere campi (solo aggiunte).

---

## Contratto API (fonte di verità per entrambi i lati)

`GET /arena/agents/{agent_id}` restituisce, in AGGIUNTA ai campi attuali:

```jsonc
{
  "agent": {
    // invariato, ma ogni elemento di open_positions guadagna:
    //   "direction": "long" | "short"
  },
  "equity": [ { "ts": "...", "equity_usd": 0.0 } ],   // invariato
  "trades": [
    {
      // campi attuali invariati, più:
      "direction": "long" | "short",
      "holding_hours": 5.2,        // (closed_at - opened_at) in ore, 1 decimale
      "return_pct": 3.1            // pnl_usd / amount_usd * 100, 2 decimali; null se amount_usd == 0
    }
  ],
  "metrics": {
    "pnl_usd": 123.45,             // equity - starting_capital
    "return_pct": 1.23,            // pnl / starting_capital * 100
    "equity_usd": 10123.45,
    "exposure_pct": 41.2,          // invested / equity * 100 (0 se equity <= 0)
    "cash_pct": 58.8,
    "n_trades": 14,
    "n_wins": 8,
    "n_losses": 6,                 // pnl < 0 (pnl == 0 conta come loss ai fini n_losses? NO: pareggi esclusi da wins e losses)
    "win_rate_pct": 57.1,          // n_wins / n_trades * 100; null se n_trades == 0
    "profit_factor": 1.8,          // gross_win / gross_loss; null se gross_loss == 0
    "expectancy_usd": 8.8,         // media pnl per trade; null se n_trades == 0
    "avg_win_usd": 30.0,           // null se n_wins == 0
    "avg_loss_usd": -20.0,         // null se n_losses == 0 (valore negativo)
    "best_trade_usd": 90.0,        // null se n_trades == 0
    "worst_trade_usd": -55.0,      // null se n_trades == 0
    "max_drawdown_pct": -4.2,      // dal running max della serie equity (negativo o 0); null se < 2 punti
    "volatility_pct": 18.0,        // std dei rendimenti giornalieri * sqrt(252) * 100; null se < 3 punti giornalieri
    "sharpe": 1.1,                 // mean/std dei rendimenti giornalieri * sqrt(252), rf = 0; null se std == 0 o < 3 punti
    "avg_holding_hours": 6.4,      // null se n_trades == 0
    "trades_per_day": 1.9,         // n_trades / max(1, giorni di calendario da born_at a oggi)
    "long":  { "n": 9, "pnl_usd": 80.0, "win_rate_pct": 66.7 },   // win_rate null se n == 0
    "short": { "n": 5, "pnl_usd": 43.45, "win_rate_pct": 40.0 },
    "insufficient_sample": false   // true se n_trades < 5 o punti giornalieri < 3
  }
}
```

Arrotondamenti: money a 2 decimali, percentuali a 2, ore a 1, sharpe/profit_factor a 2.

---

### Task 1: Backend — modulo `arena/metrics.py` con test

**Files:**
- Create: `backend/etoro_bot/arena/metrics.py`
- Test: `backend/tests/test_arena_metrics.py`

**Interfaces:**
- Consumes: `etoro_bot.arena.engine.position_direction(pos)` (già esistente: legge il prefisso `[SHORT]` da `open_reason`; funziona per SimPosition e SimTrade e per oggetti duck-typed con attributo `open_reason`).
- Produces:
  - `trade_direction(trade) -> str` — `"short"` se `open_reason` inizia con `[SHORT]`, altrimenti `"long"` (delega a `position_direction`).
  - `compute_agent_metrics(*, starting_capital_usd: float, cash_usd: float, invested_usd: float, born_at: datetime | None, trades: Sequence, equity_points: Sequence, now: datetime | None = None) -> dict[str, Any]` — restituisce ESATTAMENTE il dict `metrics` del contratto sopra. `trades` sono oggetti con attributi `pnl_usd, amount_usd, opened_at, closed_at, open_reason`; `equity_points` hanno `ts, equity_usd`.

- [ ] **Step 1: scrivere i test (falliranno)** — `backend/tests/test_arena_metrics.py`, seguendo lo stile dei test esistenti (dataclass/SimpleNamespace fittizi, niente DB):

```python
"""Test del calcolo metriche dell'arena (modulo puro, nessun DB)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from etoro_bot.arena.metrics import compute_agent_metrics, trade_direction

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _trade(pnl, amount=100.0, hours=4.0, short=False, closed_at=NOW):
    reason = "[SHORT] scommessa ribassista" if short else "trend rialzista"
    return SimpleNamespace(
        pnl_usd=pnl,
        amount_usd=amount,
        opened_at=closed_at - timedelta(hours=hours),
        closed_at=closed_at,
        open_reason=reason,
    )


def _eq(days_ago, equity):
    return SimpleNamespace(ts=NOW - timedelta(days=days_ago), equity_usd=equity)


def test_trade_direction():
    assert trade_direction(_trade(1.0)) == "long"
    assert trade_direction(_trade(1.0, short=True)) == "short"


def test_metrics_empty():
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=10_000.0, invested_usd=0.0,
        born_at=NOW - timedelta(days=3), trades=[], equity_points=[], now=NOW,
    )
    assert m["n_trades"] == 0
    assert m["win_rate_pct"] is None
    assert m["profit_factor"] is None
    assert m["max_drawdown_pct"] is None
    assert m["sharpe"] is None
    assert m["insufficient_sample"] is True
    assert m["pnl_usd"] == 0.0
    assert m["exposure_pct"] == 0.0
    assert m["cash_pct"] == 100.0
    assert m["long"] == {"n": 0, "pnl_usd": 0.0, "win_rate_pct": None}


def test_metrics_basic_counts_and_ratios():
    trades = [
        _trade(30.0), _trade(60.0), _trade(-30.0),
        _trade(-15.0, short=True), _trade(45.0, short=True),
    ]
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=9_500.0, invested_usd=590.0,
        born_at=NOW - timedelta(days=5), trades=trades, equity_points=[], now=NOW,
    )
    assert m["n_trades"] == 5
    assert m["n_wins"] == 3 and m["n_losses"] == 2
    assert m["win_rate_pct"] == 60.0
    assert m["profit_factor"] == round(135.0 / 45.0, 2)
    assert m["expectancy_usd"] == 18.0
    assert m["best_trade_usd"] == 60.0 and m["worst_trade_usd"] == -30.0
    assert m["avg_win_usd"] == 45.0 and m["avg_loss_usd"] == -22.5
    assert m["long"]["n"] == 3 and m["short"]["n"] == 2
    assert m["short"]["pnl_usd"] == 30.0
    assert m["short"]["win_rate_pct"] == 50.0
    assert m["pnl_usd"] == 90.0          # 9500 + 590 - 10000
    assert m["return_pct"] == 0.9
    assert m["trades_per_day"] == 1.0
    assert m["avg_holding_hours"] == 4.0


def test_profit_factor_null_senza_perdite():
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=10_100.0, invested_usd=0.0,
        born_at=NOW - timedelta(days=1), trades=[_trade(50.0), _trade(50.0)],
        equity_points=[], now=NOW,
    )
    assert m["profit_factor"] is None
    assert m["n_losses"] == 0 and m["avg_loss_usd"] is None


def test_max_drawdown_e_sharpe():
    pts = [_eq(6, 10_000), _eq(5, 10_400), _eq(4, 9_880),
           _eq(3, 10_100), _eq(2, 10_500), _eq(1, 10_300)]
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=10_300.0, invested_usd=0.0,
        born_at=NOW - timedelta(days=7), trades=[], equity_points=pts, now=NOW,
    )
    # picco 10400 -> minimo 9880: -5.0%
    assert m["max_drawdown_pct"] == -5.0
    assert m["volatility_pct"] is not None
    assert m["sharpe"] is not None


def test_intraday_collassa_a_ultimo_punto_del_giorno():
    # Due punti lo stesso giorno: per i rendimenti giornalieri conta l'ultimo.
    pts = [
        SimpleNamespace(ts=NOW - timedelta(days=2, hours=5), equity_usd=10_000),
        SimpleNamespace(ts=NOW - timedelta(days=2, hours=1), equity_usd=10_200),
        SimpleNamespace(ts=NOW - timedelta(days=1, hours=1), equity_usd=10_100),
    ]
    m = compute_agent_metrics(
        starting_capital_usd=10_000.0, cash_usd=10_100.0, invested_usd=0.0,
        born_at=NOW - timedelta(days=3), trades=[], equity_points=pts, now=NOW,
    )
    # solo 2 punti giornalieri -> volatilità/sharpe null, ma drawdown calcolato
    assert m["volatility_pct"] is None and m["sharpe"] is None
    assert m["max_drawdown_pct"] is not None
```

- [ ] **Step 2: eseguire i test e verificarne il fallimento** — `cd backend && uv run pytest tests/test_arena_metrics.py -v` → deve fallire con `ModuleNotFoundError: etoro_bot.arena.metrics`.

- [ ] **Step 3: implementare `backend/etoro_bot/arena/metrics.py`** (modulo puro, stdlib `statistics`):

```python
"""Metriche di performance del conto simulato di un agente dell'arena.

Modulo puro: riceve trade chiusi, serie equity e saldi, restituisce un dict
pronto per l'API. Nessun accesso a DB o rete.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Sequence

from etoro_bot.arena.engine import position_direction

TRADING_DAYS = 252
MIN_TRADES_SAMPLE = 5
MIN_DAILY_POINTS = 3


def trade_direction(trade: Any) -> str:
    """Direzione di un trade/posizione dal marcatore [SHORT] in open_reason."""
    return position_direction(trade)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _daily_closes(equity_points: Sequence[Any]) -> list[float]:
    """Ultimo valore equity per ogni giorno di calendario (UTC), in ordine."""
    by_day: dict[Any, float] = {}
    for p in sorted(equity_points, key=lambda p: p.ts):
        by_day[p.ts.date()] = p.equity_usd
    return [by_day[d] for d in sorted(by_day)]


def _split_stats(trades: Sequence[Any]) -> dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    return {
        "n": n,
        "pnl_usd": round(sum(t.pnl_usd for t in trades), 2),
        "win_rate_pct": _round(100.0 * len(wins) / n) if n else None,
    }


def compute_agent_metrics(
    *,
    starting_capital_usd: float,
    cash_usd: float,
    invested_usd: float,
    born_at: datetime | None,
    trades: Sequence[Any],
    equity_points: Sequence[Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    equity = cash_usd + invested_usd
    pnl = equity - starting_capital_usd

    wins = [t.pnl_usd for t in trades if t.pnl_usd > 0]
    losses = [t.pnl_usd for t in trades if t.pnl_usd < 0]
    pnls = [t.pnl_usd for t in trades]
    n = len(trades)
    gross_win = sum(wins)
    gross_loss = -sum(losses)

    holding_hours = [
        (t.closed_at - t.opened_at).total_seconds() / 3600.0 for t in trades
    ]
    age_days = max(1, (now - born_at).days) if born_at else 1

    # --- serie giornaliera per drawdown/volatilità/sharpe -------------------
    closes = _daily_closes(equity_points)
    max_dd: float | None = None
    if len(closes) >= 2:
        peak = closes[0]
        max_dd = 0.0
        for value in closes:
            peak = max(peak, value)
            if peak > 0:
                max_dd = min(max_dd, (value - peak) / peak * 100.0)

    returns = [
        closes[i] / closes[i - 1] - 1.0
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    volatility = sharpe = None
    if len(closes) >= MIN_DAILY_POINTS and len(returns) >= 2:
        std = statistics.stdev(returns)
        if std > 0:
            volatility = std * (TRADING_DAYS ** 0.5) * 100.0
            sharpe = statistics.mean(returns) / std * (TRADING_DAYS ** 0.5)

    longs = [t for t in trades if trade_direction(t) != "short"]
    shorts = [t for t in trades if trade_direction(t) == "short"]

    return {
        "pnl_usd": round(pnl, 2),
        "return_pct": _round(100.0 * pnl / starting_capital_usd)
        if starting_capital_usd > 0
        else None,
        "equity_usd": round(equity, 2),
        "exposure_pct": _round(100.0 * invested_usd / equity) if equity > 0 else 0.0,
        "cash_pct": _round(100.0 * cash_usd / equity) if equity > 0 else 0.0,
        "n_trades": n,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate_pct": _round(100.0 * len(wins) / n) if n else None,
        "profit_factor": _round(gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy_usd": _round(statistics.mean(pnls)) if pnls else None,
        "avg_win_usd": _round(statistics.mean(wins)) if wins else None,
        "avg_loss_usd": _round(statistics.mean(losses)) if losses else None,
        "best_trade_usd": _round(max(pnls)) if pnls else None,
        "worst_trade_usd": _round(min(pnls)) if pnls else None,
        "max_drawdown_pct": _round(max_dd) if max_dd is not None else None,
        "volatility_pct": _round(volatility),
        "sharpe": _round(sharpe),
        "avg_holding_hours": _round(statistics.mean(holding_hours), 1)
        if holding_hours
        else None,
        "trades_per_day": _round(n / age_days),
        "long": _split_stats(longs),
        "short": _split_stats(shorts),
        "insufficient_sample": n < MIN_TRADES_SAMPLE or len(closes) < MIN_DAILY_POINTS,
    }
```

- [ ] **Step 4: rieseguire i test** — `cd backend && uv run pytest tests/test_arena_metrics.py -v` → tutti PASS. Aggiustare implementazione (non i valori attesi dei test, salvo errore aritmetico dimostrato) finché passa.

### Task 2: Backend — arricchire `GET /arena/agents/{id}`

**Files:**
- Modify: `backend/etoro_bot/api/server.py` (funzione `_agent_payload` ~riga 1039 e `arena_agent_detail` ~riga 1113)
- Test: `backend/tests/test_api.py` (aggiungere/estendere il test del dettaglio agente; guardare come i test esistenti creano client e seed — c'è `conftest.py` con fixture repo/client)

**Interfaces:**
- Consumes: `compute_agent_metrics`, `trade_direction` dal Task 1; `position_direction` da `etoro_bot.arena.engine`.
- Produces: payload conforme al Contratto API sopra (usato dal frontend nei Task 3–5).

- [ ] **Step 1: test** — in `backend/tests/test_api.py` aggiungere un test che: crea un agente con il repo della fixture, aggiunge 1 posizione aperta con `open_reason="[SHORT] x"`, 2 trade chiusi (uno long vincente, uno short perdente) e 2 punti equity; chiama `GET /arena/agents/{id}` e verifica:
  - `body["metrics"]["n_trades"] == 2`, `body["metrics"]["win_rate_pct"] == 50.0`
  - `body["trades"][0]` contiene chiavi `direction`, `holding_hours`, `return_pct`
  - `body["agent"]["open_positions"][0]["direction"] == "short"`
  Replicare fedelmente lo stile di setup dei test arena già presenti in `test_api.py` (stesse fixture e helper). Eseguire: deve FALLIRE (KeyError su `metrics`).

- [ ] **Step 2: implementazione** — in `server.py`:
  - in `_agent_payload`, per ogni posizione aggiungere `"direction": position_direction(p)` (import da `etoro_bot.arena.engine`) e aggiungere al payload dell'agente `"invested_usd": round(invested, 2)`;
  - in `arena_agent_detail`, per ogni trade aggiungere:
    ```python
    "direction": trade_direction(t),
    "holding_hours": round((t.closed_at - t.opened_at).total_seconds() / 3600.0, 1),
    "return_pct": round(t.pnl_usd / t.amount_usd * 100.0, 2) if t.amount_usd else None,
    ```
    e aggiungere la chiave `"metrics"`:
    ```python
    positions = repo.sim_positions(agent_id)
    invested = sum(p.amount_usd for p in positions)
    trades = repo.sim_trades(agent_id)
    equity_series = repo.sim_equity_series(agent_id)
    metrics = compute_agent_metrics(
        starting_capital_usd=agent.starting_capital_usd,
        cash_usd=agent.cash_usd,
        invested_usd=invested,
        born_at=agent.born_at,
        trades=trades,
        equity_points=equity_series,
    )
    ```
    (riorganizzare la funzione per non chiamare `repo.sim_trades` due volte).
- [ ] **Step 3: verifica completa backend** — `cd backend && uv run pytest` → TUTTI i test passano (non solo i nuovi). `uv run ruff check .` pulito.

### Task 3: Frontend — tipi TypeScript

**Files:**
- Modify: `frontend/lib/types.ts` (sezione «Arena evolutiva», righe ~269–373)

**Interfaces:**
- Produces (usati dai Task 4–5):

```typescript
export type TradeDirection = "long" | "short";

export interface DirectionSplit {
  n: number;
  pnl_usd: number;
  win_rate_pct: number | null;
}

export interface AgentMetrics {
  pnl_usd: number;
  return_pct: number | null;
  equity_usd: number;
  exposure_pct: number;
  cash_pct: number;
  n_trades: number;
  n_wins: number;
  n_losses: number;
  win_rate_pct: number | null;
  profit_factor: number | null;
  expectancy_usd: number | null;
  avg_win_usd: number | null;
  avg_loss_usd: number | null;
  best_trade_usd: number | null;
  worst_trade_usd: number | null;
  max_drawdown_pct: number | null;
  volatility_pct: number | null;
  sharpe: number | null;
  avg_holding_hours: number | null;
  trades_per_day: number | null;
  long: DirectionSplit;
  short: DirectionSplit;
  insufficient_sample: boolean;
}
```

- [ ] **Step 1:** aggiungere i tipi sopra; estendere `AgentSimPosition` con `direction: TradeDirection`, `AgentSimTrade` con `direction: TradeDirection; holding_hours: number; return_pct: number | null`, `ArenaAgent` con `invested_usd?: number`, e `AgentDetail` con `metrics: AgentMetrics`.
- [ ] **Step 2:** `cd frontend && npx tsc --noEmit` → nessun errore.

### Task 4: Frontend — pagina di dettaglio `/training/[agentId]`

**Files:**
- Create: `frontend/app/training/[agentId]/page.tsx`
- Create: `frontend/components/charts/agent-equity-chart.tsx`
- Modify: `frontend/app/training/page.tsx` (link dalle card e dalla lineage alla pagina di dettaglio)

**Interfaces:**
- Consumes: `useArenaAgent(agentId)` e `useArena()` da `lib/queries.ts`; tipi del Task 3; `EquitySparkline`/pattern grafico da `components/charts/equity-chart.tsx`; `PageHeader`, `Stamp`, `CardSkeleton/TableSkeleton/ErrorState`, `useDisplay`, `pnlClass`.
- Produces: rotta `/training/{agentId}` raggiungibile da click sul nome agente; componente `AgentEquityChart({ points, label })` riusato dal Task 5? NO — il Task 5 usa un proprio chart a due serie; `AgentEquityChart` serve solo qui.

- [ ] **Step 1: componente grafico** — `frontend/components/charts/agent-equity-chart.tsx`: variante mono-serie del grafico equity, asse X temporale con ora (i punti arena sono intraday). Struttura ricalcata su `equity-chart.tsx`:

```tsx
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

/** Curva equity del conto simulato di un agente (punti intraday). */
export function AgentEquityChart({ points }: { points: AgentEquityPoint[] }) {
  const d = useDisplay();
  const chartConfig = {
    equity_usd: { label: "Equity", color: "var(--chart-1)" },
  } satisfies ChartConfig;
  return (
    <ChartContainer
      config={chartConfig}
      className="aspect-auto h-[240px] w-full sm:h-[320px] [&_.recharts-cartesian-axis-tick_text]:font-mono [&_.recharts-cartesian-axis-tick_text]:text-[11px]"
    >
      <LineChart data={points} margin={{ left: 4, right: 12, top: 8 }}>
        <CartesianGrid vertical={false} stroke="var(--border)" strokeWidth={1} />
        <XAxis dataKey="ts" tickLine={false} axisLine={false} minTickGap={48}
          tickFormatter={(v: string) => d.dateShort(v)} />
        <YAxis tickLine={false} axisLine={false} width={64}
          domain={["auto", "auto"]}
          tickFormatter={(v: number) => d.moneyCompact(v)} />
        <ChartTooltip content={<ChartTooltipContent
          labelFormatter={(_, payload) =>
            d.dateTime(payload?.[0]?.payload?.ts as string | undefined)}
          formatter={(value) => (
            <span className="font-mono font-medium tabular-nums">
              {d.money(typeof value === "number" ? value : null)}
            </span>
          )} />} />
        <Line dataKey="equity_usd" type="monotone"
          stroke="var(--color-equity_usd)" strokeWidth={1.5} dot={false} />
      </LineChart>
    </ChartContainer>
  );
}
```
(verificare che `useDisplay` esponga `dateShort`/`dateTime`/`moneyCompact` leggendo `lib/money.tsx`; se i nomi differiscono, adeguarsi a quelli reali.)

- [ ] **Step 2: pagina** — `frontend/app/training/[agentId]/page.tsx`, client page con `useParams()` di `next/navigation`. Layout (dall'alto in basso):
  1. `PageHeader` — eyebrow "Arena evolutiva", title = nome agente, description = "Generazione N · mese M · nato il …"; actions: bottone `variant="outline"` "← Allenamento" (Link a `/training`) e, se esiste il rivale vivo, bottone "Confronta" (Link a `/training/confronto`). Sotto il titolo gli stamp di stato (riusare la logica `AgentStatusStamp` della pagina training: Campione/Morto/Evoluto/In allenamento — copiarla in questa pagina o estrarla; PREFERIBILE estrarre `AgentStatusStamp` e `DnaGrid` in `frontend/components/arena.tsx` e importarli da entrambe le pagine).
  2. Striscia di 4 tile riassuntivi (Card come `OverviewStrip`): PnL del mese (`metrics.pnl_usd` con `pnlClass` + `return_pct`), Equity (`metrics.equity_usd`, hint cash/esposizione %), Trade chiusi (`n_trades`, hint `n_wins` W / `n_losses` L), Win rate (`win_rate_pct`, hint profit factor). Ogni valore null → "—".
  3. Card «Curva equity» con `AgentEquityChart` (se ≥ 2 punti, altrimenti empty state testuale).
  4. Card «Indicatori di performance»: griglia `grid-cols-2 sm:grid-cols-4` di coppie label/valore in stile `DnaGrid` con TUTTI gli indicatori restanti: Sharpe, Volatilità ann., Max drawdown, Profit factor, Expectancy, Miglior trade, Peggior trade, Media vincite, Media perdite, Holding medio (h), Trade/giorno, Esposizione. Se `insufficient_sample`, mostrare sotto la griglia una riga `text-muted-foreground text-xs` "Campione ridotto: indicatori poco significativi (meno di 5 trade o di 3 giorni di storia)."
  5. Card «Long vs Short»: due colonne con `n`, `pnl_usd` (colorato con `pnlClass`), `win_rate_pct` per `metrics.long` e `metrics.short`.
  6. Card «Strategia e DNA»: testo `dna.strategy`, sotto `DnaGrid`, sotto `<details>` con la memoria (come nella pagina training).
  7. Card «Posizioni aperte»: Table con colonne Simbolo, Direzione (Stamp `tone="accent"` per short con testo "SHORT", `tone="neutral"` "LONG"), Importo, Prezzo di ingresso, Aperta il, Motivazione (truncate). Empty state "Nessuna posizione aperta".
  8. Card «Tutti i trade simulati»: Table con colonne Simbolo, Direzione, Importo, Ingresso→Uscita (prezzi `font-mono`), PnL (con `pnlClass` + `return_pct` tra parentesi), Durata (`holding_hours` h), Chiusura (`d.dateTime(closed_at)`), Motivo chiusura (truncate, `title=` completo). Mostrare tutti i trade restituiti (il backend limita già a 200). Empty state "Ancora nessun trade chiuso".
  Stati: loading → `CardSkeleton`/`TableSkeleton`; errore o 404 → `ErrorState` con titolo "Agente non trovato".
- [ ] **Step 3: link dalla pagina Allenamento** — in `frontend/app/training/page.tsx`: il nome dell'agente in `AgentCard` diventa `<Link href={`/training/${agent.id}`}>` con `hover:underline`; aggiungere in fondo alla card un bottone/link "Dettagli →" (`variant="ghost" size="sm"`); nella tabella `LineageCard` il nome agente diventa lo stesso Link. Se si è scelto di estrarre `AgentStatusStamp`/`DnaGrid` in `components/arena.tsx`, aggiornare gli import qui.
- [ ] **Step 4: verifica** — `cd frontend && npm run lint && npx tsc --noEmit` → puliti.

### Task 5: Frontend — pagina confronto `/training/confronto`

**Files:**
- Create: `frontend/app/training/confronto/page.tsx`
- Create: `frontend/components/charts/duel-chart.tsx`

**Interfaces:**
- Consumes: `useArena()` per trovare i due agenti vivi; `useArenaAgent(id)` ×2 per equity+metrics+trades; tipi Task 3; componenti condivisi (`AgentStatusStamp`, `DnaGrid` se estratti).
- Produces: rotta `/training/confronto`; `DuelChart({ series }: { series: { name: string; points: AgentEquityPoint[] }[] })`.

- [ ] **Step 1: `DuelChart`** — grafico Recharts con le due curve equity sovrapposte. Implementazione: unire i punti per timestamp in un array `{ ts, a?: number, b?: number }` ordinato, due `<Line>` (`var(--chart-1)` e `var(--chart-2)`), `connectNulls`, legenda con `ChartLegend`. Stessa impalcatura `ChartContainer` di `agent-equity-chart.tsx`.
- [ ] **Step 2: pagina** — `frontend/app/training/confronto/page.tsx`:
  1. `PageHeader` eyebrow "Arena evolutiva", title "Testa a testa", description "I due agenti vivi a confronto: stesso capitale, stesso mercato, sopravvive uno solo."; action: link "← Allenamento".
  2. Se gli agenti vivi sono < 2: Card con messaggio "Servono due agenti vivi per il confronto" e stop.
  3. Grafico `DuelChart` con le due equity.
  4. Tabella comparativa: righe = indicatori (PnL del mese, Rendimento %, Equity, Trade chiusi, Win rate, Profit factor, Sharpe, Max drawdown, Volatilità, Expectancy, Holding medio, Trade/giorno, Esposizione, Long n/PnL, Short n/PnL), colonne = i due agenti (header con nome + stamp). Evidenziare il valore migliore per riga (grassetto) dove il confronto ha senso (PnL, win rate, sharpe, profit factor: più alto meglio; drawdown e volatilità: più vicino a 0 meglio).
  5. Due Card affiancate «Strategia» con `risk_profile` + testo `strategy` di ciascuno.
  6. Card «Ultimi trade» (unione dei trade dei due agenti, ordinati per `closed_at` desc, max 20, con colonna Agente).
- [ ] **Step 3: link** — nella pagina `/training` aggiungere nelle actions dell'header un bottone `variant="outline"` "Testa a testa" (Link a `/training/confronto`), accanto a Pausa/Ciclo ora.
- [ ] **Step 4: verifica** — `cd frontend && npm run lint && npx tsc --noEmit` → puliti.

### Task 6: Build, deploy e verifica end-to-end

**Files:** nessun sorgente nuovo; usa `docker-compose.yml` esistente (NON modificarlo).

- [ ] **Step 1:** `cd backend && uv run pytest && uv run ruff check .` → verde.
- [ ] **Step 2:** `cd frontend && npm run lint && npx tsc --noEmit && npm run build` → verde (il build Next è quello che gira nel Dockerfile: farlo prima in locale evita cicli di build Docker falliti).
- [ ] **Step 3:** `cd /home/mattia/docker/projects/Trading && docker compose build trading-backend trading-frontend` → immagini ok.
- [ ] **Step 4:** `docker compose up -d trading-backend trading-frontend` e attendere healthy: `docker compose ps` (backend healthy), `docker logs trading-frontend --tail 20` senza errori.
- [ ] **Step 5: smoke test API** — dall'interno della rete: `docker exec trading-backend python -c "import urllib.request,json,os; req=urllib.request.Request('http://localhost:8000/arena', headers={'X-Internal-Token': os.environ['TRADING_INTERNAL_TOKEN']}); print(json.load(urllib.request.urlopen(req)).keys())"`; poi con un id di agente reale verificare che `/arena/agents/{id}` contenga `metrics`. (Se l'header del token interno ha altro nome, leggerlo dal middleware `_require_internal_token` in `server.py` e adeguare il comando.)
- [ ] **Step 6:** smoke test frontend: `docker exec trading-frontend wget -qO- http://localhost:3000/training >/dev/null && echo OK` (o curl equivalente disponibile nell'immagine).

## Self-review

- Copertura: trade dei due bot → Task 4 §8 e Task 5 §6; strategie → Task 4 §6, Task 5 §5; profitti → tile PnL/equity; indicatori di benchmark → Task 1–2 (metrics) + Task 4 §4 + Task 5 §4; deploy → Task 6. ✓
- Contratto unico definito in testa: backend e frontend possono procedere in parallelo. ✓
- Nomi coerenti: `compute_agent_metrics`, `trade_direction`, `AgentMetrics`, `AgentEquityChart`, `DuelChart` usati identici tra i task. ✓

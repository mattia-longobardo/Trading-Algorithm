"""Primitive statistiche condivise da track record e metriche dell'arena.

Le due parti del sistema calcolavano volatilità e drawdown ciascuna a modo suo
(`statistics` da una parte, `numpy` dall'altra): stessa definizione, due
implementazioni che potevano divergere in silenzio a ogni ritocco.

Qui NON finiscono le metriche che sono legittimamente diverse fra i due
contesti: lo Sharpe del track record parte dal CAGR e sottrae il tasso privo di
rischio, quello dell'arena è la media dei rendimenti giornalieri. Sono due
definizioni scelte apposta, non una duplicazione.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

TRADING_DAYS_PER_YEAR = 252


def annualized_volatility(
    returns: Sequence[float], periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> float | None:
    """Deviazione standard campionaria dei rendimenti, annualizzata.

    None sotto i due punti: con un solo rendimento non esiste dispersione.
    """
    if len(returns) < 2:
        return None
    return float(np.std(returns, ddof=1) * np.sqrt(periods_per_year))


def max_drawdown_pct(levels: Sequence[float]) -> float | None:
    """Peggior scostamento % dal massimo corrente di una serie di livelli.

    Valore ≤ 0 (in percentuale); None sotto i due punti. Funziona sia su una
    serie equity in dollari sia su una curva cumulata dei rendimenti: conta
    solo il rapporto col picco precedente.
    """
    if len(levels) < 2:
        return None
    peak = levels[0]
    worst = 0.0
    for value in levels:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak * 100.0)
    return worst


def cumulative_levels(returns: Sequence[float]) -> list[float]:
    """Curva cumulata (1+r1, (1+r1)(1+r2), …) da una serie di rendimenti."""
    levels: list[float] = []
    running = 1.0
    for value in returns:
        running *= 1.0 + value
        levels.append(running)
    return levels

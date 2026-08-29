"""Contratto Verdict machine-readable per gli output serali (Vibe-Trading).

La riflessione EOD termina con una sezione `## Verdict` a righe
`- SIMBOLO: STATO - motivo`: parse rigido con regex, MAI interpretazione.
Malformata = `contract_violation` a giornale (non si indovina); sezione vuota
= una vera risposta "nessuna call". La UI legge il verdetto strutturato senza
ri-parsare prosa.
"""

from __future__ import annotations

import re
from typing import Any

VERDICT_HEADER = "## Verdict"
VERDICT_STATES = ("BUY", "SELL", "HOLD", "AVOID", "EXIT")

_LINE = re.compile(
    r"^-\s*([A-Z0-9.\-]{1,12}):\s*(" + "|".join(VERDICT_STATES) + r")\s*-\s*(.+)$"
)


def split_verdict(text: str) -> tuple[str, str | None]:
    """(corpo, sezione verdict | None se il marcatore manca)."""
    idx = text.find(VERDICT_HEADER)
    if idx < 0:
        return text.strip(), None
    return text[:idx].strip(), text[idx + len(VERDICT_HEADER):].strip()


def parse_verdict(section: str) -> list[dict[str, Any]] | None:
    """Righe di verdetto; None = malformato (una sola riga rotta invalida tutto)."""
    rows: list[dict[str, Any]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _LINE.match(line)
        if match is None:
            return None
        rows.append(
            {"symbol": match.group(1), "state": match.group(2),
             "reason": match.group(3).strip()}
        )
    return rows


def extract_verdict(text: str) -> tuple[str, list[dict[str, Any]] | None, str | None]:
    """(corpo senza verdict, verdetti, violazione | None).

    Violazioni: `missing_verdict` (sezione assente), `malformed_verdict`.
    """
    body, section = split_verdict(text)
    if section is None:
        return body, None, "missing_verdict"
    rows = parse_verdict(section)
    if rows is None:
        return body, None, "malformed_verdict"
    return body, rows, None
